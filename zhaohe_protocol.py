#!/usr/bin/env python3
"""
《召唤与合成》通用协议模拟器

用法:
  python3 zhaohe_protocol.py -c 21                       # CMD 21 公告(无额外参数)
  python3 zhaohe_protocol.py -c 65 -p startDay:0 endDay:0  # CMD 65 排行榜
  python3 zhaohe_protocol.py -c 65 -sid 1535356357681655808 -uid 1531416818877509632 -p startDay:0 endDay:0
  python3 zhaohe_protocol.py -list                       # 列出所有协议

协议规范(全大端):
  请求body: [long s_sessionId][int counter][byte CMD][请求字段按schema顺序]
  外层:     [int blen][int crc32][body]
  请求头:   Connection: Keep-Alive, KEY: <counter>^<毫秒时间戳>
  响应:     [int count][int len][body(可能gzip)]
  body第一个字节是protocolID(跳过), 之后按响应schema解析
  readUTF = UTF-16BE: [short N][2N字节]
"""
import argparse
import gzip
import struct
import time
import urllib.request
import zlib

URL = "http://zhaohe1.izhaohe.com:20000/heroes-server/Start"

# 协议ID -> 名称 (取自 core.dex ProtocolDefine.smali, 常用部分)
PROTOCOLS = {
    0x1: "P1_UserLogin", 0x15: "P21_GonggaoCode(公告)", 0x16: "P22_ImportNameCode",
    0x18: "P24_PaihangbangCode", 0x41: "P65_LevelTopList(等级排行榜)",
    0x64: "P100_OrderId", 0x65: "P101_OrderResult", 0x88: "P136_UserRankUpload",
    0x89: "P137_UserRankList", 0x8a: "P138_UserRankConfig", 0x8e: "P142_NewBossStatus",
    0x8f: "P143_NewBossRank", 0x90: "P144_NewBossDamage", 0x92: "P146_UploadValue",
    0x96: "P150_BanHaoAccount", 0x97: "P151_BanHaoChangeValue",
}

# ==================== 请求字段类型 ====================
# 每个协议: 请求schema = [(类型, 参数名, 默认值), ...]
# 类型: 'long','int','short','byte','bool','utf'
# 响应schema = ('plain'|'gzip', 解析器)

def w_long(v): return struct.pack(">Q", int(v) & 0xFFFFFFFFFFFFFFFF)
def w_int(v):  return struct.pack(">i", int(v))
def w_short(v): return struct.pack(">H", int(v))
def w_byte(v): return struct.pack(">B", int(v))
def w_bool(v): return struct.pack(">?", bool(v))
def w_utf(v):
    b = v.encode("utf-16-be")
    return struct.pack(">H", len(b) // 2) + b


def read_utf(buf, off):
    (n,) = struct.unpack_from(">H", buf, off); off += 2
    return buf[off:off + 2 * n].decode("utf-16-be", errors="replace"), off + 2 * n


def build_request_body(protocol_id, fields):
    """fields = [(类型,值),...] 按schema顺序写入"""
    b = w_long(0)  # s_sessionId (占位, 通常0)
    b += w_int(0)  # counter (占位)
    b += w_byte(protocol_id)
    for typ, val in fields:
        if typ == "long":  b += w_long(val)
        elif typ == "int": b += w_int(val)
        elif typ == "short": b += w_short(val)
        elif typ == "byte": b += w_byte(val)
        elif typ == "bool": b += w_bool(val)
        elif typ == "utf": b += w_utf(str(val))
        else: raise ValueError(f"未知类型 {typ}")
    return b


def send_protocol(protocol_id, fields, sid, uid, s_session_id=0, counter=1, timeout=10):
    """发送任意CMD. fields 只含 CMD后的业务字段(不含sid/uid, sid/uid单独传)"""
    # 请求头: 公共前缀 + CMD + sid + uid + 业务字段
    body = w_long(s_session_id) + w_int(counter) + w_byte(protocol_id)
    body += w_long(sid) + w_long(uid)
    for typ, val in fields:
        if typ == "long":  body += w_long(val)
        elif typ == "int": body += w_int(val)
        elif typ == "short": body += w_short(val)
        elif typ == "byte": body += w_byte(val)
        elif typ == "bool": body += w_bool(val)
        elif typ == "utf": body += w_utf(str(val))
        else: raise ValueError(f"未知类型 {typ}")

    crc = zlib.crc32(body) & 0xFFFFFFFF
    payload = struct.pack(">I", len(body)) + struct.pack(">I", crc) + body

    key = f"{counter}^{int(time.time() * 1000)}"
    req = urllib.request.Request(URL, data=payload, method="POST")
    req.add_header("Connection", "Keep-Alive")
    req.add_header("KEY", key)
    resp = urllib.request.urlopen(req, timeout=timeout)
    http_code = resp.getcode()
    data = resp.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)

    (count, length) = struct.unpack_from(">II", data, 0)
    body_resp = data[8:8 + length]
    pid = body_resp[0]  # protocolID
    off = 1
    return http_code, pid, body_resp, off


def parse_cmd21(body, off):
    (gid,) = struct.unpack_from(">i", body, off); off += 4
    title, off = read_utf(body, off)
    value, off = read_utf(body, off)
    return {"gonggaoid": gid, "title": title, "公告": value}


def parse_cmd65(body, off):
    (result,) = struct.unpack_from(">i", body, off); off += 4
    (my_rank,) = struct.unpack_from(">i", body, off); off += 4
    (my_rank_pro,) = struct.unpack_from(">i", body, off); off += 4
    (size,) = struct.unpack_from(">H", body, off); off += 2
    players = []
    for _ in range(size):
        (tmp_len,) = struct.unpack_from(">H", body, off); off += 2
        (puid,) = struct.unpack_from(">Q", body, off); off += 8
        name, off = read_utf(body, off)
        (plevel,) = struct.unpack_from(">i", body, off); off += 4
        players.append({"uid": puid, "名字": name, "等级": plevel})
    return {"result": result, "我的排名": my_rank, "myRankPro": my_rank_pro,
            "榜单人数": size, "榜单": players}


# 协议 schema 表: cmd -> (请求业务字段, 响应解析器, 描述)
SCHEMAS = {
    0x15: ([], parse_cmd21, "公告"),                                   # CMD 21
    0x41: ([("int", "startDay", 0), ("int", "endDay", 0)], parse_cmd65, "等级排行榜"),  # CMD 65
}


def main():
    ap = argparse.ArgumentParser(description="《召唤与合成》通用协议模拟器")
    ap.add_argument("-c", "--cmd", type=lambda x: int(x, 0), help="协议ID(十进制或0x十六进制)")
    ap.add_argument("-sid", type=int, default=1535356357681655808, help="玩家sid")
    ap.add_argument("-uid", type=int, default=1531416818877509632, help="玩家uid")
    ap.add_argument("-p", "--params", nargs="*", default=[], help="业务参数 key:value 覆盖schema默认")
    ap.add_argument("-list", action="store_true", help="列出所有已知协议")
    args = ap.parse_args()

    if args.list:
        print("已知协议:")
        for pid, name in sorted(PROTOCOLS.items(), key=lambda x: x[0]):
            print(f"  0x{pid:02x} ({pid}) : {name}")
        return

    if args.cmd is None:
        ap.error("需要 -c 指定协议ID, 或用 -list 查看")

    if args.cmd not in SCHEMAS:
        print(f"协议 0x{args.cmd:02x} 无内置schema, 请在 SCHEMAS 中配置")
        print(f"  对应名称: {PROTOCOLS.get(args.cmd, '未知')}")
        return

    req_schema, parser, desc = SCHEMAS[args.cmd]
    # 解析参数覆盖
    fields = []
    overrides = {}
    for p in args.params:
        if ":" in p:
            k, v = p.split(":", 1)
            overrides[k] = v
    for typ, name, default in req_schema:
        val = overrides.get(name, default)
        fields.append((typ, val))

    print(f"发送 CMD {args.cmd} ({desc}) → {URL}")
    print(f"  sid={args.sid}  uid={args.uid}  参数={dict((n, v) for _, n, _ in req_schema)}")
    try:
        http_code, pid, body, off = send_protocol(args.cmd, fields, args.sid, args.uid)
        print(f"HTTP {http_code}  响应protocolID={pid}")
        result = parser(body, off)
        for k, v in result.items():
            if isinstance(v, list):
                print(f"{k}: {len(v)}")
                for i, item in enumerate(v, 1):
                    print(f"  {i}. {item}")
            else:
                print(f"{k}: {v}")
    except Exception as e:
        print(f"请求失败: {e}")


if __name__ == "__main__":
    main()
