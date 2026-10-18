#!/usr/bin/env python3
"""x402 卖家端集成（W1-D4）。

真实 x402 流程（exact / USDC）：
  1. 买家调 buy_lead（无 X-PAYMENT）-> 卖家回 HTTP 402，
     PAYMENT-REQUIRED header 携带 paymentRequirements（JSON）。
  2. 买家钱包按 requirements 做 EIP-3009 transferWithAuthorization 签名，
     得到 paymentPayload，带 X-PAYMENT header 重试同一请求。
  3. 卖家调 facilitator POST /verify {paymentPayload, paymentRequirements} 验签。
  4. 验证通过 -> 卖家调 facilitator POST /settle 上链结算 -> 返回完整 lead。

本模块实现 1/3/4 的卖家侧代码。facilitator 用 Coinbase 公共端点。
状态：代码完整；mainnet 真实结算需配置 LEADS_PAY_TO（用户 Base 链收款地址）
      并用 Base Sepolia 测试网先行验证。未配置时 buy_lead 返回占位 402。
"""
import json
import os
import time
import uuid
import urllib.request

# facilitator（Coinbase 公共端点，mainnet 与 testnet 通用路径）
FACILITATOR = "https://api.cdp.coinbase.com/platform/v2/x402"

NETWORKS = {
    "base": "eip155:8453",          # 主网
    "base-sepolia": "eip155:84532",  # 测试网
}
USDC_DECIMALS = 6

# Base 链 USDC（Circle 官方）
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def pay_to_address() -> str | None:
    return os.environ.get("LEADS_PAY_TO") or None


def network() -> str:
    """默认主网；LEADS_NETWORK=base-sepolia 切测试网。"""
    return NETWORKS.get(os.environ.get("LEADS_NETWORK", "base"), NETWORKS["base"])


def build_payment_requirements(lead_id: str, price_usd: float) -> dict:
    """构造 x402 paymentRequirements（exact scheme）。"""
    amount_units = int(round(price_usd * 10 ** USDC_DECIMALS))
    return {
        "scheme": "exact",
        "network": network(),
        "asset": USDC_BASE,
        "amount": str(amount_units),
        "price_usd": price_usd,
        "payTo": pay_to_address() or "0xPAYTO_UNCONFIGURED__需配置LEADS_PAY_TO",
        "maxTimeoutSeconds": 300,
        "resource": f"lead:{lead_id}",
        "nonce": uuid.uuid4().hex,
        "description": f"Leads Plaza lead {lead_id} — one-time purchase",
        "mimeType": "application/json",
        "maxAmountRequired": str(amount_units),
        "facilitator": FACILITATOR,
    }


def _facilitator_post(path: str, body: dict, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        FACILITATOR + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def verify_payment(payment_payload: dict, payment_requirements: dict) -> dict:
    """调 facilitator /verify 验签。返回其原始响应（含 isValid）。"""
    return _facilitator_post("/verify", {
        "paymentPayload": payment_payload,
        "paymentRequirements": payment_requirements,
    })


def settle_payment(payment_payload: dict, payment_requirements: dict) -> dict:
    """调 facilitator /settle 上链结算。返回其原始响应（含 txHash）。"""
    return _facilitator_post("/settle", {
        "paymentPayload": payment_payload,
        "paymentRequirements": payment_requirements,
    })


def payment_required_response(lead_id: str, price_usd: float) -> dict:
    """buy_lead 无支付时的 402 响应体（MCP tool 内返回；HTTP 层由网关映射为 402）。"""
    reqs = build_payment_requirements(lead_id, price_usd)
    configured = pay_to_address() is not None
    return {
        "status": "payment_required",
        "http_status": 402,
        "payment_requirements": reqs,
        "configured": configured,
        "message": ("用钱包按 paymentRequirements 签名（EIP-3009），"
                    "带 X-PAYMENT header 重试 buy_lead。"
                    if configured else
                    "卖家收款地址尚未配置（LEADS_PAY_TO），当前为占位 402，不产生真实支付。"),
        "next_step": "facilitator verify -> settle -> 返回完整 lead（见 x402.py 文档字符串）",
    }
