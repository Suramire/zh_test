#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备指纹统一配置文件
====================
供 send_cmd4.py / send_cmd1.py / send_cmd57.py 等协议脚本统一读取，
避免各脚本各自硬编码设备信息。

各脚本用法:
    from device_fingerprint import DEVICE

注意:
  - sha1 采用"冒号分隔的证书指纹"格式(20字节大写hex), 与 send_cmd4.py 原 DEFAULT 一致
  - 如需换指纹, 只改本文件, 所有脚本自动生效
"""
import os

# 设备指纹 (2026-08-21 生成, sha1 已更换)
DEVICE = {
    "version": "1.0.78",
    "channel": "134",
    "device": "Xiaomi#Redmi#2511FRT3DD",
    "os": "15",
    "sha1": "70:F4:5E:40:54:46:49:FF:F3:E2:BB:A0:A6:D7:91:61:54:D1:70:7F",
    "phoneSign": "134_d8b6b1941ba54f3eb8cbce15aa4aa6d9",
    "more": "{deviceType:'0',versionInfo:'3.3.72#560#0.0.0-560#3.3.30#1.0.78#47',account:''}",
}


# CMD57 单独字段: rmb / version(复用 DEVICE.version) / sha1(复用 DEVICE.sha1)
# 提供便捷常量
SHA1 = DEVICE["sha1"]
VERSION = DEVICE["version"]
RMB = 0
