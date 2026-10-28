#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读写竞技场积分（GameData.playerScore）。

背景
====
竞技场界面显示的「积分」= `GameData.playerScore`（`UI_PVP.smali` 里引用 19 次；
`playerPVPzhi` 在 UI_PVP 里一次都没用到，那是另一个字段）。

它存在 soco.bin 里，是 Java 序列化的一个 String，用游戏自定义的 CryptoNumber 编码：

    0→Q  1→W  2→E  3→R  4→T  5→Y  6→U  7→I  8→O  9→P

即十进制每一位换成 "QWERTYUIOP" 里对应字符（"EQQ"=200，"WI"=17）。

定位方式
========
`GameData.readObject` 的顺序是 `ftime=readLong(); playerName=readObject(); playerScore=readObject();`
所以 playerScore 就是紧跟在 playerName 那个 TC_STRING 之后的那个 TC_STRING。
playerName 用 `soco_read.find_playername` 定位（结构法，唯一命中）。

注意
====
· 游戏运行时会随时重写 soco.bin，改之前请**完全退出游戏**。
· CMD29（竞技场结算上报）只给服务端记战报，不改这个本地值，改协议没用。

用法
====
    # 读
    python3 tools/pvp_score.py
    python3 tools/pvp_score.py --path /path/to/soco.bin

    # 改（自动备份 + 重算 CRC32）
    python3 tools/pvp_score.py --set 500
    python3 tools/pvp_score.py --set 500 --dry     # 只演练不写盘
"""
import argparse
import glob
import os
import shutil
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import soco_read as R          # noqa: E402

ALPH = "QWERTYUIOP"            # numbers[0..9]
CONTAINER_GLOB = os.path.expanduser(
    '~/Library/Containers/*/Data/Library/local/data/soco.bin')


def enc(n):
    """整数 → CryptoNumber 字符串"""
    return "".join(ALPH[int(d)] for d in str(int(n)))


def dec(s):
    """CryptoNumber 字符串 → 整数（非法字符返回 None）"""
    if not s or any(c not in ALPH for c in s):
        return None
    return int("".join(str(ALPH.index(c)) for c in s))


def autodetect():
    hits = sorted(glob.glob(CONTAINER_GLOB), key=os.path.getmtime, reverse=True)
    return hits[0] if hits else None


def locate_player_score(data):
    """定位 GameData.playerScore 的 TC_STRING。

    返回 dict(token_off, len_off, content_off, length, text, value, name)
    """
    pn = R.find_playername(data)
    if not pn:
        raise RuntimeError("没定位到 playerName，无法定位 playerScore")

    name_end = pn['off'] + len(pn['raw'])          # playerName 内容结束处 = 下一个 token 起点
    if data[name_end] != 0x74:
        raise RuntimeError(
            f"playerName 之后不是 TC_STRING(0x74)，而是 0x{data[name_end]:02x}"
            f" @0x{name_end:x}（存档结构可能变了）")

    length = int.from_bytes(data[name_end + 1:name_end + 3], 'big')
    content_off = name_end + 3
    raw = bytes(data[content_off:content_off + length])
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError:
        raise RuntimeError(f"playerScore 内容不是 ASCII: {raw.hex()}")

    value = dec(text)
    if value is None:
        raise RuntimeError(f"playerScore 内容不是合法 CryptoNumber: {text!r}")

    return {"token_off": name_end, "content_off": content_off,
            "length": length, "text": text, "value": value,
            "name": pn['enc']}


def patch_player_score(data, new_value):
    """把竞技场积分改成 new_value，返回 (new_bytes, info)。

    · 编码后长度不变 → 原地覆盖
    · 长度变化 → 拼接（Java 序列化按顺序读，长度只在自身 2 字节前缀里，
      不新增/删除对象，handle 编号不受影响）
    · 最后重算文件头 4 字节 CRC32
    """
    data = bytes(data)
    loc = locate_player_score(data)
    new_text = enc(new_value).encode('ascii')

    head = data[:loc["token_off"]]
    tail = data[loc["content_off"] + loc["length"]:]
    token = bytes([0x74]) + len(new_text).to_bytes(2, 'big') + new_text
    out = bytearray(head + token + tail)

    old_crc = int.from_bytes(bytes(out[0:4]), 'big')
    new_crc = zlib.crc32(bytes(out[4:])) & 0xffffffff
    out[0:4] = new_crc.to_bytes(4, 'big')

    info = {"old": loc["value"], "new": int(new_value),
            "old_text": loc["text"], "new_text": new_text.decode(),
            "token_off": loc["token_off"], "len_delta": len(new_text) - loc["length"],
            "old_crc": old_crc, "new_crc": new_crc,
            "old_size": len(data), "new_size": len(out)}
    return bytes(out), info


def main():
    ap = argparse.ArgumentParser(description="读写竞技场积分（GameData.playerScore）")
    ap.add_argument("--path", default=None, help="soco.bin 路径（默认自动探测）")
    ap.add_argument("--set", type=int, default=None, help="把积分改成这个值")
    ap.add_argument("--dry", action="store_true", help="只演练，不写盘")
    args = ap.parse_args()

    path = args.path or autodetect()
    if not path or not os.path.exists(path):
        sys.exit("[ERROR] 找不到 soco.bin，请用 --path 指定")

    data = open(path, 'rb').read()
    stored = int.from_bytes(data[0:4], 'big')
    calc = zlib.crc32(data[4:]) & 0xffffffff
    print(f"存档: {path}")
    print(f"  {len(data)} 字节  CRC {'OK' if stored == calc else '无效'}")

    loc = locate_player_score(data)
    print(f"  玩家昵称    : {loc['name']}")
    print(f"  竞技场积分  : {loc['value']}  （存档里存的串 {loc['text']!r} @0x{loc['content_off']:X}）")

    if args.set is None:
        return

    if args.set < 0:
        sys.exit("[ERROR] 积分不能为负")

    out, info = patch_player_score(data, args.set)
    print(f"\n改为 {info['new']}：{info['old_text']!r} → {info['new_text']!r}"
          f"（长度 {info['len_delta']:+d} 字节）")
    print(f"  CRC32 {info['old_crc']:08x} → {info['new_crc']:08x}")
    print(f"  文件 {info['old_size']} → {info['new_size']} 字节")

    # 回读校验
    loc2 = locate_player_score(out)
    if loc2["value"] != info["new"]:
        sys.exit(f"[ERROR] 回读校验失败：{loc2['value']} != {info['new']}")

    if args.dry:
        print("  [dry] 未写盘")
        return

    bak = f"{path}.bak.pvp.{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(path, bak)
    with open(path, 'wb') as f:
        f.write(out)
    print(f"  [OK] 已写入，备份 {os.path.basename(bak)}")
    print("  ⚠ 进游戏验证一次；若被服务端存档同步覆盖回来，说明服务端也存了一份。")


if __name__ == '__main__':
    main()
