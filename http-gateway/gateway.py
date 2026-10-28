#!/usr/bin/env python3
"""Leads Plaza 公开 HTTP 网关（2026-10-24，用户批准上马）。

薄封装：业务逻辑全部复用 mcp_server/server.py 的 impl 函数（定价、返利分档、
原因码 schema、试吃 credit、search 日志、conversions.db 全部同一份），本文件只做
HTTP 映射 + 限流 + CORS + 体积上限，不复制任何业务规则。

端点：
  GET  /healthz                  健康检查
  GET  /                        服务说明（给目录/买家看）
  GET  /leads                    公开搜索摘要（免费，不含 contact）
  POST /leads/{lead_id}/buy     走 buy_lead_impl；需支付时回 HTTP 402 + x402
                                paymentRequirements（含 extensions.bazaar）；
                                带 x_payment 时走 facilitator verify -> settle
  POST /leads/{lead_id}/report  走 report_conversion_impl（原因码 schema v1）
  /mcp                          MCP Streamable HTTP（同一批 tool，供 Smithery
                                填写 "MCP Server URL" 用）

运行：uvicorn gateway:app --host 127.0.0.1 --port 8090
环境变量：
  LEADS_PAY_TO            Base 主网收款地址（x402 真实结算必需；见 .env）
  LEADS_NETWORK           base（默认）| base-sepolia（测试网）
  GATEWAY_ALLOW_MOCK      true 时才允许 buy 的 mock_confirm（默认 false：
                          公网不允许免费模拟交付，防白嫖）
  GATEWAY_CORS_ORIGINS    逗号分隔的允许跨域来源（默认空 = 不放行跨域）
  GATEWAY_RATE_GET        GET 限流/分钟/IP（默认 120）
  GATEWAY_RATE_POST       POST 限流/分钟/IP（默认 30）

合规：只卖公司级公开信息，不写任何个人数据；contact 只含帖链与公司级公开信息。
"""
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

# ---- 复用 mcp_server：把 mcp_server 目录放进 sys.path，import server ----
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MCP_DIR = os.path.join(_THIS_DIR, "..", "mcp_server")
if os.path.abspath(_MCP_DIR) not in sys.path:
    sys.path.insert(0, os.path.abspath(_MCP_DIR))

import server as srv          # noqa: E402  (mcp_server 的 impl / 定价 / 返利)
import x402 as x402mod        # noqa: E402  (402 响应 + bazaar 扩展)

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

MAX_BODY_BYTES = 64 * 1024
RATE_GET = int(os.environ.get("GATEWAY_RATE_GET", "120"))
RATE_POST = int(os.environ.get("GATEWAY_RATE_POST", "30"))
ALLOW_MOCK = os.environ.get("GATEWAY_ALLOW_MOCK", "false").lower() == "true"
CORS_ORIGINS = [o.strip() for o in os.environ.get("GATEWAY_CORS_ORIGINS", "").split(",")
                if o.strip()]

srv.init_db()

# MCP streamable 应用先建出来（此时惰性创建 session manager），再由网关 lifespan 显式跑它：
# 实测：被 mount 的内层 Starlette 应用的 lifespan 不会被父应用执行，session manager
# 若不显式 run()，/mcp 请求会报 "Task group is not initialized"。
_mcp_app = srv.mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app):
    async with srv.mcp._session_manager.run():
        yield


app = FastAPI(title="Leads Plaza Gateway",
              description="Public HTTP gateway for Leads Plaza: search, buy (x402), report.",
              version="0.1.0",
              contact={"name": "Marco", "email": "marco@tryleadsplaza.com"},
              lifespan=lifespan)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-PAYMENT", "Authorization"],
    )


# ---------------- 限流（单进程固定窗口，per-IP） ----------------
class _RateLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, ip: str, limit: int) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits[ip]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


_limiter = _RateLimiter()


@app.middleware("http")
async def _guardrails(request: Request, call_next):
    # 体积上限（/mcp 走 SDK 自己的上限，不在此拦）
    if request.method == "POST" and not request.url.path.startswith("/mcp"):
        clen = request.headers.get("content-length")
        if clen is not None and int(clen) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
    # /mcp 走 SDK 自己的会话/体积管理，不在此限流（MCP 会话本身就是多请求）
    if not request.url.path.startswith("/mcp"):
        ip = request.client.host if request.client else "unknown"
        limit = RATE_GET if request.method == "GET" else RATE_POST
        if not _limiter.allow(f"{ip}:{request.method}", limit):
            return JSONResponse({"detail": "rate limited, slow down"}, status_code=429)
    return await call_next(request)


# ---------------- 请求体 ----------------
class BuyBody(BaseModel):
    trial_code: str = ""
    buyer_id: str = "default"
    mock_confirm: bool = False
    # x402 真实结算：买家把 402 响应里的 requirements 原样带回 + 签名 payload
    x_payment: dict | None = None


class ReportBody(BaseModel):
    outcome: str
    reason: str = ""
    reason_code: str = ""
    evidence: str = ""
    buyer_id: str = "default"
    contacted_at: str = ""


# ---------------- 基础端点 ----------------
@app.get("/healthz")
def healthz():
    db_ok = os.path.exists(srv.DB_PATH)
    return {"status": "ok", "leads": len(srv.LEADS), "db": db_ok,
            "network": x402mod.network(), "pay_to_configured": bool(x402mod.pay_to_address())}


@app.get("/")
def index():
    return {
        "service": "Leads Plaza",
        "description": ("QA-verified B2B leads for AI agents: search free, "
                        "buy per-lead with x402 micropayments (USDC on Base), "
                        "report conversions back."),
        "endpoints": {
            "search": "GET /leads?category=&min_qa_score=&limit=",
            "buy": "POST /leads/{lead_id}/buy (402 = x402 payment required)",
            "report": "POST /leads/{lead_id}/report",
            "mcp": "POST /mcp (MCP Streamable HTTP, same tools)",
        },
        "docs": "See https://github.com/liangh2020/leads-plaza-mcp-server",
    }


@app.get("/leads")
def search(category: str = "", min_qa_score: float = srv.MIN_LIST_QA,
           limit: int = 10, buyer_id: str = "default"):
    limit = max(1, min(limit, 50))
    return srv.search_leads_impl(category, min_qa_score, limit, buyer_id)


# ---------------- 购买（含 x402 真实结算） ----------------
def _lead_or_404(lead_id: str) -> dict:
    lead = next((l for l in srv.LEADS if l["id"] == lead_id), None)
    if not lead:
        raise HTTPException(status_code=404, detail=f"lead {lead_id} not found")
    return lead


def _settle_x402(lead: dict, x_payment: dict) -> dict:
    """买家带回 X-PAYMENT 时的 verify -> settle。requirements 由买家从 402 原样带回，
    网关按当前价格逐项核对（防改价/改收款地址），再调 facilitator。"""
    payload = x_payment.get("payload")
    reqs = x_payment.get("requirements")
    if not isinstance(payload, dict) or not isinstance(reqs, dict):
        raise HTTPException(status_code=400,
                            detail="x_payment must contain payload + requirements")
    price = srv._price_of(lead)
    if price is None:
        raise HTTPException(status_code=422, detail="lead not for sale")
    pay_to = x402mod.pay_to_address()
    if not pay_to:
        raise HTTPException(status_code=503, detail="seller payTo not configured")
    expected_units = str(int(round(price * 10 ** x402mod.USDC_DECIMALS)))
    checks = {
        "resource": reqs.get("resource") == f"lead:{lead['id']}",
        "amount": reqs.get("amount") == expected_units
                  and reqs.get("maxAmountRequired") == expected_units,
        "payTo": reqs.get("payTo") == pay_to,
        "asset": reqs.get("asset") == x402mod.usdc_asset(),
        "network": reqs.get("network") == x402mod.network_name(),
        "scheme": reqs.get("scheme") == "exact",
    }
    bad = [k for k, ok in checks.items() if not ok]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"payment requirements mismatch: {bad}")
    # facilitator 要求 paymentPayload 是完整 x402 对象（含 x402Version /
    # scheme / network），不能只传内层 {signature, authorization}。
    full_payload = {
        "x402Version": x_payment.get("x402Version", 1),
        "scheme": x_payment.get("scheme", "exact"),
        "network": x_payment.get("network"),
        "payload": payload,
    }
    try:
        ver = x402mod.verify_payment(full_payload, reqs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"facilitator verify failed: {e}")
    if not ver.get("isValid"):
        raise HTTPException(status_code=402, detail={"verify": ver})
    try:
        settled = x402mod.settle_payment(full_payload, reqs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"facilitator settle failed: {e}")
    tx = settled.get("txHash") or settled.get("transaction") or ""
    # 2026-10-26 教训：settle 成功后记账失败绝不能 500 掉已付款的 lead
    # （当晚即因此丢过一次交付：钱已划走、用户却没拿到 lead）。记账失败
    # 只记日志告警，仍返回 paid 结果与 tx_hash；对账靠 tx_hash 补。
    try:
        con = sqlite3.connect(srv.DB_PATH)
        con.execute(
            "INSERT INTO orders (lead_id, price_usd, tx_hash, mode, created_at)"
            " VALUES (?,?,?,?,?)",
            (lead["id"], price, tx, "x402", srv._now_iso()),
        )
        con.commit()
        con.close()
    except Exception as e:
        import sys as _sys
        print(f"WARNING: orders INSERT failed after settle "
              f"(lead={lead['id']} tx={tx}): {e}", file=_sys.stderr, flush=True)
    full = srv._full(lead)
    full["payment"] = {"mode": "X402", "tx_hash": tx, "price_usd": price,
                       "credit_used_usd": 0.0, "network": x402mod.network_name()}
    return {"status": "paid_x402", "lead": full}


@app.post("/leads/{lead_id}/buy")
def buy(lead_id: str, body: BuyBody,
        x_payment_header: str | None = Header(default=None, alias="X-PAYMENT")):
    lead = _lead_or_404(lead_id)
    # X-PAYMENT 可走 header（标准 x402）或 body（便于非钱包客户端调试）
    x_pay = body.x_payment
    if x_pay is None and x_payment_header:
        try:
            import json as _json
            x_pay = _json.loads(x_payment_header)
        except Exception:
            raise HTTPException(status_code=400, detail="X-PAYMENT header not JSON")
    if x_pay is not None:
        return _settle_x402(lead, x_pay)
    if body.mock_confirm and not ALLOW_MOCK:
        raise HTTPException(status_code=403,
                            detail="mock_confirm disabled on the public gateway")
    resp = srv.buy_lead_impl(lead_id, body.mock_confirm, body.trial_code, body.buyer_id)
    status = resp.get("status")
    if status == "payment_required":
        # 402 响应体即 x402mod.payment_required_response：含 payment_requirements
        # 与 extensions.bazaar（Bazaar / Agentic.Market 索引用）
        return JSONResponse(content=resp, status_code=402)
    if status in ("paid_mock", "paid_credit"):
        return resp
    if status == "not_found":
        raise HTTPException(status_code=404, detail=resp)
    if status == "not_for_sale":
        raise HTTPException(status_code=422, detail=resp)
    raise HTTPException(status_code=400, detail=resp)


# ---------------- 回传 ----------------
@app.post("/leads/{lead_id}/report")
def report(lead_id: str, body: ReportBody):
    _lead_or_404(lead_id)
    resp = srv.report_conversion_impl(lead_id, body.outcome, body.reason,
                                      body.reason_code, body.evidence,
                                      body.buyer_id, body.contacted_at)
    if resp.get("status") == "error":
        raise HTTPException(status_code=400, detail=resp)
    return resp


# ---------------- MCP Streamable HTTP（同一批 tool） ----------------
# 注意：FastMCP 的 streamable_http_app() 内部路由本身就是 /mcp，所以必须挂在根路径
# （挂到 /mcp 会变成 /mcp/mcp）。FastAPI 自有路由先注册、优先匹配，/mcp 落到这里。
app.mount("/", _mcp_app)
