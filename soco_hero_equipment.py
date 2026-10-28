#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析 soco.bin 中的英雄装备（CastleData.equipments HashMap）。

结构链（已从容器 core.dex smali 实锤）：
  soco.bin = CRC32(4B) + 多个根对象的 Java 序列化流
    根1: GameData —— 游戏魔改 SaveData 写入，TC_STRING 长度=1字节，无 TC_OBJECT
    根2: com.zhaohe.game.castle.CastleData —— 标准 ObjectOutputStream，TC_STRING 长度=2字节
      └─ equipments: java.util.HashMap  (key=Integer id, value=Equipment)
           Equipment(Serializable): 序列化字段(按 ObjectStreamClass 排序)
             原始类型: e(exp)I  id I  isLocked Z  isNew Z  k(kind)I  l(level)I  p(pinzhi)I
             对象:     buffs ArrayList<EquipmentBuff子类>
           ABuff: data:BuffData
             BuffData: base F / grow F / max F / maxgrow F / target I / type I
           GuaiWuLevelBuff 追加 faimilyIDData:CryptoNumber
           GuaiWuZhiYeBuff 追加 zhiyeData:CryptoNumber
           CryptoNumber: value:Ljava/lang/Number;  (Integer/Float/...)

用法:
  python3 soco_hero_equipment.py [soco.bin路径]
"""
import io
import os
import re
import struct
import sys

DEFAULT_SOCO = ('/Users/suramire/Library/Containers/'
                'DA269E38-2FF7-4002-9464-743920492C57/Data/Library/local/data/soco.bin')
FAMILYMAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'familyid_full_map.txt')

TC_NULL, TC_REFERENCE, TC_CLASSDESC, TC_OBJECT = 0x70, 0x71, 0x72, 0x73
TC_STRING, TC_ARRAY, TC_BLOCKDATA, TC_ENDBLOCKDATA, TC_BLOCKDATALONG = \
    0x74, 0x75, 0x77, 0x78, 0x7A
TC_ENUM = 0x7E
TC_RESET = 0x79
BASE_HANDLE = 0x7E0000
SC_WRITE_METHOD = 0x01
SC_EXTERNALIZABLE = 0x04

PRIM_WIDTH = {b'[Z': 1, b'[B': 1, b'[C': 2, b'[S': 2,
              b'[I': 4, b'[F': 4, b'[J': 8, b'[D': 8}
PRIM_READ = {
    'B': ('b', 1), 'C': ('>H', 2), 'D': ('>d', 8), 'F': ('>f', 4),
    'I': ('>i', 4), 'J': ('>q', 8), 'S': ('>h', 2), 'Z': ('>?', 1),
}

# 装备 skill_obj / BuffData.target 属性索引（EquipmentBuff.getShuXing 实锤）
ATTR = {0: '生命', 1: '攻击力', 2: '暴击率', 3: '物理强度',
        4: '法术强度', 5: '物理防御', 6: '魔法防御'}
# EFamilyClass.getIndex()（clinit 实锤）
# GameItem.convertToZhiYe(skill_obj2): 代码 + 1 后才是职业编号。
# 因此存档 tuple 的 obj2=0/1/2/3 分别对应肉盾/战士/射手/阿布野蛮人。
ZHIYE_RAW = {-1: '无', 0: '肉盾', 1: '战士', 2: '射手', 3: '阿布野蛮人'}
ZHIYE = {-1: '无', 1: '肉盾', 2: '战士', 3: '射手', 4: '阿布野蛮人'}
# 存档中英雄装备 kind = rand位×6 ∈ {6,12,...,54}（基址 0x2328 已去除）。
# item_type=100..105 是穿戴部位；GameItem.getItemType() 返回 type-100，
# 一键换装界面按 position 0..5 显示这六格。
HERO_EQUIP_KINDS = set(range(6, 55, 6))
HERO_EQUIP_RANGE = HERO_EQUIP_KINDS
# kind = 图标族位 * 6 + 品级（存档值为 1..54）。
# 品级由 (kind-1)%6 决定；用户提供的红框最高品级截图对应“传奇的”。
# 图标族位由 (kind-1)//6 决定，不能直接当作“宝物1/宝物2”。
QUALITY_NAME = {0: '普通的', 1: '优秀的', 2: '稀有的',
                3: '史诗的', 4: '神话的', 5: '传奇的'}

# 对应 UI_changeZhuangBei 的六个固定图标槽（position 0..5）。
# 由用户提供的最高品级（红框）装备截图逐项确认。
# 真正的部位由 kind 的图标族位决定：武器 3 种、衣服 2 种，其余各 1 种。
TYPE_NAME = {100: '武器', 101: '衣服', 102: '头盔',
             103: '鞋子', 104: '腰带', 105: '手腕'}


def kind_name(kind):
    if kind > 0:
        return QUALITY_NAME.get((kind - 1) % 6, '品级%s' % ((kind - 1) % 6))
    return '空'


def kind_family(kind):
    """返回图标族位（对应 item 图标族），用于诊断而非名称显示。"""
    return (kind - 1) // 6 if kind > 0 else None


def type_name(item_type):
    """对应 GameItem.getName(): zb_type[item_type % 100]。"""
    return TYPE_NAME.get(item_type, TYPE_NAME.get(item_type % 100 + 100,
                                                  '类别%s' % item_type))


def part_name(kind):
    """兼容旧调用；kind 本身不能可靠决定部位。"""
    return '图标族%s' % kind_family(kind)


def load_family_names():
    names = {}
    if not os.path.exists(FAMILYMAP_PATH):
        return names
    with open(FAMILYMAP_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            m = re.match(r'\s*family\s+(\d+)\s*=\s*(\S+)', line)
            if m:
                names[int(m.group(1))] = m.group(2)
    return names


# ── 阶段A：前缀扫描（GameData 根，魔改1字节串），只为数 handle ──
def find_castle_data(data, stream_start, track_handles=False):
    """从流头逐根对象解析，直到找到 CastleData。整个流为标准 Java 序列化。
    GameData 根由自定义 SaveData 写入，可能包含裸 blockdata，跳过即可。

    track_handles=True 时，ps.handle_alloc_offset 会记录每个 handle 的分配偏移
    （用于「在存档中插入新序列化对象」时精确计算需要后移的 handle 范围）。"""
    ps = Parser(data, stream_start, BASE_HANDLE)
    if track_handles:
        ps.handle_alloc_offset = {}
        ps.ref_positions = {}
    while ps.p < len(data):
        b = data[ps.p]
        if b == TC_BLOCKDATA:
            ps.p += 2 + data[ps.p + 1]
            continue
        if b == TC_BLOCKDATALONG:
            ps.p += 5 + int.from_bytes(data[ps.p + 1:ps.p + 5], 'big')
            continue
        if b == TC_ENDBLOCKDATA:
            ps.p += 1
            continue
        v = ps.read_content()
        if isinstance(v, dict) and v.get('__class__', '').endswith('CastleData'):
            return ps, v
    return ps, None


def walk_stream(data, stream_start=8, track_handles=True):
    """把【整个序列化流】从 stream_start 一路解析到文件末尾。

    ★ 与 find_castle_data 的区别：
      find_castle_data 找到 CastleData 就 return —— 句柄表只覆盖 CastleData 之前/之内。
      但对「插入新序列化对象后重编号句柄」这种操作，必须知道【全流】的
      句柄分配顺序和每一处 TC_REFERENCE 的位置，否则会漏改 CastleData 之后
      的那些引用（实测漏改会让游戏在 readNewArray/readClassDesc 处
      抛 ClassCastException: HashMap cannot be cast to ObjectStreamClass）。

    返回 (ps, ok, err)
      ps.handle_alloc_offset : {handle号: 分配时流偏移}
      ps.ref_positions       : {TC_REFERENCE偏移: 被引用handle号}
      ok=True 表示完整走到文件末尾；否则 err 说明在哪一偏移失败。
    """
    ps = Parser(data, stream_start, BASE_HANDLE)
    if track_handles:
        ps.handle_alloc_offset = {}
        ps.ref_positions = {}
    try:
        while ps.p < len(data):
            b = data[ps.p]
            if b == TC_BLOCKDATA:
                ps.p += 2 + data[ps.p + 1]
                continue
            if b == TC_BLOCKDATALONG:
                ps.p += 5 + int.from_bytes(data[ps.p + 1:ps.p + 5], 'big')
                continue
            if b == TC_ENDBLOCKDATA or b == TC_RESET:
                ps.p += 1
                continue
            ps.read_content()
        return ps, True, None
    except Exception as e:
        return ps, False, '%s @0x%x: %s' % (type(e).__name__, ps.p, e)


# ── 阶段B：标准 Java 序列化完整解析器（CastleData 子树）──
class Parser:
    def __init__(self, data, p, next_handle):
        self.d = data
        self.p = p
        self.h = next_handle
        self.handles = {}     # handle -> 解析结果
        self.stats = {}
        # 可选：记录每个 handle 的【分配时流偏移】。
        # Java 反序列化按出现顺序分配 handle，所以想知道「在某个偏移处插入新对象后，
        # 哪些 handle 需要后移」，就必须知道分配顺序与偏移的对应关系。
        # 默认为 None（不记录），不改变既有行为与性能。
        self.handle_alloc_offset = None
        # 可选：记录每个 TC_REFERENCE 的【位置 → 被引用 handle 号】。
        # 字节扫描无法区分「真引用」和「数据块里形似 0x71 的巧合字节」，
        # 要精确重编号必须依赖解析器的记录。
        self.ref_positions = None

    def _alloc(self, val):
        hh = self.h
        self.h += 1
        self.handles[hh] = val
        if self.handle_alloc_offset is not None:
            self.handle_alloc_offset[hh] = self.p
        return hh

    # classdesc -> dict(name, flags, fields=[(tc, fname)], super)
    def read_classdesc(self):
        d, p = self.d, self.p
        b = d[p]
        if b == TC_NULL:
            self.p = p + 1
            return None
        if b == TC_REFERENCE:
            hh = int.from_bytes(d[p + 1:p + 5], 'big')
            self.p = p + 5
            cd = self.handles.get(hh)
            assert isinstance(cd, dict) and 'fields' in cd, \
                'ref 0x%x 不是 classdesc @0x%x' % (hh, p)
            if self.ref_positions is not None:
                # ★ classDesc 引用同样是 TC_REFERENCE、同样占用共享 handle 计数器，
                #   插入新对象后也要同步后移。之前漏记这一类，导致
                #   readNewArray/readClassDesc 拿到错对象 → ClassCastException。
                self.ref_positions[p] = hh
            return cd
        assert b == TC_CLASSDESC, 'classdesc tag 0x%02x @0x%x' % (b, p)
        p += 1
        L = int.from_bytes(d[p:p + 2], 'big'); p += 2
        name_off = p
        name = bytes(d[p:p + L]).decode('utf-8'); p += L
        suid_off = p
        suid = int.from_bytes(d[p:p + 8], 'big'); p += 8
        hh = self._alloc({'name': name, 'suid': suid, 'fields': [],
                          '__name_offset__': name_off,
                          '__name_length__': L, '__suid_offset__': suid_off})  # 先占位
        flags = d[p]; p += 1
        nf = int.from_bytes(d[p:p + 2], 'big'); p += 2
        fields = []
        for _ in range(nf):
            tc = chr(d[p]); p += 1
            fl = int.from_bytes(d[p:p + 2], 'big'); p += 2
            fname = bytes(d[p:p + fl]).decode('utf-8'); p += fl
            self.p = p
            if tc in '[L':
                self.read_content()     # 类型串（TC_STRING），结果不需要
            p = self.p
            fields.append((tc, fname))
        self.p = p
        assert d[p] == TC_ENDBLOCKDATA, 'classdesc 尾 0x%02x @0x%x' % (d[p], p)
        p += 1
        self.p = p
        sup = self.read_classdesc()
        cd = self.handles[hh]
        cd.update(flags=flags, fields=fields, super=sup)
        return cd

    def read_string(self):
        d, p = self.d, self.p
        assert d[p] == TC_STRING, '@0x%x' % p
        L = int.from_bytes(d[p + 1:p + 3], 'big')     # 标准2字节
        s = bytes(d[p + 3:p + 3 + L]).decode('utf-8', 'replace')
        self._alloc(s)
        self.p = p + 3 + L
        return s

    def read_array(self):
        d = self.d
        p = self.p + 1
        self.p = p
        cd = self.read_classdesc()
        cname = cd['name'] if cd else None
        hh = self._alloc(None)                          # 占位
        p = self.p
        n = int.from_bytes(d[p:p + 4], 'big'); p += 4
        self.p = p
        w = PRIM_WIDTH.get(cname and cname.encode())
        if w is not None:
            raw = bytes(d[p:p + n * w])
            self.p = p + n * w
            fmt = {'[I': '>i', '[F': '>f', '[J': '>q', '[D': '>d',
                   '[S': '>h', '[C': '>H'}.get(cname)
            arr = list(struct.unpack('>%d%s' % (n, fmt[1]), raw)) if fmt and n else raw
        else:
            arr = [self.read_content() for _ in range(n)]
        self.handles[hh] = arr
        return arr

    def read_object(self):
        d = self.d
        assert d[self.p] == TC_OBJECT, '@0x%x' % self.p
        self.p += 1
        cd = self.read_classdesc()
        hh = self._alloc(None)                          # 占位防循环引用
        hierarchy = []
        c = cd
        while c is not None:
            hierarchy.append(c)
            c = c['super']
        hierarchy.reverse()                             # 超类在前
        obj = {'__class__': cd['name'], '__offsets__': {},
               '__class_name_offset__': cd.get('__name_offset__'),
               '__class_name_length__': cd.get('__name_length__'),
               '__class_suid_offset__': cd.get('__suid_offset__')}
        self.stats[cd['name']] = self.stats.get(cd['name'], 0) + 1
        if cd.get('flags', 0) & SC_EXTERNALIZABLE:
            # Externalizable：无默认字段，整体自定义数据直到 ENDBLOCKDATA
            obj['__custom__'] = self.read_annotation()
            self.handles[hh] = obj
            return self._post(obj)
        for c in hierarchy:
            for tc, fname in c['fields']:
                if tc in PRIM_READ:
                    fmt, w = PRIM_READ[tc]
                    obj['__offsets__'][fname] = (self.p, tc, w)
                    obj[fname] = struct.unpack_from(fmt, d, self.p)[0]
                    self.p += w
                else:
                    value = self.read_content()
                    # CryptoNumber 的 value 原本会被 _post() 简化成整数，
                    # 这样会丢失 Java 序列化中整数的写回偏移。保留它，供
                    # 伙伴装备的家族/职业词条直接编辑。
                    if fname == 'value' and cd and cd.get('name') == 'com.zhaohe.game.castle.CryptoNumber' \
                            and isinstance(value, int):
                        value = {'value': value,
                                 '__offsets__': {'value': (self.p - 4, 'I', 4)}}
                    obj[fname] = value
            if c.get('flags', 0) & SC_WRITE_METHOD:
                obj.setdefault('__custom__', []).extend(self.read_annotation())
        self.handles[hh] = obj
        return self._post(obj)

    # writeObject 自定义段：blockdata 与对象交替，直到 TC_ENDBLOCKDATA
    def read_annotation(self):
        d = self.d
        chunks = []
        while True:
            b = d[self.p]
            if b == TC_ENDBLOCKDATA:
                self.p += 1
                return chunks
            if b == TC_BLOCKDATA:
                L = d[self.p + 1]
                chunks.append(('blk', bytes(d[self.p + 2:self.p + 2 + L])))
                self.p += 2 + L
            elif b == TC_BLOCKDATALONG:
                L = int.from_bytes(d[self.p + 1:self.p + 5], 'big')
                chunks.append(('blk', bytes(d[self.p + 5:self.p + 5 + L])))
                self.p += 5 + L
            else:
                chunks.append(('val', self.read_content()))

    def read_content(self):
        b = self.d[self.p]
        if b == TC_NULL:
            self.p += 1
            return None
        if b == TC_REFERENCE:
            hh = int.from_bytes(self.d[self.p + 1:self.p + 5], 'big')
            self.p += 5
            assert hh in self.handles, '未知 handle 0x%x @0x%x' % (hh, self.p - 5)
            if self.ref_positions is not None:
                # 记录引用位置：偏移 → 被引用的 handle 号
                # 用于「插入新序列化对象」时精确重编号，避免字节扫描误判。
                self.ref_positions[self.p - 5] = hh
            return self.handles[hh]
        if b == TC_STRING:
            return self.read_string()
        if b == TC_ARRAY:
            return self.read_array()
        if b == TC_OBJECT:
            return self.read_object()
        if b == TC_ENUM:
            self.p += 1
            cd = self.read_classdesc()
            hh = self._alloc(None)
            name = self.read_content()
            self.handles[hh] = {'__enum__': cd['name'] if cd else '?', 'name': name}
            return self.handles[hh]
        raise AssertionError('content tag 0x%02x @0x%x' % (b, self.p))

    # 已知类的语义化后处理
    def _post(self, obj):
        cn = obj['__class__']
        if cn in ('java.lang.Integer', 'java.lang.Long', 'java.lang.Short',
                  'java.lang.Byte', 'java.lang.Float', 'java.lang.Double'):
            return obj.get('value')
        if cn == 'java.lang.Boolean':
            return obj.get('value')
        if cn == 'java.lang.Character':
            return chr(obj.get('value', 0))
        if cn == 'java.util.HashMap' or cn == 'java.util.LinkedHashMap':
            return self._custom_map(obj)
        if cn == 'java.util.HashSet':
            return self._custom_set(obj)
        if cn == 'java.util.ArrayList':
            return self._custom_list(obj)
        return obj

    def _custom_map(self, obj):
        out = {}
        vals = [v for kind, v in obj.get('__custom__', []) if kind == 'val']
        for i in range(0, len(vals) - 1, 2):
            k = vals[i]
            out[_hashable(k)] = vals[i + 1]
        obj['__map__'] = out
        return obj

    def _custom_set(self, obj):
        vals = [v for kind, v in obj.get('__custom__', []) if kind == 'val']
        obj['__set__'] = vals
        return obj

    def _custom_list(self, obj):
        vals = [v for kind, v in obj.get('__custom__', []) if kind == 'val']
        obj['__list__'] = vals
        return obj


def _hashable(v):
    if isinstance(v, (dict, list)):
        return repr(v)[:80]
    return v


def crypto_val(v):
    """CryptoNumber/Number -> 数值"""
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and 'value' in v:
        return crypto_val(v['value'])
    return None


# ── GameData 区的 beibaoItem（英雄装备仓库）扫描 ──
# GameItem.writeItem(oos,true) 布局：
#   writeInt(item_kind) writeInt(type)           → 8B 原始块（被后续串flush）
#   writeObject(item_level:String) writeObject(item_exp:String)  → 两个串token
#   writeInt(life,shAtk,baoji,wuq,moq,wuf,mof,skill_num)+writeBoolean
#   +5×writeInt(skill_kind,skill_obj,skill_obj2,skill_val)  → 113B
# 从第2件起，块 = 113B(上一件尾)+8B(本件头) = 121B，与两个串交替。
# 注：存档中英雄装备 item_kind = rand位×6（0x2328 基址已去掉），品质另存。

def qe_dec_safe(s):
    try:
        from soco_read import qe_dec
        return qe_dec(s.encode()) if isinstance(s, str) else qe_dec(s)
    except Exception:
        return None


def tokenize_region(ps, end):
    """把 [ps.p, end) 序列化为 token 列表。
    ('blk', bytes, 文件偏移) / ('val', 值, 文件偏移) / ('end', None, off)。遇错即停。"""
    toks = []
    d = ps.d
    while ps.p < end:
        b = d[ps.p]
        if b == TC_BLOCKDATA:
            L = d[ps.p + 1]
            toks.append(('blk', bytes(d[ps.p + 2:ps.p + 2 + L]), ps.p + 2))
            ps.p += 2 + L
        elif b == TC_BLOCKDATALONG:
            L = int.from_bytes(d[ps.p + 1:ps.p + 5], 'big')
            toks.append(('blk', bytes(d[ps.p + 5:ps.p + 5 + L]), ps.p + 5))
            ps.p += 5 + L
        elif b == TC_ENDBLOCKDATA:
            toks.append(('end', None, ps.p))
            ps.p += 1
        else:
            st = ps.p
            try:
                v = ps.read_content()
            except AssertionError:
                break
            toks.append(('val', v, st))
    return toks


# ── GameItem 记录链解码（str-pair 锚点法，不受 256B 块分割影响）──
# 单件记录流内顺序：head(8B) levelStr expStr tail(113B)；
# 相邻两件之间字节 = tail_prev(113)+head_next(8)=121B。

TAIL_LEN = 113
HEAD_LEN = 8
REC_LEN = HEAD_LEN + TAIL_LEN


def decode_gameitem_chains(toks):
    """扫描 token 流，返回 chain 列表；chain = 连续 record 列表。
    record 含每字段的文件偏移映射（供原位编辑）。"""
    chains = []
    cur = []                  # 当前链
    seg = bytearray()         # 上个 str-pair 之后的原始字节
    seg_offs = []             # 逐字节文件偏移
    open_rec = None           # 已拿到 head+双串、等待 tail 的记录

    def flush_open(seg_bytes, seg_offs_):
        # open_rec 的 tail = 当前 seg 的前 113B
        nonlocal open_rec
        if open_rec is not None and len(seg_bytes) >= TAIL_LEN:
            _fill_tail(open_rec, bytes(seg_bytes[:TAIL_LEN]), seg_offs_[:TAIL_LEN])
            cur.append(open_rec)
        open_rec = None

    def reset_chain():
        nonlocal cur, open_rec
        if len(cur) >= 3:
            chains.append(cur)
        cur = []
        open_rec = None

    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t[0] == 'blk':
            seg.extend(t[1])
            seg_offs.extend(range(t[2], t[2] + len(t[1])))
            i += 1
            continue
        if t[0] == 'val' and isinstance(t[1], str) and i + 1 < n \
                and toks[i + 1][0] == 'val' and isinstance(toks[i + 1][1], str):
            lvl, exp = t[1], toks[i + 1][1]
            lvl_off, exp_off = t[2], toks[i + 1][2]
            if open_rec is not None:
                # 上个记录的 tail + 本记录 head 应恰好 121B
                if len(seg) == REC_LEN:
                    _fill_tail(open_rec, bytes(seg[:TAIL_LEN]), seg_offs[:TAIL_LEN])
                    cur.append(open_rec)
                    head = bytes(seg[TAIL_LEN:REC_LEN])
                    hoff = seg_offs[TAIL_LEN:REC_LEN]
                else:
                    # 链断裂（中间混入其他字段）
                    flush_open(seg, seg_offs)
                    reset_chain()
                    seg, seg_offs, = bytearray(), []
                    i += 2
                    continue
            else:
                if len(seg) < HEAD_LEN:
                    seg, seg_offs = bytearray(), []
                    i += 2
                    continue
                head = bytes(seg[-HEAD_LEN:])
                hoff = seg_offs[-HEAD_LEN:]
            open_rec = _make_record(head, hoff, lvl, exp, lvl_off, exp_off)
            seg, seg_offs = bytearray(), []
            i += 2
            continue
        # 其他 token：链可能结束
        flush_open(seg, seg_offs)
        reset_chain()
        seg, seg_offs = bytearray(), []
        i += 1
    flush_open(seg, seg_offs)
    reset_chain()
    return chains


def _make_record(head, hoff, lvl, exp, lvl_off, exp_off):
    kind, itype = struct.unpack('>ii', head)
    return dict(kind=kind, type=itype,
                kind_off=hoff[0], type_off=hoff[4],
                level_str=lvl, exp_str=exp, level_off=lvl_off, exp_off=exp_off,
                level=qe_dec_safe(lvl), exp=qe_dec_safe(exp),
                tail_offs=None)


def _fill_tail(rec, tail, offs):
    vals = struct.unpack('>8i', tail[:32])
    (life, shatk, baoji, wuq, moq, wuf, mof, skill_num) = vals
    skills = []
    for s in range(5):
        off = 33 + s * 16
        k, o, o2, v = struct.unpack('>iiii', tail[off:off + 16])
        skills.append(dict(kind=k, obj=o, obj2=o2, val=v, base=off))
    rec.update(life=life, shatk=shatk, baoji=baoji, wuqiang=wuq,
               moqiang=moq, wufang=wuf, mofang=mof,
               skill_num=skill_num, skills=skills,
               tail_offs=list(offs))


def rec_int_off(rec, name):
    """返回记录某 int 字段的 4 个文件偏移（不连续返回 None）。"""
    if rec.get('tail_offs') is None:
        return None
    idx = {'life': 0, 'shatk': 4, 'baoji': 8, 'wuqiang': 12, 'moqiang': 16,
           'wufang': 20, 'mofang': 24, 'skill_num': 28}.get(name)
    if idx is not None:
        o = rec['tail_offs'][idx:idx + 4]
        return o if o[-1] - o[0] == 3 else None
    return None


def rec_skill_off(rec, si, field):
    """skill[si].field 的 4 字节文件偏移。field: kind/obj/obj2/val。"""
    if rec.get('tail_offs') is None:
        return None
    base = 33 + si * 16 + {'kind': 0, 'obj': 4, 'obj2': 8, 'val': 12}[field]
    o = rec['tail_offs'][base:base + 4]
    return o if len(o) == 4 and o[-1] - o[0] == 3 else None


def decode_beibao_records(toks):
    """兼容接口：返回所有链的记录平铺列表。"""
    out = []
    for ch in decode_gameitem_chains(toks):
        out.extend(ch)
    return out


def scan_raw_gameitems(data):
    """补扫嵌在复杂对象之间、tokenizer 未覆盖的 GameItem 连续记录。

    旧 GameData.writefile 会把 beibaoItem[15:60] 分三段写在大量其他对象之间；
    这些记录使用标准 TC_STRING + TC_BLOCKDATA，不能依赖简化 token 流。
    """
    out, seen = [], set()
    n = len(data)

    def read_string(pos):
        if pos + 3 > n or data[pos] != TC_STRING:
            return None
        size = struct.unpack('>H', data[pos + 1:pos + 3])[0]
        start, end = pos + 3, pos + 3 + size
        if end > n:
            return None
        try:
            return data[start:end].decode('utf-8'), start, end
        except UnicodeDecodeError:
            return None

    for off in range(4, n - 24):
        kind, itype = struct.unpack('>ii', data[off:off + 8])
        if kind not in HERO_EQUIP_KINDS or (itype % 100) not in range(6):
            continue
        a = read_string(off + 8)
        if a is None:
            continue
        b = read_string(a[2])
        if b is None:
            continue
        pos = b[2]
        if pos + 2 > n:
            continue
        if data[pos] == TC_BLOCKDATA:
            blen, body = data[pos + 1], pos + 2
        elif data[pos] == TC_BLOCKDATALONG and pos + 5 <= n:
            blen, body = struct.unpack('>I', data[pos + 1:pos + 5])[0], pos + 5
        else:
            continue
        if blen < TAIL_LEN or body + TAIL_LEN > n:
            continue
        rec = _make_record(data[off:off + 8], list(range(off, off + 8)),
                           a[0], b[0], a[1], b[1])
        _fill_tail(rec, bytes(data[body:body + TAIL_LEN]),
                   list(range(body, body + TAIL_LEN)))
        rec['_raw_scan'] = True
        rec['array'] = 'beibao'
        rec['slot'] = off
        if off not in seen:
            out.append(rec)
            seen.add(off)
    return out


SKILL_KIND_NAME = {0: '基础', 1: '转移召唤物', 2: '英雄额外提升',
                   3: '对家族增伤', 4: '对职业增伤'}


# ── 换装方案（ChangeZhuangBeiData）──
# writeExternal 顺序：writeObject(zhuangBeiList[8][6] key矩阵) + writeInt(autoKey)
#   + 每件 playerItem[6].key + 每件 beibaoItem[800].key（均在 blockdata 块内）
# 方案名 = GameData.heroEquipmentSuitName: String[8]。

def decode_suits(toks):
    """返回 dict(names/presets/auto_key/player_keys/beibao_keys)。"""
    czb, names = None, None
    for t in toks:
        if t[0] != 'val':
            continue
        v = t[1]
        if isinstance(v, dict) and v.get('__class__', '').endswith('ChangeZhuangBeiData'):
            czb = v
        elif names is None and isinstance(v, list) and len(v) == 8 \
                and all(isinstance(x, str) for x in v):
            names = v
    res = dict(names=names, presets=None, auto_key=None,
               player_keys=None, beibao_keys=None)
    if czb is None:
        return res
    arr, blkbytes = None, bytearray()
    for kind, v in czb.get('__custom__', []):
        if kind == 'blk':
            blkbytes += v
        elif isinstance(v, list) and arr is None:
            arr = v
    res['presets'] = arr
    n = len(blkbytes) // 4
    ints = struct.unpack('>%di' % n, bytes(blkbytes[:n * 4]))
    res['auto_key'] = ints[0]
    res['player_keys'] = list(ints[1:7])
    res['beibao_keys'] = list(ints[7:])
    return res


def assign_slots(chains, suits):
    """给链中记录标注 slot/key。
    当前真实存档的 beibaoItem 记录被 GameData 字段切成 4 条链
    （20+30+410+300），必须保持原始顺序整体与 beibao_keys 对齐；只取最长两条
    会让换装方案 key 指向错误装备。
    注：playerItem（穿戴中）在 writefile 中循环体为空，不写入存档，无法解析。
    返回 (beibao_records, key2rec)。"""
    if not chains:
        return [], {}
    # 链在流中的顺序就是槽位顺序；但旧存档可能少写若干空槽。
    beibao = [rec for chain in chains for rec in chain]
    bkeys = suits.get('beibao_keys') or []
    key2rec = {}
    # 用换装方案的 6 个固定部位作为锚点，对齐 key 序列和记录序列。
    # 这是必要的：GameData.writefile() 分段写数组时，旧版本存档会缺少部分槽位。
    refs = {}
    for row in suits.get('presets') or []:
        for col, key in enumerate(row):
            if key:
                refs.setdefault(key, set()).add(100 + col)
    # 当前穿戴的 playerItem 不写 GameItem 详情，但其 6 个 key 仍然对应
    # 固定的 6 个部位，必须参与对齐，否则无法在背包列表中反标穿戴状态。
    for col, key in enumerate(suits.get('player_keys') or []):
        if key:
            refs.setdefault(key, set()).add(100 + col)
    n, m = len(bkeys), len(beibao)
    inf = -10**9
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    take = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0
    for i in range(n + 1):
        for j in range(m + 1):
            cur = dp[i][j]
            if cur == inf:
                continue
            if i < n and cur - 1 > dp[i + 1][j]:
                dp[i + 1][j], take[i + 1][j] = cur - 1, ('skip_key', i, j)
            if j < m and cur - 1 > dp[i][j + 1]:
                dp[i][j + 1], take[i][j + 1] = cur - 1, ('skip_rec', i, j)
            if i < n and j < m:
                rec = beibao[j]
                score = 0
                allowed = refs.get(bkeys[i])
                if allowed:
                    # 方案 key 的列就是 Java 中的 item_type-100。
                    # 这里不能把“错误部位”的记录作为低分匹配，否则在
                    # 空槽/重复 key 较多时，穿戴 key 会被吞到错误记录上。
                    if rec['type'] not in allowed:
                        continue
                    score += 1000
                    score += 5 if rec['kind'] > 0 else -5
                elif rec['kind'] < 0:
                    score += 1
                if cur + score > dp[i + 1][j + 1]:
                    dp[i + 1][j + 1] = cur + score
                    take[i + 1][j + 1] = ('match', i, j)
    pairs = {}
    i, j = n, m
    while i or j:
        step = take[i][j]
        if step is None:
            break
        kind, pi, pj = step
        if kind == 'match':
            pairs[pi] = pj
        i, j = pi, pj
    for key_index, slot in pairs.items():
        rec = beibao[slot]
        rec['_key_slot'] = key_index
    for slot, rec in enumerate(beibao):
        rec['slot'] = slot
        rec['array'] = 'beibao'
        key_index = rec.get('_key_slot')
        if key_index is not None:
            rec['key'] = bkeys[key_index]
            if rec['kind'] > 0:
                key2rec[bkeys[key_index]] = rec
    return beibao, key2rec


def describe_skill(sk, fam_names):
    k, o, o2, v = sk['kind'], sk['obj'], sk['obj2'], sk['val']
    if k in (1, 2):
        attr = ATTR.get(o, '属性%s' % o)
        if k == 1:
            return '%s 转换至召唤物: %d' % (attr, v)
        # GameItem.getData()/BuffData 将 kind=2 的整数按 0.3% 显示。
        # 例如 raw=120 -> 36.0%，raw=72 -> 21.6%。保留原始值便于编辑。
        return '%s 英雄额外提升: %d (游戏显示 %.1f%%)' % (attr, v, v * 0.3)
    if k == 3:
        return '对%s(家族%d)增伤: %d' % (fam_names.get(o, '?'), o, v)
    if k == 4:
        attr = ATTR.get(o, '属性%s' % o)
        return '对%s类增加%s: %d' % (ZHIYE_RAW.get(o2, '?'), attr, v)
    return '基础词条(obj=%d,obj2=%d): %d' % (o, o2, v)


# ── 展示 ──
def describe_buff(b, level, fam_names):
    cn = b.get('__class__', '?').split('.')[-1]
    bd = b.get('data') or {}
    base, grow = bd.get('base', 0.0), bd.get('grow', 0.0)
    mx, mxg = bd.get('max', 0.0), bd.get('maxgrow', 0.0)
    target, btype = bd.get('target', 0), bd.get('type', 0)
    cur = base + grow * (level - 1)
    cap = mx + mxg * (level - 1)
    attr = ATTR.get(target, '属性%s' % target)

    def num(x):
        return ('%g' % round(x, 1))

    if cn == 'GuaiWuLevelBuff':
        fam = crypto_val(b.get('faimilyIDData'))
        nm = fam_names.get(fam, 'family%s' % fam)
        return ('召唤%s时提升怪物的等级: 1' % nm,
                dict(kind=cn, family=fam, base=base, grow=grow, max=mx, maxgrow=mxg))
    if cn == 'GuaiWuZhiYeBuff':
        zy = crypto_val(b.get('zhiyeData'))
        return ('召唤%s类怪物时提升怪物的%s: %s%%（最大 %s%%）' %
                (ZHIYE.get(zy, '职业%s' % zy), attr, num(cur), num(cap)),
                dict(kind=cn, zhiye=zy, target=attr, base=base, grow=grow))
    if cn == 'GuaiWuBaoLiuBuff':
        return '保留增加: 1', \
            dict(kind=cn, base=base, grow=grow)
    if cn == 'HeroToGuaiWuBuff':
        return ('%s转换至召唤物，转换上限为: %s（最大 %s）' % (attr, num(cap), num(cap)),
                dict(kind=cn, target=attr, base=base, grow=grow, max=mx, maxgrow=mxg))
    if cn == 'YongBingToGuaiWuBuff':
        return ('伙伴%s的 %s%% 转换至召唤物' % (attr, num(cur)),
                dict(kind=cn, target=attr, base=base, grow=grow))
    if cn == 'AddToHeroBuff':
        pct = '%' if btype in (1, 3) or abs(cur) < 200 and mx > 0 else ''
        return ('英雄的%s额外提升 %s%s（最大 %s%s）' %
                (attr, num(cur), pct, num(cap), pct),
                dict(kind=cn, target=attr, type=btype, base=base, grow=grow))
    if cn == 'SpecialGuardianBuff':
        return '特殊守护: %s（最大 %s）' % (num(cur), num(cap)), dict(kind=cn)
    # EquipmentBuff 基类：type=0 为固定值，type=1 为百分比。
    partner_attr = {0: '血量', 1: '伤害', 2: '暴击率', 3: '物理强度',
                    4: '魔法强度', 5: '物理防御', 6: '魔法防御'}.get(target, attr)
    suffix = '%' if btype == 1 else ''
    return '提升伙伴%s %s%s' % (partner_attr, num(cur), suffix), \
        dict(kind=cn, target=attr, type=btype, base=base, grow=grow)


def main():
    fp = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOCO
    data = bytearray(open(fp, 'rb').read())
    print('文件: %s (%d 字节)' % (fp, len(data)))
    p = 4
    assert data[p:p + 4] == b'\xac\xed\x00\x05', '非 Java 序列化流'
    stream_start = p + 4

    ps, castle = find_castle_data(data, stream_start)
    assert castle is not None, '未找到 CastleData 根对象'
    print('CastleData 解析完成，流推进到 0x%x / 0x%x' % (ps.p, len(data)))

    eq_field = castle.get('equipments')
    assert isinstance(eq_field, dict) and '__map__' in eq_field, 'equipments 字段异常'
    eqmap = eq_field['__map__']
    fam_names = load_family_names()

    hero, other = [], []
    for key, eq in eqmap.items():
        if not isinstance(eq, dict) or '__class__' not in eq:
            continue
        kind, level = eq.get('k', 0), eq.get('l', 0)
        entry = dict(id=eq.get('id'), kind=kind, level=level, exp=eq.get('e', 0),
                     pinzhi=eq.get('p', 0), locked=eq.get('isLocked', False),
                     new=eq.get('isNew', False))
        buffs = eq.get('buffs')
        blist = buffs.get('__list__', []) if isinstance(buffs, dict) else []
        entry['buffs'] = []
        for b in blist:
            if isinstance(b, dict):
                txt, raw = describe_buff(b, level, fam_names)
                entry['buffs'].append((txt, raw))
        (hero if kind in HERO_EQUIP_RANGE else other).append(entry)

    def show(lst, title):
        print('\n===== %s（%d 件）=====' % (title, len(lst)))
        for e in sorted(lst, key=lambda x: (x['kind'], x['id'] or 0)):
            print('\n[id=%s] kind=%d  等级:%d  经验:%d  品质:%d  %s' % (
                e['id'], e['kind'], e['level'], e['exp'], e['pinzhi'],
                '🔒锁定' if e['locked'] else ''))
            for txt, raw in e['buffs']:
                print('  ◆ %s' % txt)

    show(hero, '英雄装备 kind∈[9001,9054]（CastleData.equipments）')
    show(other, '其他装备（伙伴装备等）')

    # ── GameData 区 beibaoItem（真正的英雄装备仓库）──
    print('\n===== 扫描 GameData 区 GameItem 记录链 =====')
    toks = tokenize_region(ps, len(data))
    print('token 数: %d' % len(toks))
    chains = decode_gameitem_chains(toks)
    print('链数: %d  长度: %s' % (len(chains), [len(c) for c in chains]))
    suits = decode_suits(toks)
    beibao_chain, key2rec = assign_slots(chains, suits)
    recs = [r for ch in chains for r in ch]
    nonzero = [r for r in recs if r['kind'] > 0]
    print('记录共 %d 条，kind>0 的 %d 条（kind=-1 为空槽）' % (len(recs), len(nonzero)))
    hero_items = [r for r in nonzero if r['kind'] in HERO_EQUIP_RANGE]
    others = [r for r in nonzero if r not in hero_items]
    fam_names2 = load_family_names()

    # ── 换装方案 ──
    print('\n===== 换装方案（ChangeZhuangBeiData，%s）=====' %
          ('autoKey=%s' % suits['auto_key'] if suits['auto_key'] is not None else '未定位'))
    if suits['presets']:
        pset = set(k for k in (suits.get('player_keys') or []) if k)
        for pi, row in enumerate(suits['presets']):
            nm = (suits['names'] or ['?'] * 8)[pi] if suits['names'] else ('方案%d' % pi)
            cells = []
            for k in row:
                r = key2rec.get(k)
                if k in pset:
                    cells.append('穿戴中')
                elif r:
                    cells.append('Lv%s/%s' % (r['level'], kind_name(r['kind'])))
                else:
                    cells.append('空(%s)' % k)
            print('  [%d] %-8s: %s' % (pi, nm, ' | '.join(cells)))
    else:
        print('  （未定位到 zhuangBeiList）')

    print('\n===== 英雄装备（kind∈{6..54}，%d 件）=====' % len(hero_items))
    for r in hero_items:
        a = r.get('array', '?')
        loc = ('背包[%s]' % r.get('slot', '?')) if a == 'beibao' \
            else ('其他数组%s[%s]' % (a[5:], r.get('slot', '?')) if a.startswith('other')
                  else '%s[%s]' % (a, r.get('slot', '?')))
        print('\n%s key=%s kind=%d(%s) type=%d  等级:%s  经验:%s' % (
            loc, r.get('key', '?'), r['kind'], kind_name(r['kind']),
            r['type'], r['level'], r['exp']))
        base_attrs = []
        for nm, key in (('生命', 'life'), ('攻击力', 'shatk'), ('暴击率', 'baoji'),
                        ('物理强度', 'wuqiang'), ('法术强度', 'moqiang'),
                        ('物理防御', 'wufang'), ('魔法防御', 'mofang')):
            if r[key]:
                base_attrs.append('%s+%d' % (nm, r[key]))
        if base_attrs:
            print('  基础属性: ' + '  '.join(base_attrs))
        for sk in r['skills'][:max(r['skill_num'], 0)]:
            if sk['kind'] == 0 and sk['val'] == 0:
                continue
            print('  ◆ ' + describe_skill(sk, fam_names2))

    if others:
        print('\n===== 其他 GameItem（%d 件，按 kind 统计）=====' % len(others))
        from collections import Counter
        cnt = Counter(r['kind'] for r in others)
        print(dict(sorted(cnt.items()))[:20] if False else dict(sorted(cnt.items())))

    # 调试统计
    print('\n类实例统计(前12):', sorted(ps.stats.items(),
          key=lambda kv: -kv[1])[:12])


if __name__ == '__main__':
    main()
