#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Network reachability probe for the zhaohe game server.

Read-only from the game's point of view:
  * DNS resolution
  * TCP connect
  * HTTP GET /
  * HTTP POST with an 8-byte invalid payload (the server answers "net error!")
It never registers an account and never writes game data.
"""
import json
import socket
import ssl
import sys
import time
import urllib.request

HOST = "zhaohe1.izhaohe.com"
PORT = 20000
TIMEOUT = 3.0
LIMIT = 65536
INVALID_POST = b"\x00" * 8


class ProbeError(RuntimeError):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def _ms(t0):
    return int(round((time.perf_counter() - t0) * 1000))


def resolve(host=HOST):
    t0 = time.perf_counter()
    out = {"ok": False, "ms": 0, "ips": [], "error": None}
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        for fam, _typ, _proto, _canon, sockaddr in infos:
            ip = sockaddr[0]
            if ip not in out["ips"]:
                out["ips"].append(ip)
        out["ok"] = bool(out["ips"])
    except OSError as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    out["ms"] = _ms(t0)
    return out


def _first_ipv4(host):
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET,
                                  socket.SOCK_STREAM)
    except OSError:
        return None
    return infos[0][4][0]


def _connect(ip, port):
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((ip, port), timeout=TIMEOUT)
    except OSError as e:
        raise ProbeError("connect", "%s: %s" % (type(e).__name__, e))
    s.settimeout(TIMEOUT)
    return s, _ms(t0)


def tcp_check(name, ip, port):
    t0 = time.perf_counter()
    s = None
    try:
        s, connect_ms = _connect(ip, port)
        return {"name": name, "ip": ip, "port": port, "ok": True,
                "ms": _ms(t0), "connect_ms": connect_ms, "stage": "connected"}
    except ProbeError as e:
        return {"name": name, "ip": ip, "port": port, "ok": False,
                "ms": _ms(t0), "stage": e.stage, "error": str(e)}
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _request(method, path, body=b"", host=HOST):
    lines = [
        "%s %s HTTP/1.0" % (method, path),
        "Host: %s" % host,
        "User-Agent: zhaohe-netdiag/1",
        "Accept: */*",
        "Connection: close",
    ]
    if method == "POST":
        lines.append("Content-Type: application/octet-stream")
        lines.append("Content-Length: %d" % len(body))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _read_http(sock):
    sock.settimeout(TIMEOUT)
    raw = b""
    timed_out = False
    while len(raw) < LIMIT:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            timed_out = True
            break
        except OSError as e:
            raise ProbeError("recv", "%s: %s" % (type(e).__name__, e))
        if not chunk:
            break
        raw += chunk
    if not raw:
        raise ProbeError("recv", "read timeout with no data")
    return raw, timed_out


def _http_check(name, ip, port, method, path, body=b"", tls=False):
    t0 = time.perf_counter()
    s = None
    out = {"name": name, "ip": ip, "port": port, "ok": False, "ms": 0,
           "connect_ms": None, "tls_ms": None, "status": None,
           "body_bytes": 0, "body_hex": "", "tail": "", "stage": None,
           "error": None}
    try:
        s, out["connect_ms"] = _connect(ip, port)
        out["stage"] = "connected"
        if tls:
            tt = time.perf_counter()
            ctx = ssl._create_unverified_context()
            s = ctx.wrap_socket(s, server_hostname=HOST)
            out["tls_ms"] = _ms(tt)
            out["stage"] = "tls"
        out["stage"] = "send"
        s.sendall(_request(method, path, body=body))
        out["stage"] = "read"
        raw, timed_out = _read_http(s)
        head, _, payload = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1", "replace").split("\r\n")
        out["status"] = lines[0] if lines else ""
        out["headers"] = lines[1:11]
        out["body_bytes"] = len(payload)
        out["body_hex"] = payload[:80].hex()
        out["body_text"] = payload[:200].decode("utf-8", "replace")
        out["tail"] = payload[-120:].decode("latin-1", "replace")
        out["timed_out"] = timed_out
        out["ok"] = out["status"].startswith("HTTP/")
        out["stage"] = "done"
    except ProbeError as e:
        out["stage"] = e.stage
        out["error"] = str(e)
    except Exception as e:
        out["stage"] = out["stage"] or "unknown"
        out["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
    out["ms"] = _ms(t0)
    return out


def _tls_check(name, ip, port, server_hostname):
    t0 = time.perf_counter()
    s = None
    out = {"name": name, "ip": ip, "port": port, "ok": False,
           "ms": 0, "connect_ms": None, "tls_ms": None,
           "version": None, "cipher": None, "stage": None, "error": None}
    try:
        s, out["connect_ms"] = _connect(ip, port)
        out["stage"] = "connected"
        tt = time.perf_counter()
        ctx = ssl._create_unverified_context()
        s = ctx.wrap_socket(s, server_hostname=server_hostname)
        out["tls_ms"] = _ms(tt)
        out["version"] = s.version()
        out["cipher"] = s.cipher()[0]
        out["ok"] = True
        out["stage"] = "done"
    except ProbeError as e:
        out["stage"] = e.stage
        out["error"] = str(e)
    except Exception as e:
        out["stage"] = out["stage"] or "tls"
        out["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
    out["ms"] = _ms(t0)
    return out


def egress_ip():
    for url in ("https://icanhazip.com", "https://api.ipify.org",
                "https://ifconfig.me/ip"):
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            ip = urllib.request.urlopen(req, timeout=4).read()
            ip = ip.decode("utf-8", "replace").strip()
            if ip:
                return {"ok": True, "ms": _ms(t0), "ip": ip, "source": url}
        except Exception:
            pass
    return {"ok": False, "ms": 0, "ip": None, "source": None}


def probe(host=HOST, port=PORT, timeout=TIMEOUT):
    global TIMEOUT
    TIMEOUT = float(timeout)
    t0 = time.perf_counter()
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "host": host,
           "port": int(port), "python": sys.version.split()[0],
           "checks": []}
    dns = resolve(host)
    out["dns"] = dns
    v4 = [ip for ip in dns.get("ips") or [] if ":" not in ip]
    if not v4 and dns.get("ok"):
        v4 = [dns["ips"][0]]
    for ip in v4[:2]:
        tcp = tcp_check("tcp-%d" % port, ip, port)
        out["checks"].append(tcp)
        if tcp.get("ok"):
            out["checks"].append(_http_check(
                "get-%d" % port, ip, port, "GET", "/"))
            out["checks"].append(_http_check(
                "post-invalid-%d" % port, ip, port, "POST",
                "/heroes-server/Start", INVALID_POST))
        if port != 443:
            tcp443 = tcp_check("tcp-443", ip, 443)
            out["checks"].append(tcp443)
            if tcp443.get("ok"):
                out["checks"].append(_http_check(
                    "https-get-443", ip, 443, "GET", "/", tls=True))
    baidu_ip = _first_ipv4("baidu.com")
    if baidu_ip:
        out["checks"].append(tcp_check("baidu-tcp-443", baidu_ip, 443))
        out["checks"].append(_tls_check("baidu-tls-443", baidu_ip, 443, "baidu.com"))
    else:
        out["checks"].append({"name": "baidu-dns", "ok": False,
                              "stage": "resolve", "error": "resolve failed"})
    out["checks"].append(tcp_check("cloudflare-tcp-443", "1.1.1.1", 443))
    out["egress"] = egress_ip()
    out["ms"] = _ms(t0)
    return out


if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
