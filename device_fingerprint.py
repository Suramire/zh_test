#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备指纹模板 / 身份生成器
=========================
供 send_cmd4.py / send_cmd1.py / send_cmd57.py 等协议脚本统一读取。

★ 2026-11-15 起本文件里原来那套固定 sha1 / phoneSign 全部作废（已被服务端拉黑）：
    · 用它注册（CMD4）→ 255「联网失败」
    · 用它登录（CMD1）→ 255「帐号异常，请联系客服!」，并且会把该账号一起带走
  所以身份不再写死，一律由 new_identity() 现生成：
    · 建号时用哪一套，就把那一套跟账号一起存下来（acc["fingerprint"]，
      落在 accounts.json / MySQL 的 extra 字段）；
    · 之后登录复用同一套，别在注册和登录之间换身份。

各脚本用法:
    from device_fingerprint import new_identity

注意:
  - sha1 采用"冒号分隔的证书指纹"格式(20字节大写hex)
  - phoneSign 必须是「渠道号_32位hex」，且前缀要和 channel 一致
"""
import random
import uuid

# 线上资源版本（游戏 2026-11-14 把 localData 更到 0.0.0-569，more 里的 versionInfo 跟着走）
RES_VERSION_INFO = "3.3.83#569#0.0.0-569#3.3.33#1.0.78#47"
MORE_TEMPLATE = "{account:'',deviceType:'0',versionInfo:'%s'}"

# 一次 CMD4 / CMD1 需要的全部指纹字段
FINGERPRINT_KEYS = ("version", "channel", "device", "os", "sha1", "phoneSign", "more")


def new_sha1():
    """随机证书指纹：20 字节大写 hex，冒号分隔。"""
    return ":".join("%02X" % random.randint(0, 255) for _ in range(20))


def new_identity(channel=None):
    """生成一套全新的设备身份（每次注册/登录调用都不一样）。"""
    ch = str(channel if channel is not None else DEVICE["channel"])
    return {
        "version": DEVICE["version"],
        "channel": ch,
        "device": DEVICE["device"],
        "os": DEVICE["os"],
        "sha1": new_sha1(),
        # phoneSign 前缀必须等于渠道号，服务端会校验
        "phoneSign": f"{ch}_{uuid.uuid4().hex}",
        "more": MORE_TEMPLATE % RES_VERSION_INFO,
    }


# 设备模板：机型 / OS / 版本 / 渠道保持固定（服务端不按这几项判定），
# 每次 new_identity() 只换 sha1 + phoneSign。
DEVICE = {
    "version": "1.0.78",
    "channel": "134",
    "device": "Xiaomi#Redmi#2511FRT3DD",
    "os": "15",
    "sha1": new_sha1(),
    "phoneSign": "134_" + uuid.uuid4().hex,
    "more": MORE_TEMPLATE % RES_VERSION_INFO,
}


def fingerprint_of(ident):
    """只挑出要跟账号一起持久化的指纹字段。"""
    return {k: ident[k] for k in FINGERPRINT_KEYS if ident.get(k)}


# CMD57 单独字段: rmb / version(复用 DEVICE.version) / sha1(用新生成的)
SHA1 = DEVICE["sha1"]
VERSION = DEVICE["version"]
RMB = 0
