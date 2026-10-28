#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""soco.bin 的 Java 序列化 handle 精确追踪器。

存档结构极简：CRC32(4字节) + Java 序列化流，流内只有
  TC_ARRAY([[Ljava/lang/String; 和 [Ljava/lang/String;)
  TC_STRING / TC_REFERENCE / TC_NULL / TC_CLASSDESC
四类内容，因此可以做精确的 handle 编号模拟。

Java handle 分配规则（关键）：
  基址 0x7E0000，每遇到一个 **新对象** 分配一个递增 handle。
  分配 handle 的：TC_STRING(0x74)、TC_CLASSDESC(0x72)、TC_ARRAY(0x75)、TC_OBJECT(0x73)
  不分配的       ：TC_REFERENCE(0x71)、TC_NULL(0x70)、TC_ENDBLOCKDATA(0x78)

  注意顺序：TC_ARRAY 的 handle 在其 classdesc **之后** 才分配；
  TC_CLASSDESC 自己先分配 handle，再读它的内容。

本档字符串长度用 1 字节（游戏魔改，非标准 2 字节），与 soco_read 保持一致。
"""

BASE = 0x7E0000

TC_NULL = 0x70
TC_REFERENCE = 0x71
TC_CLASSDESC = 0x72
TC_OBJECT = 0x73
TC_STRING = 0x74
TC_ARRAY = 0x75
TC_BLOCKDATA = 0x77
TC_ENDBLOCKDATA = 0x78
TC_BLOCKDATALONG = 0x7A


class HandleMap:
    """扫描整个流，建立 handle -> (offset, kind, value) 的精确映射。"""

    def __init__(self, data):
        self.data = data
        self.next_handle = BASE
        self.by_handle = {}      # handle -> dict(off, kind, raw)
        self.strings = {}        # handle -> bytes（仅 TC_STRING）
        self._scan()

    # ── handle 分配 ──
    def _alloc(self, kind, off, raw=None):
        h = self.next_handle
        self.next_handle += 1
        self.by_handle[h] = {'off': off, 'kind': kind, 'raw': raw}
        if kind == 'string':
            self.strings[h] = raw
        return h

    # ── 扫描入口 ──
    def _scan(self):
        d = self.data
        # 跳过 4 字节 CRC + 2 字节 magic(aced) + 2 字节 version(0005)
        p = 4
        if d[p:p + 2] == b'\xac\xed':
            p += 4
        self.stream_start = p
        # 存档是多个根对象顺序写入，需要一直读到文件尾
        n = len(d)
        self.parse_error = None
        while p < n:
            try:
                np_ = self._read_value(p)
            except (ValueError, IndexError) as e:
                self.parse_error = '@0x%x: %s' % (p, e)
                break
            if np_ <= p:
                self.parse_error = '@0x%x: 未前进' % p
                break
            p = np_
        self.stream_end = p

    # ── 读一个 value（可能是 array / string / ref / null）──
    def _read_value(self, p):
        d = self.data
        b = d[p]
        if b == TC_NULL:
            return p + 1
        if b == TC_REFERENCE:
            return p + 5
        if b == TC_STRING:
            L = d[p + 2]
            raw = bytes(d[p + 3:p + 3 + L])
            self._alloc('string', p, raw)
            return p + 3 + L
        if b == TC_ARRAY:
            return self._read_array(p)
        if b == TC_BLOCKDATA:                     # 0x77 len(1) data
            return p + 2 + d[p + 1]
        if b == TC_BLOCKDATALONG:                 # 0x7A len(4) data
            return p + 5 + int.from_bytes(d[p + 1:p + 5], 'big')
        if b == TC_ENDBLOCKDATA:
            return p + 1
        if b == TC_OBJECT:
            return self._read_object(p)
        raise ValueError('未知 tag 0x%02x @0x%x' % (b, p))

    # ── TC_OBJECT ──
    def _read_object(self, p):
        obj_off = p
        p += 1
        p, cname = self._read_classdesc(p)
        self._alloc('object', obj_off, cname)
        # 本存档未出现自定义对象；若出现需按 classdesc 字段表读取，
        # 这里保守报错而不是猜测，避免 handle 编号静默错位。
        raise ValueError('遇到未支持的 TC_OBJECT %r @0x%x' % (cname, obj_off))

    # 基本类型数组的元素宽度
    _PRIM_WIDTH = {b'[Z': 1, b'[B': 1, b'[C': 2, b'[S': 2,
                   b'[I': 4, b'[F': 4, b'[J': 8, b'[D': 8}

    # ── TC_ARRAY ──
    def _read_array(self, p):
        d = self.data
        arr_off = p
        p += 1                                   # 吃掉 0x75
        p, cname = self._read_classdesc(p)        # classdesc 先分配 handle
        self._alloc('array', arr_off, cname)      # 数组自己的 handle
        n = int.from_bytes(d[p:p + 4], 'big')
        p += 4
        w = self._PRIM_WIDTH.get(cname)
        if w is not None:                         # 基本类型数组：定宽跳过
            return p + n * w
        for _ in range(n):                        # 对象数组：逐元素递归
            p = self._read_value(p)
        return p

    # ── classDesc：可能是完整定义、引用或 null ──
    def _read_classdesc(self, p):
        d = self.data
        b = d[p]
        if b == TC_NULL:
            return p + 1, None
        if b == TC_REFERENCE:
            h = int.from_bytes(d[p + 1:p + 5], 'big')
            info = self.by_handle.get(h)
            return p + 5, (info or {}).get('raw')
        if b != TC_CLASSDESC:
            raise ValueError('classdesc 位置出现 0x%02x @0x%x' % (b, p))
        cd_off = p
        p += 1
        L = int.from_bytes(d[p:p + 2], 'big')     # 类名用标准 2 字节长度
        p += 2
        cname = bytes(d[p:p + L])
        p += L
        p += 8                                    # serialVersionUID
        self._alloc('classdesc', cd_off, cname)   # 先分配 handle，再读字段
        p += 1                                    # flags
        nf = int.from_bytes(d[p:p + 2], 'big')    # fieldCount
        p += 2
        for _ in range(nf):                       # String[] 恒为 0，防御性保留
            tc = d[p]; p += 1
            fl = int.from_bytes(d[p:p + 2], 'big'); p += 2
            p += fl
            if tc in (ord('['), ord('L')):
                p = self._read_value(p)
        if d[p] == TC_ENDBLOCKDATA:
            p += 1
        p, _ = self._read_classdesc(p)            # superClassDesc
        return p, cname

    # ── 查询 ──
    def resolve(self, handle):
        """handle -> bytes（仅字符串）；非字符串或不存在返回 None。"""
        return self.strings.get(handle)

    def find_string_handles(self, raw):
        """返回所有内容等于 raw 的字符串 handle 列表（按 handle 升序）。"""
        return sorted(h for h, v in self.strings.items() if v == raw)


def build(data):
    return HandleMap(data)


if __name__ == '__main__':
    import sys
    import soco_read as R
    fp = sys.argv[1]
    d = bytearray(open(fp, 'rb').read())
    hm = HandleMap(d)
    print('流范围 0x%x - 0x%x (文件长 %d)' % (hm.stream_start, hm.stream_end, len(d)))
    print('handle 总数 %d, 其中字符串 %d' % (len(hm.by_handle), len(hm.strings)))
    for target in (b'T', b'R', b'Y', b'E'):
        hs = hm.find_string_handles(target)
        print('值 %r (=%s) 的 handle: %s%s' % (
            target, R.qe_dec(target), hs[:5], ' ...' if len(hs) > 5 else ''))
