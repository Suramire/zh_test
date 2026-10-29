#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号数据的 MySQL（TiDB）存储层。

设计要点
========
* **可选**：没配置数据库（或没装 PyMySQL）时 `enabled()` 返回 False，
  `web/server.py` 会自动退回原来的 accounts.json，服务照常可用。
* **无侵入**：`server.py` 启动时把 `offline_play` 的 `_load_account_doc` /
  `_save_account_doc` / `_account_from_file` 换成这里的实现，
  于是所有既有调用点（save_account / load_saved_account / 账号池 …）自动走数据库。

配置（二选一，环境变量优先）
==========================
1) 环境变量：
     ZHAOHE_DB_HOST      例 gateway01.ap-northeast-1.prod.aws.tidbcloud.com
     ZHAOHE_DB_PORT      默认 4000
     ZHAOHE_DB_USER
     ZHAOHE_DB_PASSWORD
     ZHAOHE_DB_NAME      默认 test
     ZHAOHE_DB_SSL_CA    CA 路径；留空=用系统默认 CA；填 "off" 关闭 TLS
2) 项目根目录的 `db.local.json`（已 .gitignore，仅本地用）：
     {"host": "...", "port": 4000, "user": "...", "password": "...",
      "database": "test", "ssl_ca": "/etc/ssl/cert.pem"}

表结构
======
    zhaohe_account  一个账号一行（uid 主键），其余不常用字段放 extra(JSON)
    zhaohe_meta     键值对，存 "current_uid"（当前账号）

用法（建表 + 从 accounts.json 导入）
==================================
    python3 web/account_db.py --init              # 建表
    python3 web/account_db.py --init --sync       # 建表 + 把 accounts.json 导进去
    python3 web/account_db.py --stats             # 看条数
"""

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TABLE = "zhaohe_account"
META = "zhaohe_meta"
DEFAULT_PORT = 4000
DEFAULT_DB = "test"

# 账号字段 -> 列名（其余键整体塞进 extra JSON）
_FIELD_TO_COL = [
    ("userName", "user_name"),
    ("passwd", "passwd"),
    ("nickName", "nick_name"),
    ("playerLevel", "player_level"),
    ("sid", "sid"),
    ("servertime", "servertime"),
    ("status", "status"),
    ("source", "source"),
]
_COL_TO_FIELD = {col: f for f, col in _FIELD_TO_COL}

SCHEMA_ACCOUNT = f"""
CREATE TABLE IF NOT EXISTS `{TABLE}` (
  `uid`          BIGINT       NOT NULL,
  `user_name`    VARCHAR(96)  NOT NULL,
  `passwd`       VARCHAR(96)  NOT NULL,
  `nick_name`    VARCHAR(96)      NULL,
  `player_level` INT              NULL,
  `sid`          BIGINT           NULL,
  `servertime`   BIGINT           NULL,
  `status`       VARCHAR(64)      NULL,
  `source`       VARCHAR(32)      NULL,
  `extra`        JSON             NULL,
  `created_at`   DATETIME         NULL,
  `updated_at`   DATETIME         NULL,
  PRIMARY KEY (`uid`),
  KEY `idx_user_name` (`user_name`),
  KEY `idx_nick_name` (`nick_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
"""

SCHEMA_META = f"""
CREATE TABLE IF NOT EXISTS `{META}` (
  `k` VARCHAR(64) NOT NULL,
  `v` TEXT            NULL,
  PRIMARY KEY (`k`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
"""


# ─────────────────────────────────────
#  配置 / 连接
# ─────────────────────────────────────
def _ssl_ca_candidates():
    return [
        os.environ.get("ZHAOHE_DB_SSL_CA") or "",
        "/etc/ssl/cert.pem",                              # macOS
        "/etc/ssl/certs/ca-certificates.crt",             # Debian/Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",               # RHEL
    ]


def config():
    """返回连接配置 dict；未配置返回 None。"""
    host = (os.environ.get("ZHAOHE_DB_HOST") or "").strip()
    if not host:
        p = os.path.join(ROOT, "db.local.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return None
        host = (cfg.get("host") or "").strip()
        if not host:
            return None
        return {
            "host": host,
            "port": int(cfg.get("port") or DEFAULT_PORT),
            "user": cfg.get("user") or "",
            "password": cfg.get("password") or "",
            "database": cfg.get("database") or DEFAULT_DB,
            "ssl_ca": cfg.get("ssl_ca") if "ssl_ca" in cfg else None,
        }

    ca = os.environ.get("ZHAOHE_DB_SSL_CA")
    if ca is None:
        ca = None          # 交给下面的自动探测
    return {
        "host": host,
        "port": int(os.environ.get("ZHAOHE_DB_PORT") or DEFAULT_PORT),
        "user": os.environ.get("ZHAOHE_DB_USER") or "",
        "password": os.environ.get("ZHAOHE_DB_PASSWORD") or "",
        "database": os.environ.get("ZHAOHE_DB_NAME") or DEFAULT_DB,
        "ssl_ca": ca,
    }


def _resolve_ca(cfg):
    """把 ssl_ca 解析成实际存在的路径；关闭 TLS 返回 False。"""
    ca = cfg.get("ssl_ca")
    if isinstance(ca, str) and ca.strip().lower() in ("off", "none", "disable", "false"):
        return False
    if ca and os.path.exists(ca):
        return ca
    for c in _ssl_ca_candidates():
        if c and c != "off" and os.path.exists(c):
            return c
    return None


def _pymysql():
    try:
        import pymysql  # noqa: F401
        return pymysql
    except Exception:
        return None


def enabled():
    """是否启用数据库存储（配置齐全 + 装了驱动）。"""
    cfg = config()
    if not cfg:
        return False
    return _pymysql() is not None


def describe():
    cfg = config()
    if not cfg:
        return "未配置"
    ca = _resolve_ca(cfg)
    return (f"{cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}"
            f"（TLS={'off' if ca is False else (ca or '系统默认')}）")


def connect():
    """新建一条连接（调用方负责 close）。"""
    cfg = config()
    if not cfg:
        raise RuntimeError("没有配置数据库（ZHAOHE_DB_HOST 或 db.local.json）")
    pymysql = _pymysql()
    if pymysql is None:
        raise RuntimeError("缺少 PyMySQL 驱动（pip install PyMySQL）")
    kw = dict(host=cfg["host"], port=cfg["port"], user=cfg["user"],
              password=cfg["password"], database=cfg["database"],
              charset="utf8mb4", autocommit=True,
              cursorclass=pymysql.cursors.DictCursor,
              connect_timeout=10, read_timeout=20, write_timeout=20)
    ca = _resolve_ca(cfg)
    if ca is not False:
        # 传 dict 即开启 TLS；ca=None 时用系统默认 CA
        kw["ssl"] = {"ca": ca}
    return pymysql.connect(**kw)


def ensure_schema(conn=None):
    """建表（幂等）。"""
    own = conn is None
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_ACCOUNT)
            cur.execute(SCHEMA_META)
    finally:
        if own:
            conn.close()


# ─────────────────────────────────────
#  行 <-> dict
# ─────────────────────────────────────
def _dt(v):
    """datetime / str -> 'YYYY-mm-dd HH:MM:SS'；空则 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)[:19]


def _row_to_acc(row):
    acc = {"uid": int(row["uid"])}
    for col, field in _COL_TO_FIELD.items():
        val = row.get(col)
        if val is not None:
            acc[field] = int(val) if col in ("player_level", "sid", "servertime") else val
    extra = row.get("extra")
    if extra:
        try:
            if isinstance(extra, (bytes, bytearray)):
                extra = extra.decode("utf-8")
            obj = json.loads(extra) if isinstance(extra, str) else extra
            if isinstance(obj, dict):
                for k, v in obj.items():
                    acc.setdefault(k, v)
        except Exception:
            pass
    if row.get("created_at"):
        acc["createdAt"] = _dt(row["created_at"])
    if row.get("updated_at"):
        acc["updated"] = _dt(row["updated_at"])
    return acc


def _acc_to_row(uid, acc):
    row = {"uid": int(uid), "extra": {}}
    for field, col in _FIELD_TO_COL:
        row[col] = acc.get(field)
    row["created_at"] = _dt(acc.get("createdAt"))
    row["updated_at"] = _dt(acc.get("updated"))
    known = {f for f, _ in _FIELD_TO_COL} | {"uid", "createdAt", "updated"}
    for k, v in (acc or {}).items():
        if k not in known:
            row["extra"][k] = v
    row["extra"] = json.dumps(row["extra"], ensure_ascii=False) if row["extra"] else None
    return row


# ─────────────────────────────────────
#  读写接口（与 offline_play 的 _load_account_doc / _save_account_doc 同签名）
# ─────────────────────────────────────
def load_doc():
    """返回 {"current": "<uid>"|None, "accounts": {"<uid>": {...}}}"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT `uid`,`user_name`,`passwd`,`nick_name`,`player_level`,"
                f"`sid`,`servertime`,`status`,`source`,`extra`,`created_at`,`updated_at` "
                f"FROM `{TABLE}`")
            rows = cur.fetchall()
            cur.execute(f"SELECT `v` FROM `{META}` WHERE `k`='current_uid'")
            m = cur.fetchone()
    finally:
        conn.close()

    accounts = {}
    for r in rows:
        acc = _row_to_acc(r)
        accounts[str(acc["uid"])] = acc
    cur_uid = None
    if m and m.get("v"):
        cur_uid = str(m["v"])
    if cur_uid not in accounts:
        cur_uid = (sorted(accounts, key=lambda u: accounts[u].get("updated") or "",
                          reverse=True) or [None])[0]
    return {"current": cur_uid, "accounts": accounts}


def save_doc(doc):
    """把整份账号数据写回数据库（逐行 UPSERT + 更新 current）。

    ★ 刻意**不做 DELETE**：本服务没有「删除账号」功能，而 DELETE 会带来
      「多实例互相覆盖」的事故（本地 + Render 同连一个库时，A 的旧快照会把
      B 刚建的号删掉）。UPSERT 只会覆盖它确实持有的行，安全得多。
    """
    accounts = doc.get("accounts") or {}
    rows = []
    for uid, acc in accounts.items():
        try:
            rows.append(_acc_to_row(uid, acc))
        except Exception:
            continue

    cols = ["uid", "user_name", "passwd", "nick_name", "player_level", "sid",
            "servertime", "status", "source", "extra", "created_at", "updated_at"]
    conn = connect()
    try:
        with conn.cursor() as cur:
            for row in rows:
                upd = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols[1:])
                cur.execute(
                    f"INSERT INTO `{TABLE}` ({', '.join('`%s`' % c for c in cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))}) "
                    f"ON DUPLICATE KEY UPDATE {upd}",
                    [row[c] for c in cols])
            cur.execute(
                f"INSERT INTO `{META}` (`k`,`v`) VALUES ('current_uid',%s) "
                f"ON DUPLICATE KEY UPDATE `v`=VALUES(`v`)",
                [str(doc.get("current")) if doc.get("current") else None])
    finally:
        conn.close()
    return doc


def stats():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{TABLE}`")
            n = cur.fetchone()["n"]
            cur.execute(f"SELECT `v` FROM `{META}` WHERE `k`='current_uid'")
            m = cur.fetchone()
    finally:
        conn.close()
    return {"accounts": int(n), "current": (m.get("v") if m else None)}


def sync_from_json(path, force=False):
    """把 accounts.json 导入数据库。表里已有数据时默认跳过（force=True 覆盖）。"""
    if not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    accounts = {str(k): v for k, v in (doc.get("accounts") or {}).items()
                if isinstance(v, dict)}
    ensure_schema()
    if not force and stats()["accounts"] > 0:
        return {"ok": True, "skipped": True,
                "error": "数据库已有数据，未覆盖（要强制导入加 --force）"}
    save_doc({"current": doc.get("current"), "accounts": accounts})
    return {"ok": True, "imported": len(accounts), "current": doc.get("current")}


# ─────────────────────────────────────
#  命令行
# ─────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="zhaohe 账号库（MySQL/TiDB）")
    ap.add_argument("--init", action="store_true", help="建表")
    ap.add_argument("--sync", action="store_true", help="把 accounts.json 导入数据库")
    ap.add_argument("--force", action="store_true", help="配合 --sync 强制覆盖")
    ap.add_argument("--stats", action="store_true", help="显示条数")
    ap.add_argument("--dsn", action="store_true", help="只打印当前配置")
    a = ap.parse_args(argv)

    if a.dsn:
        print("数据库:", describe())
        print("驱动:", "PyMySQL" if _pymysql() else "缺失")
        return 0
    if not enabled():
        print("✗ 未启用：请设置 ZHAOHE_DB_HOST 等环境变量，或创建 db.local.json")
        print("  当前配置:", describe())
        return 1

    print("连接:", describe())
    if a.init or not (a.sync or a.stats):
        ensure_schema()
        print("✓ 表已就绪:", TABLE, "/", META)
    if a.sync:
        r = sync_from_json(os.path.join(ROOT, "accounts.json"), force=a.force)
        print("✓ 导入:", r)
    if a.stats or a.sync:
        print("统计:", stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
