#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脱机（无客户端）一键流程：注册 → 登录 → 生成存档 → 上报闯关

完整链路（全部基于实测协议）
============================
1) CMD4  GenCounter          注册新账号
     req: [Long]sid(0) [Int]lang(0) [UTF]version [UTF]channel [UTF]device
          [UTF]os [UTF]sha1 [UTF]phoneSign [UTF]more
     ack: [UTF]userName [UTF]passwd [UTF]nickName [Long]uid [Long]sid
          [Long]servertime [UTF]status        status 首段 3 = 新号

2) CMD1  UserLogin           登录拿有效 sid   ★ 请求体【不含 uid】
     req: [Long]s_sessionId(0) [UTF]userName [UTF]passwd [UTF]otherName
          [Int]lang + 设备指纹
     ack: [Long]uid [Long]sid [Long]servertime [UTF]userName [UTF]passwd
          [UTF]otherName [UTF]status          status 首段 1 = 正常

3) CMD136 UserRankUpload     上报闯关进度        ★ 这就是"闯关接口"
     req: [Long]sid [Long]uid [Int]serverId [Int]type [Int]value
     例:  [Int]11 [Int]0 [Int]600     → 闯到第 6 关（value = 关卡 × 100）
     · type=0 = 大选关榜（闯关积分），type=1 = 爬塔榜
     ack: [Int]result                result=1 表示成功

4) CMD137 UserRankList     查排行榜（冒险王活动榜）  ★ --jump 使用
     · 响应只是【以查询 uid 为中心】的一段窗口，榜一不一定在里面；
       jump 会自动改用窗口里排名更高的玩家 uid 继续查，直到拿到 rank=1。
     · jump 查完后会再对两个榜分别发 CMD136：
       取榜一的分数 ÷100 再 +1，然后 ×100 作为 value 上报；
       榜一若就是自己、或没拿到 rank=1，该榜跳过。
     req: [Long]sid [Long]uid          （请求体只有这两个字段）
     ack: [Int]result [Long]endTime [Int]rank0 [Int]rank1 [Short]size
          然后 size 条记录，每条:
            [Short]记录字节长 [Long]uid [Int]rank [UTF]name [Int]type [Int]value
     · type=0 = 大选关榜，type=1 = 爬塔榜
     · rank = 名次（-1 表示未上榜）
     · value = 分数（大选关榜即关卡分数，如 6205；爬塔榜如 1818000）
     · 日志里看到的
         “[Int]1[UTF]空置的彬彬87[Int]0[Int]6205[Short]30[Long]1564707676225581056”
       即 rank=1 / name=空置的彬彬87 / type=0 / value=6205 / 记录长=30 / uid=...

5) 存档 soco.bin
     ★ 服务端【不存】关卡存档（CMD76 type=2 取回 blob_len=0），
       关卡显示以本地 GameData.daxuanguan 为准（显示章数 = 值 + 1，
       且 daxuanguan_kaiqi_backup 会在登录时覆盖它，必须同时改）。
     ★ 存档内 Netdata_uid 必须与 NetData.bin 的 uid 一致，
       否则游戏报"账号id与游戏数据不匹配"（见 modify_soco_uid.py 的说明）。

模板存档
========
默认 /Users/suramire/Documents/soco.bin（刚创建的 1 级账号存档）

用法
====
    # 一键：注册 → 登录 → 用模板生成存档 → 上报第10关 → 落地容器
    python3 offline_play.py --chapter 10

    # 不落地到容器，只生成 offline_soco.bin
    python3 offline_play.py --chapter 10 --no-write

    # 只注册登录，不上报进度、不写存档
    python3 offline_play.py --no-save --no-rank

    # 查排行榜榜一（大选关榜 / 爬塔榜），判断榜一是不是自己，
    # 然后对每个榜取榜一分数 ÷100 +1（×100）分别上报；榜一是自己则跳过
    python3 offline_play.py jump
    python3 offline_play.py --jump

    # 用【已记录账号】直接上报分数（不注册、不写存档）
    # 分数会 ×100 后作为 CMD136 的 value（和注册流程一致）
    python3 offline_play.py reuse 62                 # 大选关榜 type=0（默认）
    python3 offline_play.py reuse 62 --type 1        # 爬塔榜 type=1（仅 type 不同）

    # 模拟竞技场匹配 → 轮询几秒 → 取消：把本地积分(pScore)推给服务端刷新排行榜
    # 不带分数时读容器存档里的 GameData.playerScore
    python3 offline_play.py match                    # 默认等 5 秒后取消
    python3 offline_play.py match 1500 --wait 8      # 指定分数，等 8 秒
    python3 offline_play.py match --keep             # 不断开，留在队列里

    # 账号切换（账号池 accounts/accounts.json，当前账号 offline_account.json）
    python3 offline_play.py accounts                 # 列出账号池
    python3 offline_play.py --list-accounts
    python3 offline_play.py jump --account 1565006688996601856   # 用 uid 切换
    python3 offline_play.py jump --account 嘻嘻很酷呐7289          # 用昵称切换
    python3 offline_play.py jump --account accounts/sid_uid/xxx.json  # 用文件
    python3 offline_play.py jump --new               # 注册新号并切换为当前账号

    # 指定模板 / 干跑
    python3 offline_play.py --chapter 10 --template /path/to/soco.bin
    python3 offline_play.py --chapter 10 --dry

账号文件（唯一）
================
· 所有账号 + 当前账号都只存在 `accounts.json`：
      {"current": "<uid>", "accounts": {"<uid>": {uid,userName,passwd,nickName,...}}}
· 不带 --account / --new 时，用 current 指向的账号
· `jump/reuse --account <uid|userName|nickName|文件>` 会选中该账号并把它设为 current
· `jump --new` 注册新号后写入同一个文件并设为 current

sid / uid 记录
===============
每次注册或登录成功后，会把 sid + uid 追加写入
    accounts/sid_uid/<uid>_<YYYY-MM-DD>.json
（同一天多次登录记在同一天的同一个文件里，按时间追加）
"""
import argparse
import json
import os
import random
import re
import shutil
import struct
import sys
import time
import zlib
from datetime import datetime

sys.path.insert(0, ".")
import zhaohe_protocol as P
import send_cmd4 as S4
import soco_write as W
import soco_read as R

CONTAINER = "/Users/suramire/Library/Containers/DA269E38-2FF7-4002-9464-743920492C57/Data/Library/local"
SAVE_PATH = os.path.join(CONTAINER, "data", "soco.bin")
NETDATA_PATH = os.path.join(CONTAINER, "data", "NetData.bin")
TEMPLATE_DEFAULT = "/Users/suramire/Documents/soco.bin"
# NetData 模板（游戏正常生成的结构，含完整尾部对象）
NETDATA_TEMPLATE = "/Volumes/PortableSSD/ai_projects/zhaohe/accounts/1535687619940233216/NetData.bin"

CMD136 = 136
CMD60 = 60                        # ChangeValuesReq / Ack（批量写入玩家数值，key1=等级）
CMD29 = 29                        # DuizhanoverCodeReq / Ack（竞技场对战结算加分）
CMD22 = 22                        # ImportNameCodeReq / ImportNameCodeAck（改昵称）
# 排行榜 serverId（日志实测 11）
RANK_SERVER_ID = 11
# 排行榜 type=0 表示关卡榜
RANK_TYPE_LEVEL = 0

CMD137 = 137                      # UserRankListReq / UserRankListAck
RANK_TYPE_DAXUANGUAN = 0          # 大选关榜
RANK_TYPE_TOWER = 1               # 爬塔榜
RANK_TYPE_NAMES = {RANK_TYPE_DAXUANGUAN: "大选关榜", RANK_TYPE_TOWER: "爬塔榜"}

CMD146 = 146                      # UploadValueReq / UploadValueAck（计数上传）
CMD170 = 170                      # NewBossDamageWithDataReq / Ack（世界魔王伤害上报）
CMD142 = 142                      # NewBossStatusReq / Ack（世界魔王状态）
# 世界魔王 170 的两个附加字段（服务端实测只认 damage，这两个不影响结果）
WORLDBOSS_HERO_KIND = 2000        # kind = 出战英雄 kind（日志实测 2000）
WORLDBOSS_MONSTER_KIND = 297      # monster = 战斗里最高怪物 kind（日志实测 297/380）
WORLDBOSS_MAX_FIGHTS = 8          # getRemainingChallenges() 里 maxChallenges = 8
# 170 回包 result：0 成功；3 = 挑战次数用完（实测第 9 次开始）
WORLDBOSS_RESULT_TEXT = {
    1: "服务端拒绝（result=1）",
    2: "服务端拒绝（result=2）",
    3: f"挑战次数已用完（每期最多 {WORLDBOSS_MAX_FIGHTS} 次）",
}

# 170 的 data：真机日志抓到的 WorldBossDrillStatisticInfo（Java 序列化 + gzip，343B）
# 服务端实测不校验内容，这里作为默认样本，避免上报时 data 为空。
WORLDBOSS_SAMPLE_DATA_HEX = (
    "1f8b08000000000000005bf39681b5b888c12d393f57af2a23313f23552f3d3137552f29b1a4242755af3cbf"
    "28272529bfb8582fa528332727373f25552f1c24e604147301090597249664169764267be6a5e55b1bfcba77"
    "54b751908781a1a20068ac70566259a25e6949668e9e63515162a50f506545e325d999c713e73233307a32b0"
    "146756a556143030303097b300491ea02667b2dde296989b9953097208f37efd9ebe5bab2e811d52ce0134d9"
    "0788394b8b1898a23d7d7725a895bddab494092409146605626e9013189040390f985ab2bf08484eab282e64"
    "a86360019be4053609c467836a06b99c118b6643472674cd1e189a4180098b66c1cfbc60cd15e506102bd82f"
    "80c55f489b40d4a96f0691ac7377db016d5fd8b8c741e93f149430308603716079250301c008528e2c400dfe"
    "a813469d80d5098574770238e7817317b87861a828076739a60a003383032af8040000"
)
WORLDBOSS_SAMPLE_DATA = bytes.fromhex(WORLDBOSS_SAMPLE_DATA_HEX)

CMD176 = 176                      # NewBossRankNewReq / Ack（世界魔王排行榜）
# 176 请求的 type：与客户端 onRspQueryRank 的 switch 一致
WORLDBOSS_RANK_TYPES = {
    0: "本期伤害榜",
    1: "上期伤害榜",
    2: "代币总榜",
    3: "代币月榜",
    4: "月伤害榜",
    5: "总伤害榜",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# ★ 账号数据只存这一个文件：{"current": "<uid>", "accounts": {"<uid>": {...}}}
ACCOUNT_FILE = os.path.join(BASE_DIR, "accounts.json")
ACCOUNT_POOL_FILE = ACCOUNT_FILE          # 兼容旧引用，指向同一个文件
# 旧版两文件布局，仅用于一次性迁移
LEGACY_ACCOUNT_FILE = os.path.join(BASE_DIR, "offline_account.json")
LEGACY_POOL_FILE = os.path.join(BASE_DIR, "accounts", "accounts.json")
SID_UID_DIR = os.path.join(BASE_DIR, "accounts", "sid_uid")


# ─────────────────────────────────────
#  1) 注册
# ─────────────────────────────────────
def register(**over):
    code, r = S4.send_cmd4(0, 0, **(over or {}))
    if not isinstance(r, dict) or not r.get("uid"):
        raise RuntimeError(f"注册失败: {r}")
    return r


# ─────────────────────────────────────
#  2) 登录
# ─────────────────────────────────────
def login(user, passwd, other, timeout=20):
    """CMD1 登录。★ 不能用 P.send_protocol —— 它会多写一个 uid 字段导致错位。"""
    import send_cmd1 as S1
    try:
        code, info = S1.send_cmd1(0, user, passwd, other, lang=0, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"登录异常: {type(e).__name__}: {e}")
    if not isinstance(info, dict) or not info.get("sid"):
        raise RuntimeError(f"登录失败: {info}")
    return info


# ─────────────────────────────────────
#  3) 上报闯关（CMD136）
# ─────────────────────────────────────
def upload_rank_value(sid, uid, value, rank_type=RANK_TYPE_LEVEL,
                      server_id=RANK_SERVER_ID, timeout=20):
    """CMD136 直接上报一个分数值（value 原样发出，不做 chapter*100 换算）。

    req: [Long]sid [Long]uid [Int]serverId [Int]type [Int]value
    ack: [Int]result   1 = 成功
    """
    value = int(value)
    c, p, b = _send(CMD136, [("int", server_id), ("int", rank_type),
                             ("int", value)], sid, uid, timeout=timeout)
    ok = (p == CMD136)
    result = None
    if ok and len(b) >= 5:
        result = int.from_bytes(b[1:5], 'big', signed=True)
    return {"pid": p, "value": value, "type": rank_type, "ok": ok,
            "result": result, "error": None if ok else _err(b)}


def upload_rank_level(sid, uid, chapter, server_id=RANK_SERVER_ID, timeout=20):
    """CMD136 上报闯关进度。value = chapter * 100（日志：第6关 → 600）。"""
    return upload_rank_value(sid, uid, int(chapter) * 100,
                             rank_type=RANK_TYPE_LEVEL,
                             server_id=server_id, timeout=timeout)


# ─────────────────────────────────────
#  3.1) 玩家等级：CMD60 ChangeValues
# ─────────────────────────────────────
PLAYER_LEVEL_KEY = 1              # key 1 = "<等级>_0000"
# 其余字段按真机日志样本固定（key 2/7/10 有非零值）
PLAYER_LEVEL_DEFAULT_VALUES = {
    1: "1_0000",                  # 会被 set_player_level 覆盖
    2: "100005000",
    3: "0", 4: "0", 5: "0", 6: "0",
    7: "100036",
    8: "0", 9: "0",
    10: "9",
    11: "0", 12: "0", 13: "0", 14: "0", 15: "0", 16: "0",
}


def arena_add_score(sid, uid, add_score, is_win=1, timeout=20):
    """CMD29 DuizhanoverCodeReq：竞技场对战结算加分。

    req: [Long]sid [Long]uid [Int]iswin [Int]addScore
    ack: [Int]result [Int]score

    · iswin=1 赢，iswin=0 输；addScore 为本次要加的积分（如 25）
    · 真机日志：iswin=1, addScore=25 → ack result=0, score=0（0 = 成功）
    """
    add_score = int(add_score)
    if add_score < 0:
        raise ValueError("addScore 不能为负数")
    is_win = 1 if int(is_win) else 0

    c, p, b = _send(CMD29, [("int", is_win), ("int", add_score)],
                    sid, uid, timeout=timeout)
    out = {"ok": False, "pid": p, "is_win": is_win,
           "add_score": add_score, "result": None, "score": None,
           "error": None}
    if p == CMD29 and len(b) >= 9:
        out["result"] = int.from_bytes(b[1:5], "big", signed=True)
        out["score"] = int.from_bytes(b[5:9], "big", signed=True)
        out["ok"] = out["result"] == 0
        if not out["ok"]:
            out["error"] = f"服务端拒绝（result={out['result']}）"
    elif p != CMD29:
        out["error"] = _err(b)
    else:
        out["error"] = f"回包太短: {b.hex()}"
    return out


def set_player_level(sid, uid, level, values=None, timeout=20):
    """CMD60 ChangeValuesReq：写入玩家等级。

    req: [Long]sid [Long]uid [Short]size
         size × { [Int]key [UTF]value }
    ack: [Int]result（真机日志：成功 = 1）

    · key 1 = "<level>_0000"，如 32 级 → "32_0000"
    · 其余 key 2..16 默认按真机样本取值，可用 values 覆盖
    """
    level = int(level)
    if level <= 0:
        raise ValueError("等级必须是正整数")

    pairs = dict(PLAYER_LEVEL_DEFAULT_VALUES)
    pairs[PLAYER_LEVEL_KEY] = f"{level}_0000"
    if values:
        pairs.update({int(k): str(v) for k, v in values.items()})

    items = sorted(pairs.items())
    fields = [("short", len(items))]
    for k, v in items:
        fields.append(("int", k))
        fields.append(("utf", v))

    c, p, b = _send(CMD60, fields, sid, uid, timeout=timeout)
    out = {"ok": False, "pid": p, "level": level,
           "result": None, "values": {str(k): v for k, v in items},
           "error": None}
    if p == CMD60 and len(b) >= 5:
        out["result"] = int.from_bytes(b[1:5], "big", signed=True)
        out["ok"] = out["result"] == 1
        if not out["ok"]:
            out["error"] = f"服务端拒绝（result={out['result']}）"
    elif p != CMD60:
        out["error"] = _err(b)
    else:
        out["error"] = f"回包太短: {b.hex()}"
    return out


# ─────────────────────────────────────
#  3.2) 世界魔王：146 计数 + 170 伤害
# ─────────────────────────────────────
def upload_value(sid, uid, value_type=1, value="1", timeout=20):
    """CMD146 UploadValueReq：客户端在打世界魔王前会先发一次计数上传。

    req: [Long]sid [Long]uid [Int]type [UTF]value
    ack: [Int]result [Int]type [UTF]value
    """
    c, p, b = _send(CMD146, [("int", int(value_type)),
                             ("utf", str(value))], sid, uid, timeout=timeout)
    out = {"pid": p, "ok": p == CMD146, "result": None,
           "type": None, "value": None, "error": None}
    if p == CMD146 and len(b) >= 11:
        out["result"] = int.from_bytes(b[1:5], "big", signed=True)
        out["type"] = int.from_bytes(b[5:9], "big", signed=True)
        n = int.from_bytes(b[9:11], "big")
        out["value"] = b[11:11 + n * 2].decode("utf-16-be", "replace")
    elif p != CMD146:
        out["error"] = _err(b)
    return out


def upload_worldboss_damage(sid, uid, damage,
                            kind=WORLDBOSS_HERO_KIND,
                            monster=WORLDBOSS_MONSTER_KIND,
                            data=None, send_counter=False,
                            counter_value="1", timeout=20):
    """CMD170 NewBossDamageWithDataReq：世界魔王伤害上报。

    req: [Long]sid [Long]uid [Long]damage [Int]kind [Int]monster [ByteArray]data
    ack: [Int]result [Long]totaldamage [Int]fights

    · kind = 出战英雄 kind，monster = 战斗里最高怪物 kind
    · data = Java 序列化的 WorldBossDrillStatisticInfo（gzip）
      ★ data=None 时使用 WORLDBOSS_SAMPLE_DATA（真机日志样本 343B）；
        传 b"" 则显式发送空 data
    · ★ 实测服务端只认 damage；kind/monster/data 传 0/空都能成功
    · 客户端会先发一次 CMD146（计数上传）再发 170，但实测 170 不依赖它
    """
    damage = int(damage)
    if damage <= 0:
        raise ValueError("damage 必须是正整数")
    if data is None:
        data = WORLDBOSS_SAMPLE_DATA
    data = bytes(data)

    counter = None
    if send_counter:
        counter = upload_value(sid, uid, 1, counter_value, timeout=timeout)
        if not counter.get("ok"):
            return {"ok": False, "pid": CMD146, "damage": damage,
                    "result": None, "total_damage": None, "fights": None,
                    "counter": counter,
                    "error": f"146 失败: {counter.get('error') or counter.get('result')}"}

    c, p, b = _send(CMD170, [("long", damage), ("int", int(kind)),
                             ("int", int(monster)),
                             ("bytes", bytes(data or b""))],
                    sid, uid, timeout=timeout)
    out = {"ok": False, "pid": p, "damage": damage, "kind": int(kind),
           "monster": int(monster), "data_len": len(data or b""),
           "result": None, "total_damage": None, "fights": None,
           "remaining": None, "counter": counter, "error": None}
    if p == CMD170 and len(b) >= 17:
        out["result"] = int.from_bytes(b[1:5], "big", signed=True)
        out["total_damage"] = int.from_bytes(b[5:13], "big", signed=True)
        out["fights"] = int.from_bytes(b[13:17], "big", signed=True)
        out["ok"] = out["result"] == 0
        if out["ok"]:
            out["remaining"] = max(0, WORLDBOSS_MAX_FIGHTS - out["fights"])
        else:
            out["error"] = WORLDBOSS_RESULT_TEXT.get(
                out["result"], f"服务端拒绝（result={out['result']}）")
    elif p != CMD170:
        out["error"] = _err(b)
    else:
        out["error"] = f"回包太短: {b.hex()}"
    return out


def query_worldboss_status(sid, uid, timeout=20):
    """CMD142 NewBossStatusReq：查询世界魔王本期状态（只读，不消耗次数）。

    req: [Long]sid [Long]uid
    ack: [Long]seasonid [Long]id [Int]status [Long]totaldamage [Long]bosstype
         [Long]endtime [Long]nextstarttime [Int]fights [Int]rank [Int]previousrank
         [UTF]stageDay [Int]battleMist [Short]size  （后续是 size 长度的若干数组）
    """
    c, p, b = _send(CMD142, [], sid, uid, timeout=timeout)
    out = {"ok": False, "pid": p, "season_id": None, "boss_id": None,
           "status": None, "total_damage": None, "boss_type": None,
           "end_time": None, "next_start_time": None,
           "end_time_text": "", "next_start_text": "",
           "fights": None, "remaining": None,
           "max_fights": WORLDBOSS_MAX_FIGHTS,
           "rank": None, "previous_rank": None, "stage_day": None,
           "error": None}
    if p != CMD142:
        out["error"] = _err(b)
        return out
    try:
        off = 1
        need = off + 8 + 8 + 4 + 8 + 8 + 8 + 8 + 4 + 4 + 4 + 2
        if len(b) < need:
            out["error"] = f"回包太短: {b.hex()}"
            return out

        def i64():
            nonlocal off
            v = int.from_bytes(b[off:off + 8], "big", signed=True)
            off += 8
            return v

        def i32():
            nonlocal off
            v = int.from_bytes(b[off:off + 4], "big", signed=True)
            off += 4
            return v

        out["season_id"] = i64()
        out["boss_id"] = i64()
        out["status"] = i32()
        out["total_damage"] = i64()
        out["boss_type"] = i64()
        out["end_time"] = i64()
        out["next_start_time"] = i64()
        out["fights"] = i32()
        out["rank"] = i32()
        out["previous_rank"] = i32()
        n = int.from_bytes(b[off:off + 2], "big")
        off += 2
        out["stage_day"] = b[off:off + n * 2].decode("utf-16-be", "replace")
        off += n * 2
        # 后面的 battleMist / size / 各数组不解析（只读状态用不上）
        out["remaining"] = max(0, WORLDBOSS_MAX_FIGHTS - int(out["fights"] or 0))
        out["end_time_text"] = format_end_time(out["end_time"])
        out["next_start_text"] = format_end_time(out["next_start_time"])
        out["ok"] = True
    except Exception as e:
        out["error"] = f"解析失败: {type(e).__name__}: {e}"
    return out


def query_worldboss_rank(sid, uid, rank_type=0, timeout=20):
    """CMD176 NewBossRankNewReq：查询世界魔王排行榜（只读）。

    req: [Long]sid [Long]uid [Int]type
    ack: [Long]myrank [Long]myscore [Short]size
         size × { [Short]reclen [Long]uid [UTF]name [Int]kind [Int]level
                  [Int]like [Long]totaldamage [Int]token [Int]status }
    """
    rank_type = int(rank_type)
    c, p, b = _send(CMD176, [("int", rank_type)], sid, uid, timeout=timeout)
    out = {"ok": False, "pid": p, "type": rank_type,
           "type_name": WORLDBOSS_RANK_TYPES.get(rank_type, f"type={rank_type}"),
           "my_rank": None, "my_score": None,
           "size": None, "entries": [], "error": None}
    if p != CMD176:
        out["error"] = _err(b)
        return out
    try:
        off = 1

        def i64():
            nonlocal off
            v = int.from_bytes(b[off:off + 8], "big", signed=True)
            off += 8
            return v

        def i32():
            nonlocal off
            v = int.from_bytes(b[off:off + 4], "big", signed=True)
            off += 4
            return v

        def u16():
            nonlocal off
            v = int.from_bytes(b[off:off + 2], "big")
            off += 2
            return v

        def utf():
            nonlocal off
            n = u16()
            s = b[off:off + n * 2].decode("utf-16-be", "replace")
            off += n * 2
            return s

        out["my_rank"] = i64()
        out["my_score"] = i64()
        size = u16()
        out["size"] = size
        entries = []
        for i in range(size):
            rec_start = off
            rec_len = u16()
            e = {"rank": i + 1}
            e["uid"] = i64()
            e["name"] = utf()
            e["kind"] = i32()
            e["level"] = i32()
            e["like"] = i32()
            e["damage"] = i64()
            e["token"] = i32()
            e["status"] = i32()
            entries.append(e)
            # 记录长度从 [Short]reclen 之后算起；多出的字段直接跳过
            rec_end = rec_start + 2 + rec_len
            if off < rec_end:
                off = rec_end
            elif off > rec_end:
                raise ValueError(f"第 {i + 1} 条记录越界: {off} > {rec_end}")
        out["entries"] = entries
        out["ok"] = True
    except Exception as e:
        out["error"] = f"解析失败: {type(e).__name__}: {e}"
    return out


def _err(b):
    try:
        n = int.from_bytes(b[1:3], 'big')
        return b[3:3 + n * 2].decode('utf-16-be', 'replace')
    except Exception:
        return b.hex()


def _send(cmd, fields, sid, uid, counter=1, timeout=20):
    import gzip, time, urllib.request
    body = P.w_long(0) + P.w_int(counter) + P.w_byte(cmd)
    body += P.w_long(sid) + P.w_long(uid)
    for typ, val in fields:
        if typ == "long":
            body += P.w_long(val)
        elif typ == "int":
            body += P.w_int(val)
        elif typ == "short":
            body += P.w_short(val)
        elif typ == "utf":
            body += P.w_utf(str(val))
        elif typ == "bytes":
            val = bytes(val or b"")
            body += P.w_int(len(val)) + val
        else:
            raise ValueError(typ)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    payload = struct.pack(">I", len(body)) + struct.pack(">I", crc) + body
    req = urllib.request.Request(P.URL, data=payload, method="POST")
    req.add_header("Connection", "Keep-Alive")
    req.add_header("KEY", f"{counter}^{int(time.time() * 1000)}")
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = resp.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    _c, ln = struct.unpack_from(">II", data, 0)
    b = data[8:8 + ln]
    return resp.getcode(), b[0], b


# ─────────────────────────────────────
#  3.5) sid / uid 记录
# ─────────────────────────────────────
def sid_uid_log_path(uid, when=None):
    """按 uid + 日期命名的记录文件路径：accounts/sid_uid/<uid>_<YYYY-MM-DD>.json"""
    when = when or datetime.now()
    return os.path.join(SID_UID_DIR, f"{int(uid)}_{when.strftime('%Y-%m-%d')}.json")


def record_sid_uid(uid, sid, source="login", **extra):
    """把 sid / uid 追加写入按 uid + 日期命名的文件，返回文件路径。

    · 同一天重复执行只会在同一天的同一个文件里追加一条记录
    · 文件结构：{"uid":..., "date":"...", "records":[{time, sid, source, ...}]}
    """
    os.makedirs(SID_UID_DIR, exist_ok=True)
    now = datetime.now()
    path = sid_uid_log_path(uid, now)

    doc = {"uid": int(uid), "date": now.strftime("%Y-%m-%d"), "records": []}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and isinstance(old.get("records"), list):
                doc = old
                doc["uid"] = int(uid)
        except Exception:
            pass  # 文件损坏就重写，不阻断主流程

    rec = {"time": now.strftime("%Y-%m-%d %H:%M:%S"),
           "uid": int(uid), "sid": int(sid), "source": source}
    for k, v in extra.items():
        if v is not None:
            rec[k] = v
    doc["records"].append(rec)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return path


# ─────────────────────────────────────
#  3.6) CMD137 排行榜（冒险王：大选关榜 / 爬塔榜）
# ─────────────────────────────────────
def query_rank137(sid, uid, timeout=20):
    """请求 CMD137 拿排行榜。

    请求体：只有 [Long]sid + [Long]uid（_send 已自带，无其它字段）
    响应体：
        [Int]result  [Long]endTime  [Int]rank0  [Int]rank1  [Short]size
        然后 size 条记录，每条：
            [Short]记录字节长  [Long]uid  [Int]rank  [UTF]name  [Int]type  [Int]value
    返回 dict：
        {"ok", "pid", "result", "end_time", "rank0", "rank1", "entries", "error"}
        entries = [{"uid","rank","name","type","value"}, ...]
    """
    code, pid, b = _send(CMD137, [], sid, uid, timeout=timeout)
    if pid != CMD137 or len(b) < 5:
        return {"ok": False, "pid": pid, "error": _err(b), "entries": []}

    try:
        off = 1
        (result,) = struct.unpack_from(">i", b, off); off += 4
        (end_time,) = struct.unpack_from(">q", b, off); off += 8
        (rank0,) = struct.unpack_from(">i", b, off); off += 4
        (rank1,) = struct.unpack_from(">i", b, off); off += 4
        (size,) = struct.unpack_from(">H", b, off); off += 2

        entries = []
        for _ in range(size):
            (rec_len,) = struct.unpack_from(">H", b, off); off += 2
            rec_start = off
            (puid,) = struct.unpack_from(">Q", b, off); off += 8
            (prank,) = struct.unpack_from(">i", b, off); off += 4
            name, off = P.read_utf(b, off)
            (ptype,) = struct.unpack_from(">i", b, off); off += 4
            (pvalue,) = struct.unpack_from(">i", b, off); off += 4
            entries.append({"uid": puid, "rank": prank, "name": name,
                            "type": ptype, "value": pvalue,
                            "rec_len": rec_len, "rec_off": rec_start})
            # 按记录长度对齐（防止某些字段解析偏差导致后续错位）
            if rec_len and rec_start + rec_len > off:
                off = rec_start + rec_len
        return {"ok": True, "pid": pid, "result": result, "end_time": end_time,
                "rank0": rank0, "rank1": rank1, "entries": entries, "error": None}
    except Exception as e:
        return {"ok": False, "pid": pid, "entries": [],
                "error": f"响应解析失败: {type(e).__name__}: {e}"}


def rank_top_per_type(entries):
    """按 type 分组，取每组的榜一（rank 最小，跳过 rank<0 的未上榜记录）。

    返回 {type: entry 或 None}
    """
    tops = {}
    for e in entries:
        t = e.get("type")
        r = e.get("rank", -1)
        if r is None or r < 0:
            tops.setdefault(t, None)
            continue
        cur = tops.get(t)
        if not isinstance(cur, dict) or r < cur.get("rank", 10 ** 9):
            tops[t] = e
    return tops


def _dedupe_entries(entries):
    """多次窗口查询会重复返回同一玩家，按 (type, uid) 去重，保留 rank 最小的那条。"""
    best = {}
    for e in entries:
        key = (e.get("type"), int(e.get("uid") or 0))
        cur = best.get(key)
        if cur is None or int(e.get("rank", 10 ** 9)) < int(cur.get("rank", 10 ** 9)):
            best[key] = e
    return list(best.values())


def query_rank137_full(sid, uid, max_steps=20, verbose=True):
    """CMD137 只返回【以查询 uid 为中心】的一段窗口，榜一不一定在里面。

    如果窗口里某类榜单最高只有 rank>1，就用该玩家 uid 再查一次
    （实测服务端接受用自己的 sid + 别人的 uid 查询），逐级向上，
    直到拿到 rank=1 或不再有进展。

    返回 dict 与 query_rank137 相同，但 entries 是多次查询合并后的结果；
    result / end_time / rank0 / rank1 仍取【第一次用自己 uid 查询】的值。
    """
    info = query_rank137(sid, uid)
    if not info.get("ok"):
        return info

    entries = list(info.get("entries") or [])
    seen = {int(uid)}

    for _ in range(max_steps):
        tops = rank_top_per_type(entries)

        # 找当前窗口里“最接近榜一、且不是自己”的记录作为下一次查询中心
        target = None
        for t in (RANK_TYPE_DAXUANGUAN, RANK_TYPE_TOWER):
            e = tops.get(t)
            if not e:
                continue
            if int(e["rank"]) <= 1:
                continue
            if int(e["uid"]) == int(uid):
                continue
            if target is None or int(e["rank"]) < int(target["rank"]):
                target = e
        if target is None:
            break

        tuid = int(target["uid"])
        if tuid in seen:
            break
        seen.add(tuid)

        if verbose:
            print(f"  榜单窗口未含榜一，改用 uid={tuid}"
                  f"（当前最高 rank={target['rank']}）继续向上查 ...")
        nxt = query_rank137(sid, tuid)
        if not nxt.get("ok"):
            break

        before = {t: (rank_top_per_type(entries).get(t) or {}).get("rank")
                  for t in (RANK_TYPE_DAXUANGUAN, RANK_TYPE_TOWER)}
        entries.extend(nxt.get("entries") or [])
        after = {t: (rank_top_per_type(entries).get(t) or {}).get("rank")
                 for t in (RANK_TYPE_DAXUANGUAN, RANK_TYPE_TOWER)}

        # 没有更靠前就停，避免死循环
        if all(after.get(t) == before.get(t) for t in before):
            break

    info["entries"] = _dedupe_entries(entries)
    return info


def format_end_time(ms):
    """CMD137 的 endTime（毫秒时间戳）→ 'YYYY-MM-DD HH:MM:SS'，无效返回空串。"""
    try:
        ms = int(ms or 0)
    except Exception:
        return ""
    if ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms)


def print_rank137(info, my_uid):
    """打印两个榜单（大选关 / 爬塔）的榜一，并判断榜一是否为自己。"""
    if not info.get("ok"):
        print(f"  ✗ pid={info.get('pid')} {info.get('error')!r}")
        return

    entries = info.get("entries") or []
    end_s = format_end_time(info.get("end_time")) or "未知"

    print(f"  ✓ result={info['result']}  条目={len(entries)}  "
          f"榜单截止={end_s}")
    print(f"    rank0(大选关下一档目标)={info['rank0']}  "
          f"rank1(爬塔下一档目标)={info['rank1']}")

    tops = rank_top_per_type(entries)
    for t, label in ((RANK_TYPE_DAXUANGUAN, "大选关榜"), (RANK_TYPE_TOWER, "爬塔榜")):
        e = tops.get(t)
        if not e:
            print(f"\n  【{label} type={t}】榜上暂无数据")
            continue
        is_me = "★ 是我（当前 uid）" if int(e["uid"]) == int(my_uid) else "否"
        top_rank = int(e["rank"])
        if top_rank == 1:
            print(f"\n  【{label} type={t}】榜一")
        else:
            print(f"\n  【{label} type={t}】窗口内最高 rank={top_rank}（未取到榜一）")
        print(f"    玩家  : {e['name']}")
        print(f"    uid   : {e['uid']}")
        print(f"    分数  : {e['value']}")
        print(f"    名次  : rank={e['rank']}")
        print(f"    是我  : {is_me}")

    other = [t for t in tops if t not in (RANK_TYPE_DAXUANGUAN, RANK_TYPE_TOWER)]
    if other:
        print(f"\n  （另有未知 type 的榜单：{sorted(other)}）")


# ─────────────────────────────────────
#  3.6) 竞技场「匹配→取消」把本地积分推给服务端
# ─────────────────────────────────────
# ★★ 为什么这样做：
#   CMD25 PipeiCode 的 score 字段就是 GameData.playerScore（竞技场积分），
#   服务端收到后即更新自己的积分记录，排行榜（CMD24）随之变化。
#   所以改完存档只要「匹配一次再取消」，不用真打，排行榜分数就刷新了。
#   日志实证：本地改成 1300 → 匹配 score=1300 → 取消后 CMD24 名次 1170 → 2。
CMD25 = 25                        # PipeiCodeReq / Ack（开始匹配，携带 score）
CMD38 = 38                        # PipeiInfoReq / Ack（匹配进度轮询，约 2 秒一次）
CMD26 = 26                        # PipeioffCodeReq / Ack（取消匹配）
# 日志实测的两个 ByteArray 长度（守卫阵容 / 物品阵容；服务端只做存档，实测填 0 也能进队列）
MATCH_GUAIWU_LEN = 92
MATCH_WUPIN_LEN = 317


def query_rank24(sid, uid, timeout=25):
    """CMD24 PaihangbangCode 查竞技场排行榜。

    ack = [Int]result [Int]myRank [Short]size
          循环 size 条： [Short]记录长 [Long]uid [Int]等级 [UTF]昵称 [Int]分数

    ★ 记录里那个 Int 是【等级】不是名次；榜按分数降序，名次 = 下标 + 1，
      自己的名次取头部的 myRank（可能 > 100，说明在榜外）。
    """
    c, p, b = _send(24, [("int", 0)], sid, uid, timeout=timeout)
    if p != 24:
        return {"ok": False, "pid": p, "error": _err(b)}
    r = b[1:]
    try:
        result = int.from_bytes(r[0:4], 'big', signed=True)
        my_rank = int.from_bytes(r[4:8], 'big', signed=True)
        size = int.from_bytes(r[8:10], 'big')
        off = 10
        rows = []
        for i in range(size):
            rec_len = int.from_bytes(r[off:off + 2], 'big')
            off += 2
            end = off + rec_len
            u = int.from_bytes(r[off:off + 8], 'big'); off += 8
            lv = int.from_bytes(r[off:off + 4], 'big', signed=True); off += 4
            nm, off = P.read_utf(r, off)
            sc = int.from_bytes(r[off:off + 4], 'big', signed=True); off += 4
            rows.append({"rank": i + 1, "uid": u, "lv": lv, "name": nm, "score": sc})
            off = max(off, end)          # 记录内若有未解字段，按记录长度对齐
    except Exception as e:
        return {"ok": False, "pid": p, "error": f"解析失败: {e}"}
    mine = next((x for x in rows if int(x["uid"]) == int(uid)), None)
    return {"ok": result == 1, "pid": p, "result": result,
            "my_rank": my_rank, "entries": rows, "mine": mine}


def match_pvp(sid, uid, score, lv, name,
              guaiwu=None, wupin=None, fangjian_id=0, timeout=20):
    """CMD25 开始匹配，把 score 推给服务端。

    req = [Long]sid [Long]uid [Int]score [Int]lv [UTF]name
          [ByteArray]guaiwuData [ByteArray]wupinData [Int]fangjianID
    ack = [Int]result [Int]fangjianID [Int]fangjianType

    ★ result = 匹配倒计时秒数，> 0 表示已进队列；<= 0 是错误码。
    """
    g = bytes(guaiwu) if guaiwu is not None else bytes(MATCH_GUAIWU_LEN)
    w = bytes(wupin) if wupin is not None else bytes(MATCH_WUPIN_LEN)
    c, p, b = _send(CMD25, [("int", int(score)), ("int", int(lv)),
                            ("utf", str(name)), ("bytes", g), ("bytes", w),
                            ("int", int(fangjian_id))], sid, uid, timeout=timeout)
    if p != CMD25:
        return {"ok": False, "pid": p, "error": _err(b)}
    r = b[1:]
    result = int.from_bytes(r[0:4], 'big', signed=True) if len(r) >= 4 else 0
    fj_id = int.from_bytes(r[4:8], 'big', signed=True) if len(r) >= 8 else 0
    fj_type = int.from_bytes(r[8:12], 'big', signed=True) if len(r) >= 12 else 0
    return {"ok": result > 0, "pid": p, "result": result,
            "fangjian_id": fj_id, "fangjian_type": fj_type, "error": None}


def poll_pipei(sid, uid, timeout=20):
    """CMD38 轮询匹配进度。

    req = [Int]0
    ack = [Int]剩余秒数/结果 [Long]对手uid [Int]对手分数 [UTF]对手昵称 …（后有阵容字节）
    ★ 对手 uid 非 0 表示已匹配成功。
    """
    c, p, b = _send(CMD38, [("int", 0)], sid, uid, timeout=timeout)
    if p != CMD38:
        return {"ok": False, "pid": p, "error": _err(b)}
    r = b[1:]
    try:
        result = int.from_bytes(r[0:4], 'big', signed=True)
        opp_uid = int.from_bytes(r[4:12], 'big')
        opp_score = int.from_bytes(r[12:16], 'big', signed=True)
        opp_name, _ = P.read_utf(r, 16)
    except Exception as e:
        return {"ok": False, "pid": p, "error": f"解析失败: {e}"}
    return {"ok": True, "pid": p, "result": result, "opp_uid": opp_uid,
            "opp_score": opp_score, "opp_name": opp_name, "error": None}


def cancel_match(sid, uid, timeout=20):
    """CMD26 取消匹配。req 无字段；ack = [Int]result（1 = 成功）。"""
    c, p, b = _send(CMD26, [], sid, uid, timeout=timeout)
    if p != CMD26:
        return {"ok": False, "pid": p, "error": _err(b)}
    result = int.from_bytes(b[1:5], 'big', signed=True) if len(b) >= 5 else None
    return {"ok": result == 1, "pid": p, "result": result, "error": None}


def local_pvp_score(save_path=None):
    """从容器存档读 GameData.playerScore（竞技场积分）。

    返回 (值, 昵称, 错误说明)
    """
    try:
        if os.path.join(BASE_DIR, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(BASE_DIR, "tools"))
        import pvp_score as _PS
    except Exception as e:
        return None, None, f"导入 tools/pvp_score.py 失败: {e}"
    path = save_path or _PS.autodetect()
    if not path or not os.path.exists(path):
        return None, None, "找不到容器里的 soco.bin"
    try:
        loc = _PS.locate_player_score(open(path, 'rb').read())
    except Exception as e:
        return None, None, f"存档里定位 playerScore 失败: {e}"
    return loc["value"], loc["name"], None


# ─────────────────────────────────────
#  3.7) 改昵称（P22 ImportNameCode）+ 默认昵称自动替换
# ─────────────────────────────────────
# 注册时服务器给的默认昵称形如 Player9507983 / Player1564...
DEFAULT_NAME_RE = re.compile(r"^[Pp]layer\d+$")
# ★ P22 必须带这个前缀，否则服务器直接回「帐号异常，请联系客服!」
#   实测日志：发送 "0000_izhaohe_慈爱的纯儿230" → 回包 "慈爱的纯儿230"（前缀被服务端剥掉）
NAME_CODE_PREFIX = "0000_izhaohe_"
# 服务器提示「太长」时用来识别的关键词
TOO_LONG_WORDS = ("太长", "过长", "长度", "超出", "超过", "不能超过", "too long", "length")
# 名字被占用时用来识别的关键词
NAME_TAKEN_WORDS = ("已存在", "已占用", "重复", "同名", "使用过", "exist", "taken")
# 命中敏感词时用来识别的关键词
SENSITIVE_WORDS = ("敏感词", "敏感", "违规", "非法")
# 备用名字库（排行榜里挑不到可用名字时兜底）
FALLBACK_BASES = ["夜行者", "月下客", "山海谣", "风起时", "拾光者"]


def is_default_name(name):
    """昵称是否是 Playerxxxx 这种系统默认名。"""
    return bool(name) and bool(DEFAULT_NAME_RE.match(str(name).strip()))


def _strip_trailing_digits(name):
    """去掉名字末尾的号码，避免“名字+随机数”叠成一长串数字。"""
    return re.sub(r"\d+$", "", str(name)).strip() or str(name).strip()


def base_candidates(entries, my_uid, limit=3):
    """给出候选底名列表 [(底名, 来源描述)]，按优先级排序。

    先取排行榜里【非自己、非默认名】的前 limit 个（rank 小的优先），
    不足时用 FALLBACK_BASES 兜底（排行榜为空/全是默认名时也能改名）。
    """
    cands = []
    for e in entries or []:
        if int(e.get("uid", 0)) == int(my_uid):
            continue
        nm = str(e.get("name") or "").strip()
        if not nm or is_default_name(nm):
            continue
        r = e.get("rank", 10 ** 9)
        r = r if isinstance(r, int) and r >= 0 else 10 ** 9
        cands.append((r, nm))
    cands.sort()

    out, seen = [], set()
    for r, nm in cands:
        base = _strip_trailing_digits(nm)
        if not base or base in seen:
            continue
        seen.add(base)
        out.append((base, f"排行榜第{r}名 {nm!r}" if r != 10 ** 9 else f"排行榜 {nm!r}"))
        if len(out) >= limit:
            break
    for fb in FALLBACK_BASES:
        if len(out) >= limit + 2:
            break
        if fb in seen:
            continue
        seen.add(fb)
        out.append((fb, "备用名库"))
    return out


def pick_base_name(entries, my_uid):
    """从排行榜里挑一个【非自己】的玩家昵称，去掉尾部数字作为底名（兼容旧调用）。"""
    cs = base_candidates(entries, my_uid, limit=1)
    return cs[0][0] if cs else random.choice(FALLBACK_BASES)


def change_nickname(sid, uid, name, timeout=20, use_prefix=True):
    """P22 改昵称。

    ★ 线上必须是 NAME_CODE_PREFIX + 名字，否则服务器一律回「帐号异常，请联系客服!」。
      日志实测：Write [UTF]0000_izhaohe_慈爱的纯儿230 → Read [UTF]慈爱的纯儿230

    req: [Long]sid [Long]uid [UTF]<前缀 + 名字>
    ack: 成功 → [B]22 + [UTF]最终昵称（已去掉前缀）；失败 → [B]255 + [UTF]错误文案
    返回 {"ok","pid","name","message","sent"}
    """
    sent = (NAME_CODE_PREFIX + str(name)) if use_prefix else str(name)
    c, p, b = _send(CMD22, [("utf", sent)], sid, uid, timeout=timeout)
    if p != CMD22:
        return {"ok": False, "pid": p, "name": None, "message": _err(b), "sent": sent}
    try:
        new_name, _ = P.read_utf(b, 1)
    except Exception:
        new_name = None
    if not new_name:
        return {"ok": False, "pid": p, "name": None,
                "message": b[1:].hex() or "空响应", "sent": sent}
    return {"ok": True, "pid": p, "name": new_name, "message": None, "sent": sent}


def ensure_nickname(sid, uid, fallback_name=None, log=print, force=False):
    """上传分数前的统一前置：昵称是 Playerxxxx 就换成「别人的昵称 + 随机数字」。

    · 当前昵称优先用 CMD1 登录返回的 otherName（服务器真实昵称），
      137 里自己那条是【旧快照】（改名后榜上仍显示老名字），只作兜底
    · 底名从排行榜里的非自己玩家昵称来（去掉尾部数字）
    · 服务器回「太长」就每次少 2 个字重试；回「敏感词」就换下一个底名
    返回 (最终昵称, 是否改过, entries)
    """
    info = query_rank137(sid, uid)
    entries = info.get("entries") or []

    self_name = None
    for e in entries:
        if int(e.get("uid", 0)) == int(uid):
            self_name = e.get("name")
            break

    # 两个来源里只要有一个是「非默认名」，就认为已经改过名，直接跳过
    cands = [fallback_name, self_name]
    cur = next((c for c in cands if c and not is_default_name(c)), None) or (fallback_name or self_name)

    if not force and not is_default_name(cur):
        log(f"  昵称检查：{cur!r} 不是默认名，跳过改名")
        return cur, False, entries

    if force and not is_default_name(cur):
        log(f"  昵称检查：{cur!r}（强制改名模式）")
    else:
        log(f"  昵称检查：{cur!r} 是系统默认名，准备改名")
    bases = base_candidates(entries, uid)
    digits = str(random.randint(100, 9999))
    log(f"  候选底名（取自排行榜非自己玩家）：{[b for b, _ in bases]}")

    cuts = [0, 2, 4, 6, 8]      # 每次重试少几个字
    attempts = 0
    # 逐个底名试：每个底名先按原长度试，遇到「太长」逐级截短，
    # 遇到「敏感词」直接换下一个底名。
    for base, src in bases:
        ci = 0
        while ci < len(cuts):
            if attempts >= 10:
                break
            attempts += 1
            core = base[:max(1, len(base) - cuts[ci])]
            candidate = f"{core}{digits}"
            log(f"  → 尝试改名 {candidate!r}（第 {attempts} 次，底名来自{src}）")
            r = change_nickname(sid, uid, candidate)
            if r["ok"]:
                log(f"  ✓ 改名成功：{cur!r} → {r['name']!r}")
                return r["name"], True, entries
            msg = str(r.get("message") or "")
            if any(w in msg for w in TOO_LONG_WORDS):
                log(f"    服务器说名字太长（{msg}），少 2 个字再试")
                ci += 1
                continue
            if any(w in msg for w in SENSITIVE_WORDS):
                log(f"    含敏感词（{msg}），换下一个底名")
                break
            if any(w in msg.lower() for w in NAME_TAKEN_WORDS):
                log(f"    名字被占用（{msg}），换一组随机数字再试")
                digits = str(random.randint(100, 9999))
                continue
            log(f"    ✗ 改名失败：{msg}")
            return cur, False, entries

    log("  ✗ 改名连续失败（底名与长度都试过了），保持原名继续上传分数")
    return cur, False, entries


# ─────────────────────────────────────
#  4) 存档构造
# ─────────────────────────────────────
def locate_uid_field(data, hint_uid=None):
    """定位存档内 Netdata_uid（77 1f blockdata 内的 int64）。

    hint_uid 给定时优先精确匹配；否则返回所有 1.4e18~1.7e18 范围的 int64 候选。
    """
    n = len(data)
    if hint_uid:
        pat = struct.pack(">Q", hint_uid)
        off = data.find(pat)
        if off >= 0:
            return [(off, hint_uid)]
    cands = []
    i = 0
    while i < n - 2:
        if data[i] == 0x77 and data[i + 1] == 0x1f:
            body = i + 2
            for off in range(body, min(body + 0x1f - 7, n - 8)):
                v = int.from_bytes(data[off:off + 8], 'big')
                if 1.4e18 < v < 1.7e18:
                    cands.append((off, v))
            i += 2 + 0x1f
        else:
            i += 1
    return cands


def open_all_switches(data):
    """把模板存档里的功能开关全部打开。

    · isShow[29]        —— find_isShow
    · player_jiesuo[24] —— find_player_jiesuo
    返回 (data, info)
    """
    data = bytearray(data)
    info = {}

    s = R.find_isShow(bytes(data))
    if s:
        off, raw = s
        changed = 0
        for k in range(len(raw)):
            if data[off + k] != 1:
                data[off + k] = 1
                changed += 1
        info['isShow'] = f"{changed}/{len(raw)} 项置 1"

    j = R.find_player_jiesuo(bytes(data))
    if j:
        off, raw = j
        changed = 0
        for k in range(len(raw)):
            if data[off + k] != 1:
                data[off + k] = 1
                changed += 1
        info['player_jiesuo'] = f"{changed}/{len(raw)} 项置 1"

    return bytes(data), info


def build_save(template_path, target_uid, chapter, out_path=None,
               open_switches=True):
    """模板存档 → 全开开关 → 改章节 → 换 uid → 重算 CRC。"""
    with open(template_path, "rb") as f:
        data = f.read()
    info = {}

    # a) 功能开关全开
    if open_switches:
        data, sw = open_all_switches(data)
        info.update(sw)

    # b) 章节（三处）
    ps = W.plan_chapter_patch(data, chapter)
    for k, label, nv, st, ol, nb, ov in sorted(ps, key=lambda x: x[3], reverse=True):
        info[label] = f"{ov} -> {nv}"
        data = W.splice(data, st, ol, nb)

    # c) 换账号 uid（模板里的旧 uid → 新 uid）
    cands = locate_uid_field(data)
    if cands:
        off, old_uid = cands[0]
        if old_uid != target_uid:
            data = data[:off] + struct.pack(">Q", target_uid) + data[off + 8:]
            info['Netdata_uid'] = f"{old_uid} -> {target_uid} @0x{off:X}"
    else:
        info['Netdata_uid'] = "未定位到（跳过）"

    # d) CRC
    data = bytearray(data)
    data[0:4] = zlib.crc32(bytes(data[4:])).to_bytes(4, 'big')
    data = bytes(data)

    # 校验
    ch = R.locate_daxuanguan(data)
    if not ch or ch.get('effective') != chapter - 1:
        raise RuntimeError(f"章节校验失败: {ch}")
    u = locate_uid_field(data, target_uid)
    if not u:
        raise RuntimeError(f"uid 校验失败：存档内未找到 {target_uid}")

    if out_path:
        with open(out_path, "wb") as f:
            f.write(data)
    return data, info


# ─────────────────────────────────────
#  5) NetData.bin
# ─────────────────────────────────────
def read_existing_netdata(path=NETDATA_PATH):
    """读取现有 NetData.bin 的真实 uid / 凭证，保证存档与之一致。"""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        d = f.read()
    if len(d) < 26:
        return None
    uid = struct.unpack(">q", d[10:18])[0]
    out = {"uid": uid}
    p = 18
    for nm in ("userName", "passwd", "nickName"):
        if p + 2 > len(d):
            break
        n = int.from_bytes(d[p:p + 2], "big")
        if not (0 < n < 64) or p + 2 + n > len(d):
            break
        try:
            out[nm] = d[p + 2:p + 2 + n].decode("utf-8")
        except UnicodeDecodeError:
            break
        p += 2 + n
    return out


def write_netdata(path, uid, user, passwd, nick, template=None):
    """生成 NetData.bin（登录凭证）。

    ★ 结构（实测，来自游戏正常生成的 205~206 字节文件）:
        0..4    4 字节校验/随机
        4..8    ac ed 00 05
        8..10   77 41            ← TC_BLOCKDATA, len=0x41=65
        10..18  uid (int64 BE)
        之后    [short][UTF-8] userName / passwd / nickName
                + 4 字节 (00 00 01 30)
        之后    后续 Java 对象(75 72 00 02 5b 5a ... 结尾含 HashSet 等)
                ← ★ 缺这一段游戏读档会崩！

    因此：优先用【模板 NetData】原地改写 uid + 三个字符串，保留其余结构；
    没有模板时才从零构造（此时补上标准的尾部结构）。
    """
    tpl = template or NETDATA_TEMPLATE
    if tpl and os.path.exists(tpl):
        with open(tpl, "rb") as f:
            d = bytearray(f.read())
        # 定位 blockdata 起点
        bi = d.find(b"\x77\x41")
        if bi < 0:
            bi = d.find(b"\x77\x40")
        if bi >= 0:
            p = bi + 2
            # 改 uid
            d[p:p + 8] = struct.pack(">q", uid)
            p += 8
            # 改三个字符串（长度可变，需要重排 blockdata 长度）
            parts = [user, passwd, nick]
            newblk = struct.pack(">q", uid)
            for s in parts:
                b = s.encode("utf-8")
                newblk += struct.pack(">H", len(b)) + b
            newblk += b"\x00\x00\x01\x30"
            # blockdata 长度是单字节
            if len(newblk) > 255:
                raise ValueError("NetData blockdata 过长")
            d[bi] = 0x77
            d[bi + 1] = len(newblk)
            d[bi + 2:bi + 2 + len(newblk)] = newblk
            # 若长度变化, 需处理尾部偏移: 后面是 Java 流, 直接整体重排
            tail = bytes(d[bi + 2 + len(newblk):])
            # 重新组装: 头部(0..bi) + blockdata + 尾部
            head = bytes(d[:bi])
            data = head + bytes([0x77, len(newblk)]) + newblk + tail
            # ★ NetData.bin 头 4 字节是 CRC32(从偏移4起的全部内容)，必须重算，
            #   否则游戏读档时抛 "crcValue!=value" 直接崩溃。
            data = bytearray(data)
            data[0:4] = zlib.crc32(bytes(data[4:])).to_bytes(4, "big")
            data = bytes(data)
            with open(path, "wb") as f:
                f.write(data)
            return len(data)

    # ---- 无模板: 从零构造（补齐尾部结构）----
    def s(x):
        b = x.encode("utf-8")
        return struct.pack(">H", len(b)) + b
    blk = struct.pack(">q", uid) + s(user) + s(passwd) + s(nick) + b"\x00\x00\x01\x30"
    if len(blk) > 255:
        raise ValueError("NetData 内容过长")
    # 尾部: boolean[13] + ... + HashSet 等（来自实测模板的固定尾巴）
    tail = bytes.fromhex(
        "757200025b5a578f203914b85de20200007870000000d0101010101010101010101"
        "0101770800000000010000007571007e000000000000050000000000737200116a"
        "6176612e7574696c2e48617368536574ba44859596b8b73403000078070770c0000"
        "00023f40000000000000787371007e0003770c000000023f4000000000000078"
    )
    data = b"\x4e\xae\xfe\xfa" + b"\xac\xed\x00\x05" + bytes([0x77, len(blk)]) + blk + tail
    data = bytearray(data)
    data[0:4] = zlib.crc32(bytes(data[4:])).to_bytes(4, "big")
    data = bytes(data)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


# ─────────────────────────────────────
#  账号存储：只有一个文件 accounts.json
#  {"current": "<uid>", "accounts": {"<uid>": {uid,userName,passwd,nickName,...}}}
# ─────────────────────────────────────
def _empty_doc():
    return {"current": None, "accounts": {}}


def _load_account_doc():
    """读取唯一账号文件；兼容“文件本身就是单个账号”的旧格式。"""
    if not os.path.exists(ACCOUNT_FILE):
        return _empty_doc()
    try:
        with open(ACCOUNT_FILE, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return _empty_doc()
    if not isinstance(doc, dict):
        return _empty_doc()

    if isinstance(doc.get("accounts"), dict):
        cur = doc.get("current")
        accounts = {str(k): v for k, v in doc["accounts"].items()
                    if isinstance(v, dict)}
        return {"current": str(cur) if cur else None, "accounts": accounts}

    if doc.get("uid") and doc.get("userName"):
        uid = str(doc["uid"])
        return {"current": uid, "accounts": {uid: doc}}

    return _empty_doc()


def _save_account_doc(doc):
    tmp = ACCOUNT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNT_FILE)


def _load_account_pool():
    return _load_account_doc()["accounts"]


def _save_account_pool(pool, current=None):
    doc = _load_account_doc()
    doc["accounts"] = pool
    if current is not None:
        doc["current"] = str(current) if current else None
    _save_account_doc(doc)


def _account_from_file(path=None):
    """读取账号。

    · path 省略（或指向账号文件）→ 返回“当前账号”
    · 其它 JSON 文件 → 当作单个账号读取（导入用）
    """
    path = path or ACCOUNT_FILE
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None

    if os.path.abspath(path) == os.path.abspath(ACCOUNT_FILE) or "accounts" in doc:
        accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
        cur = doc.get("current")
        if cur and str(cur) in accounts:
            return accounts[str(cur)]
        if len(accounts) == 1:
            return next(iter(accounts.values()))
        return None

    if doc.get("userName") and doc.get("passwd") and doc.get("uid"):
        return doc
    return None


def save_account(acc, make_current=True, source=None):
    """把账号写入唯一账号文件；make_current=True 时同时设为当前账号。"""
    if not acc or not acc.get("uid") or not acc.get("userName") or not acc.get("passwd"):
        return None
    uid = int(acc["uid"])
    doc = _load_account_doc()
    rec = dict(doc["accounts"].get(str(uid)) or {})
    for k in ("uid", "userName", "passwd", "nickName", "playerLevel",
              "sid", "servertime", "status"):
        if acc.get(k) is not None:
            rec[k] = acc[k]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 导入时允许带上 createdAt，但已有记录不会被覆盖
    if acc.get("createdAt") and not rec.get("createdAt"):
        rec["createdAt"] = acc["createdAt"]
    # ★ 创建时间：独立字段，只在第一次写入时设置，后续任何保存都不覆盖
    if not rec.get("createdAt"):
        rec["createdAt"] = rec.get("updated") or now
    rec["uid"] = uid
    rec["updated"] = now
    if source:
        rec["source"] = source
    doc["accounts"][str(uid)] = rec
    if make_current:
        doc["current"] = str(uid)
    _save_account_doc(doc)
    return rec


def load_saved_account(selector=None):
    """读取账号凭证（全部来自唯一账号文件）。

    selector 省略：返回当前账号
    selector 可以是：uid / userName / nickName（支持子串）/ JSON 文件路径
    多个昵称同时匹配时会打印候选并返回 None。
    """
    if not selector:
        return _account_from_file(ACCOUNT_FILE)

    if os.path.isfile(selector):
        acc = _account_from_file(selector)
        if acc:
            return acc

    sel = str(selector).strip()
    pool = _load_account_pool()
    cur = _account_from_file(ACCOUNT_FILE)

    def _match(acc, exact=True):
        if not acc:
            return False
        if exact:
            return sel in (str(acc.get("uid")), acc.get("userName"), acc.get("nickName"))
        return (sel in str(acc.get("uid") or "")
                or sel in str(acc.get("userName") or "")
                or sel in str(acc.get("nickName") or ""))

    # 1) 精确匹配（uid / userName / nickName）
    exact = [r for r in pool.values() if _match(r, exact=True)]
    if len(exact) == 1:
        return exact[0]

    # 2) 当前账号精确匹配
    if _match(cur, exact=True):
        return cur

    # 3) 子串匹配
    part = [r for r in pool.values() if _match(r, exact=False)]
    if len(part) == 1:
        return part[0]

    # 4) 当前账号子串匹配
    if _match(cur, exact=False):
        return cur

    # 5) 多义 / 找不到
    cands = part or exact
    if cands:
        print(f"  ✗ 账号选择器 {selector!r} 匹配到多个账号，请改用 uid：")
        for r in cands:
            print(f"    uid={r.get('uid')}  user={r.get('userName')}  "
                  f"nick={r.get('nickName')}")
    else:
        print(f"  ✗ 账号文件里没有 {selector!r}，可用 `--list-accounts` 查看")
    return None


_MIGRATED = False


def migrate_current_account():
    """把旧版两文件布局（offline_account.json + accounts/accounts.json）
    合并进唯一账号文件，只做一次。"""
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True

    doc = _load_account_doc()
    changed = False

    legacy_pool = {}
    if os.path.exists(LEGACY_POOL_FILE):
        try:
            with open(LEGACY_POOL_FILE, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and isinstance(old.get("accounts"), dict):
                legacy_pool = old["accounts"]
        except Exception:
            pass

    legacy_cur = None
    if os.path.exists(LEGACY_ACCOUNT_FILE):
        try:
            with open(LEGACY_ACCOUNT_FILE, encoding="utf-8") as f:
                c = json.load(f)
            if isinstance(c, dict) and c.get("uid") and c.get("userName"):
                legacy_cur = c
                legacy_pool.setdefault(str(c["uid"]), c)
        except Exception:
            pass

    for uid, rec in legacy_pool.items():
        if not isinstance(rec, dict):
            continue
        if str(uid) not in doc["accounts"] and rec.get("userName") and rec.get("passwd"):
            doc["accounts"][str(uid)] = rec
            changed = True

    if legacy_cur and not doc.get("current"):
        doc["current"] = str(legacy_cur["uid"])
        changed = True

    if changed:
        _save_account_doc(doc)
        # 旧文件改名留存，避免以后又被误读
        for p in (LEGACY_ACCOUNT_FILE, LEGACY_POOL_FILE):
            if os.path.exists(p):
                try:
                    os.replace(p, p + ".migrated")
                except Exception:
                    pass
        print(f"  ✓ 已迁移旧账号数据 -> {os.path.basename(ACCOUNT_FILE)}"
              f"（共 {len(doc['accounts'])} 个账号）")


def list_accounts():
    """打印唯一账号文件里的所有账号，★ 标记当前账号。"""
    doc = _load_account_doc()
    pool = doc["accounts"]
    cur = _account_from_file(ACCOUNT_FILE) or {}
    cur_uid = str(cur.get("uid") or doc.get("current") or "")

    print(f"\n账号文件: {os.path.relpath(ACCOUNT_FILE, BASE_DIR)}")
    if not pool:
        print("  （还没有保存的账号）")
        return

    print(f"  {'':1} {'uid':<20} {'userName':<28} {'nickName':<20} 最近更新")
    for uid, r in sorted(pool.items(),
                         key=lambda kv: str(kv[1].get("updated") or ""),
                         reverse=True):
        mark = "★" if str(uid) == cur_uid else " "
        print(f"  {mark:1} {str(r.get('uid') or uid):<20} "
              f"{str(r.get('userName') or ''):<28} "
              f"{str(r.get('nickName') or ''):<20} "
              f"{r.get('updated') or ''}")
    if cur_uid:
        print(f"\n当前账号: uid={cur_uid}  user={cur.get('userName')}  "
              f"nick={cur.get('nickName')}")
    print("\n切换账号：python3 offline_play.py jump --account <uid|userName|nickName>")


def jump_beat_tops(sid, uid, entries):
    """jump 后置步骤：对每个榜单，把榜一分数 ÷100 再 +1，×100 后上报。

    · 大选关榜 / 爬塔榜分别处理，只有 CMD136 的 type 不同
    · 必须真的拿到 rank=1 才上传；窗口里只有 rank>1 时跳过
    · 若榜一就是自己（当前 uid），该榜跳过，不上报
    """
    tops = rank_top_per_type(entries)
    print("\n[jump] 按榜一分数 ÷100 +1（上传时 ×100）分别上报 ...")

    results = {}
    for t, label in ((RANK_TYPE_DAXUANGUAN, "大选关榜"),
                     (RANK_TYPE_TOWER, "爬塔榜")):
        e = tops.get(t)
        if not e:
            print(f"\n  【{label} type={t}】榜上暂无数据，跳过")
            continue

        if int(e["rank"]) != 1:
            print(f"\n  【{label} type={t}】未取到榜一"
                  f"（窗口内最高 rank={e['rank']}），跳过上传")
            continue

        if int(e["uid"]) == int(uid):
            print(f"\n  【{label} type={t}】榜一就是自己"
                  f"（{e['name']} 分数={e['value']}），跳过上传")
            continue

        base = int(e["value"])
        value = base + 100          # = (base / 100 + 1) * 100
        print(f"\n  【{label} type={t}】")
        print(f"    榜一    : {e['name']}  uid={e['uid']}  rank={e['rank']}  "
              f"分数={base}")
        print(f"    设置分数: {base} ÷ 100 + 1  ×100 → value={value}")

        r = upload_rank_value(sid, uid, value, rank_type=t)
        if r["ok"]:
            print(f"    ✓ CMD136 result={r['result']}  "
                  f"type={r['type']}  value={r['value']}")
        else:
            print(f"    ✗ pid={r['pid']} {r['error']!r}")
        results[t] = r
    return results


def run_jump(args):
    """jump 模式：拿一个有效 sid，请求 CMD137，打印两个榜单的榜一。

    CMD137 只返回以查询 uid 为中心的窗口，因此会自动用窗口里排名更高的
    玩家 uid 继续查，直到拿到真正的 rank=1。
    随后对每个榜单，把榜一分数 ÷100 再 +1（×100）上报；榜一是自己则跳过。
    """
    print("\n[jump] CMD137 查询排行榜（大选关榜 / 爬塔榜）...")

    acc = None if args.new else load_saved_account(args.account)
    if acc:
        print(f"  复用已有账号 uid={acc['uid']}  user={acc['userName']}"
              f"（重新登录刷新 sid）")
        lg = login(acc["userName"], acc["passwd"],
                   acc.get("nickName") or acc["userName"])
    else:
        if args.account:
            print(f"  ✗ 找不到账号 {args.account!r}，可用 --list-accounts 查看")
            sys.exit(1)
        print("  注册新账号 ...")
        acc = register()
        print(f"  ✓ 新号 uid={acc['uid']}  user={acc['userName']}")
        lg = login(acc["userName"], acc["passwd"], acc["nickName"])

    uid = int(lg.get("uid") or acc["uid"])
    sid = int(lg["sid"])
    acc.update({"uid": uid, "sid": sid, "servertime": lg.get("servertime", 0)})
    if lg.get("otherName"):
        acc["nickName"] = lg["otherName"]
    print(f"  ✓ sid={sid}  uid={uid}  status={lg.get('status')}")

    # --new / --account 都视为“切换账号”；当前账号也顺手刷新 nick/sid
    switch = bool(args.new or args.account)
    save_account(acc, make_current=True, source="jump")
    if switch:
        print(f"  ✓ 当前账号已切换 -> uid={uid}  user={acc['userName']}  "
              f"nick={acc.get('nickName')}")

    path = record_sid_uid(uid, sid, source="jump",
                          user=acc.get("userName"), nick=acc.get("nickName"))
    print(f"  sid/uid 已记录 -> {os.path.relpath(path, BASE_DIR)}")

    info = query_rank137_full(sid, uid)
    print_rank137(info, uid)
    if info.get("ok"):
        jump_beat_tops(sid, uid, info.get("entries") or [])
    else:
        print("\n[jump] 排行榜查询失败，跳过上报")
    return info


# ─────────────────────────────────────
#  reuse：用已记录账号直接上报分数
# ─────────────────────────────────────
# 和注册流程一致：传入的分数先 ×100，再作为 CMD136 的 value。
# type=0（默认）= 大选关榜，type=1 = 爬塔榜，两个分支只有 type 字段不同。


def run_reuse(args):
    """复用 offline_account.json 里的账号，直接发 CMD136 上报指定分数。

    不注册新号、不生成存档、不写容器。
    """
    score = args.score
    if score is None:
        print("  ✗ reuse 需要指定分数，例如：python3 offline_play.py reuse 62")
        sys.exit(2)

    acc = load_saved_account(args.account)
    if not acc:
        if args.account:
            print(f"  ✗ 找不到账号 {args.account!r}，可用 --list-accounts 查看")
        else:
            print("  ✗ 没有已记录的账号（offline_account.json）")
            print("    请先跑一次 `--chapter XX`，或 `jump --new` 建一个号。")
        sys.exit(1)

    print(f"\n[reuse] 复用账号 uid={acc['uid']}  user={acc['userName']}")
    lg = login(acc["userName"], acc["passwd"],
               acc.get("nickName") or acc["userName"])
    uid = int(lg.get("uid") or acc["uid"])
    sid = int(lg["sid"])
    acc.update({"uid": uid, "sid": sid, "servertime": lg.get("servertime", 0)})
    if lg.get("otherName"):
        acc["nickName"] = lg["otherName"]
    print(f"  ✓ 登录成功 sid={sid} uid={uid} status={lg.get('status')}")

    save_account(acc, make_current=True, source="reuse")
    if args.account:
        print(f"  ✓ 当前账号已切换 -> uid={uid}  user={acc['userName']}  "
              f"nick={acc.get('nickName')}")

    path = record_sid_uid(uid, sid, source="reuse",
                          user=acc.get("userName"), nick=acc.get("nickName"))
    print(f"  sid/uid 已记录 -> {os.path.relpath(path, BASE_DIR)}")

    rank_type = args.rank_type
    value = score * 100
    print("\n[reuse] 上传前检查昵称 ...")
    new_name, renamed, _ = ensure_nickname(
        sid, uid, lg.get("otherName") or acc.get("nickName"))
    if renamed:
        acc["nickName"] = new_name
        save_account(acc, make_current=True, source="reuse")

    print(f"\n[reuse] CMD136 上报 {RANK_TYPE_NAMES.get(rank_type, rank_type)}："
          f"分数={score} ×100 → value={value}  "
          f"(type={rank_type}, serverId={RANK_SERVER_ID})")
    r = upload_rank_value(sid, uid, value, rank_type=rank_type)
    if not r["ok"]:
        print(f"  ✗ pid={r['pid']} {r['error']!r}")
        return r
    print(f"  ✓ pid=136 result={r['result']}  value={r['value']}")

    # 回查 137，确认榜上的名次与分数
    print("\n[reuse] 回查 CMD137 确认结果 ...")
    info = query_rank137(sid, uid)
    if not info.get("ok"):
        print(f"  ✗ 回查失败: {info.get('error')!r}")
        return r
    entries = info.get("entries") or []
    print(f"  ✓ result={info['result']}  条目={len(entries)}")
    mine = {}
    for e in entries:
        if int(e["uid"]) == uid:
            mine.setdefault(e["type"], e)
    for t, label in ((RANK_TYPE_DAXUANGUAN, "大选关榜"), (RANK_TYPE_TOWER, "爬塔榜")):
        e = mine.get(t)
        if e:
            print(f"  【{label}】我的名次 rank={e['rank']}  分数={e['value']}")
        else:
            print(f"  【{label}】返回列表里没有本人（未上榜或超出返回条数）")
    return r


def run_match(args):
    """模拟竞技场匹配 → 轮询几秒 → 取消，把本地积分推给服务端刷新排行榜。

    不注册新号、不改存档、不上报闯关。
    """
    acc = load_saved_account(args.account)
    if not acc:
        if args.account:
            print(f"  ✗ 找不到账号 {args.account!r}，可用 --list-accounts 查看")
        else:
            print("  ✗ 没有已记录的账号")
            print("    请先跑一次 `--chapter XX`，或 `jump --new` 建一个号。")
        sys.exit(1)

    print(f"\n[match] 复用账号 uid={acc['uid']}  user={acc['userName']}")
    lg = login(acc["userName"], acc["passwd"],
               acc.get("nickName") or acc["userName"])
    uid = int(lg.get("uid") or acc["uid"])
    sid = int(lg["sid"])
    acc.update({"uid": uid, "sid": sid, "servertime": lg.get("servertime", 0)})
    if lg.get("otherName"):
        acc["nickName"] = lg["otherName"]
    print(f"  ✓ 登录成功 sid={sid} uid={uid} status={lg.get('status')}")
    save_account(acc, make_current=True, source="match")
    path = record_sid_uid(uid, sid, source="match",
                          user=acc.get("userName"), nick=acc.get("nickName"))
    print(f"  sid/uid 已记录 -> {os.path.relpath(path, BASE_DIR)}")

    # ── 分数与昵称：优先取本地存档（playerScore / playerName）──
    save_score, save_name, err = local_pvp_score(args.save)
    score = args.score if args.score is not None else save_score
    if score is None:
        print(f"  ✗ 没法确定分数：{err or '请用 `match <分数>` 显式指定'}")
        sys.exit(1)
    name = save_name or lg.get("otherName") or acc.get("nickName")
    if args.score is not None and save_score is not None and args.score != save_score:
        print(f"  ⚠ 指定分数 {args.score} 与存档积分 {save_score} 不一致，"
              f"本次推 {args.score}")
    print(f"  推送分数 score={score}  lv={args.lv}  name={name!r}")

    # ── 昵称检查：和上传分数一样，默认名先改掉 ──
    new_name, renamed, _ = ensure_nickname(sid, uid, name)
    if renamed:
        name = new_name
        acc["nickName"] = new_name
        save_account(acc, make_current=True, source="match")

    print(f"\n[match] ① CMD25 开始匹配（score={score}）...")
    m = match_pvp(sid, uid, score, args.lv, name)
    if not m["ok"]:
        print(f"  ✗ 匹配请求被拒：pid={m.get('pid')} result={m.get('result')} "
              f"{m.get('error') or ''}")
        return m
    print(f"  ✓ 已进入匹配队列：倒计时 {m['result']} 秒  "
          f"fangjianID={m['fangjian_id']} fangjianType={m['fangjian_type']}")
    print("  （服务端此刻已收到新积分，排行榜稍后即变）")

    print(f"\n[match] ② CMD38 轮询 {args.wait} 秒 ...")
    deadline = time.time() + max(0, args.wait)
    matched = None
    while time.time() < deadline:
        time.sleep(min(2.0, max(0.2, deadline - time.time())))
        info = poll_pipei(sid, uid)
        if not info.get("ok"):
            print(f"    轮询失败：pid={info.get('pid')} {info.get('error') or ''}")
            break
        if int(info["opp_uid"]) != 0:
            matched = info
            print(f"    ⚠ 匹配到对手：uid={info['opp_uid']} "
                  f"分数={info['opp_score']} 昵称={info['opp_name']!r}")
            break
        print(f"    剩余 {info['result']} 秒，暂无对手")

    if args.keep:
        print("\n[match] ③ --keep：保留匹配状态，不取消")
    else:
        print("\n[match] ③ CMD26 取消匹配 ...")
        cm = cancel_match(sid, uid)
        if cm["ok"]:
            print(f"  ✓ 已取消（result={cm['result']}）")
        else:
            print(f"  ✗ 取消失败：pid={cm.get('pid')} result={cm.get('result')} "
                  f"{cm.get('error') or ''}")
        if matched:
            print("  ⚠ 注意：本次已配上对手，取消可能让对手进入异常房间，"
                  "进游戏确认一下状态")

    # ── 回查排行榜，确认服务端积分 ──
    print("\n[match] ④ 回查 CMD24 排行榜 ...")
    rk = query_rank24(sid, uid)
    if not rk.get("ok"):
        print(f"  ✗ 查榜失败：pid={rk.get('pid')} {rk.get('error') or ''}")
        return
    print(f"  ✓ result={rk['result']}  我的名次={rk['my_rank']}")
    for row in rk["entries"][:3]:
        tag = " ★我" if int(row["uid"]) == uid else ""
        print(f"    #{row['rank']:>3}  {row['score']:>7}  lv={row['lv']:<4} {row['name']}{tag}")
    mine = rk.get("mine")
    if mine:
        print(f"  我的榜上数据：rank={mine['rank']} 分数={mine['score']} 等级={mine['lv']}")
    else:
        print(f"  不在前 100（名次 {rk['my_rank']}），分数已提交但看不到自己那行")
    return rk


# ─────────────────────────────────────
#  main
# ─────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="脱机：注册→登录→生成存档→上报闯关（支持 jump 查榜）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("action", nargs="?", default=None,
                    choices=["jump", "reuse", "match", "accounts"],
                    metavar="ACTION",
                    help="可选动作：jump = 查 CMD137 两个榜一（自动向上翻窗口直到 rank=1），"
                         "并按（榜一分数÷100+1）×100 分别上报（榜一是自己则跳过）；"
                         "reuse = 用已记录账号上报分数；"
                         "match = 模拟竞技场匹配后取消，把本地积分推给服务端刷新排行榜；"
                         "accounts = 列出账号池")
    ap.add_argument("score", nargs="?", type=int, default=None,
                    help="reuse 的分数（会 ×100 后作为 CMD136 的 value）；"
                         "match 时为要推送的竞技场积分（省略则读存档）")
    ap.add_argument("--jump", action="store_true", dest="jump_flag",
                    help="同 jump：查 CMD137 两个榜一（自动向上翻窗口直到 rank=1），"
                         "并按（榜一分数÷100+1）×100 分别上报（榜一是自己则跳过）")
    ap.add_argument("--new", action="store_true",
                    help="jump 模式下强制注册新号，并切换为当前账号")
    ap.add_argument("--account", default=None, metavar="UID|USER|NICK|FILE",
                    help="指定账号（uid / userName / nickName / JSON 文件路径），"
                         "并切换为当前账号；默认用 offline_account.json")
    ap.add_argument("--list-accounts", action="store_true", dest="list_accounts",
                    help="列出账号池里的账号（同 `accounts`）")
    ap.add_argument("--type", type=int, choices=[0, 1], default=0, dest="rank_type",
                    help="reuse 的榜单类型：0=大选关榜（默认），1=爬塔榜；两者仅 type 不同")
    ap.add_argument("--wait", type=int, default=5, dest="wait",
                    help="match 模式：进入队列后轮询多少秒再取消（默认 5）")
    ap.add_argument("--lv", type=int, default=158,
                    help="match 模式：随分数一起上报的 lv 字段（日志实测 158，默认 158）")
    ap.add_argument("--keep", action="store_true",
                    help="match 模式：轮询结束后不取消匹配（默认会发 CMD26 取消）")
    ap.add_argument("--save", default=None, metavar="PATH",
                    help="match 模式：指定 soco.bin 路径（默认自动找容器里最新的）")
    ap.add_argument("--chapter", type=int, default=10, help="目标章节（默认 10）")
    ap.add_argument("--template", default=TEMPLATE_DEFAULT,
                    help=f"模板 soco.bin（默认 {TEMPLATE_DEFAULT}）")
    ap.add_argument("--no-switches", action="store_true", help="不打开模板里的功能开关")
    ap.add_argument("--no-save", action="store_true", help="不生成/不写存档")
    ap.add_argument("--no-rank", action="store_true", help="不调用 CMD136 上报闯关")
    ap.add_argument("--no-write", action="store_true", help="只生成文件，不覆盖容器")
    ap.add_argument("--out", default="offline_soco.bin", help="存档输出路径")
    ap.add_argument("--dry", action="store_true", help="只打印计划")
    args = ap.parse_args()

    print("=" * 70)
    print("  脱机流程：注册 → 登录 → 生成存档 → 上报闯关")
    print("=" * 70)

    migrate_current_account()

    if args.list_accounts or args.action == "accounts":
        list_accounts()
        return

    # ── jump / reuse 模式：只走协议，不注册、不写存档 ──
    if args.action == "reuse":
        try:
            run_reuse(args)
        except Exception as e:
            print(f"  ✗ reuse 失败: {type(e).__name__}: {e}")
            sys.exit(1)
        return

    if args.action == "match":
        try:
            run_match(args)
        except Exception as e:
            print(f"  ✗ match 失败: {type(e).__name__}: {e}")
            sys.exit(1)
        return

    if args.jump_flag or args.action == "jump":
        try:
            run_jump(args)
        except Exception as e:
            print(f"  ✗ jump 失败: {type(e).__name__}: {e}")
            sys.exit(1)
        return

    if not args.no_save and not os.path.exists(args.template):
        print(f"✗ 模板存档不存在: {args.template}")
        sys.exit(1)

    if args.dry:
        print(f"  [dry] 模板     : {args.template}")
        print(f"  [dry] 目标章节 : 第 {args.chapter} 章（存档值 {args.chapter-1}，"
              f"CMD136 value={args.chapter*100}）")
        print(f"  [dry] 输出     : {args.out}")
        print(f"  [dry] 打开开关 : {'否' if args.no_switches else '是'}")
        return

    # ── 1) 注册 ──
    print("\n[1/5] CMD4 注册 ...")
    acc = register()
    print(f"  ✓ uid={acc['uid']}  user={acc['userName']}  pwd={acc['passwd']}")
    print(f"    nick={acc['nickName']}  status={acc['status']}")

    # ── 2) 登录 ──
    print("\n[2/5] CMD1 登录 ...")
    lg = login(acc['userName'], acc['passwd'], acc['nickName'])
    acc.update({"sid": lg['sid'], "servertime": lg.get('servertime', 0)})
    print(f"  ✓ sid={lg['sid']}  status={lg['status']}")
    save_account(acc, make_current=True, source="register+login")
    print(f"  账号已保存 -> {os.path.relpath(ACCOUNT_FILE, BASE_DIR)}（已加入账号池）")
    p = record_sid_uid(acc['uid'], lg['sid'], source="register+login",
                       user=acc.get('userName'), nick=acc.get('nickName'))
    print(f"  sid/uid 已记录 -> {os.path.relpath(p, BASE_DIR)}")

    # ── 3) 上报闯关 ──
    if args.no_rank:
        print("\n[3/5] 跳过 CMD136 上报（--no-rank）")
    else:
        print(f"\n[3/5] CMD136 上报闯关（第 {args.chapter} 关, value={args.chapter*100}）...")
        print("  上传前检查昵称 ...")
        new_name, renamed, _ = ensure_nickname(
            lg['sid'], lg['uid'], lg.get('otherName') or acc.get('nickName'))
        if renamed:
            acc['nickName'] = new_name
            save_account(acc, make_current=True, source="register+login")
        r = upload_rank_level(lg['sid'], lg['uid'], args.chapter)
        if r['ok']:
            print(f"  ✓ pid=136 result={r['result']}  (value={r['value']})")
        else:
            print(f"  ✗ pid={r['pid']} {r['error']!r}")

    if args.no_save:
        print("\n[4/5] [5/5] 跳过存档生成（--no-save）")
        return

    # ── 4) 生成存档 ──
    print(f"\n[4/5] 用模板生成存档（第 {args.chapter} 章）...")
    print(f"  模板: {args.template}")
    data, info = build_save(args.template, acc['uid'], args.chapter,
                            args.out, open_switches=not args.no_switches)
    for k, v in info.items():
        print(f"  ✓ {k}: {v}")
    ch = R.locate_daxuanguan(data)
    disp = ch.get('chapter_display', (ch.get('effective', 0) + 1))
    print(f"  ✓ 校验: 第 {disp} 章 (effective={ch.get('effective')}), "
          f"{len(data)} 字节 -> {args.out}")

    if args.no_write:
        print("\n完成（--no-write，未覆盖容器）")
        return

    # ── 5) 落地 ──
    print("\n[5/5] 写入容器 ...")

    # ★ 关键：存档内 Netdata_uid 必须与 NetData.bin 的 uid 严格一致，
    #   否则游戏报"账号id与游戏数据不匹配"甚至崩溃。
    #   这里以【本次登录拿到的 uid】为准，重新确认一次。
    exist = read_existing_netdata()
    if exist and exist.get('uid') and exist['uid'] != acc['uid']:
        print(f"  ⚠ 现有 NetData.uid={exist['uid']} 与本次账号 uid={acc['uid']} 不同，")
        print(f"    将以本次账号为准重写 NetData.bin 并同步存档 uid")
        # 重建存档，改用 NetData 的 uid 会不一致，故这里直接重写 NetData
    # 确保存档 uid == acc['uid']
    if not locate_uid_field(data, acc['uid']):
        print("  ✗ 存档内 uid 与账号不一致，中止")
        sys.exit(1)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if os.path.exists(SAVE_PATH):
        bak = f"{SAVE_PATH}.bak.{ts}"
        shutil.copy2(SAVE_PATH, bak)
        print(f"  备份原存档 -> {os.path.basename(bak)}")
    with open(SAVE_PATH, "wb") as f:
        f.write(data)
    print("  ✓ soco.bin")
    n = write_netdata(NETDATA_PATH, acc['uid'], acc['userName'],
                      acc['passwd'], acc['nickName'])
    print(f"  ✓ NetData.bin ({n} 字节)")

    print("\n" + "=" * 70)
    print(f"  完成 uid={acc['uid']}  第 {args.chapter} 章")
    print("=" * 70)


if __name__ == "__main__":
    main()
