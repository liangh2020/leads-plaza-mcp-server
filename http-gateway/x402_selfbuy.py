#!/usr/bin/env python3
"""Leads Plaza 网关自购脚本（用户在本机运行，私钥不出本机）。

流程（标准 x402 exact / EIP-3009）：
  1. GET {gateway}/leads?limit=1 拿一条 lead id
  2. POST {gateway}/leads/{id}/buy（无支付）-> HTTP 402，取 paymentRequirements
  3. 用私钥对 requirements 做 EIP-3009 transferWithAuthorization 签名
  4. 带 X-PAYMENT header 再 POST 一次 buy -> 期望 paid_x402 + 完整 lead + tx_hash

用法：
  pip install eth-account requests
  X402_BUYER_KEY=0x... python3 x402_selfbuy.py https://api.tryleadsplaza.com
  # 测试网：网关需先切 LEADS_NETWORK=base-sepolia 并重启，且钱包里有 base-sepolia USDC（水龙头领）

安全：私钥只从环境变量 X402_BUYER_KEY 读，脚本不打印、不上传、不写盘。
"""
import json
import os
import sys
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

GATEWAY = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://api.tryleadsplaza.com"
KEY = os.environ.get("X402_BUYER_KEY")
if not KEY:
    print("请先设置环境变量 X402_BUYER_KEY（买方钱包私钥，仅本机使用）")
    sys.exit(1)

acct = Account.from_key(KEY)
print(f"买方地址：{acct.address}")

leads = requests.get(f"{GATEWAY}/leads", params={"limit": 1}, timeout=20).json()
items = leads if isinstance(leads, list) else leads.get("leads", leads.get("items", []))
lead_id = items[0]["id"]
print(f"lead：{lead_id}")

r = requests.post(f"{GATEWAY}/leads/{lead_id}/buy", json={}, timeout=20)
if r.status_code != 402:
    print(f"第一步未返回 402（实际 {r.status_code}）：{r.text[:500]}")
    sys.exit(1)
reqs = r.json()["payment_requirements"]
print(f"402 requirements：network={reqs['network']} asset={reqs['asset']} "
      f"amount={reqs['amount']} payTo={reqs['payTo']}")

# EIP-3009 transferWithAuthorization 签名（x402 exact scheme 的 payload 形状）
now = int(time.time())
auth = {
    "from": acct.address,
    "to": reqs["payTo"],
    "value": int(reqs["amount"]),
    "validAfter": 0,
    "validBefore": now + 300,
    "nonce": "0x" + reqs["nonce"],
}
# EIP-712 domain 的 name/version 必须跟链上 USDC 合约一致，且因网而异：
# 测试网是 name="USDC"，主网是 name="USD Coin"，version 两边都是 "2"。
# 直接读 402 requirements 里的 extra（网关按网络填好），不要再硬编码，
# 否则签名域跟 facilitator 用的域对不上，验签必失败。
typed = {
    "types": {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ],
    },
    "domain": {
        "name": reqs.get("extra", {}).get("name", "USDC"),
        "version": reqs.get("extra", {}).get("version", "2"),
        # v1 network 是名称（base-sepolia），不是 CAIP-2；用映射取链 ID。
        "chainId": {"base": 8453, "base-sepolia": 84532,
                    "eip155:8453": 8453, "eip155:84532": 84532}[reqs["network"]],
        "verifyingContract": reqs["asset"],
    },
    "primaryType": "TransferWithAuthorization",
    "message": auth,
}
sig = acct.sign_message(encode_typed_data(full_message=typed)).signature.hex()
payload = {
    "x402Version": 1,
    "scheme": "exact",
    "network": reqs["network"],
    "payload": {"signature": sig, "authorization": auth},
    # 网关要求 X-PAYMENT 同时含 payload 与 requirements：把 402 的
    # requirements 原样带回（nonce/金额一致，网关会逐项比对），否则 400。
    "requirements": reqs,
}
r2 = requests.post(
    f"{GATEWAY}/leads/{lead_id}/buy",
    json={},
    headers={"X-PAYMENT": json.dumps(payload)},
    timeout=60,
)
print(f"第二次 buy：HTTP {r2.status_code}")
print(r2.text[:2000])
if r2.status_code == 200 and "paid_x402" in r2.text:
    print("自购成功：已拿到完整 lead 与链上 tx_hash。")
