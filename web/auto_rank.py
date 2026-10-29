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
   · 取本期伤害榜榜一的伤害 + 随机 100~200 作为目标，
     上报差额 = 目标 − 自己当前累计伤害（CMD170 的 damage 是累加的）
3. 竞技场（CMD48/24/25/26）
   · 同样只在「今日竞技场关闭前 10 分钟」动作
   · 取非自己的榜一分数 + 随机 20~100 上报（CMD25 推分后立刻 CMD26 取消）

账号
====
· 每天 0 点检查：当天没有账号才建 10 个（昵称 = 片刻 + 日期码 + 序号）；
  当天已有账号（手动建的也算）则跳过建号。
· 今天 + 之前 7 天，每天一个任务：先取当天最早的账号，登录/查榜/上报任一步
  失败（含「帐号异常」）就换当天下一个号，直到号用完或榜单截止。
· 建号后显式 UPSERT 进 MySQL 并回读校验（server 注入 db_sync 回调）。

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
NICK_PREFIX = "片刻"
BATCH_SIZE = 10
LOOKBACK_DAYS = 7
MAX_CREATE_ATTEMPTS = BATCH_SIZE + 2
LOG_LIMIT = 400
STATUS_LOG_LINES = 200

RANK_TICK_SECONDS = 60 * 60        # 常规榜：每小时校准一次
SPRINT_WINDOW_MS = 10 * 60 * 1000  # 截止前 10 分钟开始冲刺
SPRINT_INTERVAL = 2 * 60           # 冲刺阶段每 2 分钟复查一次

RANK_LOW_GAP = 100                 # 平时比榜一低 100
RANK_HIGH_GAP = 100                # 冲刺时比榜一高 100
RANK_TYPE0_CAP = 14000             # 大选关榜上限
WB_BONUS = (100, 200)              # 魔王目标 = 榜一 + 100~200
ARENA_BONUS = (20, 100)            # 竞技场目标 = 非自己榜一 + 20~100
ARENA_LV_DEFAULT = 158             # 与页面「匹配→取消」用的 lv 一致
ARENA_REPUSH_SECONDS = 180         # 同一窗口内两次推分至少间隔 3 分钟


class DeadlineReached(RuntimeError):
    """排行榜 endTime 已过。"""


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


def fmt_ts(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _local_tz():
    return datetime.now().astimezone().tzinfo


def created_day(acc):
    """账号 createdAt（本地时间字符串）→ 北京时间 YYYYMMDD。

    ★ 只认 createdAt：老账号今天被登录/改昵称时 updated 会变成今天，
      用 updated 兜底会把它误判成「今天创建的号」。
    """
    s = str(acc.get("createdAt") or "").strip()
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_local_tz())
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
        self.mu = threading.RLock()      # 保护下面的状态字段
        self.enabled = False
        self.running = False
        self.finished = False
        self.force = False
        self.day = None
        self.batch_day = None
        self.tasks = []
        self.next_rank_tick = None
        self.next_sprint_tick = None
        self.last_error = None
        self.logs = deque(maxlen=LOG_LIMIT)
        self.wb_done = set()             # "uid:end_ms" 已上报过魔王伤害
        self.arena_pushed = {}           # "uid:end_ms" -> 上次推分时间
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
                "batch_size": BATCH_SIZE,
                "lookback_days": LOOKBACK_DAYS,
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
        if not text:
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
            except Exception as e:  # 调度本身出错不能把线程带走
                self.last_error = f"{type(e).__name__}: {e}"
                self._log(f"✗ 调度异常：{self.last_error}")
                time.sleep(5)
            time.sleep(1)

    def _step(self):
        today = day_key(bj_now())
        with self.mu:
            if self.day != today:
                self.day = today
                self.tasks = []
                self.finished = False
                self.next_rank_tick = None
                self.next_sprint_tick = None
                self.wb_done.clear()
                self.arena_pushed.clear()
                self.force = True
                self._log(f"新的一天（北京时间 {today}）：准备 0 点任务")
            force = self.force
            self.force = False
            finished = self.finished
            next_rank = self.next_rank_tick
            next_sprint = self.next_sprint_tick
        if finished:
            return
        now = time.time()
        if force or next_rank is None or now >= next_rank:
            self._run_pass(full=True)
            return
        if self._any_window() and now >= (next_sprint or 0):
            self._run_pass(full=False)

    def _any_window(self):
        with self.mu:
            tasks = list(self.tasks)
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
            self._sync_tasks()
            if not self.enabled:
                return
            now_ms = int(time.time() * 1000)
            tasks = list(self.tasks)
            if not tasks:
                self._log("⚠ 今天和之前 7 天都没有可用账号，等待下一轮")
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
            if all(t.get("done") for t in tasks):
                with self.mu:
                    self.finished = True
                    self.next_rank_tick = None
                    self.next_sprint_tick = None
                reasons = "；".join(f"{t['day']}：{t.get('reason')}" for t in tasks)
                self._log(f"今日活动结束（{reasons}）。开关保持打开，明日 0 点自动开始")
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

    # ───────────── 建号 ─────────────
    def _pool(self):
        with self.lock:
            return m._load_account_pool() or {}

    def _ensure_today_batch(self):
        """当天没有账号才建 10 个；已有账号（手动建的也算）直接跳过。"""
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
        attempts = 0
        while self.enabled and len(created) < BATCH_SIZE and attempts < MAX_CREATE_ATTEMPTS:
            attempts += 1
            used = {str(a.get("nick") or "") for a in created}
            index = next((i for i in range(1, BATCH_SIZE + 1)
                          if batch_nick(today, i) not in used), None)
            if index is None:
                break
            nick = batch_nick(today, index)
            try:
                acc = self._create_account(nick)
            except Exception as e:
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

    def _create_account(self, nick):
        buf = io.StringIO()
        try:
            with self.lock, redirect_stdout(buf), redirect_stderr(buf):
                acc = m.register()
                lg = m.login(acc["userName"], acc["passwd"],
                             acc.get("nickName") or acc["userName"])
                acc.update({"sid": int(lg["sid"]),
                            "servertime": lg.get("servertime", 0)})
                if lg.get("otherName"):
                    acc["nickName"] = lg["otherName"]
                r = m.change_nickname(acc["sid"], acc["uid"], nick)
                if not r.get("ok"):
                    raise RuntimeError(f"改名失败：{r.get('message') or '服务端未接受'}")
                acc["nickName"] = r["name"]
                rec = m.save_account(acc, make_current=False, source="auto-rank")
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
        """某一天创建的所有账号（手动建的也包含），按创建时间升序。"""
        out = []
        for uid, acc in self._pool().items():
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
                task["last"] = f"异常：{msg}"
                self._log(f"[{day}] {acc['nick']} 异常：{msg} → 切换下一个账号")
                task["index"] += 1
                continue
            task["last"] = result
            return
        task.update(done=True, reason="账号已用完")
        self._log(f"[{day}] 账号已用完，停止该日任务")

    def _check_account(self, task, acc_ref, full):
        """登录 → 常规榜 → 魔王 → 竞技场，返回一句话结果。"""
        uid = int(acc_ref["uid"])
        buf = io.StringIO()
        try:
            with self.lock, redirect_stdout(buf), redirect_stderr(buf):
                pool = m._load_account_pool() or {}
                acc = pool.get(str(uid))
                if not acc:
                    raise RuntimeError("账号库里已找不到该 uid")

                lg = m.login(acc["userName"], acc["passwd"],
                             acc.get("nickName") or acc["userName"])
                acc.update({"sid": int(lg["sid"]),
                            "servertime": lg.get("servertime", 0)})
                if lg.get("otherName"):
                    acc["nickName"] = lg["otherName"]
                m.save_account(acc, make_current=False)
                sid = int(acc["sid"])

                notes = []
                notes.append(self._do_rank137(task, acc, sid, full))
                notes.append(self._do_worldboss(task, acc, sid))
                notes.append(self._do_arena(task, acc, sid))
                return " | ".join(n for n in notes if n)
        finally:
            detail = buf.getvalue().strip()
            if detail:
                self._log(detail)

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
        tops = m.rank_top_per_type(entries)
        mine = my_values(entries, uid)
        sprint = in_window(end_ms)
        notes = []

        for t, label in ((0, "大选关"), (1, "爬塔")):
            top = tops.get(t)
            if not top or int(top.get("rank", -1)) <= 0:
                notes.append(f"{label}: 无榜一数据")
                continue
            if int(top["uid"]) == uid:
                notes.append(f"{label}: 已是榜一，保持不动")
                continue

            base = int(top.get("value") or 0)
            if sprint:
                target = base + RANK_HIGH_GAP
                if t == 0:
                    target = min(target, RANK_TYPE0_CAP)
                mode = "冲刺"
            else:
                if not full:
                    notes.append(f"{label}: 非冲刺窗口，跳过")
                    continue
                target = base - RANK_LOW_GAP
                if t == 0:
                    target = min(target, RANK_TYPE0_CAP)
                mode = "常规"
            if target <= 0:
                notes.append(f"{label}: 目标分数 {target} 无效，跳过")
                continue
            if mine.get(str(t)) == target:
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
        if int(st.get("remaining") or 0) <= 0:
            task["wb"]["last"] = "本期挑战次数已用完"
            self.wb_done.add(key)
            return "魔王: 挑战次数已用完"

        rk = m.query_worldboss_rank(sid, uid, 0)
        if not rk.get("ok"):
            task["wb"]["last"] = f"排行榜查询失败：{rk.get('error')}"
            return "魔王: 排行榜查询失败"
        entries = rk.get("entries") or []
        top = entries[0] if entries else None
        if not top:
            task["wb"]["last"] = "伤害榜暂无数据"
            return "魔王: 伤害榜暂无数据"
        if int(top.get("uid") or 0) == uid:
            task["wb"]["last"] = "已是榜一"
            self.wb_done.add(key)
            return "魔王: 已是榜一"

        target = int(top.get("damage") or 0) + random.randint(*WB_BONUS)
        my_total = int(st.get("total_damage") or 0)
        delta = target - my_total
        if delta <= 0:
            task["wb"]["last"] = f"累计 {my_total} 已达标 {target}"
            self.wb_done.add(key)
            return "魔王: 累计伤害已达标"

        r = m.upload_worldboss_damage(sid, uid, delta)
        if not r.get("ok"):
            task["wb"]["last"] = f"上报失败：{r.get('error')}"
            return f"魔王: 上报失败（{r.get('error')}）"

        self.wb_done.add(key)
        task["wb"]["last"] = (f"榜一 {top.get('damage')} +{delta} → "
                              f"累计 {r.get('total_damage')}")
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
        end_ms = int(t.get("end_time") or 0)      # 今日关闭时间
        task["arena"]["end_time_ms"] = end_ms
        task["arena"]["end_time_text"] = m.format_end_time(end_ms)
        if not in_window(end_ms):
            return "竞技场: 未到截止前 10 分钟"

        rk = self.arena_rank(sid, uid) if self.arena_rank else {"ok": False,
                                                              "error": "接口未注入"}
        if not rk.get("ok"):
            task["arena"]["last"] = f"查榜失败：{rk.get('error')}"
            return "竞技场: 查榜失败"
        rows = rk.get("top") or []
        top_other = next((r for r in rows if int(r.get("uid") or 0) != uid), None)
        if not top_other:
            task["arena"]["last"] = "榜单里没有其他玩家"
            return "竞技场: 榜单里没有其他玩家"

        my_score = (rk.get("mine") or {}).get("score")
        top_score = int(top_other.get("score") or 0)
        if my_score is not None and int(my_score) > top_score:
            task["arena"]["last"] = f"已高于非自己榜一（{my_score} > {top_score}）"
            return "竞技场: 已高于非自己榜一，跳过"

        key = f"{uid}:{end_ms}"
        if time.time() - float(self.arena_pushed.get(key) or 0) < ARENA_REPUSH_SECONDS:
            task["arena"]["last"] = "3 分钟内已推过分，等待下一轮复查"
            return "竞技场: 3 分钟内已推过分"

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
