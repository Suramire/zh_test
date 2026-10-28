
"""
模拟发送《召唤与合成》CMD 4 (P4_GenCounter 生成计数器/新账号)

作用: 服务端根据设备信息生成/返回账号(userName/passwd/nickName/uid/sid)。
这是"创建新账号"的第一步, 也是最重要的协议。

请求body(全大端):
  [long s_sessionId]    会话ID(未登录=0)
  [int  counter]        计数器
  [byte 4]              protocolID
  [long sid]            玩家sid(未登录=0)
  [int  lang]           语言
  [utf  version]        版本号 1.0.78
  [utf  channel]        渠道号 134
  [utf  device]         设备机型 Xiaomi#Redmi#...
  [utf  os]             系统版本 16
  [utf  sha1]           证书SHA1/设备指纹
  [utf  phoneSign]      渠道_签名 134_xxx
  [utf  more]           设备JSON

响应(跳过protocolID后):
  [utf userName]    服务端生成的账号名(如 Player1535656359691857920)
  [utf passwd]      服务端生成的密码
  [utf nickName]    服务端生成的昵称
  [long uid]        账号ID
  [long sid]        会话ID
  [long servertime] 服务器时间
  [utf status]      状态
"""
import argparse
import gzip
import json
import os
import struct
import subprocess
import time
import urllib.request
import zlib

URL = "http://zhaohe1.izhaohe.com:20000/heroes-server/Start"
# NetDataGen.java 目录(生成 NetData.bin)
NETDATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netdata_gen")
CMD = 0x04  # 4
# 账号信息输出文件(供 send_cmd1.py 读取)
ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhaohe_account.json")
# 账号信息输出文件(供 send_cmd1.py 读取)
ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhaohe_account.json")
# NetData.bin 输出路径(与容器结构一致)
NETDATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NetData.bin")

# 从统一设备指纹配置文件读取 (device_fingerprint.py)
from device_fingerprint import DEVICE as DEFAULT


def read_utf(buf, off):
    (n,) = struct.unpack_from(">H", buf, off); off += 2
    return buf[off:off + 2 * n].decode("utf-16-be", errors="replace"), off + 2 * n


def build_body(sid, lang, version, channel, device, os_, sha1, phone_sign, more,
               s_session_id=0, counter=0):
    b = struct.pack(">Q", s_session_id & 0xFFFFFFFFFFFFFFFF)
    b += struct.pack(">i", counter)
    b += bytes([CMD])
    b += struct.pack(">Q", sid & 0xFFFFFFFFFFFFFFFF)  # GenCounter的sid(未登录=0)
    b += struct.pack(">i", lang)                      # lang
    for s in (version, channel, device, os_, sha1, phone_sign, more):
        eb = s.encode("utf-16-be")
        b += struct.pack(">H", len(eb) // 2) + eb
    return b


def send_cmd4(sid=0, lang=0, s_session_id=0, counter=0, timeout=10, **fields):
    d = dict(DEFAULT)
    d.update({k: v for k, v in fields.items() if v is not None})
    body = build_body(sid, lang, d["version"], d["channel"], d["device"], d["os"],
                      d["sha1"], d["phoneSign"], d["more"], s_session_id, counter)

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
    off = 1  # 跳过 protocolID
    userName, off = read_utf(body_resp, off)
    passwd, off = read_utf(body_resp, off)
    nickName, off = read_utf(body_resp, off)
    (uid,) = struct.unpack_from(">Q", body_resp, off); off += 8
    (new_sid,) = struct.unpack_from(">Q", body_resp, off); off += 8
    (servertime,) = struct.unpack_from(">Q", body_resp, off); off += 8
    status, off = read_utf(body_resp, off)

    return http_code, {
        "userName": userName, "passwd": passwd, "nickName": nickName,
        "uid": uid, "sid": new_sid, "servertime": servertime, "status": status,
    }


def gen_netdata_bin(uid, user, passwd, other, out_file=NETDATA_FILE):
    """调用 NetDataGen.java 生成 NetData.bin (与容器内结构一致)"""
    classpath = NETDATA_DIR
    cmd = ["java", "-cp", classpath, "NetDataGen",
           str(uid), str(user), str(passwd), str(other), out_file]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        print(f"  ⚠️ NetData.bin 生成失败: {result.stderr.strip()}")
        return False
    print(f"  ✅ NetData.bin 已生成: {out_file}")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="模拟 CMD 4 GenCounter (生成新账号)")
    ap.add_argument("--sid", type=int, default=0, help="sid(未登录=0)")
    ap.add_argument("--lang", type=int, default=0, help="语言")
    ap.add_argument("--no-netdata", action="store_true", help="不生成 NetData.bin")
    for k in DEFAULT:
        ap.add_argument(f"--{k}", default=None, help=f"{k}(默认: {DEFAULT[k]})")
    args = ap.parse_args()

    fields = {k: (getattr(args, k) if getattr(args, k) is not None else DEFAULT[k]) for k in DEFAULT}
    print(f"发送 CMD 4 (GenCounter 生成账号) → {URL}")
    print(f"  设备: {fields['device']}  渠道: {fields['channel']}  版本: {fields['version']}")
    try:
        code, info = send_cmd4(sid=args.sid, lang=args.lang, **fields)
        print(f"HTTP {code}")
        print(f"== 服务端返回的新账号 ==")
        for k, v in info.items():
            print(f"  {k}: {v}")

        # 写入约定文件供 send_cmd1.py 读取
        with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 账号已写入 {ACCOUNT_FILE}")
        print("   下一步: python3 send_cmd1.py  (用该账号登录)")

        # 生成 NetData.bin (与容器内结构一致)
        if not args.no_netdata:
            gen_netdata_bin(info["uid"], info["userName"], info["passwd"], info["nickName"])
    except Exception as e:
        print(f"请求失败: {e}")
