#!/usr/bin/env python3
"""x402 卖家端集成（W1-D4）。

真实 x402 流程（exact / USDC）：
  1. 买家调 buy_lead（无 X-PAYMENT）-> 卖家回 HTTP 402，
     PAYMENT-REQUIRED header 携带 paymentRequirements（JSON）。
  2. 买家钱包按 requirements 做 EIP-3009 transferWithAuthorization 签名，
     得到 paymentPayload，带 X-PAYMENT header 重试同一请求。
  3. 卖家调 facilitator POST /verify {paymentPayload, paymentRequirements} 验签。
  4. 验证通过 -> 卖家调 facilitator POST /settle 上链结算 -> 返回完整 lead。

本模块实现 1/3/4 的卖家侧代码。facilitator 用 PayAI 公共端点
（2026-10-27 用户拍板：coinbase.com 连不上，改用 PayAI；CDP key 与
逐请求 JWT 工作随之取消）。
状态：代码完整；mainnet 真实结算需配置 LEADS_PAY_TO（用户 Base 链收款地址）
      并用 Base Sepolia 测试网先行验证。未配置时 buy_lead 返回占位 402。
"""
import json
import os
import secrets
import time
import uuid
import urllib.request

# facilitator：PayAI 公共端点（2026-10-27 起，两个网络都走它）。
# 此前测试网走 x402.org（主网只能走 CDP 端点，需 CDP API key 的 Ed25519
# JWT 鉴权）；2026-10-27 用户称 coinbase.com 连不上、拍板改用 PayAI。
# 实测：PayAI /supported 的 v1 exact 同时列 base 与 base-sepolia；无需
# 账号/API key 即可开始（前 1000 笔免费额度），无鉴权调用不 401。
# LEADS_FACILITATOR_URL 可覆盖（换 facilitator 不用改代码）。
FACILITATOR_BY_NETWORK = {
    "base": "https://facilitator.payai.network",
    "base-sepolia": "https://facilitator.payai.network",
}
# 兼容旧引用（主网地址）
FACILITATOR = FACILITATOR_BY_NETWORK["base"]


def facilitator_url() -> str:
    """当前网络对应的 facilitator（LEADS_FACILITATOR_URL 可覆盖）。"""
    override = os.environ.get("LEADS_FACILITATOR_URL")
    if override:
        return override
    return FACILITATOR_BY_NETWORK.get(
        os.environ.get("LEADS_NETWORK", "base"), FACILITATOR_BY_NETWORK["base"])

NETWORKS = {
    "base": "eip155:8453",          # 主网
    "base-sepolia": "eip155:84532",  # 测试网
}
USDC_DECIMALS = 6

# 各网络 USDC（Circle 官方；必须按网络区分——测试网绝不能用主网地址，
# 否则 facilitator 会在测试链上对一个不存在的合约验签，verify 必失败）
USDC_BY_NETWORK = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}
# 兼容旧引用
USDC_BASE = USDC_BY_NETWORK["base"]


def pay_to_address() -> str | None:
    return os.environ.get("LEADS_PAY_TO") or None


def network() -> str:
    """默认主网；LEADS_NETWORK=base-sepolia 切测试网。CAIP-2 形式，仅用于
    healthz 等展示面；402 与结算路径必须用 v1 名称（见 network_name）。"""
    return NETWORKS.get(os.environ.get("LEADS_NETWORK", "base"), NETWORKS["base"])


# x402 v1 的 network 字段用名称（base / base-sepolia），不是 CAIP-2。
# 实测证据：x402.org/facilitator/supported 里 v1 exact 只列 base-sepolia，
# eip155:84532 只在 v2 exact 下出现；v1 + CAIP-2 混用时 facilitator
# 找不到对应 kind，verify 直接 401（2026-10-26 实测）。
NETWORK_NAMES_V1 = {
    "base": "base",
    "base-sepolia": "base-sepolia",
}
CHAIN_IDS = {
    "base": 8453,
    "base-sepolia": 84532,
}


def network_name() -> str:
    """402 paymentRequirements 与结算校验用的 v1 网络名。"""
    return NETWORK_NAMES_V1.get(
        os.environ.get("LEADS_NETWORK", "base"), "base")


def usdc_asset() -> str:
    """当前网络对应的 USDC 合约地址。"""
    return USDC_BY_NETWORK.get(
        os.environ.get("LEADS_NETWORK", "base"), USDC_BY_NETWORK["base"])


# EIP-712 domain 参数随网络不同：测试网 USDC 的 name() 是 "USDC"，
# 主网 USDC 的 name() 是 "USD Coin"，version 两边都是 "2"。
# 2026-10-26 实测：402 里不带 extra 时 facilitator 回
# invalid_exact_evm_missing_eip712_domain——它优先从 requirements 的
# extra 取 domain，缺了它又查不到链上 version() 就直接报缺 domain。
# 所以 extra 必须带上，且签名方也要用同一组 name/version。
USDC_DOMAIN_BY_NETWORK = {
    "base": {"name": "USD Coin", "version": "2"},
    "base-sepolia": {"name": "USDC", "version": "2"},
}


def usdc_domain() -> dict:
    """当前网络对应的 EIP-712 domain 参数（name/version）。"""
    return USDC_DOMAIN_BY_NETWORK.get(
        os.environ.get("LEADS_NETWORK", "base"), USDC_DOMAIN_BY_NETWORK["base"])


def build_payment_requirements(lead_id: str, price_usd: float) -> dict:
    """构造 x402 paymentRequirements（exact scheme）。"""
    amount_units = int(round(price_usd * 10 ** USDC_DECIMALS))
    return {
        "scheme": "exact",
        "network": network_name(),
        "asset": usdc_asset(),
        "amount": str(amount_units),
        "price_usd": price_usd,
        "payTo": pay_to_address() or "0xPAYTO_UNCONFIGURED__需配置LEADS_PAY_TO",
        "maxTimeoutSeconds": 300,
        "resource": f"lead:{lead_id}",
        # nonce 是 EIP-3009 的 bytes32，必须是 32 字节随机数；
        # 以前 uuid4().hex 只有 16 字节，长度就不对。
        "nonce": secrets.token_hex(32),
        "description": f"Leads Plaza lead {lead_id} — one-time purchase",
        "mimeType": "application/json",
        "maxAmountRequired": str(amount_units),
        "facilitator": facilitator_url(),
        "extra": usdc_domain(),
    }


def _facilitator_post(path: str, body: dict, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        facilitator_url() + path,
        data=json.dumps(body).encode(),
        # 2026-10-26 实测：x402.org 前面的 Cloudflare Browser Integrity
        # Check 会挡 Python urllib 的默认 UA（Python-urllib/3.x），回 403
        # error code 1010；同一 VPS 上换 curl/浏览器 UA 即恢复 200。
        # 必须带一个描述性 UA，否则 verify 连 facilitator 应用层都到不了。
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "leads-plaza-gateway/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 只报状态码会丢掉 facilitator 真正的报错原因（如 401 背后的
        # 具体错误体），把响应体带出来，下次排障直接看原因。
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(
            f"facilitator {facilitator_url()}{path} HTTP {e.code}: {detail}") from e


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


def build_bazaar_extension(lead_id: str) -> dict:
    """x402 Bazaar 发现扩展（CDP Bazaar 索引用）：402 响应自带服务元数据 +
    输入/输出 schema，首笔结算后可被 Bazaar / Agentic.Market 索引。
    规范：docs.x402.org/extensions/bazaar —— info{input,output} + schema。
    本服务经 MCP（stdio）交付，resource 即 buy_lead 这一次购买动作。"""
    return {
        "info": {
            "input": {
                "type": "mcp_tool_call",
                "method": "buy_lead",
                "serviceName": "Leads Plaza",
                "serviceDescription": (
                    "Buy one QA-verified sales lead: public company-level buying/"
                    "hiring intent signals, scored and de-duplicated. Pay per lead "
                    "in USDC via x402; report conversions back to drive QA and pricing."),
                "categories": ["data", "lead-generation", "sales", "b2b"],
                "resource": f"lead:{lead_id}",
                "params": {
                    "lead_id": {
                        "type": "string",
                        "description": "Lead id from search_leads results",
                    },
                    "mock_confirm": {
                        "type": "boolean",
                        "default": False,
                        "description": "Local integration only: simulate a paid purchase",
                    },
                    "trial_code": {
                        "type": "string",
                        "description": ("Channel code (e.g. MOLTBOOK-10) claiming the "
                                        "$5 trial credit toward eligible leads"),
                    },
                    "buyer_id": {
                        "type": "string",
                        "default": "default",
                        "description": "Buyer identity for credits and conversion reports",
                    },
                },
                "required": ["lead_id"],
            },
            "output": {
                "type": "object",
                "example": {
                    "status": "paid_mock | paid_credit",
                    "lead": {
                        "id": lead_id,
                        "category": "ai_automation_intent | funding_hiring_signal | devtool_signal",
                        "title": "<lead title>",
                        "source_url": "<public source url>",
                        "summary": "<what the company is signalling>",
                        "intent_score": 0,
                        "qa_score": 0,
                        "freshness": 0.0,
                        "price_usd": 0.0,
                        "exclusive": False,
                        "contact": {"type": "post",
                                    "value": "<public post / company-level contact>"},
                        "payment": {"mode": "MOCK | CREDIT",
                                    "tx_hash": "<tx or credit ref>",
                                    "price_usd": 0.0,
                                    "credit_used_usd": 0.0},
                    },
                },
            },
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "info": {
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "method": {"type": "string"},
                                "serviceName": {"type": "string"},
                                "serviceDescription": {"type": "string"},
                                "categories": {"type": "array",
                                               "items": {"type": "string"}},
                                "params": {"type": "object"},
                                "required": {"type": "array",
                                             "items": {"type": "string"}},
                            },
                            "required": ["type", "method", "serviceName"],
                        },
                        "output": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "example": {"type": "object"},
                            },
                        },
                    },
                    "required": ["input", "output"],
                },
            },
            "required": ["info"],
        },
    }


def payment_required_response(lead_id: str, price_usd: float) -> dict:
    """buy_lead 无支付时的 402 响应体（MCP tool 内返回；HTTP 层由网关映射为 402）。"""
    reqs = build_payment_requirements(lead_id, price_usd)
    configured = pay_to_address() is not None
    return {
        "status": "payment_required",
        "http_status": 402,
        "payment_requirements": reqs,
        "extensions": {"bazaar": build_bazaar_extension(lead_id)},
        "configured": configured,
        "message": ("用钱包按 paymentRequirements 签名（EIP-3009），"
                    "带 X-PAYMENT header 重试 buy_lead。"
                    if configured else
                    "卖家收款地址尚未配置（LEADS_PAY_TO），当前为占位 402，不产生真实支付。"),
        "next_step": "facilitator verify -> settle -> 返回完整 lead（见 x402.py 文档字符串）",
    }
