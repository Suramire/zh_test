#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zhaohe 脱机工具 —— Web 后端（纯标准库，无第三方依赖）

启动：
    python3 web/server.py --port 8000
    浏览器打开 http://127.0.0.1:8000

存储约定
========
    accounts.json                      ★ 唯一的账号文件：
                                         {"current": "<uid>",
                                          "accounts": {"<uid>": {uid,userName,passwd,nickName,...}}}
                                       新建账号写入这里；切换账号只改这里的 current；
                                       所有操作都从这里的 current 取账号。

    web/soco_template.py               内嵌的 soco.bin 模板（base64，纯内存使用，不落盘）
    accounts/<uid>/NetData.bin         该账号的登录凭证（新建账号后生成并回读校验）

★ 本服务可直接部署到服务器：除上面这些自身目录外，不读写任何本机绝对路径；
  存档/凭证一律「现生成 → 打包 zip → 由浏览器下载」，不落地到 uid 目录。

API
===
    GET  /api/state                     账号文件内容 + 当前账号 + 内嵌模板信息
    POST /api/account/register          新建账号（注册→登录→写入 accounts.json→生成 NetData.bin）
    POST /api/account/select            {uid} 切换当前账号（改 accounts.json 的 current）
    POST /api/account/login             当前账号重新登录，刷新 sid
    POST /api/account/import            {userName,passwd,nickName,uid} 导入账号
    POST /api/account/nickname          {mode:manual|auto, nickName} 独立改昵称
    POST /api/account/level             {level} 写入玩家等级（CMD60）
    POST /api/arena/score               {score,is_win} 竞技场结算加分（CMD29，★只记战报不改积分）
    POST /api/arena/match               {score,wait,lv,keep} 匹配→轮询→取消（CMD25/38/26），
                                        把本地积分推给服务端刷新排行榜
    POST /api/arena/rank                竞技场排行榜（CMD24）：我的名次 + 前 3 名
    POST /api/accounts/import           {data, select} 批量导入账号列表
    GET  /api/accounts/export?format=   导出账号列表（json/csv，直接下载）
    GET  /api/account/files?uid=        列出该账号目录下的文件
    GET  /api/account/file?uid=&name=   下载该账号目录下的文件
    POST /api/rank/query                查 CMD137 两个榜（自动向上翻窗口到 rank=1）
    POST /api/rank/upload               {score,type,rename,verify} 上报分数
    POST /api/rank/jump                 查榜一 → 分别按（分数÷100+1）×100 上报
    POST /api/worldboss/damage          {damage,kind,monster,data_hex,send_counter,precheck}
                                        世界魔王伤害上报（默认先查 CMD142 判断本期是否开放）
    POST /api/worldboss/status          世界魔王本期状态（只读）
    POST /api/worldboss/rank            {type} 世界魔王排行榜（CMD176，只读）
    GET  /api/save/export?chapter=&open_switches=&uid=
                                       导出下载 zip（soco.bin + NetData.bin），不落盘
    GET  /api/logs?uid=&limit=          最近操作日志（内存）
"""
import argparse
import base64
import glob
import gzip
import hmac
import io
import json
import os
import re
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
import zlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import offline_play as m  # noqa: E402
import zhaohe_protocol as P  # noqa: E402

# ── 数据目录 ──
# 账号池与凭证目录都跟着 DATA_DIR 走，代码里不要再直接拼 ROOT/accounts。
# 解析顺序：ZHAOHE_DATA_DIR → 自动探测已挂载的持久盘（/var/data、/data、/mnt/data）→ 项目根目录
def _resolve_data_dir():
    env = (os.environ.get("ZHAOHE_DATA_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env)), "env"
    for cand in ("/var/data", "/data", "/mnt/data"):
        try:
            if os.path.isdir(cand) and os.path.ismount(cand) and os.access(cand, os.W_OK):
                return cand, "mount"
        except OSError:
            continue
    return ROOT, "default"


DATA_DIR, DATA_DIR_SOURCE = _resolve_data_dir()


def data_persistent():
    """数据目录是否「重启/重新部署后还在」。

    · 本地/自建服务器：会持久化
    · 托管平台（Render 设了 RENDER=true）：只有挂了磁盘（挂载点）才持久化，
      否则是临时容器文件系统，重新部署或休眠唤醒后账号数据会丢
    """
    try:
        if os.path.ismount(DATA_DIR):
            return True
    except OSError:
        pass
    if os.environ.get("RENDER") or os.environ.get("DYNO") or os.environ.get("FLY_APP_NAME"):
        return False
    return True


ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")
try:
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
except OSError:
    pass

# 让 offline_play 里的取账号/记录 sid 也落到同一个数据目录
m.BASE_DIR = DATA_DIR
m.ACCOUNT_FILE = os.path.join(DATA_DIR, "accounts.json")
m.LEGACY_ACCOUNT_FILE = os.path.join(DATA_DIR, "offline_account.json")
m.LEGACY_POOL_FILE = os.path.join(ACCOUNTS_DIR, "accounts.json")
m.SID_UID_DIR = os.path.join(ACCOUNTS_DIR, "sid_uid")

STATIC_DIR = os.path.join(ROOT, "web", "static")

sys.path.insert(0, os.path.join(ROOT, "web"))
from soco_template import TEMPLATE_SIZE, data as template_bytes  # noqa: E402

LOCK = threading.RLock()
LOG_RING = []
MAX_RING = 300


# ─────────────────────────────────────
#  基础工具
# ─────────────────────────────────────
def auth_spec():
    """HTTP Basic 认证配置，形如 "user:pass"；未设置则不鉴权（适合内网/本机）。"""
    return (os.environ.get("ZHAOHE_AUTH") or "").strip()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def account_dir(uid, ensure=True):
    """账号目录（服务自身数据目录）。

    ★ 默认不创建：只有真正要写文件时（注册写 NetData.bin）才 ensure=True，
      否则单纯「看一眼文件列表」也会顺手建出空目录。
    """
    d = os.path.join(ACCOUNTS_DIR, str(int(uid)))
    if ensure:
        os.makedirs(d, exist_ok=True)
    return d


def rel(path):
    """项目内路径显示相对路径，项目外保持绝对路径。"""
    try:
        p = os.path.abspath(path)
        if p == ROOT or p.startswith(ROOT + os.sep):
            return os.path.relpath(p, ROOT)
        return p
    except Exception:
        return path


def _append_op(uid, action, ok, text, tag=None):
    """操作日志只放内存（账号数据只落 accounts.json 一个文件）。"""
    LOG_RING.append({"time": now_str(), "uid": uid, "action": action,
                     "ok": ok, "log": text, "tag": tag})
    del LOG_RING[:-MAX_RING]


def _append_health(ip, path, ua=None):
    """记录一次 /health 访问。

    ★ 探活频率很高（平台每 30s 一次），连续同一来源的请求合并为一条并累计次数，
      否则 300 条的日志环会被它刷满、真实操作全被挤掉。
    """
    now = now_str()
    text = f"健康检查 {ip}{' | ' + ua[:60] if ua else ''}"
    if LOG_RING:
        last = LOG_RING[-1]
        if last.get("tag") == "health" and last.get("action") == path \
                and last.get("uid") is None:
            last["n"] = int(last.get("n") or 1) + 1
            last["time"] = now
            last["log"] = f"{text}（连续 {last['n']} 次）"
            return
    LOG_RING.append({"time": now, "uid": None, "action": path, "ok": True,
                     "log": text, "tag": "health", "n": 1})
    del LOG_RING[:-MAX_RING]


def logs_as_text(uid=None):
    """把内存日志导出成纯文本。"""
    rows = LOG_RING
    if uid:
        rows = [r for r in rows if str(r.get("uid")) == str(uid)]
    out = [f"# 日志导出 {now_str()}  共 {len(rows)} 条", ""]
    for r in rows:
        who = f" uid={r.get('uid')}" if r.get("uid") else ""
        n = f" ×{r['n']}" if r.get("n") else ""
        out.append(f"[{r.get('time')}] {r.get('action')} "
                   f"{'OK' if r.get('ok') else 'FAIL'}{who}{n}")
        for line in str(r.get("log") or "").splitlines():
            out.append(f"    {line}")
        out.append("")
    return "\n".join(out)


def run_logged(uid, action, fn):
    """串行执行 fn，把 print 输出收集成日志返回。"""
    buf = io.StringIO()
    ok, err, data = True, None, None
    with LOCK:
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                data = fn()
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {e}"
            if not isinstance(e, (RuntimeError, ValueError)):
                buf.write("\n" + traceback.format_exc())
    text = buf.getvalue().rstrip()
    # 只读轮询类接口不进日志，否则会把自己刷满
    if action not in ("/api/logs", "/api/state", "/api/account/files"):
        _append_op(uid, action, ok, text)
    return {"ok": ok, "data": data, "logs": text.splitlines(), "error": err}


def current_account():
    m.migrate_current_account()
    return m._account_from_file(m.ACCOUNT_FILE)


def require_account():
    acc = current_account()
    if not acc:
        raise RuntimeError("还没有当前账号，请先新建或选择账号")
    return acc


def login_current():
    """用当前账号重新登录，刷新 sid，并回写唯一账号文件。"""
    acc = require_account()
    lg = m.login(acc["userName"], acc["passwd"],
                 acc.get("nickName") or acc["userName"])
    uid = int(lg.get("uid") or acc["uid"])
    acc = dict(acc)
    acc.update({"uid": uid, "sid": int(lg["sid"]),
                "servertime": lg.get("servertime", 0)})
    if lg.get("otherName"):
        acc["nickName"] = lg["otherName"]
    m.save_account(acc, make_current=True, source="web")
    print(f"  ✓ 登录成功 uid={uid}  sid={acc['sid']}  "
          f"nick={acc.get('nickName')}  status={lg.get('status')}")
    return acc, lg


def template_blob():
    """内嵌的 soco.bin 模板字节（纯内存，不写文件 —— 便于部署到服务器）。"""
    return template_bytes()


def public_account(acc):
    if not acc:
        return None
    d = os.path.join(ACCOUNTS_DIR, str(int(acc["uid"])))
    return {
        "uid": int(acc["uid"]) if acc.get("uid") else None,
        "userName": acc.get("userName"),
        "nickName": acc.get("nickName"),
        "playerLevel": acc.get("playerLevel"),
        "sid": acc.get("sid"),
        "status": acc.get("status"),
        "updated": acc.get("updated"),
        "createdAt": acc.get("createdAt"),
        "dir": rel(d) if os.path.isdir(d) else None,
    }


# ─────────────────────────────────────
#  业务动作
# ─────────────────────────────────────
def api_state(_body=None):
    m.migrate_current_account()
    pool = m._load_account_pool()
    cur = m._account_from_file(m.ACCOUNT_FILE) or {}
    cur_uid = str(cur.get("uid") or "")

    accounts = []
    for uid, r in pool.items():
        accounts.append({
            "uid": int(r.get("uid") or uid),
            "userName": r.get("userName"),
            "nickName": r.get("nickName"),
            "updated": r.get("updated"),
            "createdAt": r.get("createdAt"),
            "current": str(uid) == cur_uid,
        })
    accounts.sort(key=lambda a: str(a.get("updated") or ""), reverse=True)

    return {
        "current": public_account(cur) if cur else None,
        "accounts": accounts,
        "template": {"path": "内嵌模板（不落盘）", "exists": True,
                     "embedded": True, "size": TEMPLATE_SIZE},
        "paths": {"root": ROOT,
                  "accounts": rel(ACCOUNTS_DIR),
                  "data_dir": rel(DATA_DIR),
                  "account_file": rel(m.ACCOUNT_FILE)},
        "storage": {"dir": rel(DATA_DIR), "source": DATA_DIR_SOURCE,
                    "persistent": data_persistent(),
                    "account_file_exists": os.path.exists(m.ACCOUNT_FILE)},
    }


def api_register(_body=None):
    print("[web] 注册新账号 ...")
    acc = m.register()
    print(f"  ✓ 注册成功 uid={acc['uid']}  user={acc['userName']}")
    lg = m.login(acc["userName"], acc["passwd"], acc["nickName"])
    acc.update({"sid": int(lg["sid"]), "servertime": lg.get("servertime", 0)})
    if lg.get("otherName"):
        acc["nickName"] = lg["otherName"]
    print(f"  ✓ 登录成功 sid={acc['sid']}  nick={acc['nickName']}")

    # ★ 账号密码 uid 同步写进唯一账号文件，并设为当前账号
    rec = m.save_account(acc, make_current=True, source="web-register")
    print(f"  ✓ 已写入 {rel(m.ACCOUNT_FILE)} 并设为当前账号"
          f"（共 {len(m._load_account_pool())} 个账号）")

    d = account_dir(acc["uid"])
    nd = os.path.join(d, "NetData.bin")
    n = m.write_netdata(nd, acc["uid"], acc["userName"],
                        acc["passwd"], acc["nickName"])
    print(f"  ✓ NetData.bin 已写入 {rel(nd)} ({n} 字节)")

    # 回读校验（新建账号后对指定文件读写）
    exist = m.read_existing_netdata(nd)
    if not exist or int(exist.get("uid") or 0) != int(acc["uid"]):
        raise RuntimeError(f"NetData.bin 回读校验失败: {exist}")
    print(f"  ✓ 回读校验通过 uid={exist['uid']} user={exist.get('userName')}")

    # 再回读唯一账号文件，确认切换后能取到该账号
    back = m.load_saved_account()
    if not back or int(back["uid"]) != int(acc["uid"]):
        raise RuntimeError(f"账号文件回读失败: {back}")
    print(f"  ✓ 账号文件回读通过 current uid={back['uid']}")

    return {"account": public_account(rec), "netdata": exist}


def api_account_select(body):
    uid = str(body.get("uid") or "").strip()
    if not uid:
        raise RuntimeError("缺少 uid")
    pool = m._load_account_pool()
    rec = pool.get(uid)
    if not rec:
        raise RuntimeError(f"账号文件里没有 uid={uid}")
    m.save_account(rec, make_current=True, source="web-select")
    print(f"  ✓ 当前账号已切换 -> uid={uid}  nick={rec.get('nickName')}")
    return {"current": public_account(rec)}


def api_account_login(_body=None):
    acc, _lg = login_current()
    return {"current": public_account(acc)}


def api_account_import(body):
    uid = body.get("uid")
    user = (body.get("userName") or "").strip()
    pwd = (body.get("passwd") or "").strip()
    if not (user and pwd):
        raise RuntimeError("userName / passwd 不能为空")
    acc = {"userName": user, "passwd": pwd,
           "nickName": (body.get("nickName") or user).strip()}
    if uid:
        acc["uid"] = int(uid)
    if not acc.get("uid"):
        lg = m.login(acc["userName"], acc["passwd"], acc["nickName"])
        acc["uid"] = int(lg["uid"])
        acc["sid"] = int(lg["sid"])
        acc["nickName"] = lg.get("otherName") or acc["nickName"]
        print(f"  ✓ 登录校验通过 uid={acc['uid']}")
    rec = m.save_account(acc, make_current=True, source="web-import")
    print(f"  ✓ 已导入账号 uid={acc['uid']} user={user} -> {rel(m.ACCOUNT_FILE)}")
    return {"current": public_account(rec)}


def api_account_nickname(body):
    """独立改昵称：手动指定，或自动从榜单底名生成。"""
    acc, lg = login_current()
    mode = (body.get("mode") or "manual").lower()

    if mode == "auto":
        print("\n[nick] 自动生成昵称（从榜单取底名 + 随机数字）...")
        name, renamed, _ = m.ensure_nickname(
            acc["sid"], acc["uid"],
            lg.get("otherName") or acc.get("nickName"), force=True)
        if not renamed:
            raise RuntimeError("自动改名失败：服务端没有接受任何候选名")
    else:
        name = (body.get("nickName") or "").strip()
        if not name:
            raise RuntimeError("昵称不能为空")
        if name.startswith(m.NAME_CODE_PREFIX):
            name = name[len(m.NAME_CODE_PREFIX):]
        print(f"\n[nick] 修改昵称 -> {name!r}")
        r = m.change_nickname(acc["sid"], acc["uid"], name)
        if not r.get("ok"):
            raise RuntimeError(f"改名失败：{r.get('message') or '服务端未返回昵称'}")
        name = r["name"]
        print(f"  ✓ 改名成功：{name!r}")

    acc["nickName"] = name
    m.save_account(acc, make_current=True, source="web-nick")
    print(f"  ✓ 已更新 {rel(m.ACCOUNT_FILE)} 里的 nickName")
    return {"account": public_account(acc), "nickName": name}


def api_account_level(body):
    """写入玩家等级（CMD60 ChangeValues，key1 = "<level>_0000"）。"""
    acc, _lg = login_current()
    level = int(body.get("level") or 0)
    if level <= 0:
        raise RuntimeError("等级必须是正整数")

    print(f"\n[level] 写入玩家等级 -> {level} 级（{level}_0000）")
    r = m.set_player_level(acc["sid"], acc["uid"], level)
    if not r.get("ok"):
        raise RuntimeError(f"写入失败：{r.get('error')}")
    print(f"  ✓ 写入成功：result={r['result']}  等级 {level}")

    acc["playerLevel"] = level
    m.save_account(acc, make_current=True, source="web-level")
    return {"account": public_account(acc), "level": level,
            "result": r["result"]}


def api_arena_score(body):
    """竞技场对战结算加分（CMD29 DuizhanoverCode）。"""
    acc, _lg = login_current()
    score = int(body.get("score") or 0)
    if score < 0:
        raise RuntimeError("加分不能为负数")
    is_win = 1 if int(body.get("is_win", 1)) else 0

    print(f"\n[arena] 竞技场结算：iswin={is_win}  addScore={score}")
    r = m.arena_add_score(acc["sid"], acc["uid"], score, is_win=is_win)
    if not r.get("ok"):
        raise RuntimeError(f"结算失败：{r.get('error')}")
    print(f"  ✓ 结算成功：result={r['result']}  score={r['score']}")
    return {"account": public_account(acc), **r}


# ─────────────────────────────────────
#  竞技场：匹配推分（CMD25 → CMD38 → CMD26）+ 排行榜（CMD24）
# ─────────────────────────────────────
# ★★ 关键结论（真机日志实证）：
#   CMD25 PipeiCode 的 score 字段就是 GameData.playerScore（竞技场积分）。
#   服务端收到即更新自己的积分记录，排行榜随之刷新 —— 不必真打，也不用 CMD29。
#   实证：本地改 1300 → 匹配 score=1300 → 取消后 CMD24 名次 1170 → 2。
#   而 CMD29 只上报 iswin/addScore 战报，不参与积分计算。
CMD_PIPEI_CODE = 25      # PipeiCodeReq/Ack：开始匹配（携带 score / lv / 名字 / 两段阵容）
CMD_PIPEI_INFO = 38      # PipeiInfoReq/Ack：匹配进度轮询（游戏里约 2 秒一次）
CMD_PIPEI_OFF = 26       # PipeioffCodeReq/Ack：取消匹配
CMD_PAIHANGBANG = 24     # PaihangbangCodeReq/Ack：竞技场排行榜
CMD_PVP_TIME = 48        # PvPTimeReq/Ack：竞技场时间（今日开启/今日关闭/本赛季结束）
MATCH_GUAIWU_LEN = 92    # 日志实测：守卫阵容字节长度（服务端只存档，填 0 也能进队列）
MATCH_WUPIN_LEN = 317    # 日志实测：物品阵容字节长度
MATCH_LV_DEFAULT = 158   # 日志实测：真机随分数一起上报的 lv 字段
# 本机容器存档（仅开发机兜底；服务器部署时读不到属正常，可用环境变量 ZHAOHE_SAVE_PATH 指定）
ARENA_SAVE_GLOB = os.path.expanduser(
    "~/Library/Containers/DA269E38-2FF7-4002-9464-743920492C57/Data/Library/local/data/soco.bin")


def wire(cmd, sid, uid, fields, timeout=20, counter=1):
    """发一个协议包，返回 (pid, blob)；blob[0] 是 pid，blob[1:] 是参数。

    包头与真机一致：[Long]s_sessionId(0) [Int]counter [Byte]cmd [Long]sid [Long]uid
    fields 支持 ("int"|"long"|"short"|"utf"|"bytes", 值)。
    """
    body = P.w_long(0) + P.w_int(counter) + P.w_byte(cmd)
    body += P.w_long(sid) + P.w_long(uid)
    for typ, val in fields:
        if typ == "int":
            body += P.w_int(val)
        elif typ == "long":
            body += P.w_long(val)
        elif typ == "short":
            body += P.w_short(val)
        elif typ == "utf":
            body += P.w_utf(str(val))
        elif typ == "bytes":
            blob = bytes(val or b"")
            body += P.w_int(len(blob)) + blob
        else:
            raise RuntimeError(f"未知字段类型 {typ!r}")
    crc = zlib.crc32(body) & 0xFFFFFFFF
    payload = struct.pack(">I", len(body)) + struct.pack(">I", crc) + body
    req = urllib.request.Request(P.URL, data=payload, method="POST")
    req.add_header("Connection", "Keep-Alive")
    req.add_header("KEY", f"{counter}^{int(time.time() * 1000)}")
    data = urllib.request.urlopen(req, timeout=timeout).read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    _cnt, ln = struct.unpack_from(">II", data, 0)
    blob = data[8:8 + ln]
    return blob[0], blob


def _err_text(blob):
    """服务器错误包：[B]255 [Short]字符数 [UTF-16BE]文案"""
    try:
        n = int.from_bytes(blob[1:3], "big")
        return blob[3:3 + n * 2].decode("utf-16-be", "replace")
    except Exception:
        return blob[1:].hex()


def save_candidates(uid=None):
    """可用的 soco.bin 候选路径。

    优先级：环境变量 ZHAOHE_SAVE_PATH → accounts/<uid>/soco.bin → 本机容器存档（仅开发机）。
    ★ 部署到服务器时这些通常都不存在，属正常情况。
    """
    out = []
    env = (os.environ.get("ZHAOHE_SAVE_PATH") or "").strip()
    if env:
        out.append(os.path.expanduser(env))
    if uid:
        out.append(os.path.join(ACCOUNTS_DIR, str(int(uid)), "soco.bin"))
    out.append(os.path.expanduser(ARENA_SAVE_GLOB))
    return out


def local_arena_score(uid=None):
    """读存档里的 GameData.playerScore，返回 (值, 昵称, 路径)。

    ★ 读不到不是错误（服务器上没有本地存档很正常），调用方用默认分或让用户手填。
    """
    tools = os.path.join(ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import pvp_score as S
    tried = []
    for path in save_candidates(uid):
        if not path or not os.path.exists(path):
            continue
        tried.append(path)
        try:
            loc = S.locate_player_score(open(path, "rb").read())
        except Exception:
            continue
        return int(loc["value"]), loc["name"], path
    raise RuntimeError("读不到本地存档（服务器部署属正常，请直接填分数）"
                       + (f"，已试 {len(tried)} 个路径" if tried else ""))


def pipei_start(sid, uid, score, lv, name, fangjian_id=0, timeout=20):
    """CMD25 开始匹配。ack = [Int]result(倒计时秒) [Int]fangjianID [Int]fangjianType。

    ★ result > 0 表示已进队列；<= 0 是错误码。
    """
    pid, b = wire(CMD_PIPEI_CODE, sid, uid, [
        ("int", int(score)), ("int", int(lv)), ("utf", name),
        ("bytes", bytes(MATCH_GUAIWU_LEN)), ("bytes", bytes(MATCH_WUPIN_LEN)),
        ("int", int(fangjian_id))], timeout=timeout)
    if pid != CMD_PIPEI_CODE:
        return {"ok": False, "pid": pid, "error": _err_text(b)}
    r = b[1:]
    result = int.from_bytes(r[0:4], "big", signed=True) if len(r) >= 4 else 0
    fj_id = int.from_bytes(r[4:8], "big", signed=True) if len(r) >= 8 else 0
    fj_type = int.from_bytes(r[8:12], "big", signed=True) if len(r) >= 12 else 0
    return {"ok": result > 0, "pid": pid, "result": result,
            "fangjian_id": fj_id, "fangjian_type": fj_type, "error": None}


def pipei_poll(sid, uid, timeout=20):
    """CMD38 轮询匹配进度。ack 首个 Int 是剩余秒数，Long 是对手 uid（0 = 还没配上）。"""
    pid, b = wire(CMD_PIPEI_INFO, sid, uid, [("int", 0)], timeout=timeout)
    if pid != CMD_PIPEI_INFO:
        return {"ok": False, "pid": pid, "error": _err_text(b)}
    r = b[1:]
    try:
        remain = int.from_bytes(r[0:4], "big", signed=True)
        opp_uid = int.from_bytes(r[4:12], "big")
        opp_score = int.from_bytes(r[12:16], "big", signed=True)
        opp_name, _off = P.read_utf(r, 16)
    except Exception as e:
        return {"ok": False, "pid": pid, "error": f"解析失败: {e}"}
    return {"ok": True, "pid": pid, "remain": remain, "opp_uid": opp_uid,
            "opp_score": opp_score, "opp_name": opp_name, "error": None}


def pipei_cancel(sid, uid, timeout=20):
    """CMD26 取消匹配。ack = [Int]result（1 = 成功）。"""
    pid, b = wire(CMD_PIPEI_OFF, sid, uid, [], timeout=timeout)
    if pid != CMD_PIPEI_OFF:
        return {"ok": False, "pid": pid, "error": _err_text(b)}
    result = int.from_bytes(b[1:5], "big", signed=True) if len(b) >= 5 else None
    return {"ok": result == 1, "pid": pid, "result": result, "error": None}


def pvp_time(sid, uid, timeout=20):
    """CMD48 竞技场时间。

    ack = [Int]result [Long]bTime [Long]endTime [Long]endTime2   （都是毫秒时间戳）
    ★ 实测 + smali 对照（Netsender ack48 分支）：
      · bTime    → UI_PVP.Btime    今日竞技场开启（08:00）
      · endTime  → UI_PVP.Endtime  今日竞技场关闭（23:00）
      · endTime2 → GameData.PVPbaoxiangTime2 / 0x240c8400(一周毫秒) → 本周（赛季）结束
                    实测 1793548800000 = 2026-11-02 00:00:00（周一），与 CMD137 的 endTime 完全一致
    """
    pid, b = wire(CMD_PVP_TIME, sid, uid, [], timeout=timeout)
    if pid != CMD_PVP_TIME:
        return {"ok": False, "pid": pid, "error": _err_text(b)}
    r = b[1:]
    if len(r) < 28:
        return {"ok": False, "pid": pid, "error": f"响应过短（{len(r)} 字节）"}
    result = int.from_bytes(r[0:4], "big", signed=True)
    btime = int.from_bytes(r[4:12], "big", signed=True)
    endtime = int.from_bytes(r[12:20], "big", signed=True)
    endtime2 = int.from_bytes(r[20:28], "big", signed=True)
    return {"ok": result == 1, "pid": pid, "result": result,
            "b_time": btime, "end_time": endtime, "season_end": endtime2,
            "error": None}


_TZ = timezone(timedelta(hours=8))
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def fmt_ms(ms):
    """毫秒时间戳 → '2026-11-02 00:00（周一）'，0 视为未设置。"""
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return "未设置"
    if ms <= 0:
        return "未设置"
    dt = datetime.fromtimestamp(ms / 1000.0, _TZ)
    return f"{dt:%Y-%m-%d %H:%M}（{WEEKDAY_CN[dt.weekday()]}）"


def fmt_left(ms):
    """距某毫秒时间戳还剩多久：'4 天 4 小时' / '3 小时 12 分' / '已结束'。"""
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return "-"
    if ms <= 0:
        return "-"
    left = ms / 1000.0 - time.time()
    if left <= 0:
        return "已结束"
    d, rem = divmod(int(left), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分"


def arena_season(sid, uid):
    """取竞技场时间并整理成展示用字段（失败返回 ok=False，不抛）。"""
    info = pvp_time(sid, uid)
    if not info.get("ok"):
        return info
    info["season_end_text"] = fmt_ms(info["season_end"])
    info["season_left_text"] = fmt_left(info["season_end"])
    info["today_open_text"] = fmt_ms(info["b_time"])
    info["today_close_text"] = fmt_ms(info["end_time"])
    return info


def arena_rank(sid, uid, timeout=25, top=3):
    """CMD24 排行榜。

    ack = [Int]result [Int]myRank [Short]size
          循环 size 条：[Short]记录长 [Long]uid [Int]等级 [UTF]昵称 [Int]分数
    ★ 记录里那个 Int 是【等级】，名次 = 下标 + 1；我的名次取头部 myRank（可能 > 100）。
    """
    pid, b = wire(CMD_PAIHANGBANG, sid, uid, [("int", 0)], timeout=timeout)
    if pid != CMD_PAIHANGBANG:
        return {"ok": False, "pid": pid, "error": _err_text(b)}
    r = b[1:]
    try:
        result = int.from_bytes(r[0:4], "big", signed=True)
        my_rank = int.from_bytes(r[4:8], "big", signed=True)
        size = int.from_bytes(r[8:10], "big")
        off, rows = 10, []
        for i in range(size):
            rec_len = int.from_bytes(r[off:off + 2], "big")
            off += 2
            end = off + rec_len
            u = int.from_bytes(r[off:off + 8], "big"); off += 8
            lv = int.from_bytes(r[off:off + 4], "big", signed=True); off += 4
            nm, off = P.read_utf(r, off)
            sc = int.from_bytes(r[off:off + 4], "big", signed=True); off += 4
            rows.append({"rank": i + 1, "uid": u, "lv": lv, "name": nm, "score": sc})
            off = max(off, end)
    except Exception as e:
        return {"ok": False, "pid": pid, "error": f"解析失败: {e}"}
    mine = next((x for x in rows if int(x["uid"]) == int(uid)), None)
    return {"ok": result == 1, "pid": pid, "result": result, "my_rank": my_rank,
            "size": size, "top": rows[:max(1, int(top))], "mine": mine, "error": None}


def api_arena_rank(_body=None):
    """只查排行榜（刷新按钮用）。服务端积分与存档积分一起返回，便于对照。"""
    acc, _lg = login_current()
    save_score = save_name = None
    try:
        save_score, save_name, _p = local_arena_score(acc.get("uid"))
    except Exception as e:
        print(f"  · 读存档积分失败：{e}")
    rk = arena_rank(acc["sid"], acc["uid"])
    if not rk.get("ok"):
        raise RuntimeError(f"查榜失败：{rk.get('error')}")
    mine = rk.get("mine") or {}
    season = arena_season(acc["sid"], acc["uid"])
    print(f"\n[arena-rank] 我的名次={rk['my_rank']}  "
          f"服务端分数={mine.get('score', '不在前 100')}  存档分数={save_score}")
    if season.get("ok"):
        print(f"  赛季结束={season['season_end_text']}  剩余 {season['season_left_text']}")
        print(f"  今日开放={season['today_open_text']} ~ {season['today_close_text']}")
    else:
        print(f"  · 赛季时间获取失败：{season.get('error')}")
    for row in rk["top"]:
        tag = " ★我" if int(row["uid"]) == int(acc["uid"]) else ""
        print(f"  #{row['rank']:>3}  {row['score']:>7}  lv={row['lv']:<4} "
              f"{row['name']}{tag}")
    return {"account": public_account(acc),
            "save_score": save_score, "save_name": save_name,
            "season": season, **rk}


def api_arena_match(body):
    """模拟竞技场匹配 → 轮询 → 取消，把本地积分推给服务端刷新排行榜。

    body = {score?, wait?, lv?, keep?}
      · score 省略 → 读容器存档里的 GameData.playerScore
      · wait  默认 5 秒（进度轮询总时长）
      · keep  默认 False（结束后发 CMD26 取消）
    """
    acc, lg = login_current()
    sid, uid = int(acc["sid"]), int(acc["uid"])

    save_score = save_name = save_path = None
    try:
        save_score, save_name, save_path = local_arena_score(acc.get("uid"))
        print(f"  存档积分：{save_score}（昵称 {save_name}）")
    except Exception as e:
        print(f"  · 读存档积分失败：{e}")

    # ★ score 是【绝对值】：服务端会把自己记录的积分直接改成这个数。
    #   mode="add" 时把它当增量（存档 + 增量），避免误填小数字把排行榜打下去。
    raw_score = body.get("score")
    raw_score = "" if raw_score is None else str(raw_score).strip()
    mode = str(body.get("mode") or "absolute").strip().lower()
    if mode not in ("absolute", "add"):
        mode = "absolute"

    if mode == "add":
        if raw_score in ("", "None"):
            raise RuntimeError("加分模式必须填增量，例如 +25")
        if save_score is None:
            raise RuntimeError("加分模式需要先能读到存档积分（容器里没有 soco.bin？）")
        delta = int(raw_score)
        score = save_score + delta
        print(f"  加分模式：存档 {save_score} {'+' if delta >= 0 else '-'} {abs(delta)} = {score}")
        if score < 0:
            raise RuntimeError(f"加分后分数为负（{score}），已拒绝")
    else:
        score = int(raw_score) if raw_score not in ("", "None") else save_score
        if score is None:
            raise RuntimeError("没有指定分数，也没能从存档读到 playerScore")
        if save_score is not None and score != save_score:
            if score < save_score:
                print(f"  ⚠⚠ 注意：这是【绝对值】{score}，比存档 {save_score} 低 —— "
                      f"服务端积分会被改小，名次会掉；只想加就选「加 N 分」模式")
            else:
                print(f"  ⚠ 这是【绝对值】{score}，与存档 {save_score} 不一致，以本次值为准")

    lv = int(body.get("lv") or MATCH_LV_DEFAULT)
    name = save_name or acc.get("nickName") or lg.get("otherName")
    wait = float(body.get("wait") if str(body.get("wait") or "").strip() else 5)
    wait = max(0.0, min(30.0, wait))
    keep = bool(body.get("keep"))

    print(f"\n[arena-match] 推送 score={score}  lv={lv}  name={name!r}  等待 {wait:g} 秒")

    # 默认名（Player1234）先改掉：匹配会把名字带给服务端
    try:
        new_name, renamed, _ = m.ensure_nickname(sid, uid, name)
        if renamed:
            name = new_name
            acc["nickName"] = new_name
            m.save_account(acc, make_current=True, source="web-arena-match")
            print(f"  ✓ 昵称已改为 {new_name}")
    except Exception as e:
        print(f"  · 昵称检查跳过：{e}")

    print(f"\n[arena-match] ① CMD25 开始匹配 ...")
    res = pipei_start(sid, uid, score, lv, name)
    if not res.get("ok"):
        raise RuntimeError(f"匹配请求被拒：pid={res.get('pid')} "
                           f"result={res.get('result')} {res.get('error') or ''}")
    print(f"  ✓ 已进入队列：倒计时 {res['result']} 秒  "
          f"fangjianID={res['fangjian_id']} type={res['fangjian_type']}")
    print("  （服务端此刻已收到新积分，排行榜稍后即变）")

    polls, matched = [], None
    if wait > 0:
        print(f"\n[arena-match] ② CMD38 轮询 {wait:g} 秒 ...")
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(min(2.0, max(0.2, deadline - time.time())))
            info = pipei_poll(sid, uid)
            if not info.get("ok"):
                print(f"    轮询失败：pid={info.get('pid')} {info.get('error') or ''}")
                break
            polls.append(info)
            if int(info["opp_uid"]) != 0:
                matched = info
                print(f"    ⚠ 已配到对手：uid={info['opp_uid']} "
                      f"分数={info['opp_score']} 昵称={info['opp_name']!r}")
                break
            print(f"    剩余 {info['remain']} 秒，暂无对手")

    cancel_res = None
    if keep:
        print("\n[arena-match] ③ --keep：保留匹配状态，不取消")
    else:
        print("\n[arena-match] ③ CMD26 取消匹配 ...")
        cancel_res = pipei_cancel(sid, uid)
        if cancel_res.get("ok"):
            print(f"  ✓ 已取消（result={cancel_res['result']}）")
        else:
            print(f"  ✗ 取消失败：pid={cancel_res.get('pid')} "
                  f"result={cancel_res.get('result')} {cancel_res.get('error') or ''}")
        if matched:
            print("  ⚠ 本次已配上对手，取消可能让对方房间异常，进游戏确认一下")

    season = arena_season(sid, uid)
    if season.get("ok"):
        print(f"\n[arena-match] 赛季：结束={season['season_end_text']}  "
              f"剩余 {season['season_left_text']}")

    print("\n[arena-match] ④ 回查 CMD24 排行榜 ...")
    rk = arena_rank(sid, uid)
    if rk.get("ok"):
        print(f"  ✓ 我的名次={rk['my_rank']}  我的分数="
              f"{(rk.get('mine') or {}).get('score', '不在前 100')}")
        for row in rk["top"]:
            tag = " ★我" if int(row["uid"]) == uid else ""
            print(f"    #{row['rank']:>3}  {row['score']:>7}  "
                  f"lv={row['lv']:<4} {row['name']}{tag}")
    else:
        print(f"  ✗ 查榜失败：{rk.get('error')}")

    mine = (rk.get("mine") or {})
    return {"account": public_account(acc),
            "score": score, "lv": lv, "name": name, "mode": mode,
            "server_score": mine.get("score"),
            "my_rank": rk.get("my_rank"),
            "save_score": save_score,
            "save_path": rel(save_path) if save_path else None,
            "season": season,
            "match": res, "polls": polls, "matched": matched,
            "cancel": cancel_res, "rank": rk, "wait": wait, "keep": keep}


# ── 账号列表 导入 / 导出 ──
_EXPORT_COLS = ["uid", "userName", "passwd", "nickName", "playerLevel",
                "createdAt", "updated", "status", "sid"]


def _csv_cell(v):
    s = "" if v is None else str(v)
    if any(c in s for c in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def build_accounts_export(fmt="json"):
    """返回 (bytes, 文件名, content-type)。"""
    doc = m._load_account_doc()
    fmt = (fmt or "json").lower()

    if fmt == "csv":
        lines = [",".join(_EXPORT_COLS)]
        for uid, rec in doc["accounts"].items():
            lines.append(",".join(_csv_cell(rec.get(c)) for c in _EXPORT_COLS))
        blob = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
        return blob, "accounts.csv", "text/csv; charset=utf-8"

    payload = {"exportedAt": now_str(),
               "current": doc.get("current"),
               "accounts": doc["accounts"]}
    blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return blob, "accounts.json", "application/json; charset=utf-8"


def _normalize_import(data):
    """接受导出文件格式 / accounts 数组 / 单个账号。"""
    if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
        return list(data["accounts"].values()), data.get("current")
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        return data["accounts"], data.get("current")
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and data.get("uid"):
        return [data], None
    raise RuntimeError("无法识别的导入格式（需要导出文件、accounts 数组或单个账号）")


def api_accounts_import(body):
    raw = body.get("data")
    if raw is None:
        raise RuntimeError("缺少 data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"JSON 解析失败: {e}")

    items, file_current = _normalize_import(raw)
    select = bool(body.get("select"))

    added = updated = skipped = 0
    errors = []
    imported_uids = []

    for idx, rec in enumerate(items, 1):
        if not isinstance(rec, dict):
            skipped += 1
            errors.append(f"#{idx} 不是对象")
            continue
        uid = rec.get("uid")
        user = str(rec.get("userName") or "").strip()
        pwd = str(rec.get("passwd") or "").strip()
        if not uid or not user or not pwd:
            skipped += 1
            errors.append(f"#{idx} 缺少 uid/userName/passwd")
            continue
        try:
            uid = str(int(uid))
        except Exception:
            skipped += 1
            errors.append(f"#{idx} uid 不是数字：{uid!r}")
            continue

        exists = uid in m._load_account_pool()
        m.save_account({**rec, "uid": int(uid), "userName": user, "passwd": pwd},
                       make_current=False, source="web-import-list")
        imported_uids.append(uid)
        if exists:
            updated += 1
        else:
            added += 1

    target = None
    if select:
        target = str(file_current) if file_current and str(file_current) in imported_uids else None
        if not target and imported_uids:
            target = imported_uids[-1]
        if target:
            rec = m._load_account_pool().get(target)
            if rec:
                m.save_account(rec, make_current=True, source="web-import-list")

    print(f"  ✓ 导入完成：新增 {added}，更新 {updated}，跳过 {skipped}")
    for e in errors[:10]:
        print(f"    · {e}")
    if target:
        print(f"  ✓ 当前账号已切换 -> uid={target}")

    return {"added": added, "updated": updated, "skipped": skipped,
            "errors": errors[:50], "total": len(m._load_account_pool()),
            "current": public_account(current_account())}


def api_rank_query(_body=None):
    acc, _lg = login_current()
    print("\n[rank] 查询 CMD137（大选关榜 / 爬塔榜）...")
    info = m.query_rank137_full(acc["sid"], acc["uid"])
    if not info.get("ok"):
        raise RuntimeError(f"查询失败: {info.get('error')!r}")

    entries = info.get("entries") or []
    tops = m.rank_top_per_type(entries)
    me = {}
    for e in entries:
        if int(e.get("uid") or 0) == int(acc["uid"]):
            me.setdefault(str(e.get("type")), e)

    m.print_rank137(info, acc["uid"])
    end_text = m.format_end_time(info.get("end_time"))
    return {
        "account": public_account(acc),
        "info": {"result": info.get("result"),
                 "end_time": info.get("end_time"),
                 "end_time_text": end_text,
                 "rank0": info.get("rank0"),
                 "rank1": info.get("rank1")},
        "tops": {str(k): v for k, v in tops.items() if v},
        "me": me,
        "entries": sorted(entries, key=lambda e: (e.get("type", 0),
                                                  e.get("rank", 9999))),
    }


def api_rank_upload(body):
    acc, lg = login_current()
    score = int(body.get("score") or 0)
    if score <= 0:
        raise RuntimeError("score 必须是正整数")
    rank_type = int(body.get("type") or 0)
    value = score * 100

    if body.get("rename", True):
        print("\n[upload] 上传前检查昵称 ...")
        new_name, renamed, _ = m.ensure_nickname(
            acc["sid"], acc["uid"], lg.get("otherName") or acc.get("nickName"))
        if renamed:
            acc["nickName"] = new_name
            m.save_account(acc, make_current=True, source="web-upload")

    print(f"\n[upload] {m.RANK_TYPE_NAMES.get(rank_type, rank_type)}："
          f"分数={score} ×100 → value={value} (type={rank_type})")
    r = m.upload_rank_value(acc["sid"], acc["uid"], value, rank_type=rank_type)
    if not r.get("ok"):
        raise RuntimeError(f"CMD136 失败: pid={r.get('pid')} {r.get('error')!r}")
    print(f"  ✓ result={r['result']}  value={r['value']}")

    out = {"upload": r, "account": public_account(acc)}
    if body.get("verify", True):
        print("\n[upload] 回查 CMD137 ...")
        info = m.query_rank137(acc["sid"], acc["uid"])
        me = {}
        for e in info.get("entries") or []:
            if int(e.get("uid") or 0) == int(acc["uid"]):
                me.setdefault(str(e.get("type")), e)
        for t, label in ((0, "大选关榜"), (1, "爬塔榜")):
            e = me.get(str(t))
            if e:
                print(f"  【{label}】rank={e['rank']}  value={e['value']}")
            else:
                print(f"  【{label}】未在返回窗口内看到自己")
        out["me"] = me
    return out


def api_rank_jump(_body=None):
    acc, _lg = login_current()
    print("\n[jump] 查询榜单 ...")
    info = m.query_rank137_full(acc["sid"], acc["uid"])
    if not info.get("ok"):
        raise RuntimeError(f"查询失败: {info.get('error')!r}")
    entries = info.get("entries") or []
    m.print_rank137(info, acc["uid"])

    results = m.jump_beat_tops(acc["sid"], acc["uid"], entries)
    tops = m.rank_top_per_type(entries)
    return {
        "account": public_account(acc),
        "info": {"end_time": info.get("end_time"),
                 "end_time_text": m.format_end_time(info.get("end_time"))},
        "tops": {str(k): v for k, v in tops.items() if v},
        "uploads": {str(k): v for k, v in results.items()},
    }


# CMD170 回包 result 对应的客户端文案（取自 core2.dex 的 new_worldboss_tishi1/2）
#   1/2 → 「魔王已经提前溜了，这次的挑战没有生效」
#   3   → 「今天的挑战次数用完了哦~休息一下明天再来」
WORLDBOSS_RESULT_HINT = {
    1: "魔王已经提前溜了，这次的挑战没有生效",
    2: "魔王已经提前溜了，这次的挑战没有生效",
    3: "今天的挑战次数用完了哦~休息一下明天再来",
}


def worldboss_open_state(st):
    """由 CMD142 状态推断本期能否上报伤害。

    客户端 UI_WorldBossMain 判定 `WorldBossManager.getBossStatus() == 0` 才是开放，
    status != 0 或已过 end_time 时服务端对 CMD170 一律回 result=1（与 data 内容无关）。
    返回 True=开放 / False=已结束 / None=查询失败无法判断。
    """
    if not st or not st.get("ok"):
        return None
    status = st.get("status")
    end_ms = int(st.get("end_time") or 0)
    if status not in (0, None):
        return False
    if end_ms and int(time.time() * 1000) >= end_ms:
        return False
    return True


def api_worldboss_damage(body):
    """世界魔王伤害上报（CMD170，可选先发 CMD146；默认先查 CMD142 状态）。"""
    acc, _lg = login_current()
    damage = int(body.get("damage") or 0)
    if damage <= 0:
        raise RuntimeError("damage 必须是正整数")

    kind = int(body.get("kind") or m.WORLDBOSS_HERO_KIND)
    monster = int(body.get("monster") or m.WORLDBOSS_MONSTER_KIND)
    # data 默认用内置的真机日志样本（343B）；显式传 data_hex/data_b64 可覆盖，
    # 传 data_empty=true 则发送空 data
    data = None
    data_source = "内置样本"
    if body.get("data_hex"):
        data = bytes.fromhex(str(body["data_hex"]).strip())
        data_source = "自定义 hex"
    elif body.get("data_b64"):
        import base64
        data = base64.b64decode(str(body["data_b64"]).strip())
        data_source = "自定义 base64"
    elif body.get("data_empty"):
        data = b""
        data_source = "空"

    send_counter = bool(body.get("send_counter"))
    counter_value = str(body.get("counter_value") or "1")

    print(f"\n[worldboss] 上报伤害 {damage}  kind={kind}  monster={monster}  "
          f"data={len(data) if data is not None else len(m.WORLDBOSS_SAMPLE_DATA)}B"
          f"（{data_source}）  146={'是' if send_counter else '否'}")

    # 先查本期状态：已结束时报 170 一定是 result=1，白白浪费一次请求
    if body.get("precheck", True):
        st = m.query_worldboss_status(acc["sid"], acc["uid"])
        opened = worldboss_open_state(st)
        if opened is False:
            print(f"  ✗ 本期未开放：status={st.get('status')}  "
                  f"结束 {st.get('end_time_text')}  下期 {st.get('next_start_text')}  → 不上报")
            raise RuntimeError(
                f"本期魔王已结束，现在上报必然被拒：status={st.get('status')}，"
                f"本期结束 {st.get('end_time_text') or '未知'}，"
                f"下期开始 {st.get('next_start_text') or '未知'}。"
                f"服务端会回 result=1「{WORLDBOSS_RESULT_HINT[1]}」，与 data 无关")
        if opened is True:
            print(f"  · 本期开放中：已打 {st.get('fights')}/{st.get('max_fights')} 次"
                  f"（剩余 {st.get('remaining')}），结束 {st.get('end_time_text')}")

    r = m.upload_worldboss_damage(acc["sid"], acc["uid"], damage,
                                  kind=kind, monster=monster, data=data,
                                  send_counter=send_counter,
                                  counter_value=counter_value)
    if r.get("counter"):
        c = r["counter"]
        print(f"  ✓ 146 计数上传：result={c.get('result')} value={c.get('value')}")
    if not r.get("ok"):
        msg = r.get("error") or f"result={r.get('result')}"
        hint = WORLDBOSS_RESULT_HINT.get(r.get("result"))
        if hint:
            msg = f"{msg} ——「{hint}」"
        raise RuntimeError(f"上报失败: {msg}")

    print(f"  ✓ 上报成功：本次 {damage}  累计 {r['total_damage']}  "
          f"战斗次数 {r['fights']}（上限 {m.WORLDBOSS_MAX_FIGHTS}，"
          f"剩余 {r['remaining']}）")
    return {"account": public_account(acc),
            "damage": damage, "total_damage": r["total_damage"],
            "fights": r["fights"], "remaining": r["remaining"],
            "max_fights": m.WORLDBOSS_MAX_FIGHTS,
            "result": r["result"],
            "kind": kind, "monster": monster,
            "data_len": r["data_len"], "data_source": data_source,
            "counter": r.get("counter")}


def api_worldboss_status(body):
    """世界魔王本期状态查询（CMD142，只读，不消耗次数）。"""
    acc, _lg = login_current()
    print(f"\n[worldboss] 查询状态 uid={acc['uid']}")
    st = m.query_worldboss_status(acc["sid"], acc["uid"])
    if not st.get("ok"):
        raise RuntimeError(f"查询失败: {st.get('error')}")
    st["open"] = worldboss_open_state(st)
    st["status_text"] = ("进行中" if st["open"] else
                         ("已结束/未开放" if st["open"] is False else "未知"))
    print(f"  ✓ 累计伤害 {st['total_damage']}  战斗次数 {st['fights']}"
          f"（上限 {st['max_fights']}，剩余 {st['remaining']}）"
          f"  排名 {st['rank']}  状态 {st['status_text']}(status={st['status']})"
          f"  结束 {st['end_time_text']}")
    return {"account": public_account(acc), **st}


def api_worldboss_rank(body):
    """世界魔王排行榜查询（CMD176，只读）。"""
    acc, _lg = login_current()
    rank_type = int(body.get("type") or 0)
    print(f"\n[worldboss] 查询排行榜 type={rank_type}"
          f"（{m.WORLDBOSS_RANK_TYPES.get(rank_type, '?')}）")
    r = m.query_worldboss_rank(acc["sid"], acc["uid"], rank_type)
    if not r.get("ok"):
        raise RuntimeError(f"查询失败: {r.get('error')}")
    print(f"  ✓ {r['type_name']}  我的排名 {r['my_rank']}  我的分数 {r['my_score']}"
          f"  共 {r['size']} 条")
    return {"account": public_account(acc), **r}


def build_save_bytes(acc, chapter=10, open_switches=True):
    """用内嵌模板生成 soco.bin，返回 (bytes, info)。

    ★ 全程只在临时目录里操作，不写 accounts/<uid>/，也不碰本机任何绝对路径。
    """
    with tempfile.TemporaryDirectory(prefix="zhaohe_save_") as td:
        tpl = os.path.join(td, "template_soco.bin")
        with open(tpl, "wb") as f:
            f.write(template_blob())
        data, info = m.build_save(tpl, int(acc["uid"]), int(chapter), None,
                                  open_switches=bool(open_switches))
    return data, info


def netdata_bytes(acc):
    """取该账号 NetData.bin 的字节。

    ★ 服务端部署时 offline_play 里那条本机绝对模板路径不存在，
      所以优先用 accounts/<uid>/NetData.bin（注册时已生成并校验）；
      没有就找一个已有账号的文件当结构模板现生成，最后才从零构造。
    返回 (bytes, 来源说明)
    """
    p = os.path.join(ACCOUNTS_DIR, str(int(acc["uid"])), "NetData.bin")
    if os.path.exists(p):
        ex = m.read_existing_netdata(p)
        if ex and int(ex.get("uid") or 0) == int(acc["uid"]):
            with open(p, "rb") as f:
                return f.read(), "existing"

    tpl = None
    for cand in sorted(glob.glob(os.path.join(ACCOUNTS_DIR, "*", "NetData.bin"))):
        try:
            if os.path.getsize(cand) >= 128:
                tpl = cand
                break
        except OSError:
            continue

    with tempfile.TemporaryDirectory(prefix="zhaohe_nd_") as td:
        out = os.path.join(td, "NetData.bin")
        m.write_netdata(out, int(acc["uid"]), acc["userName"], acc["passwd"],
                        acc.get("nickName") or acc["userName"], template=tpl)
        with open(out, "rb") as f:
            blob = f.read()
    return blob, ("regenerated" if tpl else "regenerated-无模板")


def api_save_export(query):
    """生成 soco.bin + NetData.bin 并打包成 zip 返回（不落地任何文件）。

    返回 (zip_bytes, 文件名)
    """
    uid_q = (query.get("uid") or [None])[0]
    acc = require_account()
    if uid_q and str(uid_q) != str(acc["uid"]):
        acc = m._load_account_pool().get(str(uid_q)) or acc
        if str(acc.get("uid")) != str(uid_q):
            raise RuntimeError(f"账号池里没有 uid={uid_q}")

    chapter = int((query.get("chapter") or ["10"])[0] or 10)
    sw = (query.get("open_switches") or ["1"])[0]
    open_switches = str(sw).lower() not in ("0", "false", "no", "")

    print(f"[save-export] uid={acc['uid']} 章节={chapter} 打开开关={open_switches}")
    data, info = build_save_bytes(acc, chapter, open_switches)
    for k, v in info.items():
        print(f"  ✓ {k}: {v}")

    ch = m.R.locate_daxuanguan(data)
    disp = ch.get("chapter_display", ch.get("effective", 0) + 1)
    print(f"  ✓ 存档 {len(data)} 字节，第 {disp} 章 (effective={ch.get('effective')})")

    nd, nd_src = netdata_bytes(acc)
    print(f"  ✓ NetData.bin {len(nd)} 字节（{nd_src}）")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("soco.bin", data)
        z.writestr("NetData.bin", nd)
    blob = buf.getvalue()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"zhaohe_{acc['uid']}_{stamp}.zip"
    print(f"  ✓ 已打包 {name}（{len(blob)} 字节）")
    return blob, name


def api_account_files(query):
    uid = (query.get("uid") or [None])[0] or require_account()["uid"]
    d = account_dir(uid, ensure=False)
    files = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            st = os.stat(p)
            files.append({"name": name, "size": st.st_size,
                          "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return {"uid": int(uid), "dir": rel(d), "files": files}


def safe_member(uid, name):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        raise RuntimeError("非法文件名")
    p = os.path.join(account_dir(uid, ensure=False), name)
    if not os.path.isfile(p):
        raise RuntimeError("文件不存在")
    return p


def api_logs(query):
    limit = int((query.get("limit") or ["100"])[0])
    uid = (query.get("uid") or [None])[0]
    logs = LOG_RING
    if uid:
        logs = [r for r in LOG_RING if str(r.get("uid")) == str(uid)]
    return {"logs": logs[-limit:]}


# ─────────────────────────────────────
#  HTTP
# ─────────────────────────────────────
ROUTES = {
    ("GET", "/api/state"): api_state,
    ("POST", "/api/account/register"): api_register,
    ("POST", "/api/account/select"): api_account_select,
    ("POST", "/api/account/login"): api_account_login,
    ("POST", "/api/account/import"): api_account_import,
    ("POST", "/api/account/nickname"): api_account_nickname,
    ("POST", "/api/account/level"): api_account_level,
    ("POST", "/api/arena/score"): api_arena_score,
    ("POST", "/api/arena/match"): api_arena_match,
    ("POST", "/api/arena/rank"): api_arena_rank,
    ("POST", "/api/accounts/import"): api_accounts_import,
    ("POST", "/api/rank/query"): api_rank_query,
    ("POST", "/api/rank/upload"): api_rank_upload,
    ("POST", "/api/rank/jump"): api_rank_jump,
    ("POST", "/api/worldboss/damage"): api_worldboss_damage,
    ("POST", "/api/worldboss/status"): api_worldboss_status,
    ("POST", "/api/worldboss/rank"): api_worldboss_rank,
}

QUERY_ROUTES = {
    "/api/account/files": api_account_files,
    "/api/logs": api_logs,
}

MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    server_version = "zhaohe-web/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now_str(), fmt % args))

    # ---- helpers ----
    def _send_json(self, obj, status=200):
        blob = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise RuntimeError("请求体不是合法 JSON")

    def _authorized(self):
        spec = auth_spec()
        if not spec:
            return True
        hdr = self.headers.get("Authorization") or ""
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:].strip()).decode("utf-8")
        except Exception:
            return False
        return hmac.compare_digest(raw, spec)

    def _require_auth(self):
        """未通过鉴权就回 401（浏览器会弹账号密码框）。"""
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="zhaohe", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _serve_static(self, path):
        rel_path = path.lstrip("/")
        if rel_path in ("", "index.html"):
            rel_path = "index.html"
        full = os.path.abspath(os.path.join(STATIC_DIR, rel_path))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        with open(full, "rb") as f:
            blob = f.read()
        ext = os.path.splitext(full)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    # ---- verbs ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path in ("/health", "/healthz"):
            # 健康检查（免鉴权）：只回 {"status": "ok"}，同时记进底部日志
            _append_health(self.client_address[0], path,
                           self.headers.get("User-Agent"))
            self._send_json({"status": "ok"})
            return

        if not self._require_auth():
            return

        if path == "/api/logs/export":
            try:
                uid = (query.get("uid") or [None])[0]
                blob = logs_as_text(uid).encode("utf-8")
                name = f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                print(f"[logs] 导出日志 {name}（{len(blob)} 字节）")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
            return

        if path == "/api/accounts/export":
            try:
                fmt = (query.get("format") or ["json"])[0]
                blob, name, ctype = build_accounts_export(fmt)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                base, ext = os.path.splitext(name)
                name = f"{base}_{stamp}{ext}"
                print(f"[export] 导出账号列表 {name} ({len(blob)} 字节)")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
            return

        if path == "/api/account/file":
            try:
                uid = (query.get("uid") or [None])[0] or require_account()["uid"]
                name = unquote((query.get("name") or [""])[0])
                full = safe_member(uid, name)
                with open(full, "rb") as f:
                    blob = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{os.path.basename(full)}"')
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
            return

        if path == "/api/save/export":
            # ★ 直接返回 zip 流，不落地文件；日志照样进操作日志
            buf = io.StringIO()
            uid = None
            try:
                cur = current_account()
                uid = (query.get("uid") or [None])[0] or (cur.get("uid") if cur else None)
                with LOCK, redirect_stdout(buf), redirect_stderr(buf):
                    blob, name = api_save_export(query)
                _append_op(uid, path, True, buf.getvalue().rstrip())
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)
            except Exception as e:
                text = buf.getvalue().rstrip()
                _append_op(uid, path, False, text)
                self._send_json({"ok": False, "error": str(e),
                                 "logs": text.splitlines()}, 400)
            return

        if path in QUERY_ROUTES:
            fn = QUERY_ROUTES[path]
            res = run_logged(_uid_of(query), path, lambda: fn(query))
            self._send_json(res)
            return

        if path in ("/api/state",):
            res = run_logged(None, path, api_state)
            self._send_json(res)
            return

        self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._require_auth():
            return

        fn = ROUTES.get(("POST", path))
        if not fn:
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        try:
            body = self._json_body()
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return

        cur = current_account()
        uid = cur.get("uid") if cur else None
        res = run_logged(uid, path, lambda: fn(body))
        self._send_json(res)


def _uid_of(query):
    v = (query.get("uid") or [None])[0]
    return int(v) if v and str(v).isdigit() else None


def main():
    ap = argparse.ArgumentParser(description="zhaohe 脱机工具 Web 服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or 8000),
                    help="监听端口，默认取 $PORT，未设置则 8000")
    args = ap.parse_args()

    # 日志立刻落盘（systemd / docker 下方便 journalctl 实时看）
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    m.migrate_current_account()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 60)
    print("  zhaohe 脱机工具 Web 服务")
    print(f"  http://127.0.0.1:{args.port}")
    print(f"  项目目录: {ROOT}")
    print(f"  数据目录: {rel(DATA_DIR)}  （来源: {DATA_DIR_SOURCE}）")
    if not data_persistent():
        print("  ⚠ 该目录【不可持久化】：重新部署/休眠唤醒后账号数据会丢失。")
        print("    解决：在平台挂一个持久磁盘并设 ZHAOHE_DATA_DIR 指向挂载点（如 /var/data）。")
    print(f"  账号文件: {rel(m.ACCOUNT_FILE)}")
    print(f"  内嵌模板: 内存 {TEMPLATE_SIZE} 字节（不落盘）")
    print(f"  访问认证: {'已开启 Basic Auth' if auth_spec() else '未开启（仅建议内网/本机使用）'}")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
