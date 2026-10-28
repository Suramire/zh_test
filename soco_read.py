#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Merge to Summon (com.zhaohe.game522) 存档读取工具

读取 soco.bin，展示：
  - 玩家昵称（playerName）
  - 6 个数值：金币 / 钻石 / 木头 / 石头 / 结晶 / 钥匙数量
  - 上阵 3 张卡片：familyid / 名称 / kind / 等级 / 卡片数 / 进阶等级 / 属性[9] / 技能1~3 等级

★ 格式要点
  该游戏 TC_STRING 格式 = \x74 \x00 [1字节长度L] [L字节数据]
  （不是标准 Java 序列化的 2 字节大端长度）
  数值用 QWERTY 编码: Q=0 W=1 E=2 R=3 T=4 Y=5 U=6 I=7 O=8 P=9

★ 定位策略（顺序/结构，不依赖具体数值，改档后也能正确回读）
  金币 : 头部区域(0x0~0x200)内【第 1 个】QWERTY TC_STRING
  钻石 : 资源数组 ziyuan 的第 8 个元素（GameData.addziyuan: id==8→钻石, id==0→金币=ziyuan[0]），
         ziyuan 为 [Ljava.lang.String;（QWERTY 编码串数组）。全新存档中钻石是【共享串引用
         TC_REFERENCE 指向 ziyuan[1] 的"0"】，必须把该引用【原位替换】为内联 TC_STRING，
         否则木头/石头等所有"0"资源会被污染。详见 find_diamond（已废弃旧"引用块"启发式）。
  ★ 金币无缩放: 存储值 == 游戏显示值，游戏仅按 万/亿/万亿 单位美化。
    输出中「游戏显示」列即 fmt_cn(存储值)，可直接与游戏内数字对照（避免 900亿↔9000万 误读）。
  木头/石头/结晶 : CastleResource 对象（com.zhaohe.game.castle），序列化只写
                   `long r`(8字节) + `int resourceID`(4字节)，resourceID 分别
                   为 3101/3102/3103。扫描 resourceID，读其前 8 字节为真实值。
  钥匙数量       : 同为 CastleResource，resourceID = 3992，读其前 8 字节 `long r`。
                   （数值字段 resourceAmount 为 transient，不入库，改 r 安全）

★ sidecar: soco_write.py 写入后生成 soco.bin.map.json，记录各字段最终偏移与文件 CRC。
  本工具优先采用并校验 CRC 一致，避免游戏重写存档后偏移漂移误读。

用法:
  python3 soco_read.py                  # 表格展示（含 6 数值 + 上阵卡片）
  python3 soco_read.py --json           # JSON 输出（含 cards 字段）
  python3 soco_read.py --detail         # 含偏移/编码详情
  python3 soco_read.py --file X.bin     # 读取指定文件（及同名 .map.json）
"""

import os
import sys
import json
import re
import struct
import zlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOCO_PATH = os.path.join(BASE_DIR, "soco.bin")

QWERTY = "QWERTYUIOP"
MAX_INT32 = 2147483647
SIDECAR_SUFFIX = ".map.json"

# GameData.ziyuan[27]。名称只写入已从当前 core/core2 调用点确认的项目；
# 其余索引保留为“资源ID”，避免把活动货币误标成常驻资源。
ZIYUAN_NAMES = {
    0: "金币",
    1: "后勤资源",
    8: "钻石",
    13: "充值钻石",
    14: "加速券",
    15: "商店积分",
    20: "英雄装备精华",
    21: "伙伴装备精华",
    23: "活动货币23",
    24: "彩蛋币",
    25: "活动货币25",
    26: "粽子兑换币",
}

# CastleData.resources 中已确认的常用资源。其余 ID 仍完整显示为“城堡资源<ID>”，
# 不凭数值猜测名称，避免把活动道具误标成常驻材料。
CASTLE_RESOURCE_NAMES = {
    3053: "充值积分(积分兑换商城)",
    3101: "木头",
    3102: "石头",
    3103: "结晶",
    3153: "爬塔沙漏",
    3992: "钥匙数量",
}

CASTLE_FIELD_NAMES = {
    'EQUIPMENT_ID': '下一伙伴装备ID',
    'GUARDIAN_ID': '下一伙伴ID',
    'f': '城堡短整数字段f',
    'guaranteeTime': '酒馆保底次数',
    'guardianTeamIndex': '伙伴队伍索引',
    'jingying_kaiqi': '远征解锁进度',
}


# ─────────────────────────────────────
#  编码工具
# ─────────────────────────────────────
def qe_dec(raw):
    """QWERTY 编码字节 -> 整数 (如 b'YO' -> 58)"""
    try:
        s = raw.decode('ascii') if isinstance(raw, bytes) else str(raw)
        return sum(QWERTY.index(ch) * (10 ** (len(s) - i - 1)) for i, ch in enumerate(s))
    except Exception:
        return None


def fmt_cn(v):
    """整数 -> 游戏显示风格中文单位串（万/亿/万亿），便于与游戏内数值对照。
    金币等数值【无缩放】：显示值 == 存储值，游戏只按 万(1e4)/亿(1e8)/万亿(1e12)
    做单位美化。如 90000000000 -> '900亿'，9000000000000 -> '9万亿'。"""
    if v is None:
        return "-"
    n = int(v)
    if n < 0:
        return str(n)
    for unit, label in ((10 ** 12, "万亿"), (10 ** 8, "亿"), (10 ** 4, "万")):
        if n >= unit:
            q = n / unit
            s = f"{q:.4f}".rstrip("0").rstrip(".")   # 去尾零
            return f"{s}{label}"
    return str(n)


def parse_tc(data, off):
    """解析 TC_STRING，返回 (长度, 原始值字节)，否则 (None, None)"""
    if off < 0 or off + 3 >= len(data):
        return None, None
    if data[off] != 0x74 or data[off + 1] != 0x00:
        return None, None
    L = data[off + 2]
    if L == 0 or off + 3 + L > len(data):
        return None, None
    return L, bytes(data[off + 3: off + 3 + L])


def scan_tc(data):
    """扫描全部 QWERTY TC_STRING，返回 [(off, L, raw, val), ...]"""
    out = []
    i = 0
    while i < len(data) - 3:
        if data[i] == 0x74 and data[i + 1] == 0x00 and 1 <= data[i + 2] <= 40:
            L = data[i + 2]
            raw = bytes(data[i + 3: i + 3 + L])
            if all(c in QWERTY.encode() for c in raw):
                out.append((i, L, raw, qe_dec(raw)))
                i += 3 + L
            else:
                i += 1
        else:
            i += 1
    return out


# ─────────────────────────────────────
#  定位器（按结构/顺序，不依赖具体数值）
# ─────────────────────────────────────
def find_gold(data, tcs):
    """金币: 头部区域(0x0~0x200)内第 1 个 QWERTY TC_STRING"""
    cands = sorted(t for t in tcs if t[0] < 0x200)
    if not cands:
        return None
    off, L, raw, val = cands[0]
    return dict(name="金币", off=off + 3, val=val, raw=raw, enc=raw.decode('ascii', 'replace'))


def find_ziyuan_elems(data):
    """定位 ziyuan（资源 String[] 数组，QWERTY 编码），返回元素列表
    [(kind, val, tc_off), ...]，kind ∈ {'str','ref','null'}。

    ★ 模型（据 GameData.addziyuan: id==0→金币, id==8→钻石）：
        ziyuan 是 [Ljava.lang.String; 数组，元素逐个序列化。第一个"0"串(元素[1])
        被赋 handle，其后所有"0"资源(含钻石在全新存档时)都是 TC_REFERENCE 引用它。
        - str  : 内联 TC_STRING，val=解码值，tc_off=0x74 起点。
        - ref  : TC_REFERENCE(0x71+4B handle)，val=解引用的共享串值，tc_off=0x71 位置。
        - null : TC_NULL(0x70)，val=None，tc_off=0x70 位置。
    用结构法(数组首个 [Ljava.lang.String; classdesc)定位，与账号/头部串数量无关。"""
    KEY = b'\x72\x00\x13\x5b\x4c\x6a\x61\x76\x61\x2e\x6c\x61\x6e\x67\x2e\x53\x74\x72\x69\x6e\x67\x3b'
    i = data.find(KEY)
    if i < 0:
        return None
    arr_start = i - 1
    if arr_start < 0 or data[arr_start] != 0x75:
        return None
    n = len(data)
    p = arr_start + 1 + len(KEY)     # 越过 classdesc 名
    p += 8                            # suid
    p += 1                            # flags
    fc = int.from_bytes(data[p:p + 2], 'big'); p += 2
    if fc != 0:
        return None
    if p < n and data[p] == 0x78: p += 1
    if p < n and data[p] == 0x70: p += 1
    if p + 4 > n:
        return None
    length = int.from_bytes(data[p:p + 4], 'big'); p += 4
    elems = []
    for _ in range(length):
        if p >= n:
            break
        b = data[p]
        if b == 0x74:                 # 内联 TC_STRING
            L = data[p + 2]
            if p + 3 + L > n:
                elems.append(('?', None, p)); break
            raw = bytes(data[p + 3:p + 3 + L])
            s = raw.decode('ascii', 'replace')
            elems.append(('str', s, p)); p += 3 + L
        elif b == 0x71:               # TC_REFERENCE
            h = int.from_bytes(data[p + 1:p + 5], 'big')
            elems.append(('ref', h, p)); p += 5
        elif b == 0x70:               # TC_NULL
            elems.append(('null', None, p)); p += 1
        else:
            elems.append(('?', None, p)); break
    return elems


def ziyuan_resolved(data):
    """走 ziyuan 数组，按 Java OIS 句柄规则解析每个元素为真实值。
    返回 [(kind, value), ...]；ref 元素会被解引用为它所指向的内联串的真实值
    （支持钻石重定向到金币内联串的情形）。
    句柄分配: classdesc->#0, 数组实例->#1, 之后每个内联串依次+1, ref/null 不占。"""
    KEY = b'\x72\x00\x13\x5b\x4c\x6a\x61\x76\x61\x2e\x6c\x61\x6e\x67\x2e\x53\x74\x72\x69\x6e\x67\x3b'
    i = data.find(KEY)
    if i < 0:
        return None
    arr_start = i - 1
    if arr_start < 0 or data[arr_start] != 0x75:
        return None
    p = arr_start + 1 + len(KEY)
    p += 8
    p += 1
    fc = int.from_bytes(data[p:p + 2], 'big'); p += 2
    if fc != 0:
        return None
    if p < len(data) and data[p] == 0x78: p += 1
    if p < len(data) and data[p] == 0x70: p += 1
    if p + 4 > len(data):
        return None
    length = int.from_bytes(data[p:p + 4], 'big')
    p += 4
    n = len(data)
    handle_map = {}
    counter = 2
    out = []
    while p < n and len(out) < length:
        b = data[p]
        if b == 0x74:
            L = data[p + 2]
            if p + 3 + L > n:
                out.append(('?', None)); break
            raw = bytes(data[p + 3:p + 3 + L])
            s = raw.decode('ascii', 'replace')
            handle_map[counter] = s
            counter += 1
            out.append(('str', s)); p += 3 + L
        elif b == 0x71:
            h = int.from_bytes(data[p + 1:p + 5], 'big') - 0x7E0000
            out.append(('ref', handle_map.get(h))); p += 5
        elif b == 0x70:
            out.append(('null', None)); p += 1
        else:
            out.append(('?', None)); break
    return out


def read_ziyuan_details(data):
    """完整读取 GameData.ziyuan[]。

    返回每个元素的索引、名称、真实数值、存储形态、标签偏移和编码文本。
    TC_REFERENCE 会被解析为目标字符串的值，但保留原引用 handle，便于判断是否可安全写入。
    """
    elems = find_ziyuan_elems(data) or []
    resolved = ziyuan_resolved(data) or []
    rows = []
    for idx, (kind, raw_or_handle, tc_off) in enumerate(elems):
        encoded = None
        handle = None
        if kind == 'str':
            encoded = raw_or_handle
        elif kind == 'ref':
            handle = raw_or_handle
            if idx < len(resolved):
                encoded = resolved[idx][1]
        value = qe_dec(encoded.encode('ascii', 'replace')) if encoded else 0 if kind in ('ref', 'null') else None
        rows.append({
            'id': idx,
            'name': ZIYUAN_NAMES.get(idx, '资源ID%d' % idx),
            'value': value,
            'kind': kind,
            'tc_off': tc_off,
            'value_off': tc_off + 3 if kind == 'str' else tc_off,
            'encoded': encoded,
            'handle': handle,
        })
    return rows


def find_diamond(data, tcs):
    """钻石 = ziyuan[8]（GameData.addziyuan: id==8 → 钻石；id==0 → 金币=ziyuan[0]）。

    ★ 旧"金币→引用块→钻石"启发式已废弃：全新存档里金币之后是一长串共享空引用块，
      该启发式会越过它误命中 GameGuaiWu 第 0 家族的等级字段(index[0])，导致改钻石
      实际改了等级（表现为"等级变 10万"）。
    ★ 现改为结构法：定位 ziyuan String[] 数组(首个 [Ljava.lang.String; classdesc)，
      取第 8 个元素。该元素可能是：
        - str  : 内联 TC_STRING（老存档/钻石非0时），与原逻辑兼容。
        - ref  : 该槽位是【共享串引用】(指向 ziyuan[1] 的"0")，返回 kind='ref'，
                 写入时需把 0x71 引用原位替换成内联 TC_STRING，避免改共享串污染其它资源。
        - null : 空槽，返回 kind='null'，写入时原位插入内联 TC_STRING。
      gold 用 find_gold(=ziyuan[0]) 做交叉校验，确保选中的数组正确。"""
    gold = find_gold(data, tcs)
    if gold is None:
        return None
    KEY = b'\x72\x00\x13\x5b\x4c\x6a\x61\x76\x61\x2e\x6c\x61\x6e\x67\x2e\x53\x74\x72\x69\x6e\x67\x3b'
    i = data.find(KEY)
    while i >= 0:
        arr_start = i - 1
        if arr_start >= 0 and data[arr_start] == 0x75:
            elems = find_ziyuan_elems(data)
            if elems and len(elems) > 8:
                # 交叉校验：元素[0] 应即金币(值匹配或偏移匹配)
                e0 = elems[0]
                if (e0[0] == 'str' and qe_dec(e0[1].encode('ascii', 'replace')) == gold['val']) or \
                   (e0[0] == 'str' and (e0[2] + 3) == gold['off']):
                    # 结构信息(off/kind)来自 find_ziyuan_elems；真实值来自 ziyuan_resolved
                    # （按句柄规则解引用，支持钻石重定向到金币内联串的情形）。
                    rz = ziyuan_resolved(data)
                    r = rz[8] if rz else ('?', None)
                    e = elems[8]
                    if e[0] == 'str':
                        off = e[2]
                        L = data[off + 2]
                        raw = bytes(data[off + 3:off + 3 + L])
                        return dict(name="钻石", off=off + 3, val=qe_dec(raw), raw=raw,
                                    enc=raw.decode('ascii', 'replace'), kind='inline')
                    if e[0] == 'ref':
                        val = qe_dec(r[1].encode('ascii', 'replace')) if (r and r[0] == 'ref' and r[1]) else None
                        return dict(name="钻石", off=e[2], val=val, raw=None,
                                    enc='(ref→%s)' % (r[1]), kind='ref')
                    if e[0] == 'null':
                        return dict(name="钻石", off=e[2], val=0, raw=None,
                                    enc='(null)', kind='null')
                    return None
        i = data.find(KEY, i + 1)
    return None


def ziyuan_gold_handle(data):
    """返回 ziyuan 数组里金币(ziyuan[0]) 的 OIS handle 索引，用于钻石引用重定向。

    按 Java ObjectInputStream 句柄分配规则：
      - ziyuan 数组 classdesc([Ljava.lang.String;) -> #0
      - ziyuan 数组实例(TC_ARRAY)                   -> #1
      - 之后每个【内联串】依次 +1；引用(TC_REFERENCE)/空(TC_NULL) 不占句柄。
    金币恒为 ziyuan[0] 内联串 -> handle #2；"0"共享串(ziyuan[1]) -> #3。
    该函数只走 ziyuan 数组前部(前 9 个元素都是简单串/引用)，结构稳定可靠。"""
    KEY = b'\x72\x00\x13\x5b\x4c\x6a\x61\x76\x61\x2e\x6c\x61\x6e\x67\x2e\x53\x74\x72\x69\x6e\x67\x3b'
    i = data.find(KEY)
    if i < 0:
        return None
    arr_start = i - 1
    if arr_start < 0 or data[arr_start] != 0x75:
        return None
    p = arr_start + 1 + len(KEY)     # 越过 classdesc 名
    p += 8                            # suid
    p += 1                            # flags
    fc = int.from_bytes(data[p:p + 2], 'big'); p += 2
    if fc != 0:
        return None
    if p < len(data) and data[p] == 0x78: p += 1
    if p < len(data) and data[p] == 0x70: p += 1
    if p + 4 > len(data):
        return None
    p += 4                           # length
    counter = 2                      # #0 classdesc, #1 数组实例；内联串从 #2 起
    idx = 0
    n = len(data)
    while idx <= 8 and p < n:
        b = data[p]
        if b == 0x74:                # 内联 TC_STRING -> 占一个句柄
            if idx == 0:
                return counter       # 金币 = ziyuan[0] 内联 -> handle #2
            counter += 1
            p += 3 + data[p + 2]
        elif b == 0x71:              # TC_REFERENCE -> 不占句柄
            if idx == 0:
                return None          # 金币竟是引用(异常)，无法重定向
            p += 5
        elif b == 0x70:              # TC_NULL -> 不占句柄
            if idx == 0:
                return None
            p += 1
        else:
            return None
        idx += 1
    return None


def find_resource(data, rid, name, vmin=0, vmax=10_000_000_000_000):
    """定位 CastleResource: 扫描 `int resourceID`(4字节)，其紧前 8 字节即 `long r`(真实值)。
    同一 resourceID 可能在存档多处出现（如 handle/其他数据），用合理数值范围
    [vmin, vmax] 排除幽灵候选（材料~2.1e9、钥匙~1e5，远大于垃圾 3.2e17）。"""
    b = struct.pack('>i', rid)
    N = len(data)
    best = None
    pos = 0
    while True:
        pos = data.find(b, pos)
        if pos < 0 or pos + 4 > N or pos - 8 < 0:
            break
        r = struct.unpack('>q', data[pos - 8:pos])[0]
        if vmin <= r <= vmax:
            best = (pos - 8, r)          # (r 起点偏移, 值)
        pos += 1
    if best is None:
        return None
    off, val = best
    return dict(name=name, off=off, val=val, raw=bytes(data[off:off + 8]),
                enc="(long)%d" % val)


def find_materials(data):
    """木头/石头/结晶: 扫描 CastleResource 的 resourceID 3101/3102/3103。"""
    return [
        find_resource(data, 3101, "木头"),
        find_resource(data, 3102, "石头"),
        find_resource(data, 3103, "结晶"),
    ]


def find_keys(data):
    """钥匙数量: CastleResource resourceID=3992，读其 8 字节 long r。"""
    return find_resource(data, 3992, "钥匙数量")


def find_isShow(data):
    """定位 isShow[] boolean 数组，返回 (data_offset, raw_bytes) 或 None。

    搜索模式：
    1. 先找到第一个 [Z 数组（isShowziyuan, len=9），TC_ARRAY+完整 classdesc
    2. 再读紧跟的 [Z 数组（isShow）。旧版长度28，当前版本长度29。
    """
    n = len(data)
    # Step 1: find the first boolean[9] array (isShowziyuan) with full classdesc
    # Pattern: 75 72 00 02 5B 5A = TC_ARRAY + TC_CLASSDESC(len=2) + "[Z"
    marker = b'\x75\x72\x00\x02\x5b\x5a'
    pos = data.find(marker)
    if pos < 0:
        return None
    # Parse past classdesc to get length
    p = pos + len(marker) + 8  # +8 for suid
    p += 1                      # flags
    fc = struct.unpack('>H', data[p:p+2])[0]
    p += 2
    if p < n and data[p] == 0x78: p += 1  # TC_ENDBLOCKDATA
    if p < n and data[p] == 0x70: p += 1  # TC_NULL super
    if p + 4 > n:
        return None
    length = struct.unpack('>I', data[p:p+4])[0]
    if length != 9:
        return None  # not isShowziyuan
    p += 4 + length  # skip values
    # Step 2: next should be isShow (len=28) as TC_ARRAY+TC_REFERENCE
    if p + 6 >= n or data[p] != 0x75 or data[p+1] != 0x71:
        return None
    p += 6  # skip TC_ARRAY + TC_REF + handle(4)
    if p + 4 > n:
        return None
    length2 = struct.unpack('>I', data[p:p+4])[0]
    p += 4
    if not (28 <= length2 <= 64):
        return None
    if p + length2 > n:
        return None
    values = bytes(data[p:p + length2])
    if any(v not in (0, 1) for v in values):
        return None
    return (p, values)


def read_equipment_summary(data):
    """读取 CastleData/英雄 GameItem 的数量摘要；失败时返回 error 字段。"""
    try:
        import soco_hero_equipment as H
        buf = bytearray(data)
        parser, castle = H.find_castle_data(buf, 8)
        if castle is None:
            return {'error': '未找到 CastleData'}
        castle_end = parser.p
        castle_marker = b'com.zhaohe.game.castle.CastleData'
        castle_offset = data.find(castle_marker)
        eqmap = (castle.get('equipments') or {}).get('__map__', {})
        partner = sum(1 for value in eqmap.values()
                      if isinstance(value, dict) and '__class__' in value)
        tokens = H.tokenize_region(parser, len(buf))
        chains = H.decode_gameitem_chains(tokens)
        suits = H.decode_suits(tokens)
        H.assign_slots(chains, suits)
        records = [record for chain in chains for record in chain]
        hero = [record for record in records if record.get('kind') in H.HERO_EQUIP_RANGE]
        return {
            'castle_offset': castle_offset if castle_offset >= 0 else None,
            'castle_end': castle_end,
            'partner_equipment_count': partner,
            'gameitem_chain_lengths': [len(chain) for chain in chains],
            'gameitem_count': len(records),
            'hero_equipment_count': len(hero),
            'equipment_suit_count': len(suits.get('presets') or []),
        }
    except Exception as exc:
        return {'error': str(exc)}


def _unbox_java_number(value):
    """将 JavaParser 产生的 Integer/CryptoNumber 包装层还原为 Python 数值。"""
    seen = set()
    while isinstance(value, dict) and 'value' in value and id(value) not in seen:
        seen.add(id(value))
        value = value.get('value')
    return value


def _crypto_value_and_offset(value):
    """读取 CastleData 中 CryptoNumber.value 及其固定宽度原始偏移。"""
    if not isinstance(value, dict):
        return None, None
    wrapped = value.get('value')
    if isinstance(wrapped, dict):
        offsets = wrapped.get('__offsets__') or {}
        off_info = offsets.get('value')
        return _unbox_java_number(wrapped), off_info
    return _unbox_java_number(wrapped), None


def read_castle_overview(data):
    """完整读取 CastleData 的可稳定定位数据。

    返回城堡基础字段、建筑等级、全部 CastleResource、伙伴/装备数量及各字段偏移。
    偏移来自标准 Java 序列化类字段，不依赖资源数值启发式扫描。
    """
    try:
        import soco_hero_equipment as H
        parser, castle = H.find_castle_data(bytearray(data), 8)
        if castle is None:
            return {'error': '未找到 CastleData', 'fields': [], 'buildings': [], 'resources': []}

        fields = []
        for key, off_info in (castle.get('__offsets__') or {}).items():
            if not off_info or len(off_info) < 3:
                continue
            off, type_code, width = off_info
            fields.append({
                'key': key,
                'name': CASTLE_FIELD_NAMES.get(key, key),
                'value': castle.get(key),
                'offset': off,
                'type': type_code,
                'width': width,
                # 自增 ID 改错会制造主键冲突，只展示；其余固定宽度字段可编辑。
                'editable': key not in ('EQUIPMENT_ID', 'GUARDIAN_ID'),
            })

        buildings = []
        building_map = ((castle.get('buildings') or {}).get('__map__') or {})
        for map_key, building in building_map.items():
            if not isinstance(building, dict):
                continue
            level, level_off = _crypto_value_and_offset(building.get('building_level'))
            class_name = building.get('__class__') or ''
            buildings.append({
                'map_key': map_key if isinstance(map_key, int) else None,
                'building_type': building.get('buildingType'),
                'class': class_name,
                'name': class_name.rsplit('.', 1)[-1] if class_name else 'Unknown',
                'level': level,
                'level_offset': level_off[0] if level_off else None,
                'level_type': level_off[1] if level_off else None,
                'level_width': level_off[2] if level_off else None,
                # CryptoNumber<Integer>.value 可能是全流共享的 boxed Integer；直接改其
                # 对象体会连带所有引用者。当前只展示，待做引用计数后再开放单项写入。
                'editable': False,
            })
        level_offset_counts = {}
        for row in buildings:
            if row['level_offset'] is not None:
                level_offset_counts[row['level_offset']] = level_offset_counts.get(row['level_offset'], 0) + 1
        for row in buildings:
            row['shared_offset'] = bool(row['level_offset'] is not None and
                                        level_offset_counts.get(row['level_offset'], 0) > 1)
        buildings.sort(key=lambda row: (row['building_type'] is None,
                                        row['building_type'] if row['building_type'] is not None else 9999,
                                        row['name']))

        resources = []
        resource_map = ((castle.get('resources') or {}).get('__map__') or {})
        for resource_id, resource in resource_map.items():
            if not isinstance(resource_id, int) or not isinstance(resource, dict):
                continue
            off_info = (resource.get('__offsets__') or {}).get('r')
            off = off_info[0] if off_info else None
            width = off_info[2] if off_info else 8
            # ★ 解析器在部分存档版本上不给 __offsets__（新版 CastleResource 布局变了），
            #   这时用通用扫描兜底：找 `int resourceID`(4字节)，其紧前 8 字节即 `long r`。
            #   否则该资源会被判为"未定位"而无法编辑（如 3053 充值积分）。
            if off is None:
                hit = find_resource(data, resource_id,
                                   CASTLE_RESOURCE_NAMES.get(resource_id, ''),
                                   vmin=-1, vmax=10_000_000_000_000)
                if hit:
                    off = hit['off']
                    width = 8
            resources.append({
                'id': resource_id,
                'name': CASTLE_RESOURCE_NAMES.get(resource_id, '城堡资源%d' % resource_id),
                'value': resource.get('r'),
                'offset': off,
                'type': (off_info[1] if off_info else 'J'),
                'width': width,
                'editable': off is not None,
            })
        resources.sort(key=lambda row: row['id'])

        return {
            'offset': data.find(b'com.zhaohe.game.castle.CastleData'),
            'end': parser.p,
            'fields': fields,
            'buildings': buildings,
            'resources': resources,
            'resource_count': len(resources),
            'guardian_count': len(((castle.get('guardians') or {}).get('__map__') or {})),
            'partner_equipment_count': len(((castle.get('equipments') or {}).get('__map__') or {})),
            'guardian_team_count': len(((castle.get('guardianTeams') or {}).get('__map__') or {})),
        }
    except Exception as exc:
        return {'error': str(exc), 'fields': [], 'buildings': [], 'resources': []}


def analyze_save(data, path=None, include_equipment=True):
    """生成可 JSON 序列化的完整存档概览。"""
    stored_crc = int.from_bytes(data[:4], 'big') if len(data) >= 4 else None
    calculated_crc = zlib.crc32(data[4:]) & 0xffffffff if len(data) >= 4 else None
    tower = find_tower_data(data)
    cards = read_cards(data)
    show = find_isShow(data)
    unlock = find_player_jiesuo(data)
    result = {
        'path': path,
        'size': len(data),
        'stored_crc32': '%08x' % stored_crc if stored_crc is not None else None,
        'calculated_crc32': '%08x' % calculated_crc if calculated_crc is not None else None,
        'crc_valid': stored_crc == calculated_crc,
        'stream_magic': data[4:8].hex() if len(data) >= 8 else None,
        'java_stream_valid': data[4:8] == b'\xac\xed\x00\x05',
        'save_format': detect_save_format(data),
        'player_name': (find_playername(data) or {}).get('val'),
        'resources': read_ziyuan_details(data),
        'isShow': {
            'offset': show[0] if show else None,
            'count': len(show[1]) if show else 0,
            'values': list(show[1]) if show else [],
        },
        'player_jiesuo': {
            'offset': unlock[0] if unlock else None,
            'count': len(unlock[1]) if unlock else 0,
            'values': list(unlock[1]) if unlock else [],
        },
        'tower': tower,
        'cards': cards,
        'return_player': locate_returnplayer(data),
        'monster_families': read_all_guaiwu(data),
        'castle': read_castle_overview(data),
    }
    if include_equipment:
        result['equipment'] = read_equipment_summary(data)
    return result


def find_player_jiesuo(data):
    """定位 GameData.player_jiesuo boolean[24] 数组。

    GameData 序列化中有一个 boolean[24] 字段 (player_jiesuo)，
    在序列化中以 TC_ARRAY(0x75) + TC_REFERENCE(0x71) to [Z + size(0x18) + 24bytes 形式出现。
    共有 2 个这样的数组：player_jiesuo（源）、tempplayer_jiesuo（临时副本）。
    返回第一个的 (offset, raw_24bytes) 或 None。
    """
    for i in range(len(data)):
        if i + 10 < len(data) and data[i] == 0x75 and data[i+1] == 0x71:
            sz = struct.unpack('>I', data[i+6:i+10])[0]
            if sz == 24:
                vals = data[i+10:i+34]
                if len(vals) == 24 and all(b in (0, 1) for b in vals):
                    return (i + 10, bytes(vals))
    return None


def find_playername(data):
    """玩家昵称 GameData.playerName（结构法，与 soco_write.locate_playername 同判据）。

    依据反编译 GameData.java 读序列: ftime=readLong(); playerName=(String)readObject(); ...
    即 playerName 是紧接 8 字节 ftime(long, 毫秒时间戳) 之后的 TC_STRING(74 00 LL ...)，
    且其后字节为 71(下一个串被压成引用) 或 74(独立串)。用"前 8 字节是合理时间戳
    (2010~2071)"过滤误匹配，真实存档应唯一命中。
    返回 dict(name, off, val, raw, enc) 或 None。"""
    TS_MIN = 1_200_000_000_000    # 2010 年（毫秒）
    TS_MAX = 3_500_000_000_000    # 2071 年（毫秒）
    n = len(data)
    cands = []
    i = 0
    while True:
        i = data.find(b'\x74\x00', i)
        if i < 0:
            break
        L = data[i + 2]
        s_end = i + 3 + L
        if s_end <= n:
            pre = data[i - 8:i] if i >= 8 else b''
            post = data[s_end] if s_end < n else 0
            if len(pre) == 8 and pre[0] == 0:
                pre_val = int.from_bytes(pre, 'big')
                if TS_MIN <= pre_val <= TS_MAX and post in (0x71, 0x74):
                    cands.append((i, L))
        i += 1
    if not cands:
        return None
    tc_off, L = cands[0]          # 唯一命中（多命中时取首个，避免异常中断读取）
    raw = bytes(data[tc_off + 3:tc_off + 3 + L])
    try:
        val = raw.decode('utf-8')
    except UnicodeDecodeError:
        val = raw.decode('utf-8', 'replace')
    return dict(name="玩家昵称", off=tc_off + 3, val=val, raw=raw, enc=val)


# ─────────────────────────────────────
#  回归玩家数据（ReturnPlayerData）定位
#  com.zhaohe.game.huodong.playerreturn.ReturnPlayerData
#  Externalizable；writeExternal 顺序:
#    remainingScore(int) totalDays(int) activateTime(long)
#    collected(HashSet<Integer>) bought(HashSet<Integer>)
#  激活判定: activateTime != -1 且 != 0（否则为未激活）
# ─────────────────────────────────────
RETURNPLAYER_CLASS = b"com.zhaohe.game.huodong.playerreturn.ReturnPlayerData"
COMERDATA_CLASS = b"com.zhaohe.game.huodong.newcomer.NewComerData"


def _rp_parse_classdesc(data, p):
    """p 指向 0x72。解析 classdesc，返回 (end_off, flags) 或 (None, None)。"""
    n = len(data)
    if p >= n or data[p] != 0x72:
        return None, None
    q = p + 1
    nl = int.from_bytes(data[q:q + 2], 'big'); q += 2
    q += nl                       # class name
    q += 8                        # serialVersionUID
    flags = data[q]; q += 1
    fc = int.from_bytes(data[q:q + 2], 'big'); q += 2
    for _ in range(fc):
        if q >= n:
            return None, None
        t = data[q]; q += 1
        if t in (0x4c, 0x5b):      # 'L' 或 '['
            cl = int.from_bytes(data[q:q + 2], 'big'); q += 2 + cl
    # classAnnotation block（直到 0x78）
    while q < n and data[q] in (0x77, 0x78):
        if data[q] == 0x78:
            q += 1
            break
        ln = data[q + 1]; q += 2 + ln
    # super classdesc
    if q < n:
        if data[q] == 0x70:
            q += 1
        elif data[q] == 0x71:
            q += 5
        elif data[q] == 0x72:
            q, _ = _rp_parse_classdesc(data, q)
    return q, flags


def _rp_skip_key(data, p):
    """跳过一个 HashSet 元素（Integer / ref / string / null），返回新 offset 或 None。"""
    n = len(data)
    if p >= n:
        return None
    t = data[p]
    if t == 0x70:
        return p + 1
    if t == 0x71:
        return p + 5
    if t == 0x74:                  # TC_STRING（本游戏 1 字节长度）
        if p + 2 >= n:
            return None
        L = data[p + 2]
        return p + 3 + L
    if t == 0x7c:                  # TC_LONGSTRING
        L = int.from_bytes(data[p + 1:p + 5], 'big')
        return p + 5 + L
    if t == 0x73:                  # TC_OBJECT（Integer: classdesc + 若干字段）
        q = p + 1
        if q < n and data[q] == 0x71:      # classdesc 引用
            q += 5
            return _rp_skip_fields(data, q, None)
        if q < n and data[q] == 0x72:      # classdesc 内联
            ce, _flags = _rp_parse_classdesc(data, q)
            if ce is None:
                return None
            return _rp_skip_fields(data, ce, q)
    return None


def _rp_skip_fields(data, p, classdesc_off):
    """从 classdesc 结束处按字段类型跳过字段值，返回新 offset。

    classdesc_off 非空时重新解析该 classdesc 的字段类型表（内联对象）；
    为 None 时无法得知字段类型（引用 classdesc），保守按常见 Integer(int) 处理。"""
    n = len(data)
    if classdesc_off is None:
        # 引用 classdesc：不知道字段。Integer 为单 int(4)。无法通用，返回 None 让上层放弃。
        return None
    _, flags = _rp_parse_classdesc(data, classdesc_off)
    if not (flags & 0x02):         # 非 SC_SERIALIZABLE 无内联字段
        return p
    # 重新走一遍字段表
    q = classdesc_off + 1
    nl = int.from_bytes(data[q:q + 2], 'big'); q += 2 + nl + 8
    flags2 = data[q]; q += 1
    fc = int.from_bytes(data[q:q + 2], 'big'); q += 2
    for _ in range(fc):
        t = chr(data[q]); q += 1
        if t in ('L', '['):
            cl = int.from_bytes(data[q:q + 2], 'big'); q += 2 + cl
            p = _rp_skip_key(data, p)       # 对象字段：跳一个对象
            if p is None:
                return None
        elif t in ('B', 'Z', 'C'):
            p += 1
        elif t in ('S',):
            p += 2
        elif t in ('I', 'F'):
            p += 4
        elif t in ('J', 'D'):
            p += 8
        if p is None or p > n:
            return None
    return p


def _rp_parse_hashset(data, p):
    """p 指向 HashSet 的 TC_OBJECT(0x73)。返回 dict 或 None。"""
    n = len(data)
    if p >= n or data[p] != 0x73:
        return None
    q = p + 1
    if q < n and data[q] == 0x71:          # classdesc 引用
        q += 5
    elif q < n and data[q] == 0x72:        # classdesc 内联
        q, _ = _rp_parse_classdesc(data, q)
    else:
        return None
    if q is None or q >= n or data[q] != 0x77:
        return None
    blen = data[q + 1]
    bdata = q + 2
    if blen < 12 or bdata + blen > n:
        return None
    size = int.from_bytes(data[bdata + 8:bdata + 12], 'big')
    size_off = bdata + 8
    first_key = bdata + blen
    keys_end = first_key
    kinds = []
    for _ in range(size):
        if keys_end >= n:
            return None
        t = data[keys_end]
        kinds.append({0x70: 'null', 0x71: 'ref', 0x74: 'str', 0x73: 'obj'}.get(t, 'other'))
        nxt = _rp_skip_key(data, keys_end)
        if nxt is None:
            return None
        keys_end = nxt
    # keys_end 应指向 0x78(endblockdata)
    # all_safe: 全部为 ref/null 时，删除 key 区只产生字节位移、不增减 Java 序列化句柄，绝对安全
    return {'tc_off': p, 'size': size, 'size_off': size_off,
            'first_key': first_key, 'key_end': keys_end,
            'blockdata_off': q, 'kinds': kinds,
            'all_safe': all(k in ('null', 'ref') for k in kinds)}


def locate_returnplayer(data):
    """定位 soco.bin 内 ReturnPlayerData，返回 dict 或 None。

    返回字段:
      obj_off, activated(bool), activate_time(ms or None),
      remaining_score/total_days + 各自 offset,
      activate_off, collected/bought(含 size/size_off/first_key/key_end)
    """
    n = len(data)
    idx = data.find(RETURNPLAYER_CLASS)
    if idx < 0 or idx < 4:
        return None
    cd_off = idx - 3               # 0x72
    obj_off = idx - 4               # 0x73
    if data[cd_off] != 0x72 or data[obj_off] != 0x73 or data[idx - 2:idx] != b'\x00\x35':
        return None
    q, flags = _rp_parse_classdesc(data, cd_off)
    if q is None or q >= n or data[q] != 0x77:
        return None
    blen = data[q + 1]
    bdata = q + 2
    if blen < 16 or bdata + 16 > n:
        return None
    remaining_score = int.from_bytes(data[bdata:bdata + 4], 'big', signed=True)
    total_days = int.from_bytes(data[bdata + 4:bdata + 8], 'big', signed=True)
    activate_time = int.from_bytes(data[bdata + 8:bdata + 16], 'big', signed=True)
    p = bdata + blen
    collected = _rp_parse_hashset(data, p)
    bought = None
    if collected is not None:
        b_off = collected['key_end']
        if b_off < n and data[b_off] == 0x78:
            b_off += 1
        bought = _rp_parse_hashset(data, b_off)
    activated = activate_time not in (-1, 0)
    return {
        'obj_off': obj_off,
        'activated': activated,
        'activate_time': activate_time,
        'remaining_score': remaining_score,
        'total_days': total_days,
        'score_off': bdata,
        'days_off': bdata + 4,
        'activate_off': bdata + 8,
        'collected': collected,
        'bought': bought,
    }


_PHONE_STR_RE = re.compile(rb'\x74\x00\x0b(1[3-9]\d{9})')


def locate_phoneshouji(data):
    """定位 soco.bin 内 GameData.playerSHOUJI（账号绑定手机号）。

    ★ 定位原理（不再依赖"11位数字"内容，换账号也能定位）：
      GameData.readObject 顺序（smali 第 4269~4272 行）:
          readObject() -> playerSHOUJI   (String / 可能为 TC_REFERENCE 或空)
          readLong()   -> dataSaveTime   (long，毫秒时间戳)
      而 dataSaveTime 是【8 字节 long】，在流里表现为
          TC_BLOCKDATA(0x77) + 0x08 + 8字节
      且其时间戳值落在 1.5e12 ~ 2.0e12 (2020年后毫秒级) 区间时【全档唯一】。
      → 先用这个唯一锚点找到 dataSaveTime，其【紧邻前面】的槽位就是 playerSHOUJI。

      playerSHOUJI 的三种可能形态：
          'str' : 74 <len:2> <utf8>     —— 已绑定且值为内联串
          'ref' : 71 <handle:4>          —— 指向共享串(通常是空串)
          'none': 无(长度0的块/被跳过)  —— 字段为空

    返回 dict 或 None:
      kind('str'/'ref'/'none'), str_off, data_off, length, phone(''), handle
    """
    import re as _re
    n = len(data)
    # 找 dataSaveTime：77 08 + 毫秒时间戳
    anchor = None
    for m in _re.finditer(rb'\x77\x08', data):
        i = m.start()
        if i + 10 > n:
            continue
        ts = int.from_bytes(data[i + 2:i + 10], 'big')
        if 1.5e12 < ts < 2.0e12:
            anchor = i
            break
    if anchor is None:
        # 兜底: dataSaveTime 为 0（新号初始存档）时没有毫秒时间戳，
        # 改用 shouchong_num ([I] 数组) 作锚点：
        #   [I] classdesc = 72 00 02 5b 49，数组 = 75 71 <5B> <classdesc> ...
        #   数组之前紧邻 dataSaveTime(可能为 0) 与 playerSHOUJI
        for m in _re.finditer(rb'\x72\x00\x02\x5b\x49', data):
            x = m.start()
            if x < 6 or data[x - 1] != 0x75 or data[x - 6] != 0x71:
                continue
            arr = x - 6
            q = x + 5 + 8 + 1
            fc = int.from_bytes(data[q:q + 2], 'big'); q += 2
            if q < n and data[q] == 0x78:
                q += 1
            if q < n and data[q] == 0x70:
                q += 1
            ln = int.from_bytes(data[q:q + 4], 'big')
            if ln > 30:
                continue
            # 数组前的 8 字节 = dataSaveTime(0)，再往前是 playerSHOUJI
            ts_off = arr - 8
            for L in range(0, 64):
                so = ts_off - 3 - L
                if so < 0:
                    break
                if data[so] == 0x74 and int.from_bytes(data[so + 1:so + 3], 'big') == L:
                    raw = bytes(data[so + 3:so + 3 + L])
                    try:
                        phone = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        phone = raw.decode('utf-8', 'replace')
                    return {'kind': 'str', 'str_off': so, 'data_off': so + 3,
                            'length': L, 'phone': phone, 'handle': None,
                            'ts_off': ts_off, 'ts': 0}
            if ts_off >= 5 and data[ts_off - 5] == 0x71:
                return {'kind': 'ref', 'str_off': ts_off - 5, 'data_off': ts_off - 5,
                        'length': 5, 'phone': '',
                        'handle': int.from_bytes(data[ts_off - 4:ts_off], 'big'),
                        'ts_off': ts_off, 'ts': 0}
            return {'kind': 'none', 'str_off': None, 'data_off': None, 'length': 0,
                    'phone': '', 'handle': None, 'ts_off': ts_off, 'ts': 0}
        return None

    # playerSHOUJI 紧邻 dataSaveTime 之前
    # 形态1: 内联 String
    for L in range(0, 64):
        so = anchor - 3 - L
        if so < 0:
            break
        if data[so] == 0x74 and int.from_bytes(data[so + 1:so + 3], 'big') == L:
            raw = bytes(data[so + 3:so + 3 + L])
            try:
                phone = raw.decode('utf-8')
            except UnicodeDecodeError:
                phone = raw.decode('utf-8', 'replace')
            return {
                'kind': 'str', 'str_off': so, 'data_off': so + 3,
                'length': L, 'phone': phone, 'handle': None,
                'ts_off': anchor, 'ts': int.from_bytes(data[anchor + 2:anchor + 10], 'big'),
            }
    # 形态2: TC_REFERENCE
    if anchor >= 5 and data[anchor - 5] == 0x71:
        return {
            'kind': 'ref', 'str_off': anchor - 5, 'data_off': anchor - 5,
            'length': 5, 'phone': '',
            'handle': int.from_bytes(data[anchor - 4:anchor], 'big'),
            'ts_off': anchor, 'ts': int.from_bytes(data[anchor + 2:anchor + 10], 'big'),
        }
    # 形态3: 空（该字段被块长度吃掉，没有独立槽位）
    return {
        'kind': 'none', 'str_off': None, 'data_off': None,
        'length': 0, 'phone': '', 'handle': None,
        'ts_off': anchor, 'ts': int.from_bytes(data[anchor + 2:anchor + 10], 'big'),
    }


# ─────────────────────────────────────
#  实名认证（防沉迷）字段 fangchenmi_*
#  GameData.readObject 中的序列（smali GameData 第 4641~4644 行）:
#      readObject() -> String  fangchenmi_name          ★ 注意：不是真实姓名！
#      readObject() -> String  fangchenmi_shenfenzheng  ★ 通常是 TC_REFERENCE 空串
#      readLong()   -> long    fangchenmi_playTime
#      readObject() -> ReturnPlayerData   ← 用作定位锚点
#
#  ★★ 关键结论（源码实证）：
#    fangchenmi_name 并不保存玩家真实姓名。Netsender 处理
#    IdentifyRealNameAck 时，若 getOpType()==2 就写入常量字符串 "ok"
#    （Netsender.smali:2861），并置 bangdingType=1。也就是说它只是一个
#    "实名已验证"的状态标记；真实姓名/证件号只存在于服务端，不落本地存档。
#    fangchenmi_shenfenzheng 一般为空（引用共享空串）。
# ─────────────────────────────────────
FANGCHENMI_NOTES = (
    "fangchenmi_name 是实名状态标记（服务端 opType==2 时写入 \"ok\"），"
    "并非真实姓名；真实姓名与证件号只保存在服务端，不在本地存档中。"
)


def _ref_handle(data, off):
    """若 off 处是 TC_REFERENCE，返回其 handle 值，否则 None。"""
    if off + 5 <= len(data) and data[off] == 0x71:
        return int.from_bytes(data[off + 1:off + 5], 'big')
    return None


def locate_fangchenmi(data):
    """定位 soco.bin 内实名字段 fangchenmi_name / fangchenmi_shenfenzheng / playTime。

    ★ 实测布局（writeObject 的落盘顺序与 readObject 的读取顺序不同）：
        ... <TB_REF 或 TC_STRING>   fangchenmi_shenfenzheng
            74 00 02 "ok"           fangchenmi_name（实名状态标记）
            <TB_REF>                （同上，共享空串引用）
            77 08 <8 字节>          fangchenmi_playTime
            73 72 00 35 com.zhaohe.game.huodong.playerreturn.ReturnPlayerData  ← 锚点
      即: 证件号槽位 → 名字("ok") → 空串引用 → playTime块 → ReturnPlayerData 对象头

    定位：以 ReturnPlayerData 类描述符为锚点，向前取最近的 77 08 块为 playTime；
    该块之前是 "ok" 名字串与两个引用槽位。

    返回 dict 或 None:
      name/name_off/name_kind, id/id_off/id_kind, play_time/play_off, anchor_off
    """
    anchor = data.find(RETURNPLAYER_CLASS)
    if anchor < 0:
        return None
    lo = max(0, anchor - 400)

    # playTime = 锚点前最近的 77 08 块
    play_off = None
    for p in range(anchor - 12, lo, -1):
        if data[p] == 0x77 and data[p + 1] == 0x08 and p + 10 <= anchor:
            play_off = p
            break
    if play_off is None:
        return None
    play_time = int.from_bytes(data[play_off + 2:play_off + 10], 'big')

    # playTime 块之前紧邻一个引用槽（共享空串），再往前是 name 串
    t = play_off
    pre_ref_off = None
    if t >= 5 and data[t - 5] == 0x71:
        pre_ref_off = t - 5
        t -= 5

    name_off = name_val = None
    if t >= 3 and data[t - 1] != 0x74:
        # 往回找最近的 TC_STRING 起点
        s = data.rfind(b'\x74\x00', lo, t)
    else:
        s = data.rfind(b'\x74\x00', lo, t)
    if s >= 0:
        L = int.from_bytes(data[s + 1:s + 3], 'big')
        if s + 3 + L <= t + 1:
            name_off = s
            name_val = bytes(data[s + 3:s + 3 + L]).decode('utf-8', 'replace')

    # name 之前是 fangchenmi_shenfenzheng 槽位
    id_off = id_kind = None
    id_val = ''
    q = name_off if name_off is not None else play_off
    if q >= 5 and data[q - 5] == 0x71:
        id_off = q - 5
        id_kind = 'ref'
    elif q >= 1 and data[q - 1] == 0x74:
        s2 = data.rfind(b'\x74\x00', lo, q)
        if s2 >= 0:
            L2 = int.from_bytes(data[s2 + 1:s2 + 3], 'big')
            id_off = s2
            id_kind = 'str'
            id_val = bytes(data[s2 + 3:s2 + 3 + L2]).decode('utf-8', 'replace')

    return {
        'name': name_val,
        'name_off': name_off,
        'name_kind': 'str' if name_off is not None else None,
        'id': id_val,
        'id_off': id_off,
        'id_kind': id_kind,
        'play_time': play_time,
        'play_off': play_off,
        'anchor_off': anchor,
        'note': FANGCHENMI_NOTES,
    }


# ─────────────────────────────────────
#  章节进度（daxuanguan 系列）
#  GameData.readObject 的连续序列（smali GameData 第 4131~4136 行）:
#      readInt() setDaxuanguan        → daxuanguan        ★ 通关章节（0-based）
#      readInt() setdaxuanguan_kaiqi  → daxuanguan_kaiqi  已开启章节
#      readInt()                      → daxuanguan1      当前所在关（-100=无/通关后置空）
#      readInt()                      → xuanguan_id1     当前关 id
#      readInt() setYaoshi1           → yaoshi1          钥匙数
#      readFloat()                    → xuanguanX1       当前关坐标 X
#                                    → xuanguanY1       当前关坐标 Y
#
#  ★ daxuanguan 是 0-based 索引：游戏显示"第 N 章" = daxuanguan + 1
#    实测: 超小云存档 daxuanguan=137 → 显示 138 章
#          冰雪的豹子760 存档 daxuanguan=120 → 显示 121 章
#    这些字段合并在同一个 TC_BLOCKDATA（通常 len=32）内，连续排列。
# ─────────────────────────────────────
def locate_daxuanguan(data):
    """定位章节进度全部相关字段。

    ★★ 关键：daxuanguan 在登录时会被 daxuanguan_kaiqi_backup 覆盖！
        UI_Title.smali:2875  const-string ...
            invoke-static getDaXuanGuanKaiQi()   → 返回 daxuanguan_kaiqi_backup
            invoke-static setDaxuanguan(v2)      → 覆盖 daxuanguan
        所以【真正决定游戏显示章节的是 daxuanguan_kaiqi_backup】。
        只改 daxuanguan / daxuanguan_kaiqi 无效（下次登录被备份值覆盖回来）。

    字段组 A（TC_BLOCKDATA，连续 int，GameData.readObject 第 4131~4136 行）:
        daxuanguan / daxuanguan_kaiqi / daxuanguan1 / xuanguan_id1 / yaoshi1 / (float x,y)
        —— 判别：块内连续 a==b 且 0<a<200，其后 c ∈ -100..200

    字段组 B（daxuanguan_kaiqi_backup，独立 readInt，第 4655 行）:
        位置特征：`77 20`(32 字节块) 的最后一个 int，且其后紧接
        73 72 00 2d com.zhaohe.game.huodong.newcomer.NewComerData（TC_OBJECT+CLASSDESC）
        —— 这个才是登录后生效的权威值。

    返回 dict 或 None:
      chapter(组A 值) / kaiqi / stage_idx / stage_id / yaoshi / base_off / block_off
      backup / backup_off    ★ backup 为最终生效值
      effective              = backup（显示章数 = effective + 1）
    """
    n = len(data)
    res = {}

    # ── 组 A ──
    best = None
    i = 4
    while i < n - 2:
        if data[i] != 0x77:
            i += 1
            continue
        ln = data[i + 1]
        start = i + 2
        if ln < 20 or start + ln > n:
            i += 1
            continue
        body = data[start:start + ln]
        for off in range(0, len(body) - 20):
            a = int.from_bytes(body[off:off + 4], 'big', signed=True)
            b = int.from_bytes(body[off + 4:off + 8], 'big', signed=True)
            c = int.from_bytes(body[off + 8:off + 12], 'big', signed=True)
            if 0 < a < 200 and a == b and -100 <= c <= 200:
                d4 = int.from_bytes(body[off + 12:off + 16], 'big', signed=True)
                d5 = int.from_bytes(body[off + 16:off + 20], 'big', signed=True)
                # xuanguan_id1 是关卡 id（0..300 或 -1）；yaoshi1 是小整数（-1..999）。
                # 明显的浮点/随机数（如 1970339966）说明命中了别的块，跳过。
                if not (-1 <= d4 <= 400) or not (-1 <= d5 <= 100000):
                    continue
                cand = {
                    'chapter': a, 'chapter_display': a + 1,
                    'kaiqi': b, 'stage_idx': c, 'stage_id': d4, 'yaoshi': d5,
                    'block_off': i, 'base_off': start + off,
                }
                if best is None or (cand['stage_idx'] == -100 and best['stage_idx'] != -100):
                    best = cand
                break
        i += 2 + ln
    if best:
        res.update(best)

    # ── 组 B：daxuanguan_kaiqi_backup ──
    anchor = data.find(COMERDATA_CLASS)
    if anchor >= 0:
        q = None
        for x in range(max(0, anchor - 80), anchor):
            if data[x] == 0x77 and data[x + 1] == 0x20:
                q = x
        if q is not None:
            voff = q + 2 + 28          # 32 字节块的最后一个 int
            bval = int.from_bytes(data[voff:voff + 4], 'big', signed=True)
            if -100 <= bval < 300:
                res['backup'] = bval
                res['backup_off'] = voff
                res['effective'] = bval

    if not res:
        return None
    if 'effective' not in res and 'chapter' in res:
        res['effective'] = res['chapter']
    return res


# ─────────────────────────────────────
#  主角立绘形象解锁（LiHuiData）
#  包: com/zhaohe/game/lihui/
#  · LiHuiData implements Externalizable
#      writeExternal: selectedIndex(int) + useable(ArrayList) + redDot(ArrayList)
#  · LiHuiManager.getUseable(i) -> LiHuiData.getUseable(i) -> useable.get(i)
#  · 主界面左下角形象列 UI_daxuanguan.initLiHuiStageEntry(i)
#      点击回调 UI_daxuanguan$1.onClicked():
#        getUseable(i) ? 切换形象 : UI_MessageBox(getTip(i))  ← "尚未解锁"弹窗
#  · 存档结构（实测）:
#      sr 00 1f "com.zhaohe.game.lihui.LiHuiData" <suid:8> 0c 00 00 78 70
#      77 04 <selectedIndex:int>
#      73 71 <ArrayList引用> 00 00 00 <N>      ← useable 元素个数
#      77 04 00 00 00 12                       ← 内部 blockdata
#      N 个 Boolean: 73 71 <Boolean类引用> <0|1>   或  71 <引用前一个>
#      78                                       ← ENDBLOCKDATA
# ─────────────────────────────────────
LIHUIDATA_CLASS = b"com.zhaohe.game.lihui.LiHuiData"

# Java 序列化 TC 标记（本模块内部使用）
TC_NULL = 0x70
TC_REFERENCE = 0x71
TC_CLASSDESC = 0x72
TC_OBJECT = 0x73
TC_STRING = 0x74
TC_ARRAY = 0x75
TC_BLOCKDATA = 0x77
TC_ENDBLOCKDATA = 0x78


def locate_lihui_slots(data):
    """定位 LiHuiData.useable 数组每个槽位的解锁布尔值偏移。

    返回 dict 或 None:
      selected_index / selected_off
      count / slots = [(index, value_offset, kind, value)]
        kind='inline' 时 value_offset 指向该 Boolean 的字节值（0/1），可直接改
        kind='ref'    时 value_offset 指向 TC_REFERENCE，值与首个同对象一致
      block_off
    """
    n = len(data)
    i = data.find(LIHUIDATA_CLASS)
    if i < 0:
        return None
    # classdesc: sr(1) + 00 1f(2) + name(31) + suid(8) + flags(1) + fields(2) + 78 70
    name_len = int.from_bytes(data[i - 2:i], 'big') if data[i - 2] == 0 else 31
    # classdesc 体: name + suid(8) + flags(1) + fieldCount(2) + [字段] + TC_ENDBLOCKDATA + super
    p = i + name_len + 8 + 1
    if p + 2 > n:
        return None
    fc = int.from_bytes(data[p:p + 2], 'big')
    p += 2
    if data[p] == TC_ENDBLOCKDATA:
        p += 1
    if data[p] == TC_NULL:
        p += 1
    elif data[p] == TC_REFERENCE:
        p += 5

    # selectedIndex
    if data[p] != TC_BLOCKDATA or data[p + 1] != 4:
        return None
    selected_off = p + 2
    selected_index = int.from_bytes(data[p + 2:p + 6], 'big', signed=True)
    p += 6

    # useable ArrayList
    # 结构: TC_OBJECT(0x73) + classDesc(TC_REFERENCE: 0x71 + 4字节handle)
    if data[p] != TC_OBJECT:
        return None
    p += 1
    if data[p] == TC_REFERENCE:
        p += 1 + 4        # 0x71 + 4字节 handle
    elif data[p] == TC_CLASSDESC:
        return None      # 罕见：内联 classdesc，不处理
    if p + 4 > n:
        return None
    count = int.from_bytes(data[p:p + 4], 'big')
    p += 4
    if count <= 0 or count > 256:
        return None
    # 内部 blockdata（ArrayList 的 size 字段等）
    block_off = None
    if data[p] == TC_BLOCKDATA:
        block_off = p
        p += 2 + data[p + 1]

    slots = []
    for idx in range(count):
        if p >= n:
            return None
        if data[p] == TC_OBJECT:
            # Boolean 对象: 73 + classDesc + <value 1字节>
            # classDesc 有两种形态:
            #   73 71 <4字节handle>   引用已定义的 classdesc（常见）
            #   73 72 <name...>       内联 classdesc（首次出现时）
            if data[p + 1:p + 2] == b'\x72':
                # 内联 classdesc: 72 <len:2> <name> <suid:8> <flags:1> <fields:2> 78 70
                L2 = int.from_bytes(data[p + 2:p + 4], 'big')
                q2 = p + 4 + L2 + 8 + 1
                fc2 = int.from_bytes(data[q2:q2 + 2], 'big'); q2 += 2
                # 跳过字段列表（每个: type(1) + nameLen(2) + name）
                for _ in range(fc2):
                    ty2 = data[q2]; q2 += 1
                    nl2 = int.from_bytes(data[q2:q2 + 2], 'big'); q2 += 2 + nl2
                    if ty2 in (ord('L'), ord('[')):
                        if data[q2] == 0x74:
                            l3 = int.from_bytes(data[q2 + 1:q2 + 3], 'big')
                            q2 += 3 + l3
                        elif data[q2] == 0x71:
                            q2 += 5
                if data[q2] == TC_ENDBLOCKDATA:
                    q2 += 1
                if data[q2] == TC_NULL:
                    q2 += 1
                elif data[q2] == TC_REFERENCE:
                    q2 += 5
                val_off = q2
                if val_off >= n:
                    return None
                slots.append((idx, val_off, 'inline', data[val_off]))
                p = val_off + 1
                continue
            # 73 71 <classref 4字节> <value 1字节>
            if data[p + 1] != TC_REFERENCE:
                return None
            val_off = p + 6
            if val_off >= n:
                return None
            slots.append((idx, val_off, 'inline', data[val_off]))
            p = val_off + 1
        elif data[p] == TC_REFERENCE:
            slots.append((idx, p, 'ref', None))
            p += 5
        else:
            return None
    # 结束标记
    if p < n and data[p] == TC_ENDBLOCKDATA:
        p += 1
    return {
        'selected_index': selected_index,
        'selected_off': selected_off,
        'count': count,
        'slots': slots,
        'block_off': block_off,
    }


def repr_lihui_slots(data):
    """返回便于 UI 显示的形象解锁状态列表：[{index, unlocked, offset, editable}]"""
    info = locate_lihui_slots(data)
    if info is None:
        return None
    # 解出引用型槽位的实际值（引用指向首个内联 Boolean）
    inline_val = {}
    for idx, off, kind, val in info['slots']:
        if kind == 'inline':
            inline_val[idx] = val
    out = []
    for idx, off, kind, val in info['slots']:
        if kind == 'inline':
            unlocked = bool(val)
        else:
            unlocked = None       # 与其它槽共享同一对象，需靠同组内联值推断
        out.append({
            'index': idx,
            'unlocked': unlocked,
            'offset': off,
            'kind': kind,
        })
    return {'selected_index': info['selected_index'], 'slots': out}


# ─────────────────────────────────────
#  补充体力（UI_RestoreStamina）相关
#  · 体力礼盒 = CastleResource id 3165（数量走 Castle 资源系统）
#  · 今日限购剩余 = GameData.tili_buy(byte)，位于 GameData.writefile 的 tili 段
#    tili 段是一个 TC_BLOCKDATA，结构:
#      ftime2(J) SHOUJIdataVer(I) isDEMOdata(Z) istishi_bushuadd(Z)
#      tili_time(J) tili_buy(B) tili_add(I)
#      tili_huodonglingqu1(Z) tili_huodonglingqu2(Z) playerNamexiugai(Z)
#      worldboss_shuaxintime(J)
#    tili_buyMax(上限) 不在 soco.bin（服务端/配置下发）。
# ─────────────────────────────────────
STAMINA_ITEM_ID = 3165          # 体力礼盒
_TS_MIN = 1.5e12
_TS_MAX = 2.0e12


def _is_ms_ts(v):
    return _TS_MIN <= v <= _TS_MAX


def locate_tili_block(data):
    """定位 tili 段所在 TC_BLOCKDATA。

    返回 (info, hit_count)：info 为 None 表示未找到。
    info 字段: block_off(0x77 处文件偏移)、tili_buy/值+偏移、tili_add、tili_time、
              lq1/lq2/namexiugai、worldboss_shuaxintime、ftime2。
    """
    n = len(data)
    hits = []
    for p in range(4, n - 44):
        if data[p] != 0x77:
            continue
        ln = data[p + 1]
        if ln < 38 or ln > 80:
            continue
        b = p + 2                                   # blockdata 数据起点
        ftime2 = int.from_bytes(data[b:b + 8], 'big')
        if not _is_ms_ts(ftime2):
            continue
        ver = int.from_bytes(data[b + 8:b + 12], 'big')
        if not (0 <= ver <= 10000):
            continue
        if data[b + 12] not in (0, 1) or data[b + 13] not in (0, 1):
            continue
        ttime = int.from_bytes(data[b + 14:b + 22], 'big')
        if not (ttime == 0 or _is_ms_ts(ttime)):
            continue
        buy = data[b + 22]
        if not (0 <= buy <= 64):
            continue
        add = int.from_bytes(data[b + 23:b + 27], 'big')
        if not (0 <= add <= 1000000):
            continue
        if data[b + 27] not in (0, 1) or data[b + 28] not in (0, 1) or data[b + 29] not in (0, 1):
            continue
        wbt = int.from_bytes(data[b + 30:b + 38], 'big', signed=True)
        if not (wbt in (-1, 0) or _is_ms_ts(wbt)):
            continue
        hits.append({
            'block_off': p,
            'block_len': ln,
            'ftime2': ftime2,
            'data_ver': ver,
            'tili_time': ttime,
            'tili_time_off': b + 14,
            'tili_buy': buy,
            'tili_buy_off': b + 22,
            'tili_add': add,
            'tili_add_off': b + 23,
            'lq1': data[b + 27],
            'lq2': data[b + 28],
            'namexiugai': data[b + 29],
            'worldboss_shuaxintime': wbt,
        })
    if not hits:
        return None, 0
    return hits[0], len(hits)


def read_stamina_summary(data):
    """补充体力弹窗概览：体力礼盒数量 + 今日限购剩余。"""
    info, cnt = locate_tili_block(data)
    box = None
    for row in read_castle_overview(data).get('resources', []):
        if row.get('id') == STAMINA_ITEM_ID:
            box = row
            break
    return {
        'tili': info,
        'tili_hits': cnt,
        'item': box,                 # CastleResource 3165（value/offset/type）
        'item_id': STAMINA_ITEM_ID,
    }


# ── 保存格式版本检测 ──
# 2026-07-21 新版：GameData.writefile() 在末尾增加了 2 个 writeObject 调用
# （LianxuIAPjiangli_lingqu 从 [Z→对象数组），总计 234 writeObject 和 253 个 classdesc。
# 旧版 232 writeObject / 252 classdesc，大小约 140128 字节。
# 旧格式存档被新版客户端直读会因句柄偏移炸 ClassCastException。

SAVE_FORMAT_OLD = "old"
SAVE_FORMAT_NEW = "new"
NEW_FORMAT_REF_PATH = os.path.join(BASE_DIR, "soco_new_ref_fmt.bin")


def detect_save_format(data):
    sz = len(data)
    if sz >= 140000:
        return SAVE_FORMAT_NEW
    return SAVE_FORMAT_OLD


def is_old_format(data):
    return detect_save_format(data) == SAVE_FORMAT_OLD


def is_new_format(data):
    return detect_save_format(data) == SAVE_FORMAT_NEW


# ─────────────────────────────────────
#  上阵卡片读取（familyid / 等级 / 卡片数）
# ─────────────────────────────────────
GUAIWU_2D_NAME = b"[[Ljava.lang.String;"   # 外层 classdesc 名（长度 20 -> 00 14）
FAMILYMAP_PATH = os.path.join(BASE_DIR, "familyid_full_map.txt")


def load_family_names():
    """解析 familyid_full_map.txt -> {familyid: 名称}"""
    names = {}
    if not os.path.exists(FAMILYMAP_PATH):
        return names
    import re
    with open(FAMILYMAP_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            m = re.match(r'\s*family\s+(\d+)\s*=\s*(\S+)', line)
            if m:
                names[int(m.group(1))] = m.group(2)
    return names


def locate_player(data):
    """扫描 int[] 实例，返回 (off, [kind0,kind1,kind2])。
    判据：length==3 且 3 元素均为 [0,400] 内 5 的倍数（上阵槽 kind 特征）。"""
    n = len(data)
    i = 0
    while True:
        i = data.find(b'\x75', i)          # TC_ARRAY
        if i < 0:
            break
        p = i + 1
        base = None
        # 分支 A: 完整 [I classdesc
        if data[p] == 0x72 and data[p + 1:p + 3] == b'\x00\x02' and data[p + 3:p + 5] == b'[I':
            q = p + 5 + 8 + 1              # name(2)+[I(2) + suid(8) + flags(1)
            fc = int.from_bytes(data[q:q + 2], 'big'); q += 2
            if q < n and data[q] == 0x78: q += 1
            if q < n and data[q] == 0x70: q += 1
            base = q + 4
            length = int.from_bytes(data[q:q + 4], 'big')
        # 分支 B: classdesc 引用（71 + 4B handle）
        elif data[p] == 0x71:
            q = p + 5
            length = int.from_bytes(data[q:q + 4], 'big')
            base = q + 4
        else:
            i += 1
            continue
        if base is not None and length == 3 and base + 12 <= n:
            vals = [int.from_bytes(data[base + k * 4:base + k * 4 + 4], 'big') for k in range(3)]
            if all(0 <= v <= 400 and v % 5 == 0 for v in vals):
                return i, vals
        i += 1
    return None, None


def locate_guaiwu(data):
    """返回 (第0个内层元素起点 off, 外层长度 N)。"""
    key = bytes([0x00, len(GUAIWU_2D_NAME)]) + GUAIWU_2D_NAME   # 00 14 [[Ljava.lang.String;
    i = data.find(key)
    if i < 0:
        return None, None
    p = i + len(key)          # name 结尾
    p += 8                    # suid
    p += 1                    # flags
    fc = int.from_bytes(data[p:p + 2], 'big'); p += 2  # fieldcount(=0)
    if data[p] == 0x78: p += 1   # TC_ENDBLOCKDATA
    if data[p] == 0x70: p += 1   # super classdesc = null
    length = int.from_bytes(data[p:p + 4], 'big'); p += 4
    return p, length


def parse_inner_array(data, p):
    """解析 GameGuaiWu 的一个内层 String[]，返回 (元素列表, 下一个起点)。"""
    n = len(data)
    b = data[p]
    if b == 0x70:                       # 该 family 为 null
        return None, p + 1
    if b == 0x71:                       # 引用之前的数组（罕见）
        return 'ARRAY_REF', p + 5
    if b != 0x75:                       # 非 TC_ARRAY，结构异常
        return '?', p + 1
    p += 1
    if data[p] == 0x72:                 # 完整 [Ljava.lang.String; classdesc
        p += 1
        L = int.from_bytes(data[p:p + 2], 'big'); p += 2
        p += L                          # name
        p += 8                          # suid
        p += 1                          # flags
        fc = int.from_bytes(data[p:p + 2], 'big'); p += 2  # fieldcount(=0)
        if data[p] == 0x78: p += 1
        if data[p] == 0x70: p += 1
    elif data[p] == 0x71:               # classdesc 引用
        p += 5
    length = int.from_bytes(data[p:p + 4], 'big'); p += 4
    vals = []
    for _ in range(length):
        if p >= n:
            break
        eb = data[p]
        if eb == 0x74:                  # TC_STRING (74 00 L data)
            cell_off = p
            LL = data[p + 2]
            raw = bytes(data[p + 3:p + 3 + LL])
            vals.append(('str', raw, cell_off))
            p += 3 + LL
        elif eb == 0x70:                # null
            vals.append(('null', None, p))
            p += 1
        elif eb == 0x71:                # reference
            h = int.from_bytes(data[p + 1:p + 5], 'big')
            vals.append(('ref', h, p))
            p += 5
        else:
            vals.append(('?', eb))
            break
    return vals, p


def decode_cell(cell):
    """从单元格 (kind, raw) 解码 QWERTY 整数。
    - 'str' : 直接解码
    - 'null': 字段为空 => 进阶等级默认 +0
    - 'ref' : 共享串引用（本游戏非上阵卡的 f17 多为共享的 +0 串）=> 默认 +0
    """
    if cell is None:
        return 0
    if cell[0] == 'str':
        v = qe_dec(cell[1])
        return v if v is not None else 0
    return 0


_HM_CACHE = {}


def _handle_map(data):
    """构建（并缓存）精确的 Java handle 映射（见 soco_handles）。"""
    import soco_handles
    key = (len(data), bytes(data[:64]))
    hm = _HM_CACHE.get(key)
    if hm is None:
        hm = soco_handles.HandleMap(data)
        _HM_CACHE[key] = hm
    return hm


def resolve_handle(data, handle):
    """handle -> 字符串原文 bytes；非字符串/不存在返回 None。"""
    return _handle_map(data).resolve(handle)


def decode_cell_deref(data, cell):
    """与 decode_cell 相同，但对 'ref' 单元格会解引用取共享串的真实值。"""
    if cell is None:
        return 0
    if cell[0] == 'str':
        v = qe_dec(cell[1])
        return v if v is not None else 0
    if cell[0] == 'ref':
        raw = resolve_handle(data, cell[1])
        v = qe_dec(raw) if raw is not None else None
        return v if v is not None else 0
    return 0


def read_guaiwu_family(data, target_families):
    """局部遍历 GameGuaiWu，返回 {familyid: {'level':int, 'count':int, 'adv':int, ...}}。
    adv = 进阶等级(index 17)；a9 = 属性[9](index 9)；
    sk1/sk2/sk3 = 技能1/2/3 等级(index 6/7/8)。"""
    start, N = locate_guaiwu(data)
    if start is None:
        return {}, None
    out = {}
    p = start
    for fam in range(N):
        vals, p = parse_inner_array(data, p)
        if fam in target_families and isinstance(vals, list):
            lvl = decode_cell(vals[0]) if len(vals) > 0 else None
            cnt = decode_cell(vals[1]) if len(vals) > 1 else None
            adv = decode_cell(vals[17]) if len(vals) > 17 else None
            a9 = decode_cell(vals[9]) if len(vals) > 9 else None
            form5 = decode_cell_deref(data, vals[5]) if len(vals) > 5 else None
            sk1 = decode_cell(vals[6]) if len(vals) > 6 else None
            sk2 = decode_cell(vals[7]) if len(vals) > 7 else None
            sk3 = decode_cell(vals[8]) if len(vals) > 8 else None
            out[fam] = {'level': lvl, 'count': cnt, 'adv': adv, 'attr9': a9,
                        'form5': form5,
                        'sk1': sk1, 'sk2': sk2, 'sk3': sk3}
        if not isinstance(vals, list) and vals not in (None, 'ARRAY_REF'):
            break
    return out, N


GUAIWU_FIELD_INDEX = {
    'level': 0,
    'count': 1,
    'form5': 5,
    'sk1': 6,
    'sk2': 7,
    'sk3': 8,
    'attr9': 9,
    'adv': 17,
}


def read_all_guaiwu(data):
    """读取 GameGuaiWu 的全部 family，而不只读取当前上阵的三只。

    每个字段同时返回解引用后的数值与原始存储形态（str/ref/null），GUI 据此决定
    是否允许安全修改。null 不会被自动扩成新对象，避免改变 Java handle 数量。
    """
    start, count = locate_guaiwu(data)
    if start is None:
        return {'offset': None, 'count': 0, 'families': []}
    names = load_family_names()
    parsed = []
    ref_counts = {}
    p = start
    for family_id in range(count):
        cells, p = parse_inner_array(data, p)
        parsed.append(cells)
        if isinstance(cells, list):
            for cell in cells:
                if cell and cell[0] == 'ref':
                    ref_counts[cell[1]] = ref_counts.get(cell[1], 0) + 1

    hm = _handle_map(data)
    offset_to_handle = {info.get('off'): handle for handle, info in hm.by_handle.items()
                        if info.get('kind') == 'string'}
    families = []
    for family_id, cells in enumerate(parsed):
        if not isinstance(cells, list):
            families.append({
                'familyid': family_id,
                'name': names.get(family_id, '?'),
                'kind': family_id * 5,
                'present': False,
                'fields': {},
            })
            continue
        fields = {}
        for key, index in GUAIWU_FIELD_INDEX.items():
            cell = cells[index] if index < len(cells) else None
            storage = cell[0] if cell else 'missing'
            inline_handle = (offset_to_handle.get(cell[2])
                             if cell and storage == 'str' and len(cell) > 2 else None)
            incoming_refs = ref_counts.get(inline_handle, 0) if inline_handle is not None else 0
            fields[key] = {
                'index': index,
                'value': decode_cell_deref(data, cell),
                'storage': storage,
                'incoming_references': incoming_refs,
                # 被其它单元格引用的内联 String 不能单独改；否则所有引用者一起变化。
                # 未共享 str 可原位替换，ref 可等长重定向，两者都不改变 handle 数量。
                'editable': storage == 'ref' or (storage == 'str' and incoming_refs == 0),
            }
        families.append({
            'familyid': family_id,
            'name': names.get(family_id, '?'),
            'kind': family_id * 5,
            'present': True,
            'cell_count': len(cells),
            'fields': fields,
        })
    return {'offset': start, 'count': count, 'families': families}


def read_cards(data):
    """读取 3 张上阵卡片: [{slot, kind, familyid, name, level, count}, ...]"""
    names = load_family_names()
    poff, kinds = locate_player(data)
    if kinds is None:
        return None
    families = [k // 5 for k in kinds]
    guaiwu, N = read_guaiwu_family(data, set(families))
    cards = []
    for slot, (kind, fam) in enumerate(zip(kinds, families)):
        info = guaiwu.get(fam, {})
        cards.append({
            'slot': slot + 1,
            'kind': kind,
            'familyid': fam,
            'name': names.get(fam, '?'),
            'level': info.get('level'),
            'count': info.get('count'),
            'adv': info.get('adv'),
            'attr9': info.get('attr9'),
            'form5': info.get('form5'),
            'sk1': info.get('sk1'),
            'sk2': info.get('sk2'),
            'sk3': info.get('sk3'),
        })
    return {'player_off': poff, 'guaiwu_N': N, 'cards': cards}


def _player_hero_level_scan(data):
    """新版存档玩家等级定位（2026-07-21 后的格式）。

    玩家等级不再是 QWERTY TC_STRING，而是 player_Hero[0] 元素内一段
    TC_BLOCKDATA(0x77 0x0c, 12 字节) 中的【第 2 个】 4 字节大端 int，
    布局为 [0, level, 5]（第 1 个恒为 0，第 3 个为固定值/数量）。

    anchor 取上阵槽 int[3] 数组([I)结束位置，向后找第一个 77 0c blockdata。
    校验：blockdata 第 1 个 int == 0 且等级落在 1~9999，避免误匹配其它 blockdata。

    返回 dict{off, value}（off 指向等级 int 起点），否则 None。
    """
    po, _ = locate_player(data)
    if po is None:
        return None
    # 跳过 TC_ARRAY / classdesc / suid / flags / fieldcount / endblock / super
    pp = po + 1
    if pp + 1 >= len(data) or data[pp] != 0x72:
        return None
    pp += 1 + 2 + 2          # 0x72 0x00 0x02 + "[I"(2)
    pp += 8                  # suid
    pp += 1                  # flags
    pp += 2                  # fieldcount
    if pp < len(data) and data[pp] == 0x78:
        pp += 1
    if pp < len(data) and data[pp] == 0x70:
        pp += 1
    if pp + 4 > len(data):
        return None
    alen = int.from_bytes(data[pp:pp + 4], 'big')
    arr_end = pp + 4 + alen * 4

    # 从 [I 数组结束向后找第一个 77 0c blockdata
    marker = b'\x77\x0c'
    j = data.find(marker, arr_end)
    while j >= 0:
        if j + 2 + 0x0c <= len(data):
            bd = data[j + 2:j + 2 + 0x0c]
            i0, i1, i2 = (int.from_bytes(bd[k:k + 4], 'big') for k in (0, 4, 8))
            if i0 == 0 and 1 <= i1 <= 9999:
                return {'off': j + 2 + 4, 'value': i1}
        j = data.find(marker, j + 1)
    return None


def locate_player_hero_level(data):
    """定位玩家等级（真实值 = player_Hero 上阵英雄行 col1 的 QWERTY 串）。

    存档中有两个相关结构：
      (A) player_Hero 全量列表 [[Ljava.lang.String;：行数很多（如 77），
          每行 19 列，其中 col1 是各英雄【经验】而非等级。
      (B) 上阵英雄【快照】：一个仅 1 行、4 列的 String[][]，结构为
          `00 00 00 01`(行数=1) + `75 71 ?? ?? ?? ??`(行引用)
          + `00 00 00 04`(4 列) + col0(kind 标记) + col1(等级 QWERTY)
          + col2(经验) + col3(引用)。col1 即玩家等级。

    kind 标记内容随账号随机（实测有 EQIQ / EQQQ / EERQ 等），不可依赖，
    故改用结构特征 (B) 定位，对多个存档稳健。

    ⚠ 切勿用 _player_hero_level_scan 的裸 int 分支——它错读 blockdata
    里的 people_num（实测 48 级账号读到 11，692 级账号读到 50）。

    返回 dict{off, text, value}（off 指向等级 TC_STRING 起点）或 None。
    调用方写入走 plan_player_hero_level_patch 的 QWERTY TC_STRING 分支。
    """
    import re
    # 结构特征 (B)：1 行、4 列的 String[][] 快照
    pat = re.compile(rb'\x00\x00\x00\x01\x75\x71....\x00\x00\x00\x04')
    for m in pat.finditer(data):
        p = m.end()
        # col0 = kind 标记（74 00 04 ...）
        if p + 3 > len(data) or data[p] != 0x74:
            continue
        slen0 = (data[p + 1] << 8) | data[p + 2]
        p2 = p + 3 + slen0
        # col1 = 等级
        if p2 + 3 > len(data) or data[p2] != 0x74:
            continue
        slen1 = (data[p2 + 1] << 8) | data[p2 + 2]
        if p2 + 3 + slen1 > len(data):
            continue
        text = data[p2 + 3:p2 + 3 + slen1].decode('ascii', errors='replace')
        if not all(c in QWERTY for c in text):
            continue
        val = qe_dec(text)
        if val is not None and 1 <= val <= 9_999_999_999:
            return {'off': p2, 'text': text, 'value': val}

    return None


def qe_enc(v):
    """整数 -> QWERTY 串（自然位数，无前导零）"""
    if v < 0:
        raise ValueError("不支持负数")
    return ''.join(QWERTY[int(d)] for d in str(v))


def locate_soco_uid(data, anchor_uid=None):
    """定位 soco.bin 内 Netdata_uid（账号归属 uid）的 8 字节大端 long。

    soco 中该字段以裸 writeLong 形式存储（高 32 位恒为 0），位于一个
    TC_BLOCKDATA(0x77 0x1f) 之后的 blockdata 内。

    定位策略：
      1. 若给出 anchor_uid（通常来自同目录 NetData.bin），直接在 soco 中搜索其
         8 字节大端编码，命中即返回（最准确）。
      2. 否则扫描所有 0x77 0x1f 00 00 00 00 块，取其后第 8 字节起的 8 字节 long
         作为候选（兜底；uid 高 32 位为 0，故前面有 4 个 0x00）。

    返回 dict{off, value}（off 指向 8 字节 long 起点），否则 None。
    """
    # 策略 1：anchor 精确匹配
    if anchor_uid is not None:
        pat = int(anchor_uid).to_bytes(8, 'big')
        i = data.find(pat)
        if i >= 0:
            return {'off': i, 'value': anchor_uid}

    # 策略 2：blockdata 兜底
    marker = bytes([0x77, 0x1f, 0x00, 0x00, 0x00, 0x00])
    i = data.find(marker)
    while i >= 0:
        p = i + len(marker)
        # blockdata 内 uid 起点大致在 marker 后若干字节；尝试 marker 后 0~16 字节偏移
        for off in range(0, 17):
            base = p + off
            if base + 8 <= len(data):
                val = int.from_bytes(data[base:base + 8], 'big')
                # uid 合理性：高 4 字节为 0 且值足够大
                if data[base] == 0 and data[base + 1] == 0 and data[base + 2] == 0 and data[base + 3] == 0:
                    if 10 ** 12 <= val <= 10 ** 19:
                        return {'off': base, 'value': val}
        i = data.find(marker, i + 1)
    return None


# ─────────────────────────────────────
#  sidecar（偏移映射）
# ─────────────────────────────────────
def load_sidecar(path):
    sp = path + SIDECAR_SUFFIX
    if not os.path.exists(sp):
        return None
    try:
        with open(sp, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def sidecar_valid(data, m):
    if not m:
        return False
    crc_stored = m.get("crc")
    if crc_stored is None:
        return False
    if isinstance(crc_stored, str):
        crc_stored = int(crc_stored, 16)
    return (int.from_bytes(data[0:4], 'big') & 0xffffffff) == (crc_stored & 0xffffffff)


def read_field_from_sidecar(data, fe):
    name = fe["name"]
    off = fe["off"]
    if fe.get("enc") == "qwerty":
        L, raw = parse_tc(data, off - 3)
        if L is None:
            # 该字段可能是引用/重定向形态(offset 指向 0x71/0x70 而非 TC_STRING)，
            # 回退到结构法精确解析（按句柄规则解引用）。
            if name == "钻石":
                d = find_diamond(data, scan_tc(data))
                if d:
                    return d
            return None
        return dict(name=name, off=off, val=qe_dec(raw), raw=raw, enc=raw.decode('ascii', 'replace'))
    if fe.get("enc") == "long8":
        if off + 8 > len(data):
            return None
        val = int.from_bytes(data[off:off + 8], 'big')
        return dict(name=name, off=off, val=val, raw=data[off:off + 8], enc="(long)%d" % val)
    if off + 4 > len(data):
        return None
    val = int.from_bytes(data[off:off + 4], 'big')
    return dict(name=name, off=off, val=val, raw=data[off:off + 4], enc="(裸整数)%d" % val)


# ─────────────────────────────────────
#  主流程
# ─────────────────────────────────────
EXPECTED = ['金币', '钻石', '木头', '石头', '结晶', '钥匙数量']


def main():
    global SOCO_PATH
    args = sys.argv[1:]
    if '--file' in args:
        i = args.index('--file')
        if i + 1 < len(args):
            SOCO_PATH = args[i + 1]
            args = args[:i] + args[i + 2:]
    json_mode = '--json' in args
    detail_mode = '--detail' in args

    if not os.path.exists(SOCO_PATH):
        print(f"[ERROR] 未找到: {SOCO_PATH}")
        sys.exit(1)

    with open(SOCO_PATH, 'rb') as f:
        data = f.read()

    tcs = scan_tc(data)
    sidecar = load_sidecar(SOCO_PATH)
    use_sidecar = sidecar_valid(data, sidecar)
    playername = find_playername(data)

    rows = []
    if use_sidecar:
        for name in EXPECTED:
            fe = sidecar.get("fields", {}).get(name)
            if fe:
                r = read_field_from_sidecar(data, fe)
                if r:
                    rows.append(r)
    else:
        gold = find_gold(data, tcs)
        diamond = find_diamond(data, tcs)
        mats = find_materials(data)
        keys = find_keys(data)
        for f in [gold, diamond]:
            if f:
                rows.append(f)
        for m in mats:
            if m:
                rows.append(m)
        if keys:
            rows.append(keys)

    if json_mode:
        result = {r['name']: {'off': f"0x{r['off']:X}", 'val': r['val'],
                              'display': fmt_cn(r['val']), 'enc': r['enc']}
                  for r in rows}
        if playername:
            result['玩家昵称'] = {'off': f"0x{playername['off']:X}",
                                'val': playername['val'], 'enc': playername['enc']}
        cards = read_cards(data)
        if cards:
            result['cards'] = [
                {'slot': c['slot'], 'familyid': c['familyid'], 'name': c['name'],
                 'kind': c['kind'], 'level': c['level'], 'count': c['count'],
                 'adv': c['adv'], 'attr9': c['attr9'],
                 'sk1': c['sk1'], 'sk2': c['sk2'], 'sk3': c['sk3']}
                for c in cards['cards']
            ]
        detail = analyze_save(data, SOCO_PATH, include_equipment=True)
        result['_meta'] = {
            'size': detail['size'],
            'stored_crc32': detail['stored_crc32'],
            'calculated_crc32': detail['calculated_crc32'],
            'crc_valid': detail['crc_valid'],
            'stream_magic': detail['stream_magic'],
            'save_format': detail['save_format'],
        }
        result['ziyuan'] = detail['resources']
        result['isShow'] = detail['isShow']
        result['player_jiesuo'] = detail['player_jiesuo']
        result['tower'] = detail['tower']
        result['equipment'] = detail['equipment']
        result['monster_families'] = detail['monster_families']
        result['castle'] = detail['castle']
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print("=" * 74)
    print("  Merge to Summon 存档读取工具")
    print(f"  文件: {os.path.basename(SOCO_PATH)}  大小: {len(data)} 字节"
          + ("  [sidecar]" if use_sidecar else ""))
    if playername:
        extra = f"  @0x{playername['off']:X}" if detail_mode else ""
        disp = playername['val'] if playername['val'] else "(空/未设置)"
        print(f"  玩家昵称: {disp}{extra}")
    else:
        print("  玩家昵称: (未定位)")
    print("=" * 74)
    print(f"\n  {'字段':<10s} {'偏移':<9s} {'当前值':<16s} {'游戏显示':<10s} {'编码':<12s}")
    print("  " + "-" * 62)
    for r in rows:
        print(f"  {r['name']:<10s} 0x{r['off']:<7X} {str(r['val']):<16s} {fmt_cn(r['val']):<10s} {r['enc']:<12s}")

    if detail_mode:
        stored_crc = int.from_bytes(data[:4], 'big')
        calculated_crc = zlib.crc32(data[4:]) & 0xffffffff
        print(f"\n  CRC32: {stored_crc:08x}  计算值: {calculated_crc:08x}  "
              f"({'有效' if stored_crc == calculated_crc else '无效'})")
        print(f"  Java流: {data[4:8].hex()}  格式: {detect_save_format(data)}")
        print("\n  GameData.ziyuan 完整资源数组")
        print(f"  {'ID':<4s} {'名称':<16s} {'数值':<16s} {'形态':<7s} {'偏移':<10s} 编码")
        print("  " + "-" * 74)
        for resource in read_ziyuan_details(data):
            print(f"  {resource['id']:<4d} {resource['name']:<16s} "
                  f"{str(resource['value']):<16s} {resource['kind']:<7s} "
                  f"0x{resource['tc_off']:<8X} {resource.get('encoded') or '-'}")

        tower = find_tower_data(data) or {}
        print("\n  EndLessTowerData 固定字段")
        print(f"  {'字段':<24s} {'类型':<7s} {'数值':<18s} 偏移")
        for field in tower.get('fields') or []:
            print(f"  {field['name']:<24s} {field['type']:<7s} "
                  f"{str(field['value']):<18s} 0x{field['offset']:X}")

        monster = read_all_guaiwu(data)
        present = [row for row in monster.get('families', []) if row.get('present')]
        print(f"\n  GameGuaiWu 全部家族: {len(present)}/{monster.get('count', 0)}")
        print(f"  {'family':<7s} {'名称':<16s} {'kind':<6s} {'等级':<7s} {'卡数':<9s} "
              f"{'进阶':<7s} {'形态':<7s} {'熟练度':<9s} 技能1/2/3")
        for row in present:
            fields = row['fields']
            value = lambda key: fields[key]['value']
            print(f"  {row['familyid']:<7d} {row['name']:<16s} {row['kind']:<6d} "
                  f"{value('level')!s:<7s} {value('count')!s:<9s} {value('adv')!s:<7s} "
                  f"{value('form5')!s:<7s} {value('attr9')!s:<9s} "
                  f"{value('sk1')}/{value('sk2')}/{value('sk3')}")

        castle = read_castle_overview(data)
        print(f"\n  CastleData: 资源 {castle.get('resource_count', 0)} 项 / "
              f"建筑 {len(castle.get('buildings') or [])} 项 / "
              f"伙伴 {castle.get('guardian_count', 0)} / 装备 {castle.get('partner_equipment_count', 0)}")
        print(f"  {'ID':<7s} {'名称':<18s} {'数值':<18s} 偏移")
        for resource in castle.get('resources') or []:
            off = f"0x{resource['offset']:X}" if resource.get('offset') is not None else '-'
            print(f"  {resource['id']:<7d} {resource['name']:<18s} "
                  f"{str(resource['value']):<18s} {off}")

    missing = [n for n in EXPECTED if n not in [r['name'] for r in rows]]
    if missing:
        print(f"\n  ⚠ 未找到: {', '.join(missing)}")
        if not use_sidecar:
            print("    (提示: 改档后请先用 soco_write.py 写入，它会生成 .map.json 供正确回读)")

    # ── 上阵卡片 ──
    cards = read_cards(data)
    print("\n" + "=" * 74)
    print("  上阵卡片（familyid / 等级 / 卡片数 / 进阶等级 / 属性[9] / 技能1~3 等级）")
    if cards is None:
        print("  ⚠ 未定位到 player[]（上阵槽）")
    else:
        print(f"  (player@0x{cards['player_off']:X}, GameGuaiWu 族数={cards['guaiwu_N']})")
        print(f"\n  {'槽':<3s} {'familyid':<8s} {'名称':<12s} {'kind':<5s} {'等级':<5s} {'卡数':<7s} {'进阶':<5s} {'属9':<6s} {'技1':<5s} {'技2':<5s} {'技3':<5s}")
        print("  " + "-" * 76)
        for c in cards['cards']:
            print(f"  {c['slot']:<3d} {c['familyid']:<8d} {c['name']:<12s} "
                  f"{c['kind']:<5d} {str(c['level']):<5s} {str(c['count']):<7s} "
                  f"{str(c['adv']):<5s} {str(c['attr9']):<6s} {str(c['sk1']):<5s} "
                  f"{str(c['sk2']):<5s} {str(c['sk3']):<5s}")


# ─────────────────────────────────────
#  冒险之王 / EndLessTowerData 解析
# ─────────────────────────────────────

# EndLessTowerData writeExternal 字段顺序（索引从0开始）
TOWER_FIELD_ORDER = [
    # (index, type, name)
    (0, 'int', 'refreshTimes'),
    (1, 'int', 'freeRefreshTimes'),
    (2, 'float', 'totalClearScore'),
    (3, 'int', 'reviveChances'),
    (4, 'int', 'currentScore'),         # ← 目标1
    (5, 'int', 'playDays'),
    (6, 'int', 'previousPlayDays'),
    (7, 'int', 'seasonID'),
    (8, 'int', 'previousScore'),
    (9, 'bool', 'isXiaoChuComplete'),
    (10, 'bool', 'isCollected'),
    (11, 'bool', 'hasNotice'),
    (12, 'long', 'seed'),
    (13, 'int', 'failedTimes'),
    (14, 'int', 'freeTimes'),
    (15, 'int', 'unlockedStage'),       # ← 目标2
    (16, 'int', 'currentSubStage'),
]


def find_tower_data(data):
    """解析 soco.bin 中的 EndLessTowerData 的 Externalizable 序列化数据。

    返回 dict {currentScore, unlockedStage, off} 或 None。
    """
    # TC_OBJECT + TC_CLASSDESC + 类名 com.zhaohe.game.tower.EndLessTowerData
    needle = b'\x73\x72\x00\x26com.zhaohe.game.tower.EndLessTowerData'
    pos = data.find(needle)
    if pos < 0:
        return None

    # 跳过 73 72 00 26 + 38字节类名 + 8字节SUID + 1字节flags + 2字节nfields
    suid_pos = pos + 4 + 38
    nfields = data[suid_pos + 9] << 8 | data[suid_pos + 10]
    p = suid_pos + 11

    # 跳过字段描述（nfields 个）
    for _ in range(nfields):
        name_len = data[p + 1]
        p += 2 + name_len

    # TC_ENDBLOCKDATA + super (TC_NULL)
    if p < len(data) and data[p] == 0x78:
        p += 1
    if p < len(data) and data[p] == 0x70:
        p += 1

    # 读取第一个 blockdata 段（包含前 15 个字段）
    if p >= len(data) or data[p] != 0x77:
        return None
    blen = data[p + 1]
    block = data[p + 2:p + 2 + blen]

    current_score = None
    unlocked_stage = None
    current_score_off = None
    unlocked_stage_off = None
    fields = []
    bp = 0
    for idx, typ, name in TOWER_FIELD_ORDER:
        field_off = p + 2 + bp
        if typ == 'int':
            if bp + 4 > len(block):
                break
            val = struct.unpack('>i', block[bp:bp+4])[0]
            bp += 4
        elif typ == 'float':
            if bp + 4 > len(block):
                break
            val = struct.unpack('>f', block[bp:bp+4])[0]
            bp += 4
        elif typ == 'bool':
            if bp >= len(block):
                break
            val = block[bp]
            bp += 1
        elif typ == 'long':
            if bp + 8 > len(block):
                break
            val = struct.unpack('>q', block[bp:bp+8])[0]
            bp += 8
        else:
            break

        fields.append({
            'index': idx,
            'name': name,
            'type': typ,
            'value': bool(val) if typ == 'bool' else val,
            'offset': field_off,
            'width': {'int': 4, 'float': 4, 'bool': 1, 'long': 8}[typ],
            'editable': True,
        })

        if name == 'currentScore':
            current_score = val
            current_score_off = field_off
        elif name == 'unlockedStage':
            unlocked_stage = val
            unlocked_stage_off = field_off

    result = {}
    result['fields'] = fields
    if current_score is not None:
        result['currentScore'] = current_score
    if unlocked_stage is not None:
        result['unlockedStage'] = unlocked_stage
    if current_score_off is not None:
        result['currentScore_off'] = current_score_off
        result['currentScore_len'] = 4
    if unlocked_stage_off is not None:
        result['unlockedStage_off'] = unlocked_stage_off
        result['unlockedStage_len'] = 4
    if not result:
        return None
    result['off'] = pos
    return result


def find_kaka(data):
    """定位「开卡」相关字段：永恒契约(=无限卡 wuxiankan) / 月卡数量 yuekanum / 年卡天数 annualCardDays。

    依据 GameData.writefile 的写入顺序（0810 版与容器存档一致）：

      1) 月卡/无限卡区：
         writeObject(renwu:[B) 之后紧跟 14 字节 blockdata(77 0e):
             long renwuTime(8) | bool isfenxiang(1) | int yuekanum(4) | bool wuxiankan(1)
         锚点：77 0e + 前 8 字节是合理毫秒时间戳 + isfenxiang/wuxiankan ∈ {0,1}
         ⚠ 永恒契约的开通标志是 wuxiankan（不是 annualCardDays）。

      2) 年卡区：… PushMessageData → CollectionData 对象 → 12 字节 blockdata(77 0c) → HashMap
         块内 +6 = annualCardDays(int)

    返回 dict 或 None；每个字段为 {'offset', 'value', 'type'}。
    """
    TS_MIN = 1_200_000_000_000
    TS_MAX = 3_500_000_000_000
    result = {}

    # ── 1) yuekanum / wuxiankan ──
    # 该 blockdata 必须紧跟在一个 byte[] 对象之后：
    #   75 71 00 7e XX XX | 00 00 00 N | <N 字节> | 77 0e ...
    # 用"数组长度 N 正好把游标推到 77 0e"来排除其它 14 字节块（实测必需）。
    def _preceded_by_byte_array(p):
        limit = max(0, p - 4096)
        q = data.rfind(b'\x75\x71\x00\x7e', limit, p)
        while q >= 0:
            size = struct.unpack('>i', data[q + 6:q + 10])[0]
            if 0 < size <= 4096 and q + 10 + size == p:
                return True
            q = data.rfind(b'\x75\x71\x00\x7e', limit, q)
        return False

    pat = b'\x77\x0e'
    i = data.find(pat)
    while i >= 0:
        blk = i + 2
        if blk + 14 > len(data):
            break
        ts = struct.unpack('>q', data[blk:blk + 8])[0]
        isf = data[blk + 8]
        wxk = data[blk + 13]
        yk = struct.unpack('>i', data[blk + 9:blk + 13])[0]
        if (TS_MIN <= ts <= TS_MAX and isf in (0, 1) and wxk in (0, 1)
                and 0 <= yk <= 1_000_000 and _preceded_by_byte_array(i)):
            result['yuekanum'] = {
                'offset': blk + 9,
                'value': yk,
                'type': 'int',
            }
            result['wuxiankan'] = {
                'offset': blk + 13,
                'value': bool(wxk),
                'type': 'bool',
            }
            result['block_off'] = i
            break
        i = data.find(pat, i + 1)

    # ── 2) annualCardDays ──
    ci = data.find(b'CollectionData')
    if ci >= 0:
        j = data.find(b'\x77\x0c', ci, min(len(data), ci + 400))
        if j >= 0:
            off = j + 2 + 6
            if off + 4 <= len(data):
                result['annualCardDays'] = {
                    'offset': off,
                    'value': struct.unpack('>i', data[off:off + 4])[0],
                    'type': 'int',
                }

    return result or None


if __name__ == '__main__':
    main()
