#!/usr/bin/env python3
"""Leads Plaza MCP Server（v0 脚手架，W1-D1）。

三个 tool：
- search_leads：按类目/质检分过滤，返回 lead 摘要列表（免费浏览，不含 contact）
- buy_lead：返回 x402 待支付占位（402 流程待接）；mock_confirm=True 仅本地联调
- report_conversion：接收转化回传，写入本地 sqlite

运行：.venv/bin/python server.py   （stdio 模式，供 MCP 客户端连接）
约束：无真实支付、无支付网络调用、无真实个人数据（全 mock）。
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from mock_data import MOCK_LEADS

import pricing
import x402 as x402mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "conversions.db")

mcp = FastMCP("leads-plaza")

# ---- 数据源：优先用真实质检 lead，缺失时回退 mock ----
def _load_leads() -> list:
    import glob
    scored_files = sorted(glob.glob(
        os.path.join(BASE_DIR, "..", "data", "qa", "scored_leads_*.jsonl")))
    if scored_files:
        leads = []
        with open(scored_files[-1], encoding="utf-8") as f:
            for line in f:
                try: r = json.loads(line)
                except Exception: continue
                if not r.get("_listed"): continue
                extra = r.get("extra") or {}
                leads.append({
                    "id": r.get("_id"), "category": r.get("category_hint"),
                    "title": r.get("title"), "source_url": r.get("source_url"),
                    "summary": r.get("summary", ""),
                    "intent_score": r.get("_intent", 0),
                    "qa_score": r.get("_qa", 0),
                    "age_hours": pricing.age_hours_of(r.get("captured_at", "")),
                    "exclusive": False, "category_cvr": None,
                    "contact": {"type": "post",
                                "value": extra.get("author") or r.get("source_url")},
                })
        if leads: return leads
    return MOCK_LEADS

LEADS = _load_leads()

# ---- 定价参数（v0，见 docs/x402-integration-plan.md 与 mcp_server/pricing.py）----
MIN_LIST_QA = pricing.MIN_LIST_QA


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _enrich(lead: dict) -> dict:
    """补算 freshness / price_usd，不改原始数据。"""
    l = dict(lead)
    l["freshness"] = round(pricing.freshness(l["age_hours"]), 3)
    l["price_usd"] = _price_of(l)
    return l


def _price_of(lead: dict) -> float | None:
    return pricing.price_of(lead["category"], lead["age_hours"],
                            lead["qa_score"], lead.get("category_cvr"),
                            lead.get("exclusive", False))


def _summary(lead: dict) -> dict:
    """免费浏览摘要：不含 contact（付费字段）。"""
    l = _enrich(lead)
    return {
        "id": l["id"],
        "category": l["category"],
        "title": l["title"],
        "source_url": l["source_url"],
        "intent_score": l["intent_score"],
        "freshness": l["freshness"],
        "qa_score": l["qa_score"],
        "price_usd": l["price_usd"],
        "exclusive": l["exclusive"],
    }


def _full(lead: dict) -> dict:
    l = _enrich(lead)
    l.pop("age_hours", None)
    return l


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            outcome TEXT NOT NULL,          -- converted | no_response | bad_lead
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            price_usd REAL NOT NULL,
            tx_hash TEXT DEFAULT '',       -- 真实结算后回填；mock 为 MOCK-*
            mode TEXT NOT NULL,             -- mock | x402
            created_at TEXT NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS buyer_credits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id TEXT NOT NULL DEFAULT 'default',
            amount_usd REAL NOT NULL,
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    con.commit()
    con.close()


# ================= 业务逻辑（纯函数，可单测） =================

def search_leads_impl(category: str = "", min_qa_score: float = MIN_LIST_QA,
                       limit: int = 10) -> dict:
    results = [l for l in LEADS if l["qa_score"] >= min_qa_score]
    if category:
        results = [l for l in results if l["category"] == category]
    results = sorted(results, key=lambda x: x["qa_score"], reverse=True)[: max(1, limit)]
    return {
        "total": len(results),
        "note": "摘要免费浏览；contact 等完整字段需 buy_lead 支付后获取",
        "leads": [_summary(l) for l in results],
    }


def buy_lead_impl(lead_id: str, mock_confirm: bool = False) -> dict:
    lead = next((l for l in LEADS if l["id"] == lead_id), None)
    if not lead:
        return {"status": "not_found", "lead_id": lead_id}
    if lead["qa_score"] < MIN_LIST_QA:
        return {"status": "not_for_sale",
                "message": f"该 lead 质检分 {lead['qa_score']} < {MIN_LIST_QA}，未上架"}

    price = _price_of(lead)
    if price is None:
        return {"status": "not_for_sale",
                "message": f"该 lead 质检分 {lead['qa_score']} < {MIN_LIST_QA}，未上架"}

    if mock_confirm:
        # 仅本地联调用：模拟"已支付"，返回完整 lead
        tx = f"MOCK-TX-{uuid.uuid4().hex[:12].upper()}"
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT INTO orders (lead_id, price_usd, tx_hash, mode, created_at)"
            " VALUES (?,?,?,?,?)",
            (lead_id, price, tx, "mock", _now_iso()),
        )
        con.commit()
        con.close()
        full = _full(lead)
        full["payment"] = {"mode": "MOCK", "tx_hash": tx,
                           "warning": "本地联调占位，非真实结算"}
        return {"status": "paid_mock", "lead": full}

    # x402 真实链路：返回 402 + paymentRequirements。
    # payTo 未配置（LEADS_PAY_TO）时为占位模式，不产生真实支付。
    resp = x402mod.payment_required_response(lead_id, price)
    return resp


def report_conversion_impl(lead_id: str, outcome: str, reason: str = "") -> dict:
    allowed = {"converted", "no_response", "bad_lead"}
    if outcome not in allowed:
        return {"status": "error",
                "message": f"outcome 非法，应为 {sorted(allowed)} 之一"}
    if not any(l["id"] == lead_id for l in LEADS):
        return {"status": "error", "message": f"未找到 lead {lead_id}"}
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO conversions (lead_id, outcome, reason, created_at)"
        " VALUES (?,?,?,?)",
        (lead_id, outcome, reason, _now_iso()),
    )

    resp = {"status": "recorded", "lead_id": lead_id, "outcome": outcome}
    if outcome == "bad_lead":
        resp["sla"] = ("已受理坏单申报：24h 内自动退 credit 或换一条同类目等值 lead。"
                       "坏单率按周统计，≥5% 触发类目下架整顿。")
    elif outcome == "converted":
        # 成交回传奖励：7% credit 返还（v0 固定比例，见 docs/conversion-api.md）
        lead = next((l for l in LEADS if l["id"] == lead_id), None)
        price = _price_of(lead) if lead else 0
        credit = round((price or 0) * 0.07, 2)
        if credit > 0:
            con.execute(
                "INSERT INTO buyer_credits (buyer_id, amount_usd, reason, created_at)"
                " VALUES (?,?,?,?)",
                ("default", credit, f"converted:{lead_id}", _now_iso()),
            )
        resp["reward"] = (f"感谢回传！已返还 {credit} USD credit（成交价的 7%），"
                         "下次购买可抵扣。")
    con.commit()
    con.close()
    return resp


# ================= MCP tool 注册（薄封装） =================

@mcp.tool()
def search_leads(category: str = "", min_qa_score: float = 60.0,
                 limit: int = 10) -> str:
    """搜索在售 lead 摘要（免费）。category 可选：ai_automation_intent / funding_hiring_signal / devtool_signal。"""
    return json.dumps(search_leads_impl(category, min_qa_score, limit),
                      ensure_ascii=False, indent=2)


@mcp.tool()
def buy_lead(lead_id: str, mock_confirm: bool = False) -> str:
    """购买 lead。当前返回 x402 402 待支付占位（真实结算待接）；mock_confirm=True 仅本地联调模拟已支付。"""
    return json.dumps(buy_lead_impl(lead_id, mock_confirm),
                      ensure_ascii=False, indent=2)


@mcp.tool()
def report_conversion(lead_id: str, outcome: str, reason: str = "") -> str:
    """回传转化结果。outcome：converted（成交）/ no_response（无回应）/ bad_lead（坏单，触发 SLA）。"""
    return json.dumps(report_conversion_impl(lead_id, outcome, reason),
                      ensure_ascii=False, indent=2)


if __name__ == "__main__":
    init_db()
    mcp.run()
