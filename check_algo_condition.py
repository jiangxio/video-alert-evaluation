#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查 AIBOX fight 通道当前算法任务的真实 condition 参数，对比脚本默认值。"""
import json, os, ssl, urllib.request, urllib.error

URL = os.environ.get("AIBOX_URL", "")
if not URL:
    print("请设置 AIBOX_URL 环境变量（AIBOX 盒子地址）")
    raise SystemExit(1)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token: headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(URL+path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
        return json.load(r)

# 登录
r = call("POST", "/gbg/main/login", {"username": os.environ.get("AIBOX_USER", ""), "password": os.environ.get("AIBOX_PASS", "")})
token = r.get("data")
if not token:
    print("登录失败:", r); raise SystemExit(1)
print("[登录成功]")

# 列任务
r = call("GET", "/gbg/intellif/list?pageNo=1&pageSize=100&total=0&page=1&size=100", token=token)
tasks = r.get("data",{}).get("tasks",[]) or []
print(f"共 {len(tasks)} 个算法任务\n")

# 找所有任务，列出关键信息
print(f"{'通道':<8} {'名称':<16} {'状态':<6} {'algType':<14} uuid")
print("-"*80)
fight_related = []
for t in tasks:
    cc = str(t.get("channelCodes"))
    nm = str(t.get("name"))[:14]
    st = str(t.get("status"))
    alg = str(t.get("algType"))
    print(f"{cc:<8} {nm:<16} {st:<6} {alg:<14} {t.get('uuid')}")
    if cc == "70" or "fight" in str(t.get("name")).lower():
        fight_related.append(t)

print(f"\nfight 相关任务数: {len(fight_related)}")
# 也查通道列表里 fight 通道是否存在
r2 = call("GET", "/gb/channel/list?pageNo=1&pageSize=200", token=token)
chans = r2.get("data",{})
if isinstance(chans, dict):
    chans = chans.get("list") or chans.get("records") or chans.get("channels") or []
print(f"\n通道列表里 code=70 的通道:")
for c in (chans or []):
    if str(c.get("code")) == "70" or str(c.get("channelCode")) == "70":
        print(f"  {c}")
