#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Merge to Summon (com.zhaohe.game522) 存档修改工具

与 soco_read.py 配套的【写入】工具。按特征搜索定位字段，将新值写回 soco.bin。

支持字段: 金币 / 钻石 / 木头 / 石头 / 结晶 / 钥匙数量 / 卡片数量 / 进阶等级 / 熟练度[9] / 等级 / 上阵卡片槽(kind) / 周限宝箱剩余次数

★ 核心原理（可进游戏，不闪退）
  存档是 Java 序列化流。每个【新对象】(内联串/数组/对象/类描述符) 在反序列化时按出现
  顺序分配一个 handle 序号；后续 TC_REFERENCE(0x71+4B handle) 都按序号引用这些对象。
  ★ 致命陷阱: 把"引用/空"形态字段直接替换成"内联串" => 凭空多注册一个对象 =>
    后续所有引用序号整体错位 => 某个本应指向类描述符(ObjectStreamClass)的引用错指到
    String => `java.lang.String cannot be cast to java.io.ObjectStreamClass` 闪退。
  ★ 安全策略:
      - 金币 / 钻石为【内联 TC_STRING】: 原地覆盖或扩宽（inline->inline，不新增对象），安全。
      - 钻石为【引用 ref / 空 null】且 目标值==金币值: 改为【引用金币内联串】(共用句柄，
        零新增对象)，安全。金币串即钻石值，结果完全正确。
      - 钻石为【引用/空】且 目标值≠金币值: 直接改会破坏句柄 => 报错（需全量序列化重排，未实现）。
        变通: 先 `金币 <同值>` 再 `钻石 <同值>`，本工具会自动走安全重定向。

  - 金币 / 钻石: QWERTY 编码的 TC_STRING（74 00 [L] [L字节]，长度前缀 L 单字节，上限 255）。
      新值位数 <= 原宽 L: 前导 'Q'(=0) 补零到 L，零位移。
      新值位数 >  原宽 L: 【扩宽】该 TC_STRING（width=新位数、后续字节整体后移、重算 CRC32）。
          inline->inline 不新增对象，Java 用 handle 引用而非偏移，位移无妨，安全。
      ★ 无显示缩放: 金币【存储值 == 游戏显示值】，游戏仅按 万/亿/万亿 做单位美化。
        想显示 X 金，直接写 X（如 900亿 -> 写 90000000000）。
  - 木头 / 石头 / 结晶 / 钥匙数量: CastleResource 的 `long r`（8 字节大端整数），
      定长覆盖，零位移。resourceID = 3101/3102/3103（材料）、3992（钥匙）。
      例: 木头设 123 -> 8 字节 00 00 00 00 00 00 00 7B。

  ★ 校验和: 文件头 4 字节 = 整个 payload (bytes[4:]) 的 CRC32。
      任何内容改动都必须重算并写回这 4 字节，否则游戏校验失败闪退。

  ★ 基准档: 始终就地改脚本同目录的 soco.bin（保留所有未指定字段）；
      不读取任何其它目录的存档，也不需要 --source 参数。

  ★ sidecar: 写入后生成 soco.bin.map.json，记录各字段最终偏移与文件 CRC，
      供 soco_read.py 改档后正确回读。

用法:
  python3 soco_write.py 金币 90000000000            # 900亿（存储值=显示值，无缩放）
  python3 soco_write.py 钻石 5566 金币 667755
  python3 soco_write.py 钻石 2680 --dry            # 仅预览，不写入
  python3 soco_write.py 金币 123 --out new.bin     # 写到新文件（不影响原档）
  python3 soco_write.py f1 卡片数量 2026           # 改 family 1 卡片数 -> 2026
  python3 soco_write.py f-1 卡片数量 0             # 所有家族卡片数 -> 0
  python3 soco_write.py f1 2026                    # 省略关键字写法（等价上）
  python3 soco_write.py zf1 99                     # 改 family 1 进阶等级 -> 99
  python3 soco_write.py zf-1 0                     # 所有家族进阶等级 -> 0
  python3 soco_write.py zp1 2132                   # 改 family 1 熟练度[9] -> 2132
  python3 soco_write.py zp-1 0                     # 所有家族熟练度[9] -> 0
  python3 soco_write.py mlv1 108                   # 改 family 1 等级 -> 108
  python3 soco_write.py mlv-1 0                    # 所有家族等级 -> 0
  python3 soco_write.py solt1 375                 # 槽1 kind -> 375（即 familyid 75 = 哪吒）
  python3 soco_write.py solt-1 75                 # 所有槽 kind -> 75（即 familyid 15）
  python3 soco_write.py uname 反坦克炮239         # 改玩家昵称(playerName) -> 反坦克炮239
  python3 soco_write.py uname 我的昵称 --dry      # 预览昵称修改（不写入）
"""

import os
import sys
import re
import shutil
import json
import zlib
import struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soco_read as R
import soco_baoxiang as BX   # 周限宝箱(神秘宝箱 ShenMiBaoXiangData) 开启次数 定位/修改

INT32_MAX = 2147483647
KEY_MAX = 100_000_000
LONG_MAX = 9_223_372_036_854_775_807
# 编码: 金币/钻石 = QWERTY TC_STRING; 木头/石头/结晶/钥匙 = CastleResource 的 8字节 long r
ENC_MAP = {'金币': 'qwerty', '钻石': 'qwerty',
           '木头': 'long8', '石头': 'long8', '结晶': 'long8', '钥匙数量': 'long8'}

# ★ 操作对象: 始终为脚本同目录的 soco.bin（R.SOCO_PATH），就地修改、保留所有未指定字段。
#    不读取任何其它目录的存档，也不提供 --source 选项。


def qe_enc(v):
    """整数 -> QWERTY 串（自然位数，无前导零）。如 1000000 -> 'WQQQQQQ'"""
    if v < 0:
        raise ValueError("不支持负数")
    return ''.join(R.QWERTY[int(d)] for d in str(v))


def current_values(data, path):
    """复用 soco_read 的定位器，返回 {字段名: 定位 dict}。
    优先 sidecar（CRC 校验一致），否则走启发式。"""
    tcs = R.scan_tc(data)
    sidecar = R.load_sidecar(path)
    if R.sidecar_valid(data, sidecar):
        out = {}
        for name in ENC_MAP:
            fe = sidecar.get("fields", {}).get(name)
            if fe:
                out[name] = R.read_field_from_sidecar(data, fe)
        if all(out.get(k) for k in ['金币', '钻石', '木头', '钥匙数量']):
            return out
    gold = R.find_gold(data, tcs)
    diamond = R.find_diamond(data, tcs)
    mats = R.find_materials(data)
    keys = R.find_keys(data)
    return {
        '金币': gold,
        '钻石': diamond,
        '木头': mats[0] if len(mats) > 0 else None,
        '石头': mats[1] if len(mats) > 1 else None,
        '结晶': mats[2] if len(mats) > 2 else None,
        '钥匙数量': keys,
    }


def plan_patch(entry, new_val, data, gold_val=None):
    """返回 (span_start, old_len, new_bytes)。

    ★★★ 序列化句柄安全（关键，否则游戏闪退）★★★
    该存档是 Java 序列化流。每个【新对象】(内联串/数组/对象/类描述符) 在读取时会被
    分配一个 handle 序号；后续出现的 TC_REFERENCE(0x71+4B handle) 都按序号引用这些对象。
    若把"引用"原位替换成"内联串"，等于凭空多注册了一个对象 -> 后续所有引用序号整体
    错位 -> 某个本应指向类描述符(ObjectStreamClass)的引用错指到 String ->
    `java.lang.String cannot be cast to java.io.ObjectStreamClass` 闪退。
    因此：钻石若当前是【引用/空】形态，绝不能直接插内联串。

    金币/钻石:
      - 金币(ziyuan[0]) / 钻石为内联(inline): 原地覆盖/扩宽 TC_STRING。
            inline->inline 不改变对象数、不新增句柄，绝对安全（仅原位变长，后续整体位移，
            Java 用 handle 引用而非偏移，位移无妨）。
      - 钻石为【引用 ref】或【空 null】: 直接插内联串会新增句柄 -> 闪退。
           安全做法：若 目标值 == 金币当前值，把钻石【重定向为引用金币的内联串】
           (共用 handle#2，零新增句柄)。ref 情形 5->5 字节零位移；null 情形 1->5 字节仅位移。
           金币值本身就是该内联串，所以钻石读到的值 == 金币值，完全正确。
           若 目标值 != 金币值：本形态无法直接安全修改（需全量序列化重排，未实现），报错。
    """
    name = entry['name']
    if name in ('金币', '钻石'):
        kind = entry.get('kind', 'inline')
        s = qe_enc(new_val)
        if kind == 'inline':
            # 内联串：原地覆盖/扩宽，零新增句柄，绝对安全。
            cur_width = len(entry['raw'])             # 原始字段宽度（QWERTY 串）
            if len(s) <= cur_width:
                width = cur_width
                enc = ('Q' * (width - len(s))) + s
                new_bytes = b'\x74\x00' + bytes([width]) + enc.encode('ascii')
                return entry['off'] - 3, 3 + cur_width, new_bytes
            else:
                width = len(s)
                new_bytes = b'\x74\x00' + bytes([width]) + s.encode('ascii')
                return entry['off'] - 3, 3 + cur_width, new_bytes
        # —— ref / null 形态：采用句柄安全重定向 ——
        # gold_val: 默认从 data 取金币当前值；批量修改时编辑器会传入
        #           「本批次金币的新值」，使钻石能正确判定重定向（避免同时改
        #           金币+钻石为同值时，钻石校验仍看到旧金币值而误报）。
        if gold_val is None:
            try:
                tcs = R.scan_tc(data)
                g = R.find_gold(data, tcs)
                if g:
                    gold_val = g['val']
            except Exception:
                gold_val = None
        if gold_val is not None and new_val == gold_val:
            gh = R.ziyuan_gold_handle(data)
            if gh is None:
                raise ValueError("无法定位金币句柄，钻石重定向失败")
            hb = (0x7E0000 + gh).to_bytes(4, 'big')   # 金币内联串的 handle(完整值)
            new_bytes = b'\x71' + hb                   # 改成引用金币内联串
            old_len = 5 if kind == 'ref' else 1
            return entry['off'], old_len, new_bytes
        raise ValueError(
            f"钻石当前为{'引用' if kind=='ref' else '空'}形态，且目标值({new_val})≠金币值"
            f"({gold_val})。直接改会破坏 Java 序列化句柄导致闪退。\n"
            f"    安全做法：先把金币也设为 {new_val}（金币=钻石时本工具会自动让钻石引用金币串），"
            f"或换用钻石已非零(内联)的旧档再改。")
    # 8 字节大端 long r（CastleResource）
    if new_val > LONG_MAX:
        raise ValueError(f"数值 {new_val} 超过 long 上限 {LONG_MAX}")
    return entry['off'], 8, int(new_val).to_bytes(8, 'big')


def plan_ziyuan_patch(data, resource_id, new_val):
    """安全修改 GameData.ziyuan[resource_id]，返回标准 patch 元组。

    内联字符串可变长替换不会改变 handle 数量；引用/空槽只能重定向到资源数组中
    更早出现、且数值相同的内联字符串，避免新增 Java 序列化对象。
    """
    if new_val < 0:
        raise ValueError("资源数值不能为负数")
    if new_val > LONG_MAX:
        raise ValueError(f"数值 {new_val} 超过 long 上限 {LONG_MAX}")
    rows = R.read_ziyuan_details(data)
    if not (0 <= resource_id < len(rows)):
        raise ValueError(f"未定位到 ziyuan[{resource_id}]")
    row = rows[resource_id]
    encoded = qe_enc(new_val)
    if len(encoded) > 255:
        raise ValueError("QWERTY 编码长度超过 255 字节")

    if row['kind'] == 'str':
        old_width = len(row.get('encoded') or '')
        new_bytes = b'\x74\x00' + bytes([len(encoded)]) + encoded.encode('ascii')
        return ('ziyuan', f"{row['name']}[ID {resource_id}]", new_val,
                row['tc_off'], 3 + old_width, new_bytes, row['value'])

    # classdesc=#0、数组=#1，首个内联字符串从 handle #2 开始。
    inline_index = 0
    target_handle = None
    for previous in rows[:resource_id]:
        if previous['kind'] != 'str':
            continue
        if previous.get('value') == new_val:
            target_handle = 0x7E0000 + 2 + inline_index
            break
        inline_index += 1
    if target_handle is None:
        raise ValueError(
            f"ziyuan[{resource_id}] 当前为{row['kind']}，目标值 {new_val} 在前置资源中没有"
            "可复用的内联字符串。直接创建字符串会改变 handle 编号并导致存档损坏。")
    new_bytes = b'\x71' + target_handle.to_bytes(4, 'big')
    old_len = 5 if row['kind'] == 'ref' else 1
    return ('ziyuan', f"{row['name']}[ID {resource_id}]", new_val,
            row['tc_off'], old_len, new_bytes, row['value'])


def _reindex_refs_after_insert(data, insert_at, new_handle, min_handle):
    """在 insert_at 之后把 handle >= min_handle 的 TC_REFERENCE 全部 +1。

    ★ 仅用于「ziyuan 数组内在某槽位把 ref 换成内联 TC_STRING」这种
      会新增 1 个 handle 的场景。调用方必须自行做结构校验。

    风险控制：只重编号形如 0x71 + 4 字节且 handle 值落在 [min_handle, 0x7e0000+4096)
    的引用；超出范围的一律不动（多半是数据块内的巧合字节）。
    """
    d = bytearray(data)
    n = len(d)
    i = insert_at
    fixed = 0
    while i + 5 <= n:
        if d[i] == 0x71:
            hv = int.from_bytes(d[i + 1:i + 5], 'big')
            idx = hv - 0x7E0000
            if min_handle <= idx < 4096:
                d[i + 1:i + 5] = (0x7E0000 + idx + 1).to_bytes(4, 'big')
                fixed += 1
            i += 5
        else:
            i += 1
    return bytes(d), fixed


def verify_structure_intact(orig, new):
    """写入后结构校验：用 soco_read 各定位器比对关键结构是否仍可解析且值未变。

    返回 (ok, details)。ok=False 时应放弃写入。
    """
    import soco_read as _R
    checks = {
        'playername': lambda d: (_R.find_playername(d) or {}).get('val'),
        'phoneshouji': lambda d: (_R.locate_phoneshouji(d) or {}).get('phone'),
        'returnplayer': lambda d: (_R.locate_returnplayer(d) or {}).get('remaining_score'),
        'daxuanguan': lambda d: (_R.locate_daxuanguan(d) or {}).get('effective'),
        'fangchenmi': lambda d: (_R.locate_fangchenmi(d) or {}).get('name'),
        'jiesuo': lambda d: bytes((_R.find_player_jiesuo(d) or (None, b''))[1]),
    }
    details = {}
    ok = True
    for name, fn in checks.items():
        try:
            a = fn(orig)
            b = fn(new)
        except Exception as e:
            details[name] = f'ERR {e}'
            ok = False
            continue
        same = (a == b)
        details[name] = 'same' if same else f'CHANGED {a!r}->{b!r}'
        if not same:
            ok = False
    return ok, details


def plan_ziyuan_inline_patch(data, resource_id, new_val):
    """把 ziyuan[resource_id] 从 ref/null 升级为【内联字符串】并写入任意数值。

    ★ 与 plan_ziyuan_patch 的区别
      plan_ziyuan_patch  只能把 ref 重定向到数组中已存在的内联串（仅几个可选值）；
      本函数           就地写入内联 TC_STRING，因此支持任意数值。

    ★ 代价与安全
      内联字符串会分配 1 个新 handle，导致其后所有 TC_REFERENCE 编号 +1。
      本函数已自动完成重编号；但因为是启发式扫描（soco.bin 里大量类实现了
      Externalizable，字段块无通用规则，无法做完整流解析），
      调用方【必须】在写入后用 soco_read 的定位器做结构校验。

    返回 patch 列表（本函数可能返回 2 个元素：字符串替换 + 引用重编号）。
    为保持与既有 splice 框架兼容，这里返回的是一个"复合 patch"：
        (kind, label, new_val, start, old_len, new_bytes, old_val)
    其中 new_bytes 已是完整的新数据片段（含重编号后的后续内容），
    start=resource 槽位偏移，old_len=原槽位长度。
    """
    raise ValueError(
        "已停用：ref→内联串会新增 Java 序列化句柄，需要重编号其后全部引用。"
        "但 soco.bin 中大量类实现 Externalizable，无法完整解析流来确定每个 handle 归属，"
        "启发式重编号会漏改（实测导致游戏报 ClassCastException: String cannot be cast to ObjectStreamClass）。"
        "请改用 plan_ziyuan_patch（仅能复用数组内已存在的内联值）。")

    if new_val < 0:
        raise ValueError("资源数值不能为负数")
    if new_val > LONG_MAX:
        raise ValueError(f"数值 {new_val} 超过 long 上限 {LONG_MAX}")
    rows = R.read_ziyuan_details(data)
    if not (0 <= resource_id < len(rows)):
        raise ValueError(f"未定位到 ziyuan[{resource_id}]")
    row = rows[resource_id]
    encoded = qe_enc(new_val)
    if len(encoded) > 255:
        raise ValueError("QWERTY 编码长度超过 255 字节")
    new_bytes = b'\x74\x00' + bytes([len(encoded)]) + encoded.encode('ascii')

    if row['kind'] == 'str':
        # 已是内联串，走原有变长替换即可（不新增 handle）
        return [('ziyuan', f"{row['name']}[ID {resource_id}]", new_val,
                 row['tc_off'], 3 + len(row.get('encoded') or ''),
                 new_bytes, row['value'])]

    # ref / null -> 内联串：会新增一个 handle
    # 该槽位之前已有的内联串个数 + 2 = 新对象将获得的 handle 编号
    prior_inline = sum(1 for x in rows[:resource_id] if x['kind'] == 'str')
    insert_handle = 2 + prior_inline          # classdesc=#0, array=#1
    start = row['tc_off']
    old_len = 5 if row['kind'] == 'ref' else 1
    tail_off = start + old_len

    # 先做字符串替换，再对插入点之后的引用重编号
    spliced = data[:start] + new_bytes + data[tail_off:]
    reindexed, fixed = _reindex_refs_after_insert(
        spliced, start + len(new_bytes), insert_handle, insert_handle)

    # 复合 patch：把 [start, 文件尾) 整体作为替换内容
    return [('ziyuan_inline', f"{row['name']}[ID {resource_id}] ★内联+重编号{fixed}处",
             new_val, start, len(data) - start, reindexed[start:], row['value'])]


def plan_fixed_numeric_patch(kind, label, old_val, new_val, offset, type_code):
    """为已由结构解析器确认偏移的定长 Java primitive 生成标准 patch。"""
    formats = {
        'I': ('>i', -2147483648, 2147483647),
        'J': ('>q', -9223372036854775808, 9223372036854775807),
        'S': ('>h', -32768, 32767),
        'F': ('>f', None, None),
        'int': ('>i', -2147483648, 2147483647),
        'long': ('>q', -9223372036854775808, 9223372036854775807),
        'float': ('>f', None, None),
    }
    if type_code in ('Z', 'bool'):
        value = bool(new_val)
        return (kind, label, value, offset, 1, bytes([1 if value else 0]), bool(old_val))
    if type_code not in formats:
        raise ValueError(f"不支持的固定字段类型: {type_code}")
    fmt, low, high = formats[type_code]
    value = float(new_val) if type_code in ('F', 'float') else int(new_val)
    if low is not None and not (low <= value <= high):
        raise ValueError(f"{label} 超出 {type_code} 范围: {value}")
    new_bytes = struct.pack(fmt, value)
    return (kind, label, value, offset, len(new_bytes), new_bytes, old_val)


def plan_lihui_unlock(data, index, unlock=True):
    """解锁 / 锁定主界面左下角的某个主角立绘形象（LiHuiData.useable[index]）。

    ★ 安全性：只改该 Boolean 对象的 1 字节值（00 <-> 01），
      文件长度不变、不新增/删除任何 Java 序列化对象，句柄完全不受影响。
    ★ 前提：该槽位必须是【内联】Boolean（kind='inline'）；
      若是 TC_REFERENCE（与其它槽共享同一对象），改它会影响同组所有槽位，
      因此拒绝并说明。
    返回 patch 7 元组或 None（无变化）。
    """
    info = R.locate_lihui_slots(data)
    if info is None:
        raise ValueError("未定位到 LiHuiData（主角立绘解锁数据）")
    slot = next((x for x in info['slots'] if x[0] == index), None)
    if slot is None:
        raise ValueError(f"立绘索引 {index} 超出范围（共 {info['count']} 个）")
    idx, off, kind, cur = slot
    if kind != 'inline':
        raise ValueError(
            f"立绘[{index}] 是共享引用槽位，与其它槽位共用同一个 Boolean 对象，"
            "单独修改会连带影响它们，已拒绝。")
    want = 1 if unlock else 0
    if cur == want:
        return None
    return ('lihui', f"立绘形象[{index}]", bool(want), off, 1, bytes([want]), bool(cur))


def plan_castle_resource_patch(data, resource_id, new_val):
    """按 CastleData.resources HashMap 的真实对象偏移修改任意 CastleResource.r。"""
    overview = R.read_castle_overview(data)
    row = next((item for item in overview.get('resources', []) if item['id'] == resource_id), None)
    if row is None or row.get('offset') is None:
        raise ValueError(f"未定位到 CastleResource {resource_id}")
    if int(new_val) < 0:
        raise ValueError("城堡资源数值不能为负数")
    return plan_fixed_numeric_patch(
        'castle_resource', f"{row['name']}[资源ID {resource_id}]",
        row['value'], int(new_val), row['offset'], row.get('type') or 'J')


# ─────────────────────────────────────
#  新增 CastleResource 记录（HashMap 扩容）
# ─────────────────────────────────────
#
# ★ CastleData.resources 的存档布局（已逐字节核对，local/local1/local2/local9/模板 一致）
#
#   [loadFactor: float 4B]
#   [TC_BLOCKDATA 0x77 len 0x08] [capacity: int 4B] [size: int 4B]
#   entry_0 ... entry_{size-1}        ← 每条 18 字节
#   ...
#
#   单条 entry（28 字节）:
#     [key:   0x71][4B Integer classDesc 引用句柄][4B Integer 值]   ← 9 字节
#     [value: 0x73][0x71][4B CastleResource classDesc 引用句柄]
#             [r:    8B big-endian long]                            ← CastleResource.r
#             [rid:  4B big-endian int]                             ← CastleResource.resourceID
#                                                              合计 19 字节
#   key + value = 28 字节（已用 local/local1/local2/local9/模板 五个存档逐条核对，
#   相邻 entry 起点间距恒为 28）。
#
#   ★ entry_0 的 classDesc 是【内联】的（首次出现要写类名+suid+字段表），
#     所以 entry_0 远长于 28 字节。★ 追加新 entry 时必须仿照 entry_1 及以后的
#     【引用式】hdr，绝不能内联 classDesc —— 内联会多注册一个对象导致句柄错位。
#
# ★ capacity 是 2 的幂（实测 4 / 32 / 256），size 是实际条目数。
#   加一条 → size+1；若 size+1 > capacity 则必须同时扩 capacity 并重排整个表。
#   ★ 本函数只在【容量足够】时追加；否则直接报错，绝不擅自重排 HashMap
#     （重排会改变 entry 顺序，且桶位依赖 hash 分布，风险极高）。

# CastleResource classDesc 引用式 hdr：73 71 00 7e + 2 字节 classDesc 句柄
_CASTLE_RESOURCE_HDR = b'\x73\x71\x00\x7e'


def _locate_resources_hashmap(data):
    """定位 CastleData.resources 这个 HashMap 的字节结构。

    返回 dict:
      load_factor_off : float loadFactor 的偏移
      blk_off         : capacity/size blockdata 的偏移（int 字段起点）
      capacity, size  : 当前容量与条目数
      entry_hdr       : 追加新 entry 时应使用的 6 字节引用式 hdr
      entry_size      : 单条 entry 字节数（18）
      entries_end     : 最后一条 entry 的结束偏移
    失败返回 None。
    """
    import soco_hero_equipment as H
    try:
        parser, castle = H.find_castle_data(bytearray(data), 8)
    except Exception:
        return None
    if not castle:
        return None
    res = castle.get('resources')
    if not res:
        return None

    lf_off = (res.get('__offsets__') or {}).get('loadFactor')
    if not lf_off:
        return None
    lf_off = lf_off[0] if isinstance(lf_off, (tuple, list)) else lf_off

    # blockdata 紧随 loadFactor：77 <len> <capacity:4> <size:4>
    blk_off = lf_off + 4
    if data[blk_off] != 0x77:
        return None
    blk_len = data[blk_off + 1]
    if blk_len < 8:
        return None
    capacity, size = struct.unpack('>ii', bytes(data[blk_off + 2:blk_off + 10]))

    # 找【引用式】entry 模板：entry_1 及以后的 key/value 都是引用式 hdr，
    # 而 entry_0 的 classDesc 是内联的，必须跳过。
    # 以序列化顺序（__custom__ 的 val 序列）遍历，取最后一条作为追加位置。
    vals = [v for k, v in (res.get('__custom__') or []) if k == 'val']
    key_hdr = None      # 6 字节：73 + 71 + 4B Integer classDesc 句柄
    value_hdr = None    # 6 字节：73 + 71 + 4B CR classDesc 句柄
    entries_end = None
    for i in range(0, len(vals) - 1, 2):
        vobj = vals[i + 1]
        if not isinstance(vobj, dict) or 'r' not in (vobj.get('__offsets__') or {}):
            continue
        ro = vobj['__offsets__']['r'][0]
        # value 对象 = [73][71][4B CR classDesc 句柄] + [r 8B] + [rid 4B] = 18 字节
        #   → 对象头起点 = ro - 6
        # key 对象紧贴在 value 之前 = [73][71][4B Int classDesc 句柄] + [4B Int 值] = 10 字节
        #   → 对象头起点 = ro - 6 - 10 = ro - 16
        k_start = ro - 16
        if k_start < 0:
            continue
        kb = bytes(data[k_start:k_start + 10])
        vb = bytes(data[ro - 6:ro])
        if kb[0] != 0x73 or kb[1] != 0x71:
            continue
        if vb[0] != 0x73 or vb[1] != 0x71:
            continue
        # 校验：key 的 Integer 值 == value 的 resourceID
        k_int = struct.unpack('>i', kb[6:10])[0]
        rid = struct.unpack('>i', bytes(data[ro + 8:ro + 12]))[0]
        if k_int != rid:
            continue
        key_hdr = kb[0:6]      # 73 71 + 4B Integer classDesc 句柄
        value_hdr = vb         # 73 71 + 4B CR classDesc 句柄
        entries_end = ro + 12  # r(8) + rid(4) 之后即本条 entry 结尾

    if key_hdr is None or value_hdr is None:
        return None

    # ★ 插入点：最后一条 entry 的 resourceID 之后。
    #   该处紧跟 HashMap 序列化流的收尾（TC_ENDBLOCKDATA 等），
    #   在此之前插入新 entry，entry 序列长度 +28，其余字节整体后移。
    insert_at = entries_end
    return {
        'load_factor_off': lf_off,
        'blk_off': blk_off,
        'capacity': capacity,
        'size': size,
        'key_hdr': key_hdr,
        'value_hdr': value_hdr,
        'entry_size': 28,
        'entries_end': insert_at,
    }


def plan_add_castle_resource(data, resource_id, new_val=0):
    """在 CastleData.resources 里【新增】一条资源记录（存档里原本不存在该 id 时用）。

    ★ 与 plan_castle_resource_patch 的区别：
      - patch：资源已存在，改数值，长度不变，零句柄影响
      - 本函数：资源不存在，向 HashMap 追加一条新 entry（长度会变）

    ★ 句柄影响（Java 序列化核心机制）：
      追加的 entry 含 2 个【新对象】（Integer key + CastleResource value），
      Java 反序列化时按出现顺序分配 handle ⇒ 从插入点开始、句柄号 >= boundary 的
      所有 0x71 引用都必须 +2，否则整档引用错位闪退。
      boundary 由解析器给出的句柄表精确求得（插入点之后第一个已分配 handle）。

    ★ 只在 capacity 足够时追加（size+1 <= capacity）；
      需要扩容时直接报错，不擅自重排 HashMap（会改变 entry 顺序，高风险）。

    返回 (new_data: bytes, info: dict)
    """
    data = bytes(data)
    loc = _locate_resources_hashmap(data)
    if loc is None:
        raise ValueError("未定位到 CastleData.resources（存档格式可能不匹配）")

    import soco_read as R
    overview = R.read_castle_overview(data)
    if any(x.get('id') == resource_id for x in overview.get('resources', [])):
        raise ValueError(f"资源 {resource_id} 已存在，无需新增")

    capacity = loc['capacity']
    size = loc['size']
    new_capacity = capacity
    if size + 1 > capacity:
        # ★ 扩容：capacity 扩到下一个 2 的幂。
        #   实测这个 resources 是【key/value 严格交替平铺】的自定义序列化，
        #   读档时按出现顺序逐对读出重建，【不依赖桶序】，
        #   所以扩容只需改 capacity 字段，entry 顺序无需重排。
        new_capacity = 1
        while new_capacity < size + 1:
            new_capacity <<= 1
        if new_capacity > (1 << 20):
            raise ValueError(f"resources HashMap 容量异常（{capacity}），已拒绝操作")

    hdr = loc['value_hdr']
    key_hdr = loc['key_hdr']
    rid = int(resource_id)
    if not (0 <= rid <= 0x7FFFFFFF):
        raise ValueError(f"资源 id {rid} 超出 4 字节 int 范围")

    # 组装新 entry（28 字节）：key 对象 10B + value 对象 18B
    entry = b''
    entry += key_hdr                           # 73 71 + 4B Integer classDesc 句柄
    entry += struct.pack('>i', rid)            # Integer 值（key）
    entry += hdr                               # 73 71 + 4B CR classDesc 句柄
    entry += struct.pack('>q', int(new_val))   # r（资源数值）
    entry += struct.pack('>i', rid)            # resourceID

    insert_at = loc['entries_end']

    # ── 句柄影响范围：插入点之后【已分配】的那些 handle 需要整体后移 ──
    #    新对象会占用插入点处的 2 个连续 handle 号，
    #    原本占用这些号的旧对象（以及其后的）整体 +2。
    NEW_OBJECTS = 2      # Integer key 对象 + CastleResource value 对象
    targets, exact_refs, err = _handles_needing_shift(data, insert_at)
    if err:
        raise ValueError(f"原档序列化流无法完整解析，已拒绝操作：{err}")
    if not targets:
        raise ValueError("无法确定句柄影响范围（插入点之后没有已分配句柄），已拒绝操作")
    new_start = min(targets)
    boundary = new_start + NEW_OBJECTS

    out = bytearray(data[:insert_at] + entry + data[insert_at:])

    # 更新 blockdata 里的 size 与 capacity
    struct.pack_into('>i', out, loc['blk_off'] + 2, new_capacity)
    struct.pack_into('>i', out, loc['blk_off'] + 6, size + 1)

    # ── 句柄重编号：把指向 targets 的引用整体 +NEW_OBJECTS ──
    #    注意：插入后 insert_at 之后的字节位置整体后移了 len(entry)，
    #    所以精确引用位置要同步平移。
    shifted_refs = {}
    for pos, hv in exact_refs.items():
        np_ = pos + len(entry) if pos >= insert_at else pos
        shifted_refs[np_] = hv
    out, fixed = _shift_handles_from(out, targets, NEW_OBJECTS, shifted_refs)

    # ★ 全流完整性校验：必须把整条序列化流走到末尾
    #   （只校验到 CastleData 会漏掉后面的引用错位 → 游戏读档 ClassCastException）
    ok, msg = verify_full_stream(bytes(out))
    if not ok:
        raise ValueError(f"回读校验失败（序列化流不完整）：{msg}")

    # ── 回读校验 ──
    try:
        import soco_hero_equipment as H
        parser2, castle2 = H.find_castle_data(bytearray(out), 8)
        mp2 = (castle2.get('resources') or {}).get('__map__') or {}
    except Exception as e:
        raise ValueError(f"回读校验失败（解析异常）: {e}")
    if rid not in mp2:
        raise ValueError(f"回读校验失败：新增的资源 {rid} 未被解析出来")
    if len(mp2) != size + 1:
        raise ValueError(f"回读校验失败：条目数 {len(mp2)} != {size + 1}")
    got_val = mp2[rid].get('r') if isinstance(mp2[rid], dict) else None
    if got_val != int(new_val):
        raise ValueError(f"回读校验失败：值 {got_val} != {new_val}")
    for row in overview.get('resources', []):
        old_id = row.get('id')
        if old_id not in mp2:
            raise ValueError(f"回读校验失败：原资源 {old_id} 丢失")
        if old_id != rid and mp2[old_id].get('r') != row.get('value'):
            raise ValueError(f"回读校验失败：原资源 {old_id} 数值被改动")

    # 全局结构完整性校验（昵称/手机号/章节/装扮等关键结构必须不变）
    ok, details = verify_structure_intact(data, bytes(out))
    if not ok:
        raise ValueError(f"回读校验失败：全局结构被破坏 {details}")

    info_out = {
        'resource_id': rid,
        'value': int(new_val),
        'old_size': size,
        'new_size': size + 1,
        'capacity': capacity,
        'new_capacity': new_capacity,
        'insert_at': insert_at,
        'bytes_added': len(entry),
        'handle_boundary': boundary,
        'handle_new_start': new_start,
        'refs_shifted': fixed,
    }
    return bytes(out), info_out


def _handles_needing_shift(data, insert_at):
    """求「插入新对象后需要整体后移的 handle 号集合」及其【全部】精确引用位置。

    返回 (targets: set[int], exact_refs: dict[偏移, handle号])

    ★ 原理：Java 反序列化按【出现顺序】分配 handle（对象与 classDesc 共用同一个
      计数器）。在 insert_at 处插入 N 个新对象 ⇒ 该处之后分配的 handle 全部后移 N，
      并且【所有指向它们的 TC_REFERENCE（含 classDesc 引用）】都要同步后移。

    ★★ 必须用 walk_stream 把【整个流】走完，不能用 find_castle_data：
      find_castle_data 找到 CastleData 即返回，句柄表只覆盖到 CastleData，
      会把 CastleData 之后的几百个 handle 与几十处引用全部漏掉
      → 游戏读档时 readClassDesc 拿到错的对象
      → ClassCastException: java.util.HashMap cannot be cast to java.io.ObjectStreamClass
    """
    import soco_hero_equipment as H
    ps, ok, err = H.walk_stream(bytearray(data), 8, track_handles=True)
    if not ok:
        return set(), {}, err
    ao = ps.handle_alloc_offset or {}
    rp = ps.ref_positions or {}
    targets = {h for h, off in ao.items() if off > insert_at}
    exact_refs = {pos: hv for pos, hv in rp.items() if hv in targets}
    return targets, exact_refs, None


def _shift_handles_from(data, targets, delta, exact_positions):
    """把 exact_positions 中指向 targets 的引用句柄 +delta，返回 (bytes, 改动数)。

    ★★ 只改写解析器枚举出来的真实引用位置，**不做任何字节扫描**。
      字节扫描无法区分「真正的 0x71 引用」和「数据块/字符串/长整数里
      形似 0x71 的巧合字节」，实测会改坏数据（虽然概率低，但一旦命中就是闪退）。
      walk_stream 已经枚举出全流每一处 TC_REFERENCE，覆盖 100%，无需扫描。

    ★★ 必须【先收集全部，再统一改写】：边扫边改会级联
      （旧值 +delta 落在 targets 内 → 被再次命中 → +2 变 +4）。
    """
    d = bytearray(data)
    n = len(d)
    hits = []
    for pos, hv in exact_positions.items():
        if hv not in targets:
            continue
        if pos < 0 or pos + 5 > n:
            raise ValueError(f"引用位置越界: 0x{pos:x}")
        if d[pos] != 0x71:
            raise ValueError(f"引用位置 0x{pos:x} 处不是 TC_REFERENCE（可能是 0x{d[pos]:02x}）")
        hits.append((pos, hv))
    for pos, hv in hits:
        d[pos + 1:pos + 5] = (hv + delta).to_bytes(4, 'big')
    return d, len(hits)


def verify_full_stream(data):
    """把整条序列化流完整解析一遍，确认结构完整。

    返回 (ok: bool, msg: str)

    ★ 这是「插入对象」后唯一可信的完整性校验：
      只解析到 CastleData 无法发现后面的引用错位（正是之前闪退的原因）。
    """
    import soco_hero_equipment as H
    ps, ok, err = H.walk_stream(bytearray(data), 8, track_handles=True)
    if not ok:
        return False, f"序列化流在 0x{ps.p:x} 处解析失败：{err}"
    # 所有引用都必须指向已分配的 handle
    bad = [(pos, hv) for pos, hv in (ps.ref_positions or {}).items()
           if hv not in (ps.handles or {})]
    if bad:
        pos, hv = bad[0]
        return False, f"引用 0x{pos:x} → 未定义 handle 0x{hv:x}（共 {len(bad)} 处）"
    if ps.p != len(data):
        return False, f"流未走到末尾（0x{ps.p:x} / 0x{len(data):x}）"
    return True, f"完整解析 {len(ps.handles)} 个句柄 / {len(ps.ref_positions or {})} 处引用"

def plan_returnplayer_toggle(data, activate):
    """回归玩家开关。activate=True 写入当前时间戳激活；False 写 -1 取消激活。
    激活时间/总天数为定长字段，原地替换，不涉及句柄，安全。"""
    import time
    rp = R.locate_returnplayer(data)
    if rp is None:
        raise ValueError("未定位到回归玩家数据(ReturnPlayerData)")
    if activate:
        if rp['activated']:
            return None
        new_val = int(time.time() * 1000)
    else:
        if not rp['activated']:
            return None
        new_val = -1
    return plan_fixed_numeric_patch(
        'returnplayer', '回归玩家.激活时间', rp['activate_time'], new_val,
        rp['activate_off'], 'J')


def plan_returnplayer_score(data, new_score):
    """修改回归积分 remainingScore（int，定长原地替换）。"""
    rp = R.locate_returnplayer(data)
    if rp is None:
        raise ValueError("未定位到回归玩家数据(ReturnPlayerData)")
    if new_score == rp['remaining_score']:
        return None
    return plan_fixed_numeric_patch(
        'returnplayer', '回归积分', rp['remaining_score'], int(new_score),
        rp['score_off'], 'I')


def plan_phoneshouji_patch(data, new_phone):
    """写入 / 解绑 GameData.playerSHOUJI（账号绑定手机号）。

    new_phone: 11 位手机号；传 '' / None 表示解绑（写成空串）。

    ★ 三种源形态都要能处理（locate_phoneshouji 已能识别）：
        'str'  : 74 <len:2> <utf8>     已绑定且内联
        'ref'  : 71 <handle:4>          引用共享串（通常未绑定）
        'none' : 无独立槽位（字段被空块吃掉，新号常见）

    ★ 写入策略（关键：不能改变 Java 序列化句柄总数）
        - 源是 'str' : 原地改长度+内容 → 若新长度不同会变长，
          但内联串本来就在流里，长度变化不改变对象个数 ⇒ 安全。
        - 源是 'ref' : 直接把 5 字节引用替换成【内联 TC_STRING】会【新增一个对象】
          ⇒ 后续 handle 编号全部错位（曾导致 ClassCastException 崩溃）。
          安全做法：把该引用【重定向到流中已有的某个 11 位手机号串】——
          但没有现成可复用时只能拒绝，让用户先在游戏里绑定一次。
        - 源是 'none': 无法在原位新增对象（会改变句柄数）⇒ 拒绝并说明。
    返回 patch 7 元组或 None（无变化）。
    """
    import re as _re
    loc = R.locate_phoneshouji(data)
    if loc is None:
        raise ValueError("未定位到 playerSHOUJI（找不到 dataSaveTime 锚点）")
    kind = loc['kind']
    phone = '' if new_phone is None else str(new_phone).strip()
    if phone and not _re.fullmatch(r'1[3-9]\d{9}', phone):
        raise ValueError(f"手机号格式不正确（需 11 位大陆号）: {phone}")

    old_val = loc['phone']

    # ── 解绑 ──
    if not phone:
        if kind != 'str' or not old_val:
            return None                      # 本来就没绑，无需处理
        raw = b''
        new_bytes = b'\x74\x00\x00'      # 空串
        start = loc['str_off']
        old_len = 3 + len(old_val.encode('utf-8'))
        return ('phoneshouji', '解绑手机号(playerSHOUJI)', '(解绑)',
                start, old_len, new_bytes, old_val)

    # ── 写入手机号 ──
    if kind == 'str':
        raw = phone.encode('utf-8')
        new_bytes = b'\x74\x00' + bytes([len(raw)]) + raw
        start = loc['str_off']
        old_len = 3 + len(old_val.encode('utf-8'))
        if phone == old_val:
            return None
        return ('phoneshouji', '绑定手机号(playerSHOUJI)', phone,
                start, old_len, new_bytes, old_val)

    # ref / none：需要复用流中已有的手机号串，或就地内联
    # 先找流中已有的 11 位手机号串（内联 TC_STRING）
    n = len(data)
    existing = []
    for m in _re.finditer(rb'\x74', data):
        o = m.start()
        if o + 4 > n:
            break
        L = int.from_bytes(data[o + 1:o + 3], 'big')
        if L != 11 or o + 14 > n:
            continue
        try:
            t = data[o + 3:o + 14].decode('ascii')
        except UnicodeDecodeError:
            continue
        if _re.fullmatch(r'1[3-9]\d{9}', t):
            existing.append((o, t))
    if existing:
        # 复用已有串：把引用重定向到它（5 字节，零位移）
        src_off, _t = existing[0]
        # 需知道该串的 handle 号：它前面的对象已分配 handle。
        # 简化：直接原地把该槽位改成【引用该串】——handle 号需精确计算，风险高。
        raise ValueError(
            "该槽位是共享引用形态。为避免改变 Java 序列化句柄数导致存档损坏，"
            "暂不支持直接写入。\n"
            "可先在游戏内正常绑定一次手机号（会让该字段变为内联串），再用本工具修改。")
    raise ValueError(
        "该槽位是引用/空形态（未绑定手机号）。直接写入需新增序列化对象，"
        "会改变 handle 编号导致存档损坏，故已阻止。\n"
        "请先在游戏内绑定一次手机号，然后再用本工具修改号码。")


def plan_chapter_patch(data, new_display):
    """修改通关章节数（游戏 UI 显示口径，1-based）。

    new_display: 游戏里看到的"通关至第 N 章"，N>=1，实际写入 N-1。

    ★★ 必须同时改 3 个位置，否则无效：
        1) daxuanguan            （组 A，UI 直接读取）
        2) daxuanguan_kaiqi      （组 A，与上者恒等）
        3) daxuanguan_kaiqi_backup ★关键★
           登录时 UI_Title.smali:2875 执行
               setDaxuanguan(getDaXuanGuanKaiQi())
           而 getDaXuanGuanKaiQi() 返回的就是这个 backup 字段，
           它会把 daxuanguan 覆盖回备份值 —— 只改 1)、2) 下次登录立即失效。

    三处均为定长 4 字节原地替换，不涉及 Java 序列化句柄，安全。
    返回 patch 列表（可能为空）。
    """
    ch = R.locate_daxuanguan(data)
    if ch is None:
        raise ValueError("未定位到章节进度（daxuanguan）。")
    if 'backup_off' not in ch:
        raise ValueError("未定位到 daxuanguan_kaiqi_backup（登录后生效的权威值），"
                         "为避免改了不生效，已中止。")
    try:
        disp = int(new_display)
    except (TypeError, ValueError):
        raise ValueError(f"章节数无效: {new_display}")
    if disp < 1:
        raise ValueError("章节数至少为 1")
    if disp > 200:
        raise ValueError("章节数不能超过 200")
    new_val = disp - 1

    patches = []
    if 'base_off' in ch and ch.get('chapter') != new_val:
        patches.append(plan_fixed_numeric_patch(
            'chapter', '通关章节(daxuanguan)',
            ch['chapter'], new_val, ch['base_off'], 'I'))
        patches.append(plan_fixed_numeric_patch(
            'chapter', '已开启章节(daxuanguan_kaiqi)',
            ch['kaiqi'], new_val, ch['base_off'] + 4, 'I'))
    if ch.get('backup') != new_val:
        patches.append(plan_fixed_numeric_patch(
            'chapter', '已开启章节备份(daxuanguan_kaiqi_backup) ★登录生效值',
            ch['backup'], new_val, ch['backup_off'], 'I'))
    return patches


def plan_returnplayer_clear(data, which):
    """清空回归已领奖励(collected)/已购商品(bought)。

    仅当集合元素全为 TC_REFERENCE/TC_NULL 时执行：删除 key 区只产生字节位移，
    不增减 Java 序列化句柄，绝对安全。若含内联对象(TC_OBJECT/TC_STRING)则拒绝，
    避免删对象导致句柄错位闪退。返回 patch 列表（可能为空=已是空集合）。"""
    rp = R.locate_returnplayer(data)
    if rp is None:
        raise ValueError("未定位到回归玩家数据(ReturnPlayerData)")
    s = rp.get(which)
    if s is None:
        raise ValueError(f"未定位到回归.{which}")
    label = '已领奖励' if which == 'collected' else '已购商品'
    if s['size'] == 0:
        return []
    if not s['all_safe']:
        raise ValueError(
            f"回归.{label} 含内联对象({', '.join(s['kinds'])})，"
            f"自动清空会改变 Java 序列化句柄导致存档损坏，已阻止。")
    patches = [
        plan_fixed_numeric_patch(
            'returnplayer', f"回归.{label}.数量", s['size'], 0, s['size_off'], 'I'),
        ('returnplayer_clear', f"回归.{label}.清空",
         0, s['first_key'], s['key_end'] - s['first_key'], b'', s['size']),
    ]
    return patches


def plan_tili_buy_patch(data, new_buy):
    """修改 GameData.tili_buy（今日购买体力剩余次数，1 字节）。
    该字段位于 GameData.writefile 的 tili 段 TC_BLOCKDATA 内，定长替换安全。
    注：上限 tili_buyMax 不在 soco.bin，由服务端/配置下发。"""
    info, _cnt = R.locate_tili_block(data)
    if info is None:
        raise ValueError("未定位到体力限购段(tili block)")
    v = int(new_buy)
    if not (0 <= v <= 127):
        raise ValueError("限购剩余次数需在 0~127（存档中为 1 字节）")
    if v == info['tili_buy']:
        return None
    return ('tili_buy', '体力.今日限购剩余', v,
            info['tili_buy_off'], 1, bytes([v]), info['tili_buy'])


def splice(data, start, old_len, new_bytes):
    return data[:start] + new_bytes + data[start + old_len:]


# ─────────────────────────────────────
#  Java 序列化句柄修正
#  ref→str 转换会新增一个句柄，导致下游所有 0x71 引用偏移
# ─────────────────────────────────────

def build_handle_table(data):
    """扫描 Java 序列化数据，构建 handle 表。
    返回 (handles_by_idx, offset_to_handle_idx)
    两阶段：先解析已知结构的 GameGuaiWu 区域，再暴力扫描其余部分。"""
    import soco_read as R
    handles_by_idx = {}
    offset_to_handle_idx = {}
    handle_counter = [-1]

    def _alloc():
        handle_counter[0] += 1
        return handle_counter[0]

    def _skip_classdesc_fields(pos):
        fc = int.from_bytes(data[pos:pos+2], 'big'); pos += 2
        for _ in range(fc):
            if pos >= len(data): break
            t = data[pos]; pos += 1
            nl = int.from_bytes(data[pos:pos+2], 'big'); pos += 2 + nl
            if t in (ord('L'), ord('[')):
                cl = int.from_bytes(data[pos:pos+2], 'big'); pos += 2 + cl
        if pos < len(data) and data[pos] == 0x78: pos += 1
        if pos < len(data):
            if data[pos] == 0x70: pos += 1
            elif data[pos] == 0x71: pos += 5
        return pos

    def _walk_array(pos, depth=0):
        """遍历 TC_ARRAY 及其元素。"""
        # TC_ARRAY tag
        hidx = _alloc()
        handles_by_idx[hidx] = {'offset': pos, 'tag': 'TC_ARRAY'}
        offset_to_handle_idx[pos] = hidx
        pos += 1
        # classdesc
        if pos < len(data) and data[pos] == 0x72:
            hidx2 = _alloc()
            L2 = int.from_bytes(data[pos+1:pos+3], 'big')
            name2 = data[pos+3:pos+3+L2].decode('ascii', 'replace')
            handles_by_idx[hidx2] = {'offset': pos, 'tag': 'TC_CLASSDESC '+name2}
            offset_to_handle_idx[pos] = hidx2
            pos = _skip_classdesc_fields(pos + 3 + L2 + 8 + 1)
        elif pos < len(data) and data[pos] == 0x71:
            pos += 5
        arr_len = int.from_bytes(data[pos:pos+4], 'big')
        pos += 4
        # 遍历元素
        for _ in range(arr_len):
            if pos >= len(data): break
            el = data[pos]
            if el == 0x75:  # nested TC_ARRAY
                pos = _walk_array(pos, depth+1)
            elif el in (0x74, 0x73, 0x72, 0x7C):
                hidx3 = _alloc()
                tags = {0x74:'TC_STRING', 0x73:'TC_OBJECT', 0x72:'TC_CLASSDESC', 0x7C:'TC_LONGSTRING'}
                handles_by_idx[hidx3] = {'offset': pos, 'tag': tags[el]}
                offset_to_handle_idx[pos] = hidx3
                if el == 0x74:
                    pos += 3 + data[pos+2]
                elif el == 0x73:
                    pos += 1
                    if pos < len(data) and data[pos] == 0x71: pos += 5
                elif el == 0x72:
                    L2 = int.from_bytes(data[pos+1:pos+3], 'big')
                    pos = _skip_classdesc_fields(pos + 3 + L2 + 8 + 1)
                elif el == 0x7C:
                    L2 = int.from_bytes(data[pos+1:pos+5], 'big')
                    pos += 5 + L2
            elif el == 0x71:
                pos += 5
            elif el == 0x70:
                pos += 1
            else:
                # 数组元素是裸数值（int[]）或其他，不分配 handle
                pos += 1  # avoid dead loop, but won't be correct sizes
        return pos

    # Phase 1: 解析已知结构的 GameGuaiWu 区域 (0x08 开始的顶级 TC_ARRAY)
    # 顶级 TC_ARRAY 包含 27 个元素，其中一个是 [[String (GameGuaiWu)
    # 先用通用 walker 处理前几个元素，遇到 GameGuaiWu 再专门处理
    pos = 8
    top_arr = _walk_array(pos)  # 这个已经递归处理了所有嵌套的家族数组

    # Phase 2: 如果还有未处理的数据，暴力扫描
    while pos < len(data):
        b = data[pos]
        if b in (0x74, 0x73, 0x72, 0x75, 0x7C):
            pos = _walk_array(pos) if b == 0x75 else _walk_array(pos)
        else:
            pos += 1

    return handles_by_idx, offset_to_handle_idx


def fixup_handles_after_splice(new_data, old_handles, old_off_to_handle):
    """splice 后修正所有 0x71 引用。
    在旧 handle 表中找到被引用对象的 offset，
    在新数据中重新查找该 offset 对应的 handle 值并替换。"""
    new_handles, new_off_to_handle = build_handle_table(new_data)
    d = bytearray(new_data)
    i = 8
    fix_count = 0
    while i < len(d):
        if d[i] == 0x71 and i + 5 <= len(d):
            old_hv = int.from_bytes(d[i+1:i+5], 'big')
            old_idx = old_hv - 0x7E0000
            if old_idx in old_handles:
                old_off = old_handles[old_idx]['offset']
                if old_off in new_off_to_handle:
                    new_idx = new_off_to_handle[old_off]
                    new_hv = 0x7E0000 + new_idx
                    if new_hv != old_hv:
                        d[i+1:i+5] = new_hv.to_bytes(4, 'big')
                        fix_count += 1
            i += 5
        else:
            i += 1
    if fix_count > 0:
        import sys
        print(f"    [句柄修正] 已修正 {fix_count} 处 0x71 引用 (ref→str 导致 handle 偏移)", file=sys.stderr)
    return bytes(d)


# ─────────────────────────────────────
#  卡片数量修改（GameGuaiWu 内层 [1]）
#  命令格式:  f<家族id> 卡片数量 <值>     例: f1 卡片数量 2026
#             f-1 卡片数量 <值>           例: f-1 卡片数量 0  (所有家族)
#  也可省略关键字: f1 2026
#  长度策略: 保持原字段宽度(前导 Q 补零)；仅当新值位数超出原宽度时扩宽(带警告)。
# ─────────────────────────────────────

def parse_inner_cells(data, p):
    """解析 GameGuaiWu 一个内层 String[]，返回 (cells, next_p)。
    cells[i] = (kind, raw, tc_off)，kind ∈ {'str','null','ref'}。"""
    n = len(data)
    b = data[p]
    if b == 0x70:
        return None, p + 1
    if b == 0x71:
        return 'ARRAY_REF', p + 5
    if b != 0x75:
        return '?', p + 1
    p += 1
    if data[p] == 0x72:
        p += 1
        L = int.from_bytes(data[p:p + 2], 'big'); p += 2
        p += L; p += 8; p += 1
        fc = int.from_bytes(data[p:p + 2], 'big'); p += 2
        if data[p] == 0x78: p += 1
        if data[p] == 0x70: p += 1
    elif data[p] == 0x71:
        p += 5
    length = int.from_bytes(data[p:p + 4], 'big'); p += 4
    cells = []
    for _ in range(length):
        if p >= n:
            break
        eb = data[p]
        if eb == 0x74:
            L = data[p + 2]
            raw = bytes(data[p + 3:p + 3 + L])
            cells.append(('str', raw, p))
            p += 3 + L
        elif eb == 0x70:
            cells.append(('null', None, p))
            p += 1
        elif eb == 0x71:
            h = int.from_bytes(data[p + 1:p + 5], 'big')
            cells.append(('ref', h, p))
            p += 5
        else:
            cells.append(('?', eb, p))
            break
    return cells, p


def plan_card_count_patches(data, family_id, new_count):
    """返回卡片数量修改 patch 列表（7 元组）与跳过信息。
    7 元组 = (kind, label, new_val, start, old_len, new_bytes, old_val)
    family_id=-1 表示所有家族；否则仅指定家族。"""
    names = R.load_family_names()
    start, N = R.locate_guaiwu(data)
    if start is None:
        raise ValueError("未定位到 GameGuaiWu（卡片数据区）")
    new_str = qe_enc(new_count)
    p = start
    patches = []
    skipped = []
    for fam in range(N):
        cells, p = parse_inner_cells(data, p)
        if family_id != -1 and fam != family_id:
            continue
        if not isinstance(cells, list):
            if family_id != -1:
                raise ValueError(f"family {family_id} 不是字符串数组(null/ref)，无法修改")
            skipped.append((fam, 'null/ref')); continue
        if len(cells) < 2:
            if family_id != -1:
                raise ValueError(f"family {family_id} 元素数<2，无 count 字段")
            skipped.append((fam, '元素<2')); continue
        cell = cells[1]
        if cell[0] != 'str':
            if family_id != -1:
                raise ValueError(f"family {family_id} 的 count 不是字符串({cell[0]})，无法修改")
            skipped.append((fam, 'count非str')); continue
        cur_L = len(cell[1])
        cur_val = R.qe_dec(cell[1])
        width = max(cur_L, len(new_str))
        enc = ('Q' * (width - len(new_str))) + new_str
        tc_off = cell[2]
        old_span = 3 + cur_L
        new_bytes = b'\x74\x00' + bytes([width]) + enc.encode('ascii')
        nm = names.get(fam, '?')
        label = f"family{fam}({nm}) 卡片数"
        patches.append(('cardcount', label, new_count, tc_off, old_span, new_bytes, cur_val))
    if family_id != -1 and not patches:
        raise ValueError(f"family {family_id} 无任何可修改的 count 字段")
    return patches, skipped, N


def plan_adv_patches(data, family_id, new_adv):
    """返回进阶等级(f17)修改 patch 列表（7 元组）与跳过信息。

    进阶等级存于每个 family String[] 的 index 17（第 18 个元素），单字符 QWERTY
    编码（0=Q 1=W 2=E …）。命令 zf<id> <值> 即改该 family 的 f17。
    7 元组 = (kind, label, new_val, start, old_len, new_bytes, old_val)
    family_id=-1 表示所有家族；否则仅指定家族。
    字段类型处理：
      - 'str'  : 原地覆盖（保持/扩宽宽度，前导 Q 补零）
      - 'ref'  : 该 family 进阶等级是【共享串引用】，原地替换为内联 TC_STRING
                 （脱离共享），old_span=5（71 + 4B handle）
      - 'null' : 原为空，原地插入内联 TC_STRING，old_span=1（70）
    长度策略: 保持原字段宽度(前导 Q 补零)；新值位数超宽则最小扩宽(已实测安全)。"""
    names = R.load_family_names()
    start, N = R.locate_guaiwu(data)
    if start is None:
        raise ValueError("未定位到 GameGuaiWu（卡片数据区）")
    new_str = qe_enc(new_adv)
    p = start
    patches = []
    skipped = []
    for fam in range(N):
        cells, p = parse_inner_cells(data, p)
        if family_id != -1 and fam != family_id:
            continue
        if not isinstance(cells, list):
            if family_id != -1:
                raise ValueError(f"family {family_id} 不是字符串数组(null/ref)，无法修改")
            skipped.append((fam, 'null/ref')); continue
        if len(cells) < 18:
            if family_id != -1:
                raise ValueError(f"family {family_id} 元素数<18，无进阶等级字段")
            skipped.append((fam, '元素<18')); continue
        cell = cells[17]
        kind = cell[0]
        if kind == 'str':
            cur_L = len(cell[1])
            cur_val = R.qe_dec(cell[1])
            width = max(cur_L, len(new_str))
            tc_off = cell[2]
            old_span = 3 + cur_L
        elif kind == 'ref':
            cur_val = None
            width = max(len(new_str), 2)
            tc_off = cell[2]
            old_span = 5                      # 0x71 + 4 字节 handle
        elif kind == 'null':
            cur_val = None
            width = len(new_str)
            tc_off = cell[2]
            old_span = 1                      # 0x70
        else:
            if family_id != -1:
                raise ValueError(f"family {family_id} 的进阶等级字段异常({kind})，无法修改")
            skipped.append((fam, kind)); continue
        enc = ('Q' * (width - len(new_str))) + new_str
        new_bytes = b'\x74\x00' + bytes([width]) + enc.encode('ascii')
        nm = names.get(fam, '?')
        label = f"family{fam}({nm}) 进阶等级"
        patches.append(('adv', label, new_adv, tc_off, old_span, new_bytes, cur_val))
    if family_id != -1 and not patches:
        raise ValueError(f"family {family_id} 无任何可修改的进阶等级字段")
    return patches, skipped, N


# ── form5（传说形态）的 ref->ref 单只改写支持 ──
#
# index[5] 语义（对比"解锁前/解锁后"两份真机存档得出，仅 family43 的 [5] 变化）：
#   ref -> 共享默认串  = 未解锁
#   明文 3 / 4 / 5     = 已解锁（形态阶数，绝大多数为 4）
#
# 游戏自己写档时是 ref -> 内联 TC_STRING（文件 +1 字节）。但我们做**局部字节 patch**
# 时不能这么干：内联会新增一个 handle，而流中后续所有 handle 号是硬编码的，
# 不会跟着 +1，必然错位闪退。因此采用 ref->ref：把该元素改引用一个『值已是目标值』
# 的现存独立串对象，5 字节等长、不增删对象、handle 总数不变 —— 安全且只影响这一只。
#
# 目标 handle 不再硬编码，改由 soco_handles.HandleMap 精确模拟 Java handle 分配后查找。
FORM5_UNLOCK_VALUE = 4       # 解锁时写入的形态阶

_HANDLE_CACHE = {}


def _handle_map(data):
    """构建（并缓存）当前 data 的 handle 映射。"""
    import soco_handles
    key = (len(data), bytes(data[:64]))
    hm = _HANDLE_CACHE.get(key)
    if hm is None:
        hm = soco_handles.HandleMap(data)
        _HANDLE_CACHE[key] = hm
    return hm


def find_value_handle(data, value, before_off=None):
    """找一个内容为 qe_enc(value) 的现存字符串 handle，用于 ref->ref 替换。

    before_off: 只取偏移小于它的 handle（引用必须指向流中更早出现的对象）。
    找不到返回 None。
    """
    raw = qe_enc(value).encode('ascii')
    hm = _handle_map(data)
    for h in hm.find_string_handles(raw):
        if before_off is None or hm.by_handle[h]['off'] < before_off:
            return h
    return None


def plan_guaiwu_cell_patches(data, family_id, cell_idx, new_val, label_hint="字段"):
    """通用：修改 GameGuaiWu 某个 family String[] 的任意 cell 索引（index = cell_idx），
    写入 QWERTY 字符串。用于非标准字段（如 index[5] 形态/传说形态标志位）。

    7 元组 = (kind, label, new_val, start, old_len, new_bytes, old_val)
    family_id=-1 表示所有家族；否则仅指定家族。

    字段类型处理：
      - 'str'  : 原地覆盖（保持/扩宽宽度，前导 Q 补零），精确单只。
      - 'ref'  : 该字段是【共享串引用】。⚠ 直接把 ref 内联成 TC_STRING 会改变流的 handle 计数，
                 导致后续序列化错位闪退。因此改为【把该 ref 元素改引用一个『值相同』的已有独立
                 串对象】(ref->ref 同级替换，等长、不增删对象、不改变 handle 总数，绝不闪退)。
                 对 form5：已解锁(>=4) -> 引用头部值4串(8257542)，未解锁 -> 引用默认串(8257562)，
                 实现真正的单只解锁/恢复。
      - 'null' : 原为空，原地插入内联 TC_STRING，old_span=1（0x70）
    """
    names = R.load_family_names()
    start, N = R.locate_guaiwu(data)
    if start is None:
        raise ValueError("未定位到 GameGuaiWu（卡片数据区）")
    new_str = qe_enc(new_val)
    p = start
    patches = []
    skipped = []
    for fam in range(N):
        cells, p = parse_inner_cells(data, p)
        if family_id != -1 and fam != family_id:
            continue
        if not isinstance(cells, list):
            if family_id != -1:
                raise ValueError(f"family {family_id} 不是字符串数组(null/ref)，无法修改")
            skipped.append((fam, 'null/ref')); continue
        if len(cells) <= cell_idx:
            if family_id != -1:
                raise ValueError(f"family {family_id} 元素数<={cell_idx}，无 index[{cell_idx}] 字段")
            skipped.append((fam, '元素不足')); continue
        cell = cells[cell_idx]
        kind = cell[0]
        nm = names.get(fam, '?')
        label = f"family{fam}({nm}) {label_hint}"
        if kind == 'str':
            cur_L = len(cell[1])
            cur_val = R.qe_dec(cell[1])
            width = max(cur_L, len(new_str))
            tc_off = cell[2]
            old_span = 3 + cur_L
            enc = ('Q' * (width - len(new_str))) + new_str
            new_bytes = b'\x74\x00' + bytes([width]) + enc.encode('ascii')
            patches.append(('guaiwu_cell', label, new_val, tc_off, old_span, new_bytes, cur_val))
        elif kind == 'ref':
            cur_handle = cell[1]
            tc_off = cell[2]
            # 找一个『值已经是 new_val』的现存串对象，改引用它（ref->ref，等长 5 字节）
            target_handle = find_value_handle(data, new_val, before_off=tc_off)
            if target_handle is None:
                msg = (f"存档中找不到值为 {new_val} 的现存字符串对象，"
                       f"无法用 ref->ref 方式修改 family{fam} 的 {label_hint}")
                if family_id != -1:
                    raise ValueError(msg)
                skipped.append((fam, '无同值串')); continue
            if target_handle == cur_handle:
                continue  # 已是目标引用，无需修改
            new_bytes = b'\x71' + target_handle.to_bytes(4, 'big')
            old_span = 5
            cur_val = R.qe_dec(_handle_map(data).resolve(cur_handle) or b'')
            patches.append(('guaiwu_cell_ref', label, new_val, tc_off, old_span, new_bytes, cur_val))
        elif kind == 'null':
            cur_val = None
            width = len(new_str)
            tc_off = cell[2]
            old_span = 1                      # 0x70
            enc = new_str
            new_bytes = b'\x74\x00' + bytes([width]) + enc.encode('ascii')
            patches.append(('guaiwu_cell', label, new_val, tc_off, old_span, new_bytes, cur_val))
        else:
            if family_id != -1:
                raise ValueError(f"family {family_id} 的 index[{cell_idx}] 字段异常({kind})，无法修改")
            skipped.append((fam, kind)); continue
    if family_id != -1 and not patches:
        raise ValueError(f"family {family_id} 无任何可修改的 index[{cell_idx}] 字段")
    return patches, skipped, N


def plan_safe_guaiwu_cell_patch(data, family_id, cell_idx, new_val, label_hint="字段"):
    """只允许不增加 Java handle 的单个怪物字段修改。

    str->str 可原位变长，ref->ref 可重定向；null/missing 会增加新对象，明确拒绝。
    """
    if int(new_val) < 0:
        raise ValueError("怪物家族字段不能为负数")
    family_rows = R.read_all_guaiwu(data).get('families', [])
    if family_id < len(family_rows):
        field_info = next((info for info in family_rows[family_id].get('fields', {}).values()
                           if info.get('index') == cell_idx), None)
        if field_info is not None and not field_info.get('editable'):
            refs = field_info.get('incoming_references', 0)
            extra = f"，被其它 {refs} 个字段引用" if refs else ""
            raise ValueError(
                f"family {family_id} index[{cell_idx}] 当前为 {field_info.get('storage')}"
                f"{extra}，无法安全单独修改")
    start, count = R.locate_guaiwu(data)
    if start is None or not (0 <= family_id < count):
        raise ValueError(f"family {family_id} 不存在")
    p = start
    target = None
    for fam in range(count):
        cells, p = parse_inner_cells(data, p)
        if fam == family_id:
            target = cells
            break
    if not isinstance(target, list) or cell_idx >= len(target):
        raise ValueError(f"family {family_id} 没有 index[{cell_idx}] 字段")
    storage = target[cell_idx][0]
    if storage not in ('str', 'ref'):
        raise ValueError(
            f"family {family_id} index[{cell_idx}] 当前为 {storage}，"
            "写入会新增 Java handle，已为安全起见拒绝")
    patches, _, _ = plan_guaiwu_cell_patches(
        data, family_id, cell_idx, int(new_val), label_hint=label_hint)
    if not patches:
        return None
    if len(patches) != 1:
        raise ValueError("怪物字段定位结果不唯一")
    return patches[0]


def plan_attr9_patches(data, family_id, new_val):
    """返回 index[9]（熟练度）修改 patch 列表（7 元组）与跳过信息。

    index[9] 是每个 family String[] 的第 10 个元素，存放某种按卡的熟练度数值
    （如 MiaoSiTe/family72 = 2132，BingXinLing/family37 = 321）。命令 zp<id> <值>
    即改该 family 的 index[9]。
    7 元组 = (kind, label, new_val, start, old_len, new_bytes, old_val)
    family_id=-1 表示所有家族；否则仅指定家族。
    字段类型处理（与进阶等级一致）：
      - 'str'  : 原地覆盖（保持/扩宽宽度，前导 Q 补零）
      - 'ref'  : 该 family index[9] 是【共享串引用】，原地替换为内联 TC_STRING
                 （脱离共享），old_span=5（71 + 4B handle）
      - 'null' : 原为空，原地插入内联 TC_STRING，old_span=1（70）
    长度策略: 保持原字段宽度(前导 Q 补零)；新值位数超宽则最小扩宽(已实测安全)。"""
    names = R.load_family_names()
    start, N = R.locate_guaiwu(data)
    if start is None:
        raise ValueError("未定位到 GameGuaiWu（卡片数据区）")
    new_str = qe_enc(new_val)
    p = start
    patches = []
    skipped = []
    for fam in range(N):
        cells, p = parse_inner_cells(data, p)
        if family_id != -1 and fam != family_id:
            continue
        if not isinstance(cells, list):
            if family_id != -1:
                raise ValueError(f"family {family_id} 不是字符串数组(null/ref)，无法修改")
            skipped.append((fam, 'null/ref')); continue
        if len(cells) < 10:
            if family_id != -1:
                raise ValueError(f"family {family_id} 元素数<10，无 index[9] 字段")
            skipped.append((fam, '元素<10')); continue
        cell = cells[9]
        kind = cell[0]
        if kind == 'str':
            cur_L = len(cell[1])
            cur_val = R.qe_dec(cell[1])
            width = max(cur_L, len(new_str))
            tc_off = cell[2]
            old_span = 3 + cur_L
        elif kind == 'ref':
            cur_val = None
            width = max(len(new_str), 2)
            tc_off = cell[2]
            old_span = 5                      # 0x71 + 4 字节 handle
        elif kind == 'null':
            cur_val = None
            width = len(new_str)
            tc_off = cell[2]
            old_span = 1                      # 0x70
        else:
            if family_id != -1:
                raise ValueError(f"family {family_id} 的 index[9] 字段异常({kind})，无法修改")
            skipped.append((fam, kind)); continue
        enc = ('Q' * (width - len(new_str))) + new_str
        new_bytes = b'\x74\x00' + bytes([width]) + enc.encode('ascii')
        nm = names.get(fam, '?')
        label = f"family{fam}({nm}) 熟练度[9]"
        patches.append(('attr9', label, new_val, tc_off, old_span, new_bytes, cur_val))
    if family_id != -1 and not patches:
        raise ValueError(f"family {family_id} 无任何可修改的 index[9] 字段")
    return patches, skipped, N


def plan_level_patches(data, family_id, new_level):
    """返回等级(index[0])修改 patch 列表（7 元组）与跳过信息。

    等级存于每个 family String[] 的 index 0（第 1 个元素），QWERTY 编码。
    命令 mlv<id> <值> 即改该 family 的等级（如 mlv1 108 = family1 等级设 108）。
    7 元组 = (kind, label, new_val, start, old_len, new_bytes, old_val)
    family_id=-1 表示所有家族；否则仅指定家族。
    字段类型处理（与其它卡片字段一致）：
      - 'str'  : 原地覆盖（保持/扩宽宽度，前导 Q 补零）
      - 'ref'  : 该 family 等级是【共享串引用】，原地替换为内联 TC_STRING
                 （脱离共享），old_span=5（71 + 4B handle）
      - 'null' : 原为空，原地插入内联 TC_STRING，old_span=1（70）
    长度策略: 保持原字段宽度(前导 Q 补零)；新值位数超宽则最小扩宽(已实测安全)。"""
    names = R.load_family_names()
    start, N = R.locate_guaiwu(data)
    if start is None:
        raise ValueError("未定位到 GameGuaiWu（卡片数据区）")
    new_str = qe_enc(new_level)
    p = start
    patches = []
    skipped = []
    for fam in range(N):
        cells, p = parse_inner_cells(data, p)
        if family_id != -1 and fam != family_id:
            continue
        if not isinstance(cells, list):
            if family_id != -1:
                raise ValueError(f"family {family_id} 不是字符串数组(null/ref)，无法修改")
            skipped.append((fam, 'null/ref')); continue
        if len(cells) < 1:
            if family_id != -1:
                raise ValueError(f"family {family_id} 元素数<1，无等级字段")
            skipped.append((fam, '元素<1')); continue
        cell = cells[0]
        kind = cell[0]
        if kind == 'str':
            cur_L = len(cell[1])
            cur_val = R.qe_dec(cell[1])
            width = max(cur_L, len(new_str))
            tc_off = cell[2]
            old_span = 3 + cur_L
        elif kind == 'ref':
            cur_val = None
            width = max(len(new_str), 2)
            tc_off = cell[2]
            old_span = 5                      # 0x71 + 4 字节 handle
        elif kind == 'null':
            cur_val = None
            width = len(new_str)
            tc_off = cell[2]
            old_span = 1                      # 0x70
        else:
            if family_id != -1:
                raise ValueError(f"family {family_id} 的等级字段异常({kind})，无法修改")
            skipped.append((fam, kind)); continue
        enc = ('Q' * (width - len(new_str))) + new_str
        new_bytes = b'\x74\x00' + bytes([width]) + enc.encode('ascii')
        nm = names.get(fam, '?')
        label = f"family{fam}({nm}) 等级"
        patches.append(('level', label, new_level, tc_off, old_span, new_bytes, cur_val))
    if family_id != -1 and not patches:
        raise ValueError(f"family {family_id} 无任何可修改的等级字段")
    return patches, skipped, N

def locate_player_base(data):
    """返回 player int[3] 中第一个 int（槽0）的字节偏移 base，以及 [k0,k1,k2]。
    复用 soco_read.locate_player 的判据，但额外给出值起点 base。
    base+s*4 即槽 s(0起) 的 kind 值起点；每个 kind 为 4 字节大端整数。"""
    n = len(data)
    i = 0
    while True:
        i = data.find(b'\x75', i)          # TC_ARRAY
        if i < 0:
            break
        p = i + 1
        base = None
        q = None
        if data[p] == 0x72 and data[p + 1:p + 3] == b'\x00\x02' and data[p + 3:p + 5] == b'[I':
            q = p + 5 + 8 + 1              # name(2)+[I(2) + suid(8) + flags(1)
            fc = int.from_bytes(data[q:q + 2], 'big'); q += 2
            if q < n and data[q] == 0x78: q += 1
            if q < n and data[q] == 0x70: q += 1
            base = q + 4
            length = int.from_bytes(data[q:q + 4], 'big')
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
                return base, vals
        i += 1
    return None, None


def plan_slot_patches(data, slot_num, new_kind):
    """返回上阵卡片槽 kind 修改 patch 列表（7 元组）。

    上阵 3 槽的 kind 存于 GameData.player（int[3]），每个 kind = 5 × familyid。
    命令 solt<槽号> <kind> 即改该槽（如 solt1 375 = 槽1 上阵 kind375 = family75 哪吒）。
    7 元组 = (kind, label, new_val, start, old_len, new_bytes, old_val)
    slot_num ∈ {1,2,3} 表示单槽；-1 表示所有槽。
    长度策略: kind 为定长 4 字节大端整数，【原地覆盖、零位移】，安全。
    注意: 直接写入的 XXX 即存档里的 kind 值（= 5 × familyid），与之前 'familyid 375'
          用法一致；若希望按 familyid 设置，请自行 ×5（如 family75 -> 375）。"""
    base, vals = locate_player_base(data)
    if base is None:
        raise ValueError("未定位到 GameData.player（上阵卡片槽）")
    if slot_num == -1:
        targets = [0, 1, 2]
    else:
        if not (1 <= slot_num <= 3):
            raise ValueError(f"槽号必须 1~3 或 -1，收到 {slot_num}")
        targets = [slot_num - 1]
    if new_kind < 0 or new_kind > INT32_MAX:
        raise ValueError(f"kind 数值 {new_kind} 超出 int32 范围 [0, {INT32_MAX}]")
    patches = []
    for s in targets:
        off = base + s * 4
        old_val = vals[s]
        new_bytes = int(new_kind).to_bytes(4, 'big')
        label = f"槽{s + 1} kind(=5×familyid)"
        patches.append(('slot', label, new_kind, off, 4, new_bytes, old_val))
    return patches, [], 3


def locate_playername(data):
    """结构法定位 GameData.playerName。

    依据反编译源码 GameData.java:4585-4587 的读序列:
        ftime = readLong();  playerName = (String) readObject();  playerScore = (String) readObject();
    即 playerName 是紧接在 8 字节 ftime(long) 之后的那个 TC_STRING(74 00 LL ...)。
    而 playerScore 若与前面某串相同会被压成 TC_REFERENCE(71)，否则是独立 TC_STRING(74)。

    定位策略: 扫描所有 TC_STRING，筛选【前 8 字节是"合理时间戳 long"(毫秒, 2010~2071) 且 紧跟字节 ∈ {71,74}】者。
    要求前面是时间戳可过滤掉绝大多数误匹配（如金币/钻石等串前的 long 并非时间戳）。
    在真实存档中应唯一命中 playerName（houqinqingbaoData.playerName 其后为 int，且 playeruid 通常非时间戳范围）。
    返回 (tc_off, cur_L, post_byte) 或 None。"""
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
                    cands.append((i, L, post))
        i += 1
    if not cands:
        return None
    if len(cands) > 1:
        raise ValueError(f"playerName 定位到多个候选 {[hex(c[0]) for c in cands]}，无法唯一确定")
    return cands[0]


def plan_playername_patch(data, new_name):
    """返回玩家昵称修改 patch（7 元组）。

    new_name: 任意字符串（中文/英文/数字均可）。以 UTF-8 写入。
    长度策略: 【可变长 splice】—— 直接替换 TC_STRING 的内容与长度前缀，后续字节整体位移。
              因 Java 序列化 TC_REFERENCE 用 handle 序号引用（不依赖绝对偏移），位移不影响引用正确性，
              故该操作安全（与之前成功验证的"进阶等级扩宽"同策略）。
    返回 (kind, label, new_val, start, old_len, new_bytes, old_val)。
    ⚠ 若昵称当前为 TC_REFERENCE(71)/TC_NULL(70)（即昵称与前面串相同或被置空），本函数暂不支持，
      会抛出明确错误，需改用其它方式处理。"""
    loc = locate_playername(data)
    if loc is None:
        raise ValueError("未定位到 GameData.playerName（ftime 之后的 TC_STRING）。"
                         "可能昵称当前为引用/空，暂不支持直接修改。")
    tc_off, cur_L, post = loc
    raw = bytes(data[tc_off + 3:tc_off + 3 + cur_L])
    try:
        old_val = raw.decode('utf-8')
    except UnicodeDecodeError:
        old_val = repr(raw)
    new_utf8 = new_name.encode('utf-8')
    new_L = len(new_utf8)
    if new_L > 255:
        raise ValueError(f"昵称 UTF-8 长度 {new_L} 超过 TC_STRING 单字节长度前缀上限 255")
    start = tc_off                       # TC_STRING 起点
    old_len = 3 + cur_L                  # 74 00 L + content
    new_bytes = b'\x74\x00' + bytes([new_L]) + new_utf8
    label = "玩家昵称(playerName)"
    return ('playername', label, new_name, start, old_len, new_bytes, old_val)


def plan_chest_patch(data, new_remaining, which="card"):
    """返回周限宝箱剩余次数修改 patch（7 元组）。

    which='card'  -> 卡片宝箱(神秘宝箱 ShenMiBaoXiangData)第 1 个 QWERTY 计数串
    which='equip' -> 装备宝箱 第 2 个 QWERTY 计数串(仅双宝箱存档有效)

    剩余 = 5 - 已开启次数(opened)；存档仅存已开启次数，为一个 QWERTY 单字符串
    (Q0 W1 E2 R3 T4 Y5)。将剩余设为 N(0..5) 即把该串字符改为 (5-N) 对应字符。
    单字符替换，【零位移】，CRC 由主流程统一重算。
    7 元组 = (kind, label, new_val, start, old_len, new_bytes, old_val)
       kind='chest'，start=字符偏移，old_len=1，new_bytes=单字节字符。"""
    if which not in ("card", "equip"):
        raise ValueError("which 必须为 'card' 或 'equip'")
    if not (0 <= new_remaining <= BX.MAX_OPEN):
        raise ValueError(f"宝箱剩余次数必须 0..{BX.MAX_OPEN}")
    r = BX.analyze(data)
    if not r["found"]:
        raise ValueError("未定位到 ShenMiBaoXiangData")
    slot = r[which]
    if slot is None:
        avail = "双宝箱" if r["mode"] == "dual" else "单宝箱(仅卡片宝箱)"
        raise ValueError(f"该存档为{avail}，无法定位【{'卡片' if which=='card' else '装备'}宝箱】计数串")
    off, cur_opened, cur_remaining = slot
    target_opened = BX.MAX_OPEN - new_remaining
    ch = BX.QWERTY[target_opened]
    label = ("卡片宝箱(神秘宝箱) 剩余" if which == "card" else "装备宝箱 剩余")
    new_bytes = bytes([ord(ch)])
    return ('chest', label, new_remaining, off, 1, new_bytes, cur_remaining)


def verify_written(new_bytes, new_val):
    """直接从写入字节解码校验（TC_STRING / 8 字节 long / 句柄安全重定向引用）。"""
    if len(new_bytes) >= 3 and new_bytes[0] == 0x74 and new_bytes[1] == 0x00:
        L = new_bytes[2]
        raw = new_bytes[3:3 + L]
        if all(c in R.QWERTY.encode() for c in raw):
            return (R.qe_dec(raw) == new_val), R.qe_dec(raw)
        return False, None
    if new_bytes and new_bytes[0] == 0x71:
        # 句柄安全重定向：钻石改为引用金币内联串，值由金币保证 == new_val
        return True, new_val
    if len(new_bytes) == 8:
        got = int.from_bytes(new_bytes, 'big')
        return (got == new_val), got
    if len(new_bytes) == 4:
        got = int.from_bytes(new_bytes, 'big')
        return (got == new_val), got
    return False, None



def verify_at(data, name, pos, new_val):
    """直接读取最终缓冲区指定位置（pos=值起点），校验写入是否成功。"""
    if name in ('金币', '钻石'):
        tc = pos - 3                       # pos 为值起点，TC 起点需回退 3 字节
        if bytes(data[tc:tc + 2]) != b'\x74\x00':
            return False, None
        L = data[tc + 2]
        raw = bytes(data[tc + 3:tc + 3 + L])
        if not all(c in R.QWERTY.encode() for c in raw):
            return False, None
        return (R.qe_dec(raw) == new_val), R.qe_dec(raw)
    if name in ('木头', '石头', '结晶', '钥匙数量'):
        if pos + 8 > len(data):
            return False, None
        got = int.from_bytes(bytes(data[pos:pos + 8]), 'big')
        return (got == new_val), got
    got = int.from_bytes(bytes(data[pos:pos + 4]), 'big')
    return (got == new_val), got


def build_sidecar(data, patches, new_crc):
    """记录每个字段的【最终值起点】偏移与编码，以及文件 CRC。

    关键: 基于【已修改】的 data 重新定位所有数值字段偏移，而非沿用旧偏移。
    原因: 金币/钻石扩宽(TC_STRING 变长)会使后续所有字段整体后移，旧偏移失效。
          直接在已改好的 data 上用结构定位，偏移自然正确（与游戏流式反序列化一致）。
    长度保持类修改(无位移)下，重新定位结果与原偏移相同，行为不变。"""
    tcs = R.scan_tc(data)
    gold = R.find_gold(data, tcs)
    diamond = R.find_diamond(data, tcs)
    mats = R.find_materials(data)
    keys = R.find_keys(data)
    located = {'金币': gold, '钻石': diamond,
               '木头': mats[0] if len(mats) > 0 else None,
               '石头': mats[1] if len(mats) > 1 else None,
               '结晶': mats[2] if len(mats) > 2 else None,
               '钥匙数量': keys}
    fields = {}
    for name, entry in located.items():
        if entry is None:
            continue
        fields[name] = {"name": name, "off": entry['off'], "enc": ENC_MAP.get(name, 'int')}
    return {"crc": new_crc, "fields": fields}


def main():
    args = sys.argv[1:]
    dry = False
    out_path = None
    source = R.SOCO_PATH          # 始终就地改脚本同目录的 soco.bin（保留所有未指定字段）
    pairs = []
    card_requests = []      # [(family_id, value_str), ...]，f<id> / f-1
    adv_requests = []        # [(family_id, value_str), ...]，zf<id> / zf-1（进阶等级）
    attr_requests = []       # [(family_id, value_str), ...]，zp<id> / zp-1（index[9] 属性）
    level_requests = []      # [(family_id, value_str), ...]，mlv<id> / mlv-1（等级）
    slot_requests = []        # [(slot_num, value_str), ...]，solt1/2/3 <kind> / solt-1 <kind>（上阵槽）
    uname_requests = []        # [name_str, ...]，uname <昵称>（玩家昵称 playerName）
    chest_requests = []         # [value_str, ...]，bx <剩余> / baoxiang <剩余>（卡片宝箱剩余 0..5）
    equip_chest_requests = []   # [value_str, ...]，bxe <剩余> / equipbx <剩余>（装备宝箱剩余 0..5，仅双宝箱存档）
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--dry':
            dry = True
            i += 1
        elif a == '--out':
            out_path = args[i + 1]
            i += 2
        else:
            mz = re.match(r'^zf(-?\d+)$', a)
            if mz:
                # 进阶等级修改: zf<id> <值> / zf-1 <值>
                fid = int(mz.group(1))
                j = i + 1
                if j >= len(args) or args[j].startswith('-'):
                    print(f"[ERROR] 参数不完整: {a} 缺少进阶等级数值")
                    sys.exit(1)
                adv_requests.append((fid, args[j]))
                i = j + 1
            else:
                mzp = re.match(r'^zp(-?\d+)$', a)
                if mzp:
                    # index[9] 属性修改: zp<id> <值> / zp-1 <值>
                    fid = int(mzp.group(1))
                    j = i + 1
                    if j >= len(args) or args[j].startswith('-'):
                        print(f"[ERROR] 参数不完整: {a} 缺少属性数值")
                        sys.exit(1)
                    attr_requests.append((fid, args[j]))
                    i = j + 1
                else:
                    mlv = re.match(r'^mlv(-?\d+)$', a)
                    if mlv:
                        # 等级修改: mlv<id> <值> / mlv-1 <值>
                        fid = int(mlv.group(1))
                        j = i + 1
                        if j >= len(args) or args[j].startswith('-'):
                            print(f"[ERROR] 参数不完整: {a} 缺少等级数值")
                            sys.exit(1)
                        level_requests.append((fid, args[j]))
                        i = j + 1
                    else:
                        solt = re.match(r'^solt(-?\d+)$', a)
                        if solt:
                            # 上阵槽 kind 修改: solt<槽号> <kind> / solt-1 <kind>
                            snum = int(solt.group(1))
                            if snum != -1 and not (1 <= snum <= 3):
                                print(f"[ERROR] 槽号非法: {a}（应为 solt1/solt2/solt3 或 solt-1）")
                                sys.exit(1)
                            j = i + 1
                            if j >= len(args) or args[j].startswith('-'):
                                print(f"[ERROR] 参数不完整: {a} 缺少 kind 数值")
                                sys.exit(1)
                            slot_requests.append((snum, args[j]))
                            i = j + 1
                        else:
                            if a in ('bx', 'baoxiang'):
                                # 卡片宝箱(神秘宝箱)剩余次数修改: bx <剩余> / baoxiang <剩余>  (剩余 0..5)
                                j = i + 1
                                if j >= len(args) or args[j].startswith('-'):
                                    print(f"[ERROR] 参数不完整: {a} 缺少剩余次数数值(0~{BX.MAX_OPEN})")
                                    sys.exit(1)
                                chest_requests.append(args[j])
                                i = j + 1
                                continue
                            elif a in ('bxe', 'equipbx'):
                                # 装备宝箱剩余次数修改: bxe <剩余> / equipbx <剩余>  (剩余 0..5，仅双宝箱存档)
                                j = i + 1
                                if j >= len(args) or args[j].startswith('-'):
                                    print(f"[ERROR] 参数不完整: {a} 缺少剩余次数数值(0~{BX.MAX_OPEN})")
                                    sys.exit(1)
                                equip_chest_requests.append(args[j])
                                i = j + 1
                                continue
                            elif a == 'uname':
                                # 玩家昵称修改: uname <昵称>
                                j = i + 1
                                if j >= len(args):
                                    print(f"[ERROR] 参数不完整: uname 缺少昵称")
                                    sys.exit(1)
                                uname_requests.append(args[j])
                                i = j + 1
                                continue
                            else:
                                m = re.match(r'^f(-?\d+)$', a)
                            if m:
                                # 卡片数量修改: f<id> [卡片数量] <值> / f-1 [卡片数量] <值>
                                fid = int(m.group(1))
                                j = i + 1
                                if j < len(args) and args[j] == '卡片数量':
                                    j += 1
                                if j >= len(args) or args[j].startswith('-'):
                                    print(f"[ERROR] 参数不完整: {a} 缺少卡片数量数值")
                                    sys.exit(1)
                                card_requests.append((fid, args[j]))
                                i = j + 1
                            else:
                                if i + 1 >= len(args):
                                    print(f"[ERROR] 参数不完整: {a} 缺少数值")
                                    sys.exit(1)
                                pairs.append((a, args[i + 1]))
                                i += 2

    if not pairs and not card_requests and not adv_requests and not attr_requests and not level_requests and not slot_requests and not uname_requests and not chest_requests and not equip_chest_requests:
        print("用法: python3 soco_write.py 字段 数值 [字段 数值 ...] [--dry] [--out 文件]")
        print("      卡片数量: f<家族id> 卡片数量 <值>   例: f1 卡片数量 2026")
        print("                 f-1 卡片数量 <值>         例: f-1 卡片数量 0（所有家族）")
        print("      进阶等级: zf<家族id> <值>           例: zf1 99")
        print("                 zf-1 <值>                例: zf-1 0（所有家族）")
        print("      熟练度[9] : zp<家族id> <值>           例: zp1 2132")
        print("                 zp-1 <值>                例: zp-1 0（所有家族）")
        print("      等级    : mlv<家族id> <值>          例: mlv1 108")
        print("                 mlv-1 <值>               例: mlv-1 0（所有家族）")
        print("      上阵卡槽: solt<槽号> <kind>          例: solt1 375（槽1 kind=375=family75 哪吒）")
        print("                 solt-1 <kind>            例: solt-1 75（所有槽 kind=75=familyid15）")
        print("      玩家昵称: uname <昵称>              例: uname 反坦克炮239（改 playerName）")
        print("      卡片宝箱: bx <剩余>                 例: bx 5（神秘宝箱剩余设为 5，需关闭游戏后导入）")
        print("                 baoxiang <剩余>          例: baoxiang 5（同 bx，剩余 0~5）")
        print("      装备宝箱: bxe <剩余>                例: bxe 5（装备宝箱剩余设为 5，仅双宝箱存档有效）")
        print("                 equipbx <剩余>           例: equipbx 5（同 bxe，剩余 0~5）")
        sys.exit(1)

    target = out_path or R.SOCO_PATH
    if not os.path.exists(source):
        print(f"[ERROR] 未找到脚本同目录的存档: {source}\n        请确认 soco.bin 与脚本在同一目录")
        sys.exit(1)

    with open(source, 'rb') as f:
        data = bytearray(f.read())
    orig_data = bytes(data)  # 保存原始数据用于句柄修正

    cur = current_values(data, source)

    patches = []          # 数值类 patch（6 字段），传给 build_sidecar
    for name, valstr in pairs:
        if name not in cur:
            print(f"[ERROR] 未知字段: {name}（支持: {', '.join(cur.keys())}）")
            sys.exit(1)
        entry = cur[name]
        if entry is None:
            print(f"[ERROR] 未定位到字段: {name}")
            sys.exit(1)
        try:
            new_val = int(valstr)
        except ValueError:
            print(f"[ERROR] 数值非法: {valstr}")
            sys.exit(1)
        try:
            start, old_len, new_bytes = plan_patch(entry, new_val, data)
        except ValueError as e:
            print(f"[ERROR] {name}: {e}")
            sys.exit(1)
        kind = 'num' if name in ('金币', '钻石') else 'long8'
        patches.append((kind, name, new_val, start, old_len, new_bytes, entry['val']))

    # 卡片数量 patch（f<id> 卡片数量 <值> / f-1 卡片数量 <值>）
    card_patches = []
    for fid, valstr in card_requests:
        try:
            nv = int(valstr)
        except ValueError:
            print(f"[ERROR] 卡片数量数值非法: {valstr}")
            sys.exit(1)
        try:
            cp, skipped, N = plan_card_count_patches(data, fid, nv)
        except ValueError as e:
            print(f"[ERROR] f{fid}: {e}")
            sys.exit(1)
        card_patches.extend(cp)
        if fid == -1 and skipped:
            print(f"  (f-1 跳过 {len(skipped)} 个非str/null家族: "
                  + ', '.join(f'fam{f}({r})' for f, r in skipped[:20])
                  + (' ...' if len(skipped) > 20 else ''))

    # 进阶等级 patch（zf<id> <值> / zf-1 <值>）
    adv_patches = []
    for fid, valstr in adv_requests:
        try:
            nv = int(valstr)
        except ValueError:
            print(f"[ERROR] 进阶等级数值非法: {valstr}")
            sys.exit(1)
        try:
            ap, skipped, N = plan_adv_patches(data, fid, nv)
        except ValueError as e:
            print(f"[ERROR] zf{fid}: {e}")
            sys.exit(1)
        adv_patches.extend(ap)
        if fid == -1 and skipped:
            print(f"  (zf-1 跳过 {len(skipped)} 个无法修改的家族: "
                  + ', '.join(f'fam{f}({r})' for f, r in skipped[:20])
                  + (' ...' if len(skipped) > 20 else ''))

    # index[9] 属性 patch（zp<id> <值> / zp-1 <值>）
    attr_patches = []
    for fid, valstr in attr_requests:
        try:
            nv = int(valstr)
        except ValueError:
            print(f"[ERROR] 属性数值非法: {valstr}")
            sys.exit(1)
        try:
            atp, skipped, N = plan_attr9_patches(data, fid, nv)
        except ValueError as e:
            print(f"[ERROR] zp{fid}: {e}")
            sys.exit(1)
        attr_patches.extend(atp)
        if fid == -1 and skipped:
            print(f"  (zp-1 跳过 {len(skipped)} 个无法修改的家族: "
                  + ', '.join(f'fam{f}({r})' for f, r in skipped[:20])
                  + (' ...' if len(skipped) > 20 else ''))

    # 等级 patch（mlv<id> <值> / mlv-1 <值>）
    level_patches = []
    for fid, valstr in level_requests:
        try:
            nv = int(valstr)
        except ValueError:
            print(f"[ERROR] 等级数值非法: {valstr}")
            sys.exit(1)
        try:
            lp, skipped, N = plan_level_patches(data, fid, nv)
        except ValueError as e:
            print(f"[ERROR] mlv{fid}: {e}")
            sys.exit(1)
        level_patches.extend(lp)
        if fid == -1 and skipped:
            print(f"  (mlv-1 跳过 {len(skipped)} 个无法修改的家族: "
                  + ', '.join(f'fam{f}({r})' for f, r in skipped[:20])
                  + (' ...' if len(skipped) > 20 else ''))

    # 上阵卡槽 patch（solt<槽号> <kind> / solt-1 <kind>）
    slot_patches = []
    for snum, valstr in slot_requests:
        try:
            nv = int(valstr)
        except ValueError:
            print(f"[ERROR] kind 数值非法: {valstr}")
            sys.exit(1)
        try:
            sp, skipped, N = plan_slot_patches(data, snum, nv)
        except ValueError as e:
            print(f"[ERROR] solt{snum}: {e}")
            sys.exit(1)
        slot_patches.extend(sp)
        if nv % 5 != 0:
            print(f"  [提示] solt{snum} 的 kind={nv} 不是 5 的倍数，游戏中 familyid = kind/5 "
                  f"将不是整数，可能无法正确识别卡片。如需 familyid={nv}，请改用 kind={nv*5}。")

    # 玩家昵称 patch（uname <昵称>）
    name_patches = []
    for nm in uname_requests:
        try:
            np_ = plan_playername_patch(data, nm)
        except ValueError as e:
            print(f"[ERROR] uname: {e}")
            sys.exit(1)
        name_patches.append(np_)

    # 周限宝箱剩余次数 patch（bx <剩余> / baoxiang <剩余> = 卡片宝箱）
    chest_patches = []
    for valstr in chest_requests:
        try:
            nv = int(valstr)
        except ValueError:
            print(f"[ERROR] 宝箱剩余次数数值非法: {valstr}")
            sys.exit(1)
        try:
            cp = plan_chest_patch(data, nv, which="card")
        except ValueError as e:
            print(f"[ERROR] 宝箱: {e}")
            sys.exit(1)
        chest_patches.append(cp)

    # 装备宝箱剩余次数 patch（bxe <剩余> / equipbx <剩余>，仅双宝箱存档）
    equip_chest_patches = []
    for valstr in equip_chest_requests:
        try:
            nv = int(valstr)
        except ValueError:
            print(f"[ERROR] 装备宝箱剩余次数数值非法: {valstr}")
            sys.exit(1)
        try:
            cp = plan_chest_patch(data, nv, which="equip")
        except ValueError as e:
            print(f"[ERROR] 装备宝箱: {e}")
            sys.exit(1)
        equip_chest_patches.append(cp)

    all_patches = patches + card_patches + adv_patches + attr_patches + level_patches + slot_patches + name_patches + chest_patches + equip_chest_patches
    # 任意顺序 splice 均可，按偏移从高到低更稳妥（可变长度也安全）
    all_patches.sort(key=lambda p: p[3], reverse=True)

    print("=" * 60)
    print("  修改预览" + ("  (--dry, 不写入)" if dry else ""))
    print("=" * 60)
    for kind, name, new_val, start, old_len, new_bytes, old_val in all_patches:
        extra = f"  ({R.fmt_cn(new_val)})" if name in ('金币', '钻石') else ""
        print(f"  [{kind}] {name}: {old_val} -> {new_val}{extra}  @0x{start:X} (替换 {old_len} 字节)")

    # ⚠ 金币/钻石 风险提示
    qw_widen = [(name, new_val, old_len, len(new_bytes))
                for kind, name, new_val, start, old_len, new_bytes, old_val
                in all_patches if kind == 'num' and new_bytes[0] == 0x74 and len(new_bytes) != old_len]
    qw_redirect = [(name, new_val, old_len, len(new_bytes))
                   for kind, name, new_val, start, old_len, new_bytes, old_val
                   in all_patches if kind == 'num' and new_bytes[0] == 0x71]
    if qw_widen:
        print("\n  ⚠ 风险提示: 以下 QWERTY 串字段原宽度不足以容纳新值，已【扩宽】该 TC_STRING")
        print("    （保持 String 类型、整体后移后续字节）。Java 序列化用 handle 引用，理论安全；")
        print("    但此为破坏性位移操作，建议先用 --out 写入临时档、确认能进游戏后再覆盖 soco.bin。")
        for nm, nv, ol, nl in qw_widen:
            print(f"      - {nm}: 字节 {ol} -> {nl}  [+{nl - ol} 字节位移]")
    if qw_redirect:
        print("\n  ★ 句柄安全重定向: 钻石原为非内联(引用/空)形态，已改为【引用金币内联串】")
        print("    （共用句柄、零新增对象，不会破坏 Java 序列化句柄表，可安全进游戏）。")
        print("    前提: 钻石目标值 == 金币当前值（金币串即钻石值）。")
        for nm, nv, ol, nl in qw_redirect:
            print(f"      - {nm}: {ol} -> {nl} 字节（引用重定向，非扩宽）")

    if dry:
        return

    for kind, name, new_val, start, old_len, new_bytes, _ in all_patches:
        data = bytearray(splice(data, start, old_len, new_bytes))

    # ★ 句柄修正: 若有 ref→str 转换，修正下游 0x71 引用
    has_ref_conv = any(p[4] == 5 and p[5][0] == 0x74
                       for p in all_patches if p[5] is not None and isinstance(p[5], bytes))
    if has_ref_conv:
        # 需要原数据的 handle 表做修正 → 重新扫描
        old_handles, old_off2h = build_handle_table(orig_data)
        data = bytearray(fixup_handles_after_splice(data, old_handles, old_off2h))

    # ★ 重写头 4 字节 CRC32 校验和（覆盖 bytes[4:] 整个 payload）
    old_crc = int.from_bytes(bytes(data[0:4]), 'big')
    new_crc = zlib.crc32(bytes(data[4:])) & 0xffffffff
    data[0:4] = new_crc.to_bytes(4, 'big')
    print(f"\n  CRC32 校验和: {old_crc:08x} -> {new_crc:08x}  (已重写文件头 4 字节)")

    # 备份 + 写入（仅当目标是默认同目录 soco.bin 时备份）
    if target == R.SOCO_PATH and os.path.exists(R.SOCO_PATH):
        bak = R.SOCO_PATH + '.bak'
        shutil.copy2(R.SOCO_PATH, bak)
        print(f"  已备份原存档 -> {os.path.basename(bak)}")
    with open(target, 'wb') as f:
        f.write(data)
    print(f"  已写入 -> {target}")

    # sidecar（仅数值类字段；卡片数量由 soco_read 结构化读取，不入 sidecar）
    sidecar = build_sidecar(data, patches, new_crc)
    sidecar_path = target + R.SIDECAR_SUFFIX
    with open(sidecar_path, 'w', encoding='utf-8') as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=2)
    print(f"  已写入偏移映射 -> {os.path.basename(sidecar_path)}")

    # 校验
    print("\n  校验:")
    all_ok = True
    for kind, name, new_val, start, old_len, new_bytes, _ in all_patches:
        if kind == 'playername':
            Lb = new_bytes[2]
            got = new_bytes[3:3 + Lb].decode('utf-8', 'replace')
            ok = (got == new_val)
        elif kind == 'chest':
            ch = chr(new_bytes[0]) if new_bytes else '?'
            got = (BX.MAX_OPEN - BX.QWERTY.index(ch)) if ch in BX.QWERTY else None
            ok = (got == new_val)
        else:
            ok, got = verify_written(new_bytes, new_val)
        all_ok = all_ok and ok
        grew = len(new_bytes) - old_len
        tag = '' if grew == 0 else (f'  [+{grew}字节]' if grew > 0 else f'  [{grew}字节]')
        disp = f'  (游戏显示 {R.fmt_cn(new_val)})' if name in ('金币', '钻石') else ''
        print(f"    {name}: 期望 {new_val} / 实际 {got}  {'OK' if ok else '✗ 不匹配'}{tag}{disp}")

    if not all_ok:
        print("\n  [警告] 存在校验未通过的字段，请检查存档。")


def plan_player_hero_level_patch(data, new_level):
    """修改 player_Hero[0][1] 玩家等级。

    等级是 QWERTY TC_STRING，保持原宽度、前导 Q 补齐（无上限限制，
    新等级位数超过原宽度时自动扩展 TC_STRING 长度）。

    返回 (start, old_len, new_bytes) 或抛出 ValueError。
    """
    if new_level < 1:
        raise ValueError("玩家等级必须 >= 1")

    loc = R.locate_player_hero_level(data)
    if loc is None:
        raise ValueError("未定位到 player_Hero 等级字段")

    off = loc['off']

    # ── 新版：裸 int ──
    if loc.get('text') is None:
        old_len = 4
        new_bytes = new_level.to_bytes(4, 'big')
        return off, old_len, new_bytes

    # ── 旧版：QWERTY TC_STRING ──
    new_text = qe_enc(new_level)
    old_text = loc['text']
    old_len = 3 + len(old_text)  # 0x74 + 2B length + text

    width = max(len(old_text), len(new_text))
    padded = new_text.rjust(width, 'Q')  # Q=0 前导补齐

    new_bytes = bytes([0x74, 0x00, width]) + padded.encode('ascii')

    return off, old_len, new_bytes


def plan_player_jiesuo_patch(data, new_bools):
    """原地修改 GameData.player_jiesuo boolean[24] 数组。

    参数:
        data: 当前存档 bytes
        new_bools: 长度为 24 的 list/tuple，元素为 bool 或 int(0/1)

    返回 (offset, old_len, new_bytes) 或抛出 ValueError。
    零位移（数组长度固定 24），不需要 CRC 重算（由调用者统一做）。
    """
    loc = R.find_player_jiesuo(data)
    if loc is None:
        raise ValueError("未定位到 player_jiesuo boolean[24]")
    off, old_b = loc
    if len(new_bools) != 24:
        raise ValueError(f"new_bools 必须为 24 元素，实际 {len(new_bools)}")
    new_bytes = bytes([1 if b else 0 for b in new_bools])
    return off, 24, new_bytes


if __name__ == '__main__':
    main()
