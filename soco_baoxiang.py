#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
soco_baoxiang.py — 周限宝箱(神秘宝箱 ShenMiBaoXiangData) 与 装备宝箱 开启次数 定位/修改

★ 两种宝箱
   游戏里有两类"周限宝箱"：
     · 卡片宝箱(神秘宝箱)  —— 对应 ShenMiBaoXiangData
     · 装备宝箱            —— 与卡片宝箱并列的另一类宝箱
   账号分两种：
     A) 只有卡片宝箱：存档里只有一个 QWERTY 计数串(已开启次数)。
     B) 卡片 + 装备 都有：存档里出现【两个】QWERTY 计数串。
        —— 经用户确认：双宝箱存档中，【第一个】QWERTY 串=卡片宝箱，
           【第二个】QWERTY 串=装备宝箱(旧工具只读了第二个，所以读成了装备宝箱次数)。

★ 编码(两类宝箱通用)
   剩余次数 = 5 - 已开启次数(opened)，【存档只存已开启次数】。
   已开启次数 = ShenMiBaoXiangData 对象内的一个 QWERTY 单字符串:
        Q=0  W=1  E=2  R=3  T=4  Y=5  (QWERTY 编码: Q0 W1 E2 R3 T4 Y5 U6 I7 O8 P9)
   该串位于对象体内(对象尾部簇附近，紧邻 "YQ"(=50) 哨兵前后)。
   注意：不同存档间绝对偏移会浮动，故用【结构锚点 + 取值规则】定位，不用固定偏移。

★ 定位规则(对本仓库三档存档均验证)
   在 ShenMiBaoXiangData 对象体内取所有 value∈[0,5] 的单字符 QWERTY 串:
     - 仅 1 个 -> 即卡片宝箱已开启次数(场景 A)。
     - ≥2 个  -> 第 1 个=卡片宝箱，第 2 个=装备宝箱(场景 B)。

★ 修改
   --card N  / --set-remaining N : 设【卡片宝箱】剩余 N(0..5)，改写第 1 个 QWERTY 串。
   --equip N                      : 设【装备宝箱】剩余 N(0..5)，改写第 2 个 QWERTY 串(仅双宝箱存档有效)。
   --scan                         : 诊断——列出全文件所有单字符 QWERTY 串及其所属类(用于定位装备宝箱)。
   --dry                          : 仅演示不写入。
   单字符改动，零位移，重算 CRC。
"""
import sys, zlib, argparse

QWERTY = "QWERTYUIOP"
CLASS = b"ShenMiBaoXiangData"
MAX_OPEN = 5  # 周限宝箱每周上限 5

def qe_dec(s):
    """QWERTY 串 -> 整数(按位十进制)。如 'QW'->1, 'R'->3。"""
    try:
        return sum(QWERTY.index(c) * (10 ** (len(s) - i - 1)) for i, c in enumerate(s))
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 底层定位
# ---------------------------------------------------------------------------
def find_class(data):
    """返回 ShenMiBaoXiangData 的 TC_OBJECT+TC_CLASSDESC 起点(73 72)，找不到返回 None。"""
    p = data.find(CLASS)
    if p < 0:
        return None
    q = data.rfind(b"\x73\x72", max(0, p - 30), p + 4)
    return q if q >= 0 else p

def object_end(data, start):
    """返回该顶层对象结束位置(下一个对象的 ff 73 72 分隔符；找不到则文件尾)。"""
    p = data.find(b"\xff\x73\x72", start + 1)
    return p if p >= 0 else len(data)

def collect_qwerty(data, a, b, maxval=MAX_OPEN):
    """在 [a,b) 内收集所有【单字符】 QWERTY TC_STRING(74 00 01 XX)，返回 [(off, s, val)]。"""
    out = []
    i = a
    n = min(b, len(data))
    while i < n - 3:
        if data[i] == 0x74 and data[i + 1] == 0x00 and data[i + 2] == 0x01:
            c = data[i + 3]
            if 32 <= c < 127 and chr(c) in QWERTY:
                s = chr(c)
                out.append((i + 3, s, qe_dec(s)))
                i += 4
                continue
        i += 1
    return out

def enclosing_class(data, pos):
    """返回 pos 之前最近的 TC_CLASSDESC 类名(用于 --scan 诊断)。"""
    best = None
    i = 0
    n = min(pos, len(data))
    while i < n - 4:
        if data[i] == 0x72 and data[i + 1] == 0x00 and 1 <= data[i + 2] <= 200:
            L = data[i + 2]
            nm = data[i + 3:i + 3 + L]
            if b"." in nm and all(32 <= x < 127 for x in nm):
                best = nm.decode("ascii", "replace")
                i += 3 + L
                continue
        i += 1
    return best or "?"

# ---------------------------------------------------------------------------
# 高级分析
# ---------------------------------------------------------------------------
def analyze(data):
    """返回两宝箱分析结果 dict。"""
    c = find_class(data)
    if c is None:
        return {"found": False, "reason": "未定位 ShenMiBaoXiangData"}
    end = object_end(data, c)
    allq = collect_qwerty(data, c, end, maxval=MAX_OPEN)
    # 候选：单字符、value∈[0,5] 的 QWERTY(排除 2 字符的 'YQ'=50 哨兵)
    cands = [(o, s, v) for (o, s, v) in allq if v is not None and 0 <= v <= MAX_OPEN]
    card = cands[0] if len(cands) >= 1 else None
    equip = cands[1] if len(cands) >= 2 else None
    return {
        "found": True,
        "class_off": c,
        "obj_end": end,
        "candidates": cands,
        "card": (card[0], card[2], MAX_OPEN - card[2]) if card else None,   # (off, opened, remaining)
        "equip": (equip[0], equip[2], MAX_OPEN - equip[2]) if equip else None,
        "mode": "dual" if equip else ("single" if card else "none"),
    }

def locate_opened(data):
    """向后兼容：返回 (off, cur_opened, tail) 即【卡片宝箱】信息(原工具语义)。"""
    r = analyze(data)
    if not r["found"] or not r["card"]:
        return None
    off, opened, _ = r["card"]
    # tail: 仅用于兼容旧输出，列出对象内候选
    tail = [(o, s, v) for (o, s, v) in r["candidates"]]
    return off, opened, tail

def crc_ok(path):
    d = open(path, "rb").read()
    return int.from_bytes(d[0:4], "big") == (zlib.crc32(d[4:]) & 0xFFFFFFFF)

# ---------------------------------------------------------------------------
# 修改
# ---------------------------------------------------------------------------
def set_remaining(data, which, new_remaining, dry=False):
    """which='card'|'equip'。改对应 QWERTY 串为 (5-new_remaining)。返回结果 dict。"""
    if which not in ("card", "equip"):
        raise ValueError("which must be 'card' or 'equip'")
    if not (0 <= new_remaining <= MAX_OPEN):
        raise ValueError(f"剩余次数必须 0..{MAX_OPEN}")
    r = analyze(data)
    if not r["found"]:
        raise ValueError("未定位 ShenMiBaoXiangData")
    slot = r[which]
    if slot is None:
        avail = "双宝箱" if r["mode"] == "dual" else "单宝箱(仅卡片)"
        raise ValueError(f"该存档为{avail}，无法定位【{'卡片' if which=='card' else '装备'}宝箱】计数串")
    off, cur_opened, cur_remain = slot
    target_opened = MAX_OPEN - new_remaining
    ch = QWERTY[target_opened]
    old_ch = data[off]
    if old_ch == ord(ch):
        return {"changed": False, "which": which, "off": off,
                "old": chr(old_ch), "new": ch, "remaining": cur_remain,
                "msg": f"已为剩余 {new_remaining} (已开启 {target_opened})，无需改动"}
    if dry:
        return {"changed": True, "dry": True, "which": which, "off": off,
                "old": chr(old_ch), "new": ch, "remaining_from": cur_remain, "remaining_to": new_remaining}
    data[off] = ord(ch)
    old_crc = int.from_bytes(bytes(data[0:4]), "big")
    new_crc = zlib.crc32(bytes(data[4:])) & 0xFFFFFFFF
    data[0:4] = new_crc.to_bytes(4, "big")
    return {"changed": True, "which": which, "off": off, "old": chr(old_ch),
            "new": ch, "remaining_from": cur_remain, "remaining_to": new_remaining,
            "crc_from": old_crc, "crc_to": new_crc}

def plan_chest_patch(data, new_remaining, which="card"):
    """供 soco_write.py 调用：返回 (kind,label,new_remaining,off,width,new_bytes,cur_remaining)。"""
    r = analyze(data)
    if not r["found"]:
        raise ValueError("未定位 ShenMiBaoXiangData")
    slot = r[which]
    if slot is None:
        avail = "双宝箱" if r["mode"] == "dual" else "单宝箱(仅卡片)"
        raise ValueError(f"该存档为{avail}，无法定位【{'卡片' if which=='card' else '装备'}宝箱】计数串")
    off, cur_opened, cur_remain = slot
    target_opened = MAX_OPEN - new_remaining
    ch = QWERTY[target_opened]
    label = "卡片宝箱" if which == "card" else "装备宝箱"
    return ("chest", label, new_remaining, off, 1, ch.encode("ascii"), cur_remain)

def plan_chest_patch_idx(data, new_remaining, idx):
    """按候选序号 idx(0 起, 支持负数如 -1=最后一个) 生成 patch。
    供 editor 定位 卡片宝箱(cands[1]) / 装备宝箱(cands[-1])。
    返回 (kind, label, new_remaining, off, width, new_bytes, cur_remain)。"""
    if not (0 <= new_remaining <= MAX_OPEN):
        raise ValueError(f"剩余次数必须 0..{MAX_OPEN}")
    r = analyze(data)
    if not r["found"]:
        raise ValueError("未定位 ShenMiBaoXiangData")
    cands = r["candidates"]
    n = len(cands)
    if n == 0:
        raise ValueError("未定位到任何宝箱计数串")
    if not (-n <= idx < n):
        raise ValueError(f"候选序号 {idx} 越界 (共 {n} 个)")
    off, s, v = cands[idx]
    target_opened = MAX_OPEN - new_remaining
    ch = QWERTY[target_opened]
    ridx = idx % n  # 负索引换算为正索引
    if ridx == 1:
        label = "卡片宝箱"
    elif ridx == n - 1:
        label = "装备宝箱"
    else:
        label = f"宝箱{idx}"
    return ("chest", label, new_remaining, off, 1, ch.encode("ascii"), MAX_OPEN - v)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def scan_all(data):
    """诊断：列出全文件所有单字符 QWERTY 串及其所属类。"""
    out = []
    i = 0
    n = len(data)
    while i < n - 3:
        if data[i] == 0x74 and data[i + 1] == 0x00 and data[i + 2] == 0x01:
            c = data[i + 3]
            if 32 <= c < 127 and chr(c) in QWERTY:
                s = chr(c)
                out.append((i + 3, s, qe_dec(s), enclosing_class(data, i + 3)))
                i += 4
                continue
        i += 1
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="soco.bin 路径")
    ap.add_argument("--set-remaining", type=int, default=None, help="设【卡片宝箱】剩余 0..5 (兼容别名)")
    ap.add_argument("--card", type=int, default=None, help="设【卡片宝箱】剩余 0..5")
    ap.add_argument("--equip", type=int, default=None, help="设【装备宝箱】剩余 0..5 (仅双宝箱存档)")
    ap.add_argument("--scan", action="store_true", help="诊断：列出全文件所有单字符 QWERTY 串")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    data = bytearray(open(args.path, "rb").read())

    if args.scan:
        print(f"[scan] 文件: {args.path}  全文件单字符 QWERTY 串:")
        for off, s, v, cls in scan_all(data):
            print(f"       0x{off:X}  '{s}' = {v}   类: {cls}")
        return

    r = analyze(data)
    if not r["found"]:
        print(f"[ERROR] {r.get('reason')}")
        sys.exit(1)
    print(f"[info] 文件: {args.path}")
    print(f"[info] ShenMiBaoXiangData 对象: 0x{r['class_off']:X}..0x{r['obj_end']:X}  模式: {r['mode']}")
    print(f"[info] 候选 QWERTY 计数串(value 0..5):")
    for o, s, v in r["candidates"]:
        print(f"       0x{o:X}  '{s}' = {v}")
    if r["card"]:
        print(f"[result] 卡片宝箱: 已开启={r['card'][1]}  剩余={r['card'][2]}")
    else:
        print("[result] 卡片宝箱: 未定位")
    if r["equip"]:
        print(f"[result] 装备宝箱: 已开启={r['equip'][1]}  剩余={r['equip'][2]}")
    else:
        print("[result] 装备宝箱: 未定位 (本存档为单宝箱 / 装备宝箱非 QWERTY 编码)")

    # 应用修改：先全部在内存中改完，再统一落盘(支持 --card 与 --equip 同时改)
    any_change = False
    for which, val in (("card", args.card if args.card is not None else args.set_remaining),
                       ("equip", args.equip)):
        if val is None:
            continue
        try:
            res = set_remaining(data, which, val, dry=args.dry)
        except ValueError as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
        if not res["changed"]:
            print(f"[info] {which}: {res['msg']}")
            continue
        if args.dry:
            print(f"[dry] {which}: 0x{res['off']:X} {res['old']!r} -> {res['new']!r}  "
                  f"剩余 {res['remaining_from']} -> {res['remaining_to']}")
            continue
        any_change = True
        print(f"[stage] {which}: 0x{res['off']:X} {res['old']!r} -> {res['new']!r}  "
              f"剩余 {res['remaining_from']} -> {res['remaining_to']}  "
              f"CRC {res['crc_from']:08x}->{res['crc_to']:08x}")
    if any_change and not args.dry:
        open(args.path, "wb").write(data)
        assert crc_ok(args.path), "CRC 自洽失败"
        print(f"[write] 已写入 {args.path} 并校验 CRC 通过。")

if __name__ == "__main__":
    main()
