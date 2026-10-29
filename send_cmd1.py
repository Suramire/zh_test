#!/usr/bin/env python3
"""
模拟发送《召唤与合成》CMD 1 (P1_UserLogin 用户登录)
用 CMD 4 (GenCounter) 生成的账号登录。账号信息默认从 send_cmd4.py 写入的 zhaohe_account.json 读取。

用法:
  python3 send_cmd1.py                                  # 从 zhaohe_account.json 读取账号
  python3 send_cmd1.py --account-file other.json        # 指定账号文件
  python3 send_cmd1.py --sid 123 --user PlayerX --passwd 456 --other PlayerY  # 手动指定

请求body(全大端):
  [long s_sessionId][int counter][byte 1]
  [long sid]        GenCounter返回的sid
  [utf userName]    GenCounter返回的账号名
  [utf passwd]      GenCounter返回的密码
  [utf otherName]   GenCounter返回的昵称
  [int lang]
  [utf version][utf channel][utf device][utf os][utf sha1][utf phoneSign][utf more]

响应(可能gzip, 跳protocolID后):
  [long uid][long sid][long servertime]
  [utf userName][utf passwd][utf otherName][utf status]
"""
import argparse
import base64
import gzip
import json
import os
import struct
import subprocess
import time
import urllib.request
import zlib

URL = "http://zhaohe1.izhaohe.com:20000/heroes-server/Start"
# 强制走本地代理(FlClash mixed-port 7890), 避免本机IP被服务器封禁
# PROXY = "http://127.0.0.1:7890"
# _proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
# urllib.request.install_opener(urllib.request.build_opener(_proxy_handler))
CMD = 0x01  # 1
CMD135 = 0x87  # 135 实名认证
ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhaohe_account.json")
# 登录结果输出文件(供 send_cmd65.py 等后续CMD读取 uid/sid)
LOGIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhaohe_login.json")
# 实名认证默认身份信息(姓名#身份证号)
REALNAME_INFO = "彭青娥#350500199107238624"
# 实名认证结果输出文件
REALNAME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhaohe_realname.json")
# NetData.bin (send_cmd4.py 生成, 登录凭证)
NETDATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NetData.bin")
# NetDataGen.java 目录(重新生成 NetData.bin)
NETDATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netdata_gen")

# 从统一设备指纹配置文件读取 (device_fingerprint.py)
from device_fingerprint import DEVICE as DEFAULT_DEVICE


def read_utf(buf, off):
    (n,) = struct.unpack_from(">H", buf, off); off += 2
    return buf[off:off + 2 * n].decode("utf-16-be", errors="replace"), off + 2 * n


def w_utf(s):
    b = s.encode("utf-16-be")
    return struct.pack(">H", len(b) // 2) + b


def parse_netdata_bin(path):
    """解析 NetData.bin, 返回 (CRC校验是否通过, 字段字典)
    结构: [4字节 CRC32 大端] + Java ObjectOutputStream 数据(14个字段)
    """
    d = open(path, "rb").read()
    if len(d) < 4:
        return False, {"错误": "文件过短"}

    # CRC 校验
    crc_file = int.from_bytes(d[:4], "big") & 0xFFFFFFFF
    crc_calc = zlib.crc32(d[4:]) & 0xFFFFFFFF
    crc_ok = crc_file == crc_calc

    fields = {"CRC": f"0x{crc_file:08x}", "CRC校验": "✅通过" if crc_ok else "❌不匹配"}

    off = 4
    if d[off:off + 4] != b"\xac\xed\x00\x05":
        return crc_ok, {"错误": "缺少Java序列化magic"}
    off += 4
    # TC_BLOCKDATA
    if d[off] != 0x77:
        return crc_ok, fields
    off += 1
    blen = d[off]; off += 1
    fields["blockdata长度"] = blen

    # blockdata 内: uid(long) + userName + passwd + otherName + chongxin(bool) + sdk_uid(UTF)
    (uid,) = struct.unpack_from(">q", d, off); off += 8
    fields["1.uid"] = uid
    for label in ("2.userName", "3.passwd", "4.otherName"):
        (n,) = struct.unpack_from(">H", d, off); off += 2
        fields[label] = d[off:off + n].decode("utf-8"); off += n
    fields["5.chongxin"] = d[off] == 1; off += 1
    (n,) = struct.unpack_from(">H", d, off); off += 2
    fields["6.sdk_uid"] = d[off:off + n].decode("utf-8"); off += n

    # blockdata 外对象字段: 标注类型(不深解析)
    tail = d[off:]
    fields["7.japaneseMaiDians"] = "boolean[13]全true" if tail[:2].hex() == "7572" else "?" + tail[:2].hex()
    # 后续字段: sdk_type(int)+sdk_youke_first+tw_shouciDenglu+changePhone 在下一个blockdata
    # 简化: 标注尾部剩余
    fields["尾部对象字段"] = f"{len(tail)}字节 (boolean[]/HashSet, 未深解析)"
    return crc_ok, fields


def gen_netdata_bin(uid, user, passwd, other, out_file=NETDATA_FILE):
    """用登录结果重新生成 NetData.bin (与游戏UserLogin后行为一致)"""
    if not os.path.isdir(NETDATA_DIR):
        print(f"  ⚠️ 缺少 {NETDATA_DIR}/NetDataGen.class, 跳过NetData.bin重写")
        return False
    cmd = ["java", "-cp", NETDATA_DIR, "NetDataGen",
           str(uid), str(user), str(passwd), str(other), out_file]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        print(f"  ⚠️ NetData.bin 重写失败: {result.stderr.strip()}")
        return False
    print(f"  ✅ NetData.bin 已用登录结果重写: {out_file}")
    return True


def encode_realname_data(name_id):
    """构造实名认证 data 字段: Base64(每字符Unicode码逗号分隔)"""
    ascii_str = ",".join(str(ord(c)) for c in name_id) + ","
    return base64.b64encode(ascii_str.encode()).decode()


def send_cmd135(sid, uid, data, phone="", code="", op_type=4, rtype=1,
                s_session_id=0, counter=0, timeout=10):
    """发送 CMD 135 实名认证. 返回 (http_code, result, opType)"""
    body = struct.pack(">Q", s_session_id & 0xFFFFFFFFFFFFFFFF)
    body += struct.pack(">i", counter)
    body += bytes([CMD135])
    body += struct.pack(">Q", sid & 0xFFFFFFFFFFFFFFFF)
    body += struct.pack(">Q", uid & 0xFFFFFFFFFFFFFFFF)
    for s in (phone, code, data):
        eb = s.encode("utf-16-be")
        body += struct.pack(">H", len(eb) // 2) + eb
    body += struct.pack(">i", op_type)
    body += struct.pack(">i", rtype)

    crc = zlib.crc32(body) & 0xFFFFFFFF
    payload = struct.pack(">I", len(body)) + struct.pack(">I", crc) + body

    key = f"{counter}^{int(time.time() * 1000)}"
    req = urllib.request.Request(URL, data=payload, method="POST")
    req.add_header("Connection", "Keep-Alive")
    req.add_header("KEY", key)
    resp = urllib.request.urlopen(req, timeout=timeout)
    http_code = resp.getcode()
    data_r = resp.read()
    if data_r[:2] == b"\x1f\x8b":
        data_r = gzip.decompress(data_r)

    (count, length) = struct.unpack_from(">II", data_r, 0)
    body_resp = data_r[8:8 + length]
    off = 1  # 跳过 protocolID
    (result,) = struct.unpack_from(">i", body_resp, off); off += 4
    (op_type_r,) = struct.unpack_from(">i", body_resp, off); off += 4
    return http_code, result, op_type_r


def do_realname(sid, uid, name_id):
    """执行实名认证, 写 zhaohe_realname.json"""
    data = encode_realname_data(name_id)
    print(f"\n== 实名认证 (CMD 135) ==")
    print(f"  身份信息: {name_id}")
    print(f"  data: {data}")
    try:
        code, result, op_type = send_cmd135(sid, uid, data)
        print(f"  HTTP {code}  result={result}  opType={op_type}")
        with open(REALNAME_FILE, "w", encoding="utf-8") as f:
            json.dump({"sid": sid, "uid": uid, "name_id": name_id,
                       "data": data, "result": result, "opType": op_type},
                      f, ensure_ascii=False, indent=2)
        if result == 0:
            print(f"  ✅ 实名认证成功 (result=0), 已写入 {REALNAME_FILE}")
        else:
            print(f"  ⚠️ 实名认证 result={result} (非0, 可能失败/需处理)")
    except Exception as e:
        print(f"  ⚠️ 实名认证请求失败: {e}")


def build_body(sid, user, passwd, other, lang, device, s_session_id=0, counter=1):
    b = struct.pack(">Q", s_session_id & 0xFFFFFFFFFFFFFFFF)
    b += struct.pack(">i", counter)
    b += bytes([CMD])
    b += struct.pack(">Q", sid & 0xFFFFFFFFFFFFFFFF)
    b += w_utf(user) + w_utf(passwd) + w_utf(other)
    b += struct.pack(">i", lang)
    for k in ("version", "channel", "device", "os", "sha1", "phoneSign", "more"):
        b += w_utf(device[k])
    return b


def _err255_text(body):
    """解析 cmd=255 错误包的文本（UTF-16BE）；解不出来就返回原始 hex。"""
    try:
        n = int.from_bytes(body[1:3], "big")
        txt = body[3:3 + n * 2].decode("utf-16-be")
        return txt or body.hex()
    except Exception:
        return body.hex()


def send_cmd1(sid, user, passwd, other, lang=0, device=None, s_session_id=0,
              counter=1, timeout=10):
    dev = dict(DEFAULT_DEVICE)
    if device:
        dev.update({k: v for k, v in device.items() if v is not None})
    body = build_body(sid, user, passwd, other, lang, dev, s_session_id, counter)

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
    # ★ cmd=255 是服务端的错误包：body = ff [Short]字符数 [UTF-16BE 文本]，
    #   例如「帐号异常，请联系客服!」= 账号被风控/封禁，重试没有任何意义。
    if body_resp and body_resp[0] == 255:
        raise RuntimeError(f"登录被拒绝：{_err255_text(body_resp)}")
    # ★ 正常回包 = 1 + 8*3 + 4 个 UTF 串（实测 127B）。只回 25B 时说明服务端只发了
    #   头部（uid/sid/servertime）就断了 —— 常见于同账号并发登录或服务端抖动，
    #   直接解下去会抛「unpack_from requires a buffer of at least 27 bytes」，看不懂。
    if len(body_resp) < 26:
        raise RuntimeError(
            f"登录回包异常（半截包）: cmd={body_resp[0] if body_resp else '?'} "
            f"len={len(body_resp)} raw={data.hex()}")
    off = 1  # 跳过 protocolID
    (uid,) = struct.unpack_from(">Q", body_resp, off); off += 8
    (new_sid,) = struct.unpack_from(">Q", body_resp, off); off += 8
    (servertime,) = struct.unpack_from(">Q", body_resp, off); off += 8
    userName, off = read_utf(body_resp, off)
    passwd2, off = read_utf(body_resp, off)
    otherName, off = read_utf(body_resp, off)
    status, off = read_utf(body_resp, off)

    return http_code, {
        "uid": uid, "sid": new_sid, "servertime": servertime,
        "userName": userName, "passwd": passwd2, "otherName": otherName,
        "status": status,
    }


def load_account(path):
    if not os.path.exists(path):
        print(f"❌ 账号文件不存在: {path}")
        print(f"   请先运行 python3 send_cmd4.py 生成账号")
        raise SystemExit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="模拟 CMD 1 UserLogin (用CMD4生成的账号登录)")
    ap.add_argument("--account-file", default=ACCOUNT_FILE, help="账号JSON文件(默认 zhaohe_account.json)")
    ap.add_argument("--sid", type=int, default=None, help="sid(默认读文件)")
    ap.add_argument("--user", default=None, help="userName(默认读文件)")
    ap.add_argument("--passwd", default=None, help="passwd(默认读文件)")
    ap.add_argument("--other", default=None, help="otherName(昵称, 默认读文件)")
    ap.add_argument("--lang", type=int, default=0, help="语言")
    ap.add_argument("--device", default=None, help="可传 key:value 覆盖设备字段")
    ap.add_argument("--no-realname", action="store_true", help="不做实名认证")
    ap.add_argument("--name-id", default=REALNAME_INFO, help=f"实名信息 姓名#身份证号(默认: {REALNAME_INFO})")
    args = ap.parse_args()

    if args.sid is None or args.user is None or args.passwd is None:
        acc = load_account(args.account_file)
        sid = args.sid if args.sid is not None else int(acc["sid"])
        user = args.user if args.user is not None else acc["userName"]
        passwd = args.passwd if args.passwd is not None else acc["passwd"]
        other = args.other if args.other is not None else acc["nickName"]
        print(f"从文件读取账号: {args.account_file}")
    else:
        sid, user, passwd = args.sid, args.user, args.passwd
        other = args.other if args.other else user

    device_over = None
    if args.device:
        device_over = {}
        for kv in args.device.split(","):
            if ":" in kv:
                k, v = kv.split(":", 1)
                device_over[k] = v

    print(f"发送 CMD 1 (UserLogin) → {URL}")
    print(f"  sid={sid}  user={user}  other={other}")
    try:
        code, info = send_cmd1(sid, user, passwd, other, args.lang, device_over)
        print(f"HTTP {code}")
        print(f"== 登录结果 ==")
        for k, v in info.items():
            print(f"  {k}: {v}")
        # 写入登录结果文件(供 send_cmd65.py 等后续CMD读取 uid/sid)
        with open(LOGIN_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 登录结果已写入 {LOGIN_FILE}")
        print("   下一步: python3 send_cmd65.py  (用登录的 uid/sid 查排行榜)")
        # status 格式: 形如 "1#0#1" / "2#0#1"
        # 首段: 1=账号存在/登录成功, 2=新账号; 非 1/2 才可能异常
        s0 = info["status"].split("#")[0]
        if s0 not in ("1", "2"):
            print(f"\n⚠️ status 首段={s0}, 非 1/2, 可能登录异常")
        else:
            print(f"\n✅ 登录成功 (status 首段={s0})")

        # 用登录结果重写 NetData.bin (与游戏 UserLogin 成功后行为一致)
        print("\n== 重写 NetData.bin (用登录结果) ==")
        gen_netdata_bin(info["uid"], info["userName"], info["passwd"], info["otherName"])

        # 读取并打印 NetData.bin 字段
        if os.path.exists(NETDATA_FILE):
            crc_ok, nd = parse_netdata_bin(NETDATA_FILE)
            print(f"\n== NetData.bin 内容 ({NETDATA_FILE}) ==")
            for k, v in nd.items():
                print(f"  {k}: {v}")
            if not crc_ok:
                print("  ⚠️ NetData.bin CRC校验失败, 游戏可能拒绝加载")
        else:
            print(f"\n⚠️ 未找到 NetData.bin: {NETDATA_FILE} (先运行 python3 send_cmd4.py)")

        # 实名认证 (登录成功后进行, 用登录后的 sid/uid)
        # if not args.no_realname:
        #     do_realname(info["sid"], info["uid"], args.name_id)
    except Exception as e:
        print(f"请求失败: {e}")
