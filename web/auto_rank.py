#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动刷榜：每天北京时间 0 点建号，常规榜每小时校准一次，截止前 10 分钟冲榜。

三个榜
======
1. 常规榜（CMD137：大选关 type=0 / 爬塔 type=1）
   · 每小时一次：把自己的分数压到「榜一 − 100」（榜一是自己就跳过）
   · 榜单截止前 10 分钟：改成「榜一 + 100」，type=0 上限 14000
2. 世界魔王榜（CMD142/176/170）
   · 只在「魔王本期截止前 10 分钟」动作，0 点到晚上 8 点不执行
   · 取本期伤害榜第一名的伤害 × 110% 作为目标，
     上报差额 = 目标 − 自己当前累计伤害（CMD170 的 damage 是累加的）
3. 竞技场（CMD48/24/25/26）
   · 同样只在「今日竞技场关闭前 10 分钟」动作
   · 取非自己的榜一分数 + 随机 20~100 上报（CMD25 推分后立刻 CMD26 取消）

比分数口径
==========
★ 所有「榜一」都排除数据库里已有的 uid：自己的号（今天/前 7 天的任务号、固定号）
  互相之间不算榜一，只跟其他玩家比，避免几个号拿着彼此的分数反复 ±100 刷个不停。
★ 目标：其他玩家里的第一名（不是固定号）。其他玩家分数都低于固定号时，同样取
  「其他玩家中分数最高的那一个」作目标，不会把固定号当成要冲的对象。

固定号（坐榜号）
==========
· 除了每天 10 个轮训号，额外建 ANCHOR_COUNT=25 个固定号（一次性建满，不按天累加），
  昵称 = 4~5 个有意义的中文 + 3~5 位数字（自动避开敏感词/重名）。
· 固定号不参与轮训冲榜（不进日任务、也不参与魔王/竞技场）。
· 它们只做一件事：每天北京时间 21:00 把两个榜分数推一档 ——
  大选关 3806 起、每天 +3000、封顶 13902；爬塔 8101000 起、每天 +1000。
  天数从各账号自己的创建日算，直到榜单截止为止。
· 冲榜号永不越过它们：冲刺目标会被压在「最小固定号分 − 100」以下。

账号
====
· 每天 0 点检查：当天没有轮训账号才建 10 个（昵称 = 别氪了 + 日期码 + 序号）；
  当天已有账号（手动建的也算）则跳过建号；固定号另行按总数补齐。
· 今天 + 之前 7 天，每天一个任务：先取当天最早的账号，登录/查榜/上报任一步
  失败（含「帐号异常」）就换当天下一个号，直到号用完或榜单截止。
· 早于 7 天的账号也逐个登录，但只参与魔王榜和竞技场，不碰常规榜。

开关
====
· 开关在内存里，服务重启后默认关闭；打开即立刻跑一轮，之后每天 0 点自动开始。
· 所有网络操作都拿服务端全局 LOCK，和页面手动操作串行，避免 sid 互相干扰。
"""
import io
import random
import threading
import time
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

import offline_play as m

BEIJING = timezone(timedelta(hours=8))
NICK_PREFIX = "别氪了"
NICK_FALLBACK_PREFIX = "片刻"          # 「别氪了」前缀不可用时兜底回退到这个前缀
BATCH_SIZE = 10
LOOKBACK_DAYS = 7
MAX_CREATE_ATTEMPTS = BATCH_SIZE + 2
NICK_RETRY_LIMIT = 6               # 改名被服务端拒（敏感词/占用）时换数字重试的上限
LOG_LIMIT = 400
STATUS_LOG_LINES = 200

RANK_TICK_SECONDS = 60 * 60        # 常规榜：每小时校准一次
SPRINT_WINDOW_MS = 10 * 60 * 1000  # 截止前 10 分钟开始冲刺
SPRINT_INTERVAL = 2 * 60           # 冲刺阶段每 2 分钟复查一次

RANK_LOW_GAP = 100                 # 平时比榜一低 100
RANK_HIGH_GAP = 100                # 冲刺时比榜一高 100
RANK_TYPE0_CAP = 14000             # 大选关榜上限
WB_MULT = 1.1                  # 魔王目标 = 第一名伤害的 110%
ARENA_BONUS = (20, 100)            # 竞技场目标 = 非自己榜一 + 20~100
ARENA_LV_DEFAULT = 158             # 与页面「匹配→取消」用的 lv 一致
ARENA_REPUSH_SECONDS = 180         # 已废弃：现在每期每号只推一次

# ── 固定号（坐榜号）─────────────────────────────────────────────
# 除了每天 BATCH_SIZE 个轮训号之外，另外建 ANCHOR_COUNT 个「固定号」：
#   · 昵称 = 4~5 个有意义的中文 + 3~5 位数字（与轮训号的日期码命名区分开）
#   · 只做一件事：每天 21:00 把自己的两个榜分数往上推一档，不参与轮训冲榜
#   · 冲榜号算「榜一」时会把它们排除；冲刺时也不会越过它们的分数
ANCHOR_ROLE = "anchor"             # 账号记录里的角色标记（role 字段）
ANCHOR_COUNT = 25                  # 固定号总数（一次性建满，不按天累加）
ANCHOR_MAX_ATTEMPTS = ANCHOR_COUNT + 6
ANCHOR_NICK_RETRY = 10             # 固定号改名被拒时的重抽次数上限
ANCHOR_RETRY_SECONDS = 300         # 补建没建满时，等 5 分钟再试（免得 1 秒一跳刷爆服务端）
ANCHOR_TYPE0_START = 3806          # 大选关榜：初始分
ANCHOR_TYPE0_DAILY = 3000          # 大选关榜：每天 +3000
ANCHOR_TYPE0_CAP = 13902           # 大选关榜：上限
ANCHOR_TYPE1_START = 8101000       # 爬塔榜：初始分（= 810 层 × 10000 + 1000）
ANCHOR_TYPE1_DAILY = 1000          # 爬塔榜：每天 +1000
ANCHOR_SCORE_HOUR = 21             # 每天 21:00（北京时间）执行一遍加分
ANCHOR_SCORE_MINUTE = 0
ANCHOR_GUARD = True                # 冲榜号不越过固定号：目标分压在最小固定号分之下

# 固定号昵称的词库：4 字 / 5 字各一份，拼 3~5 位数字
ANCHOR_WORDS_4 = (
    "清风明月", "山高水长", "春华秋实", "云淡风轻", "星河灿烂", "海阔天空",
    "岁月静好", "长夜微光", "雨过天晴", "柳暗花明", "晴空万里", "静水流深",
    "大江东去", "竹影清风", "烟波浩渺", "千山暮雪", "月白风清", "松风入梦",
    "溪水长流", "晨钟暮鼓", "笑看风云", "江湖夜雨", "野鹤闲云", "剑指苍穹",
    "青云直上", "长风万里", "一叶知秋", "落雪听禅", "听风听雨", "踏雪寻梅",
    "灯火阑珊", "半盏清茶", "远山含雪", "孤舟蓑影", "淡看浮华", "初心不改",
)
ANCHOR_WORDS_5 = (
    "人间四月天", "春风十里路", "长风破浪时", "落花人独立", "月是故乡明",
    "大漠孤烟直", "长河落日圆", "山色空蒙雨", "一箭定江山", "万卷古今情",
    "天涯共此时", "江清月近人", "星辰入我怀", "清风不问归", "夜雨寄北心",
    "孤影踏长街", "一笑渡流年", "听雨看落花", "仗剑走天涯", "云深不知处",
)


def anchor_words():
    """固定号词库（4 字 + 5 字合在一起）。"""
    return ANCHOR_WORDS_4 + ANCHOR_WORDS_5


def anchor_nick(used=None):
    """生成一个固定号昵称：4~5 个中文 + 3~5 位数字。

    used 里已有的昵称不会再生成（同一批内部不重复）。
    """
    used = used or set()
    for _ in range(200):
        word = random.choice(anchor_words())
        n = random.choice((3, 4, 5))
        num = random.randint(10 ** (n - 1), 10 ** n - 1)   # 定长、不以 0 开头
        nick = f"{word}{num}"
        if nick not in used:
            return nick
    return f"{random.choice(anchor_words())}{random.randint(100, 999)}"


def is_anchor(acc):
    """账号是不是固定号（坐榜号）。"""
    return str((acc or {}).get("role") or "") == ANCHOR_ROLE


def days_since_day(day, today=None):
    """YYYYMMDD → 距离今天过了几天（同一天返回 0，容错返回 0）。"""
    try:
        d0 = datetime.strptime(str(day), "%Y%m%d")
        d1 = datetime.strptime(str(today or day_key(bj_now())), "%Y%m%d")
        return max(0, (d1 - d0).days)
    except Exception:
        return 0


def anchor_scores(acc, today=None):
    """固定号「今天应该报到多少分」：({type0 分}, {type1 分})。

    · 大选关：3806 起，每天 +3000，封顶 13902
    · 爬塔  ：8101000 起，每天 +1000（不封顶）
    天数从账号自己的创建日算（同一天建的一批永远同步）。
    """
    d = days_since_day(created_day(acc), today)
    t0 = min(ANCHOR_TYPE0_START + ANCHOR_TYPE0_DAILY * d, ANCHOR_TYPE0_CAP)
    t1 = ANCHOR_TYPE1_START + ANCHOR_TYPE1_DAILY * d
    return t0, t1


def arena_sprint_enabled():
    """竞技场冲刺总开关：环境变量 ZHAOHE_ARENA_SPRINT=0 可整体关掉。"""
    import os
    return str(os.environ.get("ZHAOHE_ARENA_SPRINT", "1")).lower() \
        not in ("0", "false", "no", "off")


class DeadlineReached(RuntimeError):
    """排行榜 endTime 已过。end_ms 是被探到的截止时间（可能为 0）。"""

    def __init__(self, text="已截止", end_ms=0):
        super().__init__(text)
        self.end_ms = int(end_ms or 0)


def bj_now():
    return datetime.now(BEIJING)


def day_key(dt):
    return dt.strftime("%Y%m%d")


def day_code(day):
    """把 YYYYMMDD 散列成「4 位数字 + 1 位字母」，例如 20261029 -> 1932Q。

    纯函数：同一天永远得到同一个码；相邻日期差异很大，便于肉眼区分批次。
    """
    n = int(day)
    h = (n * 2654435761 + 0x9E3779B9) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 2246822519) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 3266489917) & 0xFFFFFFFF
    h ^= h >> 16
    return f"{h % 10000:04d}{'ABCDEFGHIJKLMNOPQRSTUVWXYZ'[(h // 10000) % 26]}"


def batch_nick(day, index):
    return f"{NICK_PREFIX}{day_code(day)}{int(index):02d}"


def fallback_prefix(name):
    """「别氪了」前缀不可用时，回退到旧前缀「片刻」（保持尾部日期码/序号不变）。

    只做单向回退：名字以主前缀开头才换成兜底前缀，否则返回 None。
    """
    name = str(name or "")
    if name.startswith(NICK_PREFIX):
        return NICK_FALLBACK_PREFIX + name[len(NICK_PREFIX):]
    return None


def move_last_to_front(name):
    """昵称重复时：把最后一位字符移到名字最前面。"""
    name = str(name or "")
    if len(name) <= 1:
        return name
    return name[-1] + name[:-1]


def fmt_ts(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def created_day(acc):
    """账号 createdAt（本地时间字符串）→ 北京时间 YYYYMMDD。

    ★ 只认 createdAt：老账号今天被登录/改昵称时 updated 会变成今天，
      用 updated 兜底会把它误判成「今天创建的号」。
    """
    s = str(acc.get("createdAt") or "").strip()
    try:
        # ★ createdAt 统一按北京时间写（offline_play.now_bj_str），这里也必须按
        #   北京时间解析；用「本机时区」会让 Render（UTC）把号算到别的日期上。
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
        return day_key(dt.astimezone(BEIJING))
    except Exception:
        return s[:10].replace("-", "")


def my_ranks(entries, uid):
    """从 CMD137 entries 里取自己每个榜的最小名次。"""
    ranks = {}
    for e in entries or []:
        try:
            if int(e.get("uid") or 0) != int(uid):
                continue
            r = int(e.get("rank", -1))
        except Exception:
            continue
        if r <= 0:
            continue
        t = str(e.get("type"))
        if t not in ranks or r < ranks[t]:
            ranks[t] = r
    return ranks


def my_values(entries, uid):
    """自己每个榜的当前分数值（value 原样）。"""
    vals = {}
    for e in entries or []:
        try:
            if int(e.get("uid") or 0) != int(uid):
                continue
            t = str(e.get("type"))
            v = int(e.get("value"))
        except Exception:
            continue
        if t not in vals or v > vals[t]:
            vals[t] = v
    return vals


def top_of_others(entries, own_uids, rank_type=None):
    """在榜单 entries 里找「不是我们自己号」的榜一（rank 最小的一条）。

    ★ 数据库里已有的 uid 全部排除：不同日期的任务号互相之间不算「榜一」，
      否则几个号会拿彼此的分数反复 ±100，永远刷个不停。
    """
    best = None
    for e in entries or []:
        try:
            if rank_type is not None and str(e.get("type")) != str(rank_type):
                continue
            if int(e.get("uid") or 0) in own_uids:
                continue
            r = int(e.get("rank", -1))
        except Exception:
            continue
        if r <= 0:
            continue
        if best is None or r < int(best.get("rank", -1)):
            best = e
    return best


def is_banned_error(err):
    """登录失败是否是服务端判定的「帐号异常」（cmd=255）。"""
    msg = str(err)
    return ("登录被拒绝" in msg or "帐号异常" in msg or "账号异常" in msg)


def in_window(end_ms, now_ms=None):
    """是否处在「截止前 10 分钟」窗口内。"""
    try:
        end_ms = int(end_ms or 0)
    except Exception:
        return False
    if end_ms <= 0:
        return False
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return end_ms - SPRINT_WINDOW_MS <= now_ms < end_ms


class AutoRank:
    def __init__(self, lock, db_sync=None, pvp_time=None, arena_rank=None):
        self.lock = lock                 # 服务端全局 LOCK，串行化所有协议操作
        self.db_sync = db_sync           # 建号后显式落库并回读校验的回调（server 注入）
        self.pvp_time = pvp_time         # CMD48 竞技场时间（server 注入）
        self.arena_rank = arena_rank     # CMD24 竞技场排行榜（server 注入）
        self.on_banned = None            # 帐号异常时删号的回调（server 注入）
        self.mu = threading.RLock()      # 保护下面的状态字段
        self.enabled = False
        self.running = False
        self.finished = False
        self.force = False
        self.day = None
        self.batch_day = None
        self.tasks = []
        self.historical_tasks = {}     # uid -> 7 天前账号的魔王 / 竞技任务
        self.next_rank_tick = None
        self.next_sprint_tick = None
        self.last_error = None
        self.logs = deque(maxlen=LOG_LIMIT)
        self.banned = set()              # 已被服务端判「帐号异常」并删掉的 uid
        self.own_uids = set()            # 数据库里所有账号的 uid（比分数时排除）
        self.wb_done = set()             # "uid:end_ms" 已上报过魔王伤害
        self.wb_owner = {}               # end_ms -> 本期负责冲魔王的 uid（只允许一个）
        self.arena_pushed = {}           # "uid:end_ms" -> 上次推分时间
        self.own_uids = set()            # 比分数时排除的 uid（所有自有号，含固定号）
        # ── 固定号（坐榜号）──
        self.anchor_uids = set()         # 固定号 uid（单独存一份，方便快速判断）
        self.anchor_expected = {}        # {type: {uid: 今天应报的分}} —— 榜上没刷新时的兵底
        self.anchor_names = {}           # {uid: 昵称} —— 状态页用（纯内存，不查库）
        self.anchor_score_day = None     # 最近一次执行 21:00 加分的日期
        self.anchor_ended_ms = 0         # 已判定截止的榜单 endTime（>0 = 已停止加分）
        self.anchor_batch_day = None     # 补建固定号时的去重标记（只记日志用）
        self.anchor_last = ""            # 固定号最近一次执行结果（状态页展示）
        self.anchor_creating = False     # 正在补建固定号
        self.anchor_next_try = 0.0       # 补建失败后的下次重试时间戳
        self.thread = threading.Thread(target=self._loop, name="auto-rank", daemon=True)

    # ───────────── 对外接口 ─────────────
    def start(self):
        if not self.thread.is_alive():
            self.thread.start()
        return self

    def status(self):
        with self.mu:
            now = time.time()
            tasks = list(self.tasks)
            return {
                "enabled": self.enabled,
                "running": self.running,
                "finished": self.finished,
                "today": day_key(bj_now()),
                "day": self.day,
                "next_tick_at": self.next_rank_tick,
                "next_tick_text": fmt_ts(self.next_rank_tick),
                "seconds_to_tick": (max(0, int(self.next_rank_tick - now))
                                    if self.next_rank_tick else None),
                "sprint_active": any(self._task_in_window(t) for t in tasks),
                "interval_seconds": RANK_TICK_SECONDS,
                "sprint_interval_seconds": SPRINT_INTERVAL,
                "sprint_window_seconds": SPRINT_WINDOW_MS // 1000,
                "arena_sprint_enabled": arena_sprint_enabled(),
                "batch_size": BATCH_SIZE,
                "lookback_days": LOOKBACK_DAYS,
                "anchor": self._anchor_view(now),
                "tasks": [self._task_view(t) for t in tasks],
                "logs": list(self.logs)[-STATUS_LOG_LINES:],
                "last_error": self.last_error,
                "server_now": fmt_ts(now),
            }

    def set_enabled(self, on):
        on = bool(on)
        with self.mu:
            if on == self.enabled:
                return self.status()
            self.enabled = on
            if on:
                self.force = True
                if self.finished:
                    self._log("开关已打开：今日任务已结束，等待明日 0 点自动开始")
                else:
                    self._log("开关已打开：立即执行一轮（0 点也会自动触发）")
            else:
                self._log("开关已关闭：停止自动刷榜（今日进度保留，重新打开会接着跑）")
        return self.status()

    def trigger(self):
        with self.mu:
            self.enabled = True
            if self.finished:
                self._log("手动触发：今日任务已结束（榜单截止 / 账号用完），等待明日 0 点")
                return self.status()
            self.force = True
            self._log("手动触发：立即执行一轮（常规榜 + 冲刺窗口检查）")
        return self.status()

    # ───────────── 日志 ─────────────
    def _log(self, text):
        text = str(text or "").rstrip()
        # redirect_stdout 时会捕获到服务端自己的 HTTP 访问日志，滤掉
        text = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith(('"GET ', '"POST ')))
        if not text.strip():
            return
        stamp = bj_now().strftime("%m-%d %H:%M:%S")
        with self.mu:
            for line in text.splitlines():
                if len(line) > 2000:
                    line = line[:2000] + " …"
                self.logs.append(f"[{stamp}] {line}")

    # ───────────── 调度循环 ─────────────
    def _loop(self):
        while True:
            try:
                if self.enabled:
                    self._step()
                # 固定号缓存：开关关着也要保持新鲜（状态页靠它，且不能每 3 秒查库）
                if time.time() >= getattr(self, "anchor_cache_at", 0.0):
                    self.anchor_cache_at = time.time() + 30.0
                    self._refresh_anchor_state()
            except Exception as e:  # 调度本身出错不能把线程带走
                self.last_error = f"{type(e).__name__}: {e}"
                self._log(f"✗ 调度异常：{self.last_error}")
                time.sleep(5)
            time.sleep(1)

    def _step(self):
        today = day_key(bj_now())
        # 固定号每日 21:00 加分的优先级高于轮训：到点就先把它跑完（一次），
        # 不碰 force / 下轮时间，轮训 1 秒后的下一跳会接着跑。
        if self.enabled and self._anchor_score_due(today):
            self._run_anchor_scores(today)
            return
        # 固定号没建满就补：★ 与「今日任务是否结束」无关，0 次建号、finished
        # 都不影响它；补建失败后等 ANCHOR_RETRY_SECONDS 再试，不刷请求。
        if self.enabled and self._anchor_need_create():
            self._ensure_anchors()
            return
        with self.mu:
            if self.day != today:
                self.day = today
                self.tasks = []
                self.historical_tasks = {}
                self.finished = False
                self.next_rank_tick = None
                self.next_sprint_tick = None
                self.wb_done.clear()
                self.wb_owner.clear()
                self.arena_pushed.clear()
                self.force = True
                self._log(f"新的一天（北京时间 {today}）：准备 0 点任务")
            force = self.force
            self.force = False
            finished = self.finished
            next_rank = self.next_rank_tick
            next_sprint = self.next_sprint_tick
        if finished and not self.historical_tasks:
            return
        now = time.time()
        if force or next_rank is None or now >= next_rank:
            self._run_pass(full=True)
            return
        if self._any_window() and now >= (next_sprint or 0):
            self._run_pass(full=False)

    def _any_window(self):
        with self.mu:
            tasks = list(self.tasks) + list(self.historical_tasks.values())
        return any(self._task_in_window(t) for t in tasks)

    def _task_in_window(self, task, now_ms=None):
        if task.get("done"):
            return False
        ends = [task.get("end_time_ms") or 0,
                (task.get("wb") or {}).get("end_time_ms") or 0,
                (task.get("arena") or {}).get("end_time_ms") or 0]
        return any(in_window(ms, now_ms) for ms in ends)

    def _run_pass(self, full):
        with self.mu:
            if self.running:
                return
            self.running = True
        try:
            if not self.enabled:
                return
            if full:
                self._ensure_today_batch()
                if not self.enabled:
                    return
                # 固定号（坐榜号）：没满才补；补建失败过就走 5 分钟退避
                if self._anchor_need_create():
                    self._ensure_anchors()
            if not self.enabled:
                return
            self._sync_tasks()
            if not self.enabled:
                return
            # 比分数只跟其他玩家比：先把自己库里所有 uid 收集起来
            self.own_uids = self._collect_own_uids()
            # 固定号的 uid / 今天应报的分（冲刺限幅用）
            self._refresh_anchor_state()
            now_ms = int(time.time() * 1000)
            tasks = list(self.tasks)
            if not tasks:
                self._log("⚠ 今天和之前 7 天都没有可用账号，改为检查历史账号")
                self._run_historical_pass()
                return
            self._log(f"开始{'常规' if full else '冲刺'}轮：{len(tasks)} 个日期任务"
                      + ("" if full else "（只处理截止前 10 分钟内的榜）"))
            for task in tasks:
                if not self.enabled:
                    self._log("开关已关闭，本轮中止")
                    return
                if task.get("done"):
                    continue
                if not full:
                    # 冲刺轮只处理「窗口内」的任务；截止时间还不知道的也查一次，
                    # 否则刚启动/上一轮查询失败时永远进不了冲刺。
                    known = [task.get("end_time_ms") or 0,
                             (task.get("wb") or {}).get("end_time_ms") or 0,
                             (task.get("arena") or {}).get("end_time_ms") or 0]
                    if not self._task_in_window(task, now_ms) and any(known):
                        continue
                self._pass_task(task, full)
            self._run_historical_pass()
            if all(t.get("done") for t in tasks):
                with self.mu:
                    self.finished = True
                    self.next_rank_tick = now_ms / 1000 + RANK_TICK_SECONDS \
                        if self.historical_tasks else None
                    self.next_sprint_tick = 0 if self._any_window() else None
                reasons = "；".join(f"{t['day']}：{t.get('reason')}" for t in tasks)
                self._log(f"最近账号任务结束（{reasons}）。历史账号仍按计划检查魔王 / 竞技场")
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._log(f"✗ 本轮执行失败：{self.last_error}")
        finally:
            with self.mu:
                self.running = False
                now = time.time()
                if full:
                    self.next_rank_tick = now + RANK_TICK_SECONDS
                    if self.next_sprint_tick is None:
                        self.next_sprint_tick = 0
                else:
                    self.next_sprint_tick = now + SPRINT_INTERVAL

    # ───────────── 固定号（坐榜号）─────────────
    def _anchor_accounts(self):
        """账号库里所有固定号（按创建时间升序）。"""
        out = []
        for uid, acc in (self._pool() or {}).items():
            if not is_anchor(acc):
                continue
            out.append({
                "uid": str(acc.get("uid") or uid),
                "nick": str(acc.get("nickName") or ""),
                "created": str(acc.get("createdAt") or ""),
                "acc": acc,
            })
        out.sort(key=lambda a: (a["created"], int(a["uid"])))
        return out

    def _refresh_anchor_state(self, today=None):
        """把固定号的 uid 集合 + 今天应收到的分数缓存起来（给冲刺限幅和状态页用）。"""
        today = today or day_key(bj_now())
        uids, expected, names = set(), {0: {}, 1: {}}, {}
        for ref in self._anchor_accounts():
            try:
                uid = int(ref["uid"])
            except Exception:
                continue
            uids.add(uid)
            names[uid] = ref.get("nick") or ""
            t0, t1 = anchor_scores(ref.get("acc") or {}, today)
            expected[0][uid] = t0
            expected[1][uid] = t1
        with self.mu:
            self.anchor_uids = uids
            self.anchor_expected = expected
            self.anchor_names = names
        return uids

    def _anchor_floor(self, entries, rank_type):
        """某个榜上「最低的固定号分数」——冲刺目标不得越过它。

        优先用榜上的真实值（榜可能还没刷新）；榜上看不到固定号时退到「今天应报的分」。
        没有任何固定号时返回 None（= 不限幅）。
        """
        anchors = self.anchor_uids or set()
        if not anchors:
            return None
        floor = None
        for e in entries or []:
            try:
                if str(e.get("type")) != str(rank_type):
                    continue
                if int(e.get("uid") or 0) not in anchors:
                    continue
                v = int(e.get("value"))
            except Exception:
                continue
            floor = v if floor is None else min(floor, v)
        if floor is None:
            vals = list((self.anchor_expected.get(int(rank_type)) or {}).values())
            if vals:
                floor = min(vals)
        return floor

    def _anchor_next_at(self, today=None):
        """今天（或明天）21:00 的时间戳。"""
        now = bj_now()
        target = now.replace(hour=ANCHOR_SCORE_HOUR, minute=ANCHOR_SCORE_MINUTE,
                             second=0, microsecond=0)
        if (now.hour, now.minute, now.second) >= (ANCHOR_SCORE_HOUR,
                                                   ANCHOR_SCORE_MINUTE, 1):
            target += timedelta(days=1)
        return target.timestamp()

    def _anchor_score_due(self, today=None):
        """今天 21:00 之后、且今天还没跑过 → True（重启晚于 21:00 也会补跑）。

        ★ 榜单截止后不永久停掉：每天仍会用 1 个号探一次 endTime（成本只有一次登录），
          这样下个赛季开始时能自动续上。
        """
        today = today or day_key(bj_now())
        with self.mu:
            if self.anchor_score_day == today:
                return False
        now = bj_now()
        if (now.hour, now.minute) < (ANCHOR_SCORE_HOUR, ANCHOR_SCORE_MINUTE):
            return False
        return bool(self._anchor_accounts())

    def _anchor_need_create(self):
        """固定号是否还没建满（且不在冷却期）。"""
        with self.mu:
            if self.anchor_creating:
                return False
            if time.time() < self.anchor_next_try:
                return False
            n = len(self.anchor_uids)
        # ★ 这里用缓存判断，不去查库（_step 每秒都跑）
        return n < ANCHOR_COUNT

    def create_anchors(self):
        """手动补齐固定号（★ 与自动刷榜开关无关，开关关着也能建）。

        后台线程执行，接口立即返回；进度看状态页的 anchor.count 和日志。
        """
        with self.mu:
            if self.anchor_creating:
                self._log("固定号补建已在进行中，请稍候（看日志进度）")
                return self.status()
            self.anchor_next_try = 0.0        # 手动触发不受冷却限制
        threading.Thread(target=self._create_anchors_worker,
                         name="anchor-create", daemon=True).start()
        self._log(f"手动补建固定号：目标 {ANCHOR_COUNT} 个（后台执行）")
        return self.status()

    def _create_anchors_worker(self):
        """后台线程：建满固定号（force=True，不受开关控制）。"""
        try:
            self._ensure_anchors(force=True)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._log(f"✗ 固定号补建异常：{self.last_error}")

    def _anchor_view(self, now=None):
        """状态页用的固定号摘要（★ 纯内存，绝不查库 / 不拿网络锁）。

        status() 是前端每 3 秒轮询的热路径；一旦这里去拿 self.lock（建号/刷榜
        可能长时间持有），整页接口就会全部超时。所以只用 _refresh_anchor_state
        的缓存，不碰 _pool()。
        """
        with self.mu:
            uids = set(self.anchor_uids)
            expected = {t: dict(v) for t, v in (self.anchor_expected or {}).items()}
            names = dict(getattr(self, "anchor_names", {}) or {})
        scores = []
        for uid in sorted(uids)[:3]:
            scores.append({"nick": names.get(uid) or str(uid),
                           "type0": expected.get(0, {}).get(uid),
                           "type1": expected.get(1, {}).get(uid)})
        with self.mu:
            day = self.anchor_score_day
            ended = self.anchor_ended_ms
            last = self.anchor_last
            creating = self.anchor_creating
            next_try = self.anchor_next_try
        return {
            "count": len(uids),
            "target": ANCHOR_COUNT,
            "creating": bool(creating),
            "retry_at_text": (fmt_ts(next_try) if next_try else ""),
            "score_day": day,
            "ended": bool(ended),
            "last": last,
            "next_at": self._anchor_next_at(),
            "next_at_text": fmt_ts(self._anchor_next_at()),
            "scores_preview": scores,
            "type0_rule": f"{ANCHOR_TYPE0_START} 起 +{ANCHOR_TYPE0_DAILY}/天，封顶 {ANCHOR_TYPE0_CAP}",
            "type1_rule": f"{ANCHOR_TYPE1_START} 起 +{ANCHOR_TYPE1_DAILY}/天",
        }

    def _ensure_anchors(self, force=False):
        """把固定号补满 ANCHOR_COUNT 个（一次性建满，不按天累加）。

        force=True：手动触发，不受自动刷榜开关限制（开关关着也能建）。
        """
        with self.mu:
            if self.anchor_creating:
                return
            self.anchor_creating = True
        try:
            self._ensure_anchors_inner(force)
        finally:
            with self.mu:
                self.anchor_creating = False

    def _ensure_anchors_inner(self, force=False):
        anchors = self._anchor_accounts()
        missing = ANCHOR_COUNT - len(anchors)
        if missing <= 0:
            with self.mu:
                self.anchor_next_try = 0.0
            if self.anchor_batch_day != day_key(bj_now()):
                self.anchor_batch_day = day_key(bj_now())
                self._log(f"固定号已满 {len(anchors)}/{ANCHOR_COUNT} 个，跳过创建")
            return

        self.anchor_batch_day = day_key(bj_now())
        self._log(f"固定号还差 {missing} 个（现有 {len(anchors)}/{ANCHOR_COUNT}），开始创建 …")
        created = []
        used = {a["nick"] for a in anchors}
        attempts = 0
        target = missing
        # force 时仅当进程/逻辑显式中止才停，不受 enabled 影响
        while (force or self.enabled) and len(created) < target \
                and attempts < missing + 6:
            attempts += 1
            nick = anchor_nick(used)
            try:
                acc = self._create_account(nick, role=ANCHOR_ROLE,
                                           source="auto-rank-anchor",
                                           nick_factory=lambda u=used: anchor_nick(u))
            except Exception as e:
                used.add(nick)
                self._log(f"✗ 固定号 {nick} 创建失败：{type(e).__name__}: {e}")
                time.sleep(1)
                continue
            used.add(str(acc.get("nickName") or nick))
            created.append(acc)
            self._log(f"✓ 固定号 {len(anchors) + len(created)}/{ANCHOR_COUNT}："
                      f"{acc.get('nickName')} uid={acc.get('uid')}")
            # 建完立刻把「初始排行榜分数」报上去（3806 / 8101000），
            # 不等当天 21:00 —— 否则新号会空榜掛到晚上。sid 刚拿到，直接复用。
            try:
                self._push_initial_anchor_scores(acc)
            except Exception as e:
                self._log(f"  ⚠ uid={acc.get('uid')} 初始分数上报失败（21:00 会重试）："
                          f"{type(e).__name__}: {e}")
            time.sleep(0.5)

        self._refresh_anchor_state()

        if len(created) < target:
            with self.mu:
                self.anchor_next_try = time.time() + ANCHOR_RETRY_SECONDS
            self._log(f"⚠ 固定号只建成 {len(created)}/{target} 个（尝试 {attempts} 次），"
                      f"{ANCHOR_RETRY_SECONDS // 60} 分钟后自动继续补（也可点「补齐固定号」立即重试）")
        else:
            with self.mu:
                self.anchor_next_try = 0.0
            self._log(f"固定号已建满 {len(anchors) + len(created)}/{ANCHOR_COUNT} 个")

    def _push_initial_anchor_scores(self, acc):
        """建号后立即上报初始榜分（d=0 → 大选关 3806 / 爬塔 8101000）。

        复用 _create_account 刚拿到的 sid，不再多登录一次。
        """
        uid = int(acc["uid"])
        t0, t1 = anchor_scores(acc)
        notes = []
        with self.lock:
            for t, val in ((0, t0), (1, t1)):
                r = m.upload_rank_value(int(acc["sid"]), uid, val, rank_type=t)
                if not r.get("ok"):
                    raise RuntimeError(f"type={t} 上报失败 pid={r.get('pid')} "
                                       f"{r.get('error')!r}")
                notes.append(f"{'大选关' if t == 0 else '爬塔'} {r.get('value')}")
        self._log(f"  · 初始榜分：{'，'.join(notes)}")

    def _run_anchor_scores(self, today):
        """每天 21:00：把所有固定号的两个榜分数推到「今天应报的分数」。

        · 大选关 3806 起、每天 +3000、封顶 13902；爬塔 8101000 起、每天 +1000。
        · 天数按各账号自己的创建日算（同一天建的一批永远同步）。
        · 先用第一个号探一次榜单 endTime：已截止就不再动其他号。
        """
        with self.mu:
            if self.running:
                return
            self.running = True
            self.anchor_score_day = today      # 今天只跑一次（失败不自动重试，避免刷请求）
        try:
            anchors = self._anchor_accounts()
            if not anchors:
                self._log("固定号：账号库里没有固定号，跳过 21:00 加分")
                return
            self._refresh_anchor_state(today)
            self._log(f"固定号 21:00 加分开始：{len(anchors)} 个号"
                      f"（大选关 +{ANCHOR_TYPE0_DAILY} 封顶 {ANCHOR_TYPE0_CAP}，"
                      f"爬塔 +{ANCHOR_TYPE1_DAILY}）")
            ok = failed = 0
            stopped = False
            for idx, ref in enumerate(anchors, 1):
                if not self.enabled:
                    self._log("固定号加分：开关已关闭，中止")
                    return
                try:
                    done = self._push_anchor_scores(ref, today, probe=(idx == 1))
                except DeadlineReached as e:
                    end_ms = int(getattr(e, "end_ms", 0) or 0)
                    with self.mu:
                        self.anchor_ended_ms = end_ms or 1
                    self.anchor_last = (f"榜单已截止（{e}），已停止每日加分"
                                        f"（下赛季开始时自动恢复）")
                    self._log(f"固定号：榜单已截止（{e}），停止每日加分（每日仍会探一次）")
                    stopped = True
                    break
                except Exception as e:
                    failed += 1
                    self._log(f"✗ 固定号 {ref['nick']}（uid={ref['uid']}）加分失败："
                              f"{type(e).__name__}: {e}")
                    continue
                ok += 1
                if done:
                    self._log(f"✓ 固定号 {idx}/{len(anchors)} {ref['nick']}：{done}")
                time.sleep(0.3)
            if not stopped:
                self.anchor_last = f"成功 {ok}，失败 {failed}"
                self._log(f"固定号 21:00 加分完成：成功 {ok}，失败 {failed}")
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._log(f"✗ 固定号加分异常：{self.last_error}")
        finally:
            with self.mu:
                self.running = False

    def _push_anchor_scores(self, ref, today, probe=False):
        """给一个固定号上报两个榜分数；返回一句话结果（已是最新则返回空串）。"""
        uid = int(ref["uid"])
        buf = io.StringIO()
        try:
            with self.lock, redirect_stdout(buf), redirect_stderr(buf):
                pool = m._load_account_pool() or {}
                acc = pool.get(str(uid)) or ref.get("acc") or {}
                if not acc:
                    raise RuntimeError("账号库里已找不到该 uid")
                try:
                    lg = m.login(acc["userName"], acc["passwd"],
                                 acc.get("nickName") or acc["userName"],
                                 fingerprint=acc.get("fingerprint"))
                except Exception as e:
                    if is_banned_error(e):
                        self._drop_banned(acc, e)
                    raise
                acc.update({"sid": int(lg["sid"]),
                            "servertime": lg.get("servertime", 0)})
                if lg.get("otherName") and lg["otherName"] != acc.get("nickName"):
                    acc["nickName"] = lg["otherName"]
                m.save_account(acc, make_current=False)   # role 会跟着写回去
                sid = int(acc["sid"])

                # 用第一个号探一次截止时间（省请求：其余号不再查榜）
                if probe:
                    info = m.query_rank137(sid, uid)
                    if not info.get("ok"):
                        raise RuntimeError(f"查榜失败：{info.get('error')!r}")
                    end_ms = int(info.get("end_time") or 0)
                    if end_ms and int(time.time() * 1000) >= end_ms:
                        raise DeadlineReached(m.format_end_time(end_ms) or "已截止",
                                              end_ms)
                    # 探到未截止 → 清掉旧的「已截止」标记（新赛季自动恢复）
                    with self.mu:
                        self.anchor_ended_ms = 0

                t0, t1 = anchor_scores(acc, today)
                notes = []
                for t, val in ((0, t0), (1, t1)):
                    r = m.upload_rank_value(sid, uid, val, rank_type=t)
                    if not r.get("ok"):
                        raise RuntimeError(f"{t} 上报失败 pid={r.get('pid')} "
                                           f"{r.get('error')!r}")
                    notes.append(f"{'大选关' if t == 0 else '爬塔'} → {r.get('value')}")
                return "；".join(notes)
        finally:
            detail = buf.getvalue().strip()
            if detail:
                self._log(detail)

    # ───────────── 建号 ─────────────
    def _pool(self):
        with self.lock:
            return m._load_account_pool() or {}

    def _collect_own_uids(self):
        """数据库里所有账号的 uid：算榜一/魔王/竞技场时全部排除。"""
        uids = set()
        for key, acc in (self._pool() or {}).items():
            try:
                uids.add(int(acc.get("uid") or key))
            except Exception:
                continue
        for u in self.banned:
            try:
                uids.add(int(u))
            except Exception:
                pass
        return uids

    def _ensure_today_batch(self):
        """当天没有轮训账号才建 10 个；已有账号（手动建的也算）直接跳过。

        ★ 固定号（坐榜号）不按天算，单独用 _ensure_anchors() 按总数补齐。
        """
        today = day_key(bj_now())
        existing = self._accounts_of_day(today)
        if existing:
            if self.batch_day != today:
                self.batch_day = today
                self._log(f"今日已有 {len(existing)} 个账号，跳过建号"
                          f"（主号 {existing[0]['nick']}）")
            return
        self.batch_day = today

        self._log(f"今日还没有账号，开始创建 {BATCH_SIZE} 个 …")
        created = []
        failed = set()          # 建号失败的名字，别在下一轮又抽到同一个
        attempts = 0
        while self.enabled and len(created) < BATCH_SIZE and attempts < MAX_CREATE_ATTEMPTS:
            attempts += 1
            used = {str(a.get("nick") or "") for a in created} | failed
            index = next((i for i in range(1, BATCH_SIZE + 1)
                          if batch_nick(today, i) not in used), None)
            if index is None:
                break
            nick = batch_nick(today, index)
            try:
                acc = self._create_account(nick)
            except Exception as e:
                failed.add(nick)
                self._log(f"✗ 建号 {nick} 失败：{type(e).__name__}: {e}")
                time.sleep(1)
                continue
            created.append({"uid": str(acc.get("uid")), "nick": acc.get("nickName")})
            self._log(f"✓ 今日账号 {len(created)}/{BATCH_SIZE}：{acc.get('nickName')} "
                      f"uid={acc.get('uid')}")
            time.sleep(0.5)  # 建号之间轻节流，避免被服务端当成刷号

        if len(created) < BATCH_SIZE:
            self._log(f"⚠ 今日只建成 {len(created)}/{BATCH_SIZE} 个账号"
                      f"（尝试 {attempts} 次），先用已有的继续")

    def _create_account(self, nick, role=None, source="auto-rank",
                        nick_factory=None):
        """建号 + 登录 + 改名 + 落库。

        nick_factory 给了就是固定号：名字被拒时整条重新抽一个（而不是像轮训号
        那样回退前缀 / 交换末位），因为固定号的名字是「中文词组 + 数字」格式。
        """
        buf = io.StringIO()
        retry_limit = ANCHOR_NICK_RETRY if nick_factory else NICK_RETRY_LIMIT
        try:
            with self.lock, redirect_stdout(buf), redirect_stderr(buf):
                acc = m.register()
                try:
                    lg = m.login(acc["userName"], acc["passwd"],
                                 acc.get("nickName") or acc["userName"],
                                 fingerprint=acc.get("fingerprint"))
                except Exception as e:
                    if is_banned_error(e):
                        self._drop_banned(acc, e)
                    raise
                acc.update({"sid": int(lg["sid"]),
                            "servertime": lg.get("servertime", 0)})
                if lg.get("otherName"):
                    acc["nickName"] = lg["otherName"]
                # ★ 服务端会按「敏感词表」拒名字，而且数字也算（实测「别氪了8511W01」
                #   回「含有敏感词:51」）；名字被占用也会拒。这里就地换名重试，
                #   不要直接抛错——否则同一个批次号会一直撞同一堵墙。
                r = None
                cand = nick
                fell_back = False      # 是否已从「别氪了」回退到「片刻」
                for attempt in range(1, retry_limit + 1):
                    r = m.change_nickname(acc["sid"], acc["uid"], cand)
                    if r.get("ok"):
                        break
                    msg = str(r.get("message") or "")
                    sensitive = any(w in msg for w in m.SENSITIVE_WORDS)
                    taken = any(w.lower() in msg.lower() for w in m.NAME_TAKEN_WORDS)
                    if nick_factory is not None:
                        # ★ 固定号：整条重抽（词组 + 数字都换）
                        nxt = nick_factory()
                        self._log(f"  · 固定号昵称 {cand!r} 不可用（{msg}），"
                                  f"第 {attempt} 次重抽 -> {nxt!r}")
                    elif not fell_back and cand.startswith(NICK_PREFIX):
                        # ①「氪」名字不可用 → 整体回退到旧前缀「片刻」（只做一次）
                        fb = fallback_prefix(cand)
                        if fb:
                            fell_back = True
                            nxt = fb
                            self._log(f"  · 昵称 {cand!r} 不可用（{msg}），"
                                      f"第 {attempt} 次回退前缀 -> {nxt!r}")
                        else:
                            break
                    elif taken:
                        # ② 昵称重复 → 把最后一位字符串移动到名字最前面
                        nxt = move_last_to_front(cand)
                        self._log(f"  · 昵称 {cand!r} 已被占用（{msg}），"
                                  f"第 {attempt} 次移动末位 -> {nxt!r}")
                    elif sensitive:
                        nxt = m.avoid_sensitive(cand, m.sensitive_token(msg))
                        self._log(f"  · 昵称 {cand!r} 含敏感词（{msg}），"
                                  f"第 {attempt} 次换数字 -> {nxt!r}")
                    else:
                        break
                    if nxt == cand:      # 兜底：保证每轮都真的换了名字
                        nxt = m.vary_digits(cand)
                    cand = nxt
                if not r.get("ok"):
                    raise RuntimeError(f"改名失败：{r.get('message') or '服务端未接受'}")
                acc["nickName"] = r["name"]
                if role:
                    acc["role"] = role
                rec = m.save_account(acc, make_current=False, source=source)
                rec = rec or acc
                if self.db_sync:
                    res = self.db_sync(rec)
                    if res.get("ok"):
                        self._log(f"  · uid={acc['uid']} 已同步到 {res.get('detail')}")
                    else:
                        self._log(f"  ⚠ uid={acc['uid']} 同步数据库失败："
                                  f"{res.get('detail')}")
                return rec
        finally:
            detail = buf.getvalue().strip()
            if detail:
                self._log(detail)

    # ───────────── 任务（每天一个） ─────────────
    def _sync_tasks(self):
        """按「今天 + 前 7 天」同步任务列表：缺的补上，账号变多的刷新列表。"""
        now = bj_now()
        with self.mu:
            by_day = {t["day"]: t for t in self.tasks}
        for offset in range(LOOKBACK_DAYS + 1):
            day = day_key(now - timedelta(days=offset))
            accs = self._accounts_of_day(day)
            if not accs:
                continue
            t = by_day.get(day)
            if t is None:
                t = {"day": day, "accounts": accs, "index": 0, "done": False,
                     "reason": "", "last": "", "end_time_ms": 0,
                     "end_time_text": "",
                     "wb": {"end_time_ms": 0, "end_time_text": "", "last": ""},
                     "arena": {"end_time_ms": 0, "end_time_text": "", "last": ""}}
                by_day[day] = t
                self._log(f"[{day}] 待处理账号 {len(accs)} 个，主号 {accs[0]['nick']}")
            elif not t.get("done"):
                t["accounts"] = accs
                if t.get("index", 0) >= len(accs):
                    t["index"] = len(accs) - 1
        with self.mu:
            self.tasks = [by_day[d] for d in sorted(by_day, reverse=True)]

    def _accounts_of_day(self, day):
        """某一天创建的「轮训账号」（手动建的也包含），按创建时间升序。

        ★ 固定号（坐榜号）单独管理，不进日任务，也不轮训冲榜。
        """
        out = []
        for uid, acc in self._pool().items():
            if str(acc.get("uid") or uid) in self.banned:
                continue
            if is_anchor(acc):
                continue
            if created_day(acc) != day:
                continue
            out.append({
                "uid": str(acc.get("uid") or uid),
                "nick": str(acc.get("nickName") or ""),
                "created": str(acc.get("createdAt") or ""),
            })
        out.sort(key=lambda a: (a["created"], int(a["uid"])))
        return out

    def _task_view(self, t):
        accs = t.get("accounts") or []
        idx = t.get("index") or 0
        cur = accs[idx] if idx < len(accs) else None
        wb = t.get("wb") or {}
        arena = t.get("arena") or {}
        return {
            "day": t.get("day"),
            "day_text": f"{str(t.get('day'))[:4]}-{str(t.get('day'))[4:6]}-"
                        f"{str(t.get('day'))[6:]}",
            "accounts": len(accs),
            "index": idx,
            "current_nick": (cur or {}).get("nick") or "",
            "current_uid": (cur or {}).get("uid") or "",
            "done": bool(t.get("done")),
            "reason": t.get("reason") or "",
            "end_time_text": t.get("end_time_text") or "",
            "wb_end_text": wb.get("end_time_text") or "",
            "wb_last": wb.get("last") or "",
            "arena_end_text": arena.get("end_time_text") or "",
            "arena_last": arena.get("last") or "",
            "last": t.get("last") or "",
        }

    def _drop_banned(self, acc, err):
        """服务端判「帐号异常」：从账号库删除，并立刻从任务表里摘掉这个号。"""
        uid = str(acc.get("uid") or "")
        nick = str(acc.get("nickName") or "")
        self.banned.add(uid)
        res = (self.on_banned(acc) if self.on_banned
               else {"ok": False, "detail": "删号回调未注入"})
        if res.get("ok"):
            self._log(f"✗ {nick}（uid={uid}）被服务端判定「帐号异常」，"
                      f"已从数据库删除（{res.get('detail')}）")
        else:
            self._log(f"✗ {nick}（uid={uid}）被服务端判定「帐号异常」，"
                      f"删除失败：{res.get('detail')}（本次先跳过该号）")
        with self.mu:
            for t in self.tasks:
                t["accounts"] = [a for a in (t.get("accounts") or [])
                                 if str(a.get("uid")) != uid]
        return res

    def _pass_task(self, task, full):
        day = task["day"]
        accs = task["accounts"]
        while task["index"] < len(accs):
            if not self.enabled:
                return
            acc = accs[task["index"]]
            self._log(f"[{day}] 使用 {acc['nick']}（uid={acc['uid']}）")
            try:
                result = self._check_account(task, acc, full)
            except DeadlineReached as e:
                task.update(done=True, reason=f"排行榜已截止（{e}）")
                self._log(f"[{day}] 排行榜已截止（{e}），停止该日任务")
                return
            except Exception as e:
                msg = str(e) or type(e).__name__
                if is_banned_error(e):
                    task["last"] = f"{acc['nick']} 帐号异常，已删除"
                    self._log(f"[{day}] {acc['nick']} 帐号异常已删除 → 切换下一个账号")
                else:
                    task["last"] = f"异常：{msg}"
                    self._log(f"[{day}] {acc['nick']} 异常：{msg} → 切换下一个账号")
                task["index"] += 1
                continue
            task["last"] = result
            return
        task.update(done=True, reason="账号已用完")
        self._log(f"[{day}] 账号已用完，停止该日任务")

    def _check_account(self, task, acc_ref, full, historical=False):
        """登录 → 常规榜（历史账号跳过）→ 魔王 → 竞技场，返回一句话结果。"""
        uid = int(acc_ref["uid"])
        buf = io.StringIO()
        try:
            with self.lock, redirect_stdout(buf), redirect_stderr(buf):
                pool = m._load_account_pool() or {}
                acc = pool.get(str(uid))
                if not acc:
                    raise RuntimeError("账号库里已找不到该 uid")

                try:
                    lg = m.login(acc["userName"], acc["passwd"],
                                 acc.get("nickName") or acc["userName"],
                                 fingerprint=acc.get("fingerprint"))
                except Exception as e:
                    if is_banned_error(e):
                        self._drop_banned(acc, e)
                    raise
                acc.update({"sid": int(lg["sid"]),
                            "servertime": lg.get("servertime", 0)})
                if lg.get("otherName"):
                    acc["nickName"] = lg["otherName"]
                m.save_account(acc, make_current=False)
                if historical and self.db_sync:
                    # 文件模式 save_doc 会整份覆盖；MySQL 模式只 UPSERT 当前登录账号，
                    # 避免旧快照把本轮已删除的其他异常账号重新写回数据库。
                    saved = self.db_sync(acc)
                    if not saved.get("ok"):
                        self._log(f"⚠ 历史账号 uid={uid} 登录状态保存失败："
                                  f"{saved.get('detail')}")
                sid = int(acc["sid"])

                notes = []
                if not historical:
                    notes.append(self._do_rank137(task, acc, sid, full))
                notes.append(self._do_worldboss(task, acc, sid))
                notes.append(self._do_arena(task, acc, sid))
                return " | ".join(n for n in notes if n)
        finally:
            detail = buf.getvalue().strip()
            if detail:
                self._log(detail)

    # ───────────── 历史账号（只冲魔王 / 竞技场） ─────────────
    def _historical_accounts(self):
        """创建日期早于最近 7 天窗口的所有账号（不含固定号）。"""
        cutoff = day_key(bj_now() - timedelta(days=LOOKBACK_DAYS))
        out = []
        for uid, acc in (self._pool() or {}).items():
            uid = str(acc.get("uid") or uid)
            if is_anchor(acc):       # 固定号不参与魔王 / 竞技场
                continue
            day = created_day(acc)
            # 以账号库为准：成功删号后记录会消失；删除失败则下轮重试，
            # 不能因为内存 banned 集合把其它正常历史账号一并过滤掉。
            if not day or day >= cutoff:
                continue
            out.append({"uid": uid, "nick": str(acc.get("nickName") or ""),
                        "created": str(acc.get("createdAt") or "")})
        out.sort(key=lambda a: (a["created"], int(a["uid"])))
        return out

    def _sync_historical_tasks(self):
        """同步历史账号任务，保留已查询到的榜单截止时间供后续冲刺调度。"""
        current = {a["uid"]: a for a in self._historical_accounts()}
        for uid in list(self.historical_tasks):
            if uid not in current:
                self.historical_tasks.pop(uid, None)
        for uid, ref in current.items():
            if uid in self.historical_tasks:
                self.historical_tasks[uid]["account"] = ref
                continue
            self.historical_tasks[uid] = {
                "day": "历史账号", "account": ref, "done": False,
                "end_time_ms": 0, "end_time_text": "",
                "wb": {"end_time_ms": 0, "end_time_text": "", "last": ""},
                "arena": {"end_time_ms": 0, "end_time_text": "", "last": ""},
            }
        return [self.historical_tasks[uid] for uid in current]

    def _run_historical_pass(self):
        """登录全部历史账号，只执行魔王、竞技场；账号异常时走统一删号链路。"""
        tasks = self._sync_historical_tasks()
        if not tasks:
            self._log("历史账号：没有 7 天前的账号，跳过")
            return
        self._log(f"历史账号：{len(tasks)} 个，逐个登录并检查魔王 / 竞技场")
        ok = failed = banned = 0
        for task in tasks:
            if not self.enabled:
                self._log("历史账号：开关已关闭，本轮中止")
                return
            ref = task["account"]
            try:
                result = self._check_account(task, ref, full=False,
                                             historical=True)
            except Exception as e:
                msg = str(e) or type(e).__name__
                if is_banned_error(e):
                    # 以删号回调结果为准，避免删除失败仍提示“已删号”。
                    removed = ref["uid"] not in (self._pool() or {})
                    self.historical_tasks.pop(ref["uid"], None)
                    banned += 1
                    self._log(f"✗ 历史账号 {ref['nick']}（uid={ref['uid']}）"
                              + ("帐号异常，已删号并通知前端刷新" if removed
                                 else "帐号异常，但账号库删除失败，请查看上条日志"))
                else:
                    failed += 1
                    self._log(f"✗ 历史账号 {ref['nick']}（uid={ref['uid']}）：{msg}")
                continue
            ok += 1
            self._log(f"✓ 历史账号 {ref['nick']}（uid={ref['uid']}）：{result}")
        self._log(f"历史账号本轮完成：成功 {ok}，异常删号 {banned}，其他失败 {failed}")

    # ───────────── 常规榜 CMD137 ─────────────
    def _do_rank137(self, task, acc, sid, full):
        uid = int(acc["uid"])
        info = m.query_rank137_full(sid, uid)
        if not info.get("ok"):
            raise RuntimeError(f"查榜失败：{info.get('error')!r}")

        end_ms = int(info.get("end_time") or 0)
        if end_ms:
            task["end_time_ms"] = end_ms
            task["end_time_text"] = m.format_end_time(end_ms)
        if end_ms and int(time.time() * 1000) >= end_ms:
            raise DeadlineReached(task["end_time_text"] or "已截止")

        entries = info.get("entries") or []
        mine = my_values(entries, uid)
        sprint = in_window(end_ms)
        notes = []
        own = self.own_uids or {uid}

        for t, label in ((0, "大选关"), (1, "爬塔")):
            # ★ 只跟「其他玩家」比：自己库里（含其他日期的任务号 + 固定号）全部排除。
            #   所以目标永远是「其他玩家里的第一名」，不会是那 25 个固定号。
            top = top_of_others(entries, own, t)
            if not top:
                notes.append(f"{label}: 榜上没有其他玩家")
                continue

            base = int(top.get("value") or 0)
            my = mine.get(str(t))
            # 固定号地板：冲刺目标不允许越过它（避免冲榜号去冲固定号）
            floor = self._anchor_floor(entries, t) if ANCHOR_GUARD else None
            if sprint:
                target = base + RANK_HIGH_GAP
                if t == 0:
                    target = min(target, RANK_TYPE0_CAP)
                if floor is not None and target > floor - RANK_LOW_GAP:
                    # 其他玩家分数已经逼近/超过固定号：不越过固定号，宁可少冲
                    capped = floor - RANK_LOW_GAP
                    notes.append(f"{label}: 目标 {target} 会越过固定号 {floor}，"
                                 f"压到 {capped}")
                    target = capped
                    if target <= base:
                        notes.append(f"{label}: 已避开固定号（{floor}），不冲")
                        continue
                mode = "冲刺"
                if my is not None and my >= target:
                    notes.append(f"{label}: 已达标（{my} ≥ {target}），跳过")
                    continue
            else:
                if not full:
                    notes.append(f"{label}: 非冲刺窗口，跳过")
                    continue
                if my is not None and my > base:
                    notes.append(f"{label}: 已高于其他玩家榜一 {base}，保持不动")
                    continue
                target = base - RANK_LOW_GAP
                if t == 0:
                    target = min(target, RANK_TYPE0_CAP)
                if floor is not None:
                    target = min(target, floor - RANK_LOW_GAP)
                mode = "常规"
            if target <= 0:
                notes.append(f"{label}: 目标分数 {target} 无效，跳过")
                continue
            if my == target:
                notes.append(f"{label}: 已是目标 {target}，跳过")
                continue

            r = m.upload_rank_value(sid, uid, target, rank_type=t)
            if r.get("ok"):
                notes.append(f"{label}: {mode} {mine.get(str(t), '-')} → {target}")
            else:
                notes.append(f"{label}: {mode} 上报失败 pid={r.get('pid')} "
                             f"{r.get('error')!r}")
        return "；".join(notes)

    # ───────────── 世界魔王 ─────────────
    def _do_worldboss(self, task, acc, sid):
        uid = int(acc["uid"])
        st = m.query_worldboss_status(sid, uid)
        if not st.get("ok"):
            task["wb"]["last"] = f"状态查询失败：{st.get('error')}"
            return f"魔王: 状态查询失败"
        end_ms = int(st.get("end_time") or 0)
        task["wb"]["end_time_ms"] = end_ms
        task["wb"]["end_time_text"] = st.get("end_time_text") or ""
        if not in_window(end_ms):
            return "魔王: 未到截止前 10 分钟"

        key = f"{uid}:{end_ms}"
        if key in self.wb_done:
            return "魔王: 本期已上报过"
        # 本期已经由别的账号冲过 → 不再动手（榜单刷新有延迟，光靠查榜拦不住）
        owner = self.wb_owner.get(end_ms)
        if owner and int(owner) != uid:
            task["wb"]["last"] = f"本期已由 uid={owner} 冲过，跳过"
            return "魔王: 本期已由其他账号冲过，跳过"
        if int(st.get("remaining") or 0) <= 0:
            task["wb"]["last"] = "本期挑战次数已用完"
            self.wb_done.add(key)
            return "魔王: 挑战次数已用完"

        rk = m.query_worldboss_rank(sid, uid, 0)
        if not rk.get("ok"):
            task["wb"]["last"] = f"排行榜查询失败：{rk.get('error')}"
            return "魔王: 排行榜查询失败"
        entries = rk.get("entries") or []
        own = self.own_uids or {uid}

        # ★ 冲之前先看「真正的榜一」是谁：如果已经是库里自己的账号，
        #   这一轮直接跳过 —— 否则每个号都踩着上一个人的伤害再 +100~200，
        #   榜上就会被一串同前缀的自建号霸榜，一眼假。
        first = None
        for e in entries:
            if int(e.get("rank") or -1) == 1:
                first = e
                break
        if first and int(first.get("uid") or 0) in own:
            task["wb"]["last"] = (f"榜一已是我方账号（{first.get('name')}），"
                                  f"本轮跳过")
            return "魔王: 榜一已是自有账号，跳过"

        # 目标分仍然只跟「其他玩家」比（排除自己库里所有号），
        # 万一榜上除了自己没别人就直接跳过
        top = top_of_others(entries, own) or first
        if not top:
            task["wb"]["last"] = "伤害榜上没有其他玩家"
            return "魔王: 伤害榜上没有其他玩家"

        # 魔王目标 = 第一名伤害的 110%
        target = int((int(top.get("damage") or 0) * WB_MULT) // 1)
        my_total = int(st.get("total_damage") or 0)
        delta = target - my_total
        if delta <= 0:
            task["wb"]["last"] = f"累计 {my_total} 已达标 {target}"
            self.wb_done.add(key)
            self.wb_owner[end_ms] = uid      # 本期由这个号代表，别让别的号再抬
            return "魔王: 累计伤害已达标"

        r = m.upload_worldboss_damage(sid, uid, delta)
        if not r.get("ok"):
            task["wb"]["last"] = f"上报失败：{r.get('error')}"
            return f"魔王: 上报失败（{r.get('error')}）"

        self.wb_done.add(key)
        self.wb_owner[end_ms] = uid
        task["wb"]["last"] = (f"榜一 {top.get('damage')} +{delta} → "
                              f"累计 {r.get('total_damage')}（本期由本号负责冲）")
        return (f"魔王: 榜一 {top.get('damage')} → 目标 {target}"
                f"（本次 +{delta}，累计 {r.get('total_damage')}）")

    # ───────────── 竞技场 ─────────────
    def _do_arena(self, task, acc, sid):
        uid = int(acc["uid"])
        if not self.pvp_time:
            task["arena"]["last"] = "竞技场接口未注入"
            return "竞技场: 接口未注入"
        t = self.pvp_time(sid, uid)
        if not t.get("ok"):
            task["arena"]["last"] = f"时间查询失败：{t.get('error')}"
            return "竞技场: 时间查询失败"
        # ★ 用「赛季/榜单结束时间」（CMD48 endTime2，每周一 0 点）而不是「今日关闭时间」，
        #   只在榜单结束前 10 分钟才冲，避免每天 23:00 都推分把分数越抬越高。
        end_ms = int(t.get("season_end") or t.get("end_time") or 0)
        task["arena"]["end_time_ms"] = end_ms
        task["arena"]["end_time_text"] = m.format_end_time(end_ms)
        if not in_window(end_ms):
            return "竞技场: 未到榜单结束前 10 分钟"

        rk = self.arena_rank(sid, uid) if self.arena_rank else {"ok": False,
                                                              "error": "接口未注入"}
        if not rk.get("ok"):
            task["arena"]["last"] = f"查榜失败：{rk.get('error')}"
            return "竞技场: 查榜失败"
        rows = rk.get("top") or []
        # ★ 只跟其他玩家比：自己库里的号（含其他日期任务号）全部排除
        top_other = top_of_others(rows, self.own_uids or {uid})
        if not top_other:
            task["arena"]["last"] = "榜单里没有其他玩家"
            return "竞技场: 榜单里没有其他玩家"

        my_score = (rk.get("mine") or {}).get("score")
        top_score = int(top_other.get("score") or 0)
        if my_score is not None and int(my_score) > top_score:
            task["arena"]["last"] = f"已高于非自己榜一（{my_score} > {top_score}）"
            return "竞技场: 已高于非自己榜一，跳过"

        key = f"{uid}:{end_ms}"
        # ★ 每期每个号只推一次：以前是「3 分钟内不重复」，10 分钟窗口里同一个号能推
        #   3~4 次，每次 +20~100，几十个号叠起来分数就被抬得很高。现在窗口内一次即止。
        if key in self.arena_pushed:
            task["arena"]["last"] = "本期已经推过分，不再重复推"
            return "竞技场: 本期已推过，跳过"
        if not arena_sprint_enabled():
            task["arena"]["last"] = "竞技场冲刺已关闭（ZHAOHE_ARENA_SPRINT=0）"
            return "竞技场: 冲刺已关闭"

        target = top_score + random.randint(*ARENA_BONUS)
        name = acc.get("nickName") or acc.get("userName")
        lv = int(acc.get("playerLevel") or ARENA_LV_DEFAULT)
        res = m.match_pvp(sid, uid, target, lv, name)
        if not res.get("ok"):
            task["arena"]["last"] = f"推分被拒：{res.get('error') or res.get('result')}"
            return f"竞技场: 推分被拒（{res.get('result')}）"
        cancel = m.cancel_match(sid, uid)
        self.arena_pushed[key] = time.time()
        task["arena"]["last"] = (f"{my_score if my_score is not None else '-'} → "
                                 f"{target}（榜一 {top_score}）")
        return (f"竞技场: 非自己榜一 {top_score} → 上报 {target}"
                f"（取消匹配 {'OK' if cancel.get('ok') else '失败'}）")
