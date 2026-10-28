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
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mock_data import MOCK_LEADS

import pricing
import x402 as x402mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "conversions.db")

# 公网 Streamable HTTP 必须显式放行 Host：SDK 默认只认 localhost，
# 不改则远程请求一律 421 "Invalid Host header"（2026-10-28 公网实测）。
# 服务端 MCP 客户端（agent、目录站探针）一般不带 Origin，可过；
# 浏览器端跨站 Origin 仍会被拦，属有意保留。
mcp = FastMCP(
    "leads-plaza",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "api.tryleadsplaza.com",
            "api.tryleadsplaza.com:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            "https://api.tryleadsplaza.com",
        ],
    ),
)

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

# 冷启动补贴（见 docs/cold-start-subsidy-2026-10-20.md，2026-10-20 用户拍板生效）
# 2026-10-23 用户拍板改为按回传 richness 分档：结构化数据越全、返利越高。
COLD_START_REBATE_CAP = 100     # 冷启动 converted 回传次数上限
REBATE_RATES = {                # (冷启动期, 常态) × richness
    "rich": (0.25, 0.10),       # reason_code + evidence
    "coded": (0.15, 0.07),      # 仅 reason_code
    "basic": (0.07, 0.03),      # 仅 outcome（含旧式自由文本）
}


def _rebate_rate(richness: str, converted_count: int) -> float:
    cold, base = REBATE_RATES.get(richness, REBATE_RATES["basic"])
    return cold if converted_count <= COLD_START_REBATE_CAP else base

# 试吃 credit（见 docs/trial-credits.md，2026-10-20 用户拍板规则，2026-10-22 v0 落地）
TRIAL_CREDIT_USD = 5.0
TRIAL_CHANNEL_CODES = {"MOLTBOOK-10", "SALUTE-10", "IH-10"}
TRIAL_VALID_DAYS = 30
TRIAL_QA_MIN, TRIAL_QA_MAX = 60.0, 75.0   # 试吃范围：非独家、qa 60–75 档

# 回传原因码 schema v1（2026-10-22 定死，见 docs/conversion-api.md）：
# outcome 三档不变，reason_code 按 outcome 分组枚举；other_* 必须带 evidence。
# richness：basic=仅 outcome（可带自由文本 reason）| coded=带合法 reason_code |
#           rich=coded + evidence 非空。richness 落库，为返利按丰富度分档留口（分档规则另行拍板）。
REASON_CODES = {
    "converted": {
        "deal_closed": "已签约/付款成交",
        "meeting_booked": "约到有效会议/商机",
        "pilot_started": "付费试点/试用已启动",
        "other_converted": "其他正向转化（必须填 evidence 说明）",
    },
    "no_response": {
        "no_reply": "已触达，无回复",
        "bounced": "退信/渠道不可达",
        "auto_reply_only": "只有自动回复",
        "unsubscribed": "对方明确拒收/退订",
        "other_silent": "其他无回应情形（必须填 evidence 说明）",
    },
    "bad_lead": {
        "wrong_info": "公司/联系信息错误",
        "no_real_intent": "无真实意向（质检误判）",
        "already_solved": "需求已解决/已采购别家",
        "company_gone": "公司关闭/项目终止",
        "stale_info": "信息已过期（信号失效）",
        "duplicate_known": "买家已知悉的重复 lead",
        "wrong_fit": "行业/规模等画像不符",
        "other_bad": "其他坏单情形（必须填 evidence 说明）",
    },
}


def _report_richness(outcome: str, reason_code: str, evidence: str) -> str:
    if reason_code in REASON_CODES.get(outcome, {}):
        return "rich" if (evidence or "").strip() else "coded"
    return "basic"


def _converted_count(con):
    row = con.execute(
        "SELECT COUNT(*) FROM conversions WHERE outcome='converted'").fetchone()
    return row[0] if row else 0


def _credit_balance(con, buyer_id: str = "default") -> float:
    """可用 credit 余额：reward/refund 永久有效；trial 仅 30 天内有效；consume 为负数扣减。"""
    now = _now_iso()
    row = con.execute(
        """SELECT COALESCE(SUM(amount_usd), 0) FROM buyer_credits
           WHERE buyer_id = ?
             AND (source IN ('reward', 'refund', 'consume')
                  OR (source = 'trial' AND (expires_at = '' OR expires_at > ?)))""",
        (buyer_id, now)).fetchone()
    return round(row[0] or 0.0, 2)


def _claim_trial_credit(con, buyer_id: str, trial_code: str):
    """领取试吃 credit。返回 (ok, message)。同一身份限领一次；渠道码校验。"""
    code = (trial_code or "").strip().upper()
    if code not in TRIAL_CHANNEL_CODES:
        return False, f"渠道码无效，应为 {sorted(TRIAL_CHANNEL_CODES)} 之一"
    if con.execute(
            "SELECT 1 FROM trial_claims WHERE buyer_id = ?",
            (buyer_id,)).fetchone():
        return False, "该身份已领取过试吃 credit（每身份限领一次）"
    now = _now_iso()
    expires = (datetime.now(timezone.utc)
               + timedelta(days=TRIAL_VALID_DAYS)).isoformat()
    con.execute(
        "INSERT INTO trial_claims (buyer_id, code, granted_usd, expires_at, created_at)"
        " VALUES (?,?,?,?,?)",
        (buyer_id, code, TRIAL_CREDIT_USD, expires, now),
    )
    con.execute(
        "INSERT INTO buyer_credits (buyer_id, amount_usd, reason, source, expires_at, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (buyer_id, TRIAL_CREDIT_USD, f"trial:{code}", "trial", expires, now),
    )
    return True, (f"已领取 ${TRIAL_CREDIT_USD} 试吃 credit（30 天有效），"
                 f"可用于非独家、qa 60–75 档 lead 抵扣")


def _trial_eligible(lead: dict) -> bool:
    """试吃抵扣范围：非独家、qa 60–75 档。"""
    return (not lead.get("exclusive")) and TRIAL_QA_MIN <= lead["qa_score"] < TRIAL_QA_MAX


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
            source TEXT DEFAULT 'reward',  -- trial | reward | refund | consume
            expires_at TEXT DEFAULT '',    -- trial credit 30 天有效期；其余为空
            created_at TEXT NOT NULL
        )"""
    )
    # 老库迁移：无 source/expires_at 列时补上（存量记为 reward）
    _cols = [r[1] for r in con.execute("PRAGMA table_info(buyer_credits)")]
    if "source" not in _cols:
        con.execute("ALTER TABLE buyer_credits ADD COLUMN source TEXT DEFAULT 'reward'")
    if "expires_at" not in _cols:
        con.execute("ALTER TABLE buyer_credits ADD COLUMN expires_at TEXT DEFAULT ''")
    con.execute(
        """CREATE TABLE IF NOT EXISTS trial_claims (
            buyer_id TEXT PRIMARY KEY,     -- 同一身份限领一次（防薅：v0 按 buyer_id）
            code TEXT NOT NULL,            -- 渠道码，来源统计
            granted_usd REAL NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    # conversions 迁移：补结构化回传字段（原因码 schema v1，2026-10-22）
    _ccols = [r[1] for r in con.execute("PRAGMA table_info(conversions)")]
    for _col, _decl in (("buyer_id", "TEXT DEFAULT 'default'"),
                        ("reason_code", "TEXT DEFAULT ''"),
                        ("evidence", "TEXT DEFAULT ''"),
                        ("richness", "TEXT DEFAULT 'basic'"),
                        ("contacted_at", "TEXT DEFAULT ''")):
        if _col not in _ccols:
            con.execute(f"ALTER TABLE conversions ADD COLUMN {_col} {_decl}")
    # search 日志（需求图谱原料，2026-10-22）：每次 search 落一行，零结果单独可查
    con.execute(
        """CREATE TABLE IF NOT EXISTS search_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id TEXT NOT NULL DEFAULT 'default',
            category TEXT DEFAULT '',
            min_qa_score REAL DEFAULT 0,
            limit_n INTEGER DEFAULT 0,
            result_count INTEGER NOT NULL DEFAULT 0,
            zero_result INTEGER NOT NULL DEFAULT 0,  -- 1 = 零结果（需求缺口）
            created_at TEXT NOT NULL
        )"""
    )
    con.commit()
    con.close()


# ================= 业务逻辑（纯函数，可单测） =================

def _log_search(buyer_id: str, category: str, min_qa_score: float,
                limit: int, result_count: int) -> None:
    """search 落日志（需求图谱/缺口分析原料）。日志失败不阻断搜索本身。"""
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            """CREATE TABLE IF NOT EXISTS search_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                buyer_id TEXT NOT NULL DEFAULT 'default',
                category TEXT DEFAULT '',
                min_qa_score REAL DEFAULT 0,
                limit_n INTEGER DEFAULT 0,
                result_count INTEGER NOT NULL DEFAULT 0,
                zero_result INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""")
        con.execute(
            "INSERT INTO search_logs (buyer_id, category, min_qa_score, limit_n,"
            " result_count, zero_result, created_at) VALUES (?,?,?,?,?,?,?)",
            (buyer_id, category, min_qa_score, limit, result_count,
             1 if result_count == 0 else 0, _now_iso()))
        con.commit()
        con.close()
    except Exception:
        pass


def search_leads_impl(category: str = "", min_qa_score: float = MIN_LIST_QA,
                       limit: int = 10, buyer_id: str = "default") -> dict:
    results = [l for l in LEADS if l["qa_score"] >= min_qa_score]
    if category:
        results = [l for l in results if l["category"] == category]
    results = sorted(results, key=lambda x: x["qa_score"], reverse=True)[: max(1, limit)]
    _log_search(buyer_id, category, min_qa_score, limit, len(results))
    return {
        "total": len(results),
        "note": "摘要免费浏览；contact 等完整字段需 buy_lead 支付后获取",
        "leads": [_summary(l) for l in results],
    }


def buy_lead_impl(lead_id: str, mock_confirm: bool = False,
                  trial_code: str = "", buyer_id: str = "default") -> dict:
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

    con = sqlite3.connect(DB_PATH)
    trial_note = ""
    if trial_code:
        ok, msg = _claim_trial_credit(con, buyer_id, trial_code)
        trial_note = msg
        con.commit()
        if not ok and "渠道码无效" in msg:
            con.close()
            return {"status": "error", "message": msg}

    # 试吃 credit 抵扣：仅非独家、qa 60–75 档；不足部分走 x402 正常支付
    credit_used = 0.0
    if _trial_eligible(lead):
        avail = _credit_balance(con, buyer_id)
        credit_used = min(avail, price)
        if credit_used > 0:
            con.execute(
                "INSERT INTO buyer_credits (buyer_id, amount_usd, reason, source, created_at)"
                " VALUES (?,?,?,?,?)",
                (buyer_id, -credit_used, f"buy:{lead_id}", "consume", _now_iso()),
            )
            con.commit()
    remainder = round(price - credit_used, 2)

    def _paid(mode: str):
        tx_prefix = "CREDIT" if mode == "credit" else "MOCK-TX"
        tx = f"{tx_prefix}-{uuid.uuid4().hex[:12].upper()}"
        con.execute(
            "INSERT INTO orders (lead_id, price_usd, tx_hash, mode, created_at)"
            " VALUES (?,?,?,?,?)",
            (lead_id, price, tx, mode, _now_iso()),
        )
        con.commit()
        con.close()
        full = _full(lead)
        pay = {"mode": "CREDIT" if mode == "credit" else "MOCK",
               "tx_hash": tx,
               "price_usd": price,
               "credit_used_usd": credit_used}
        if mode == "mock":
            pay["warning"] = "本地联调占位，非真实结算"
        full["payment"] = pay
        resp = {"status": "paid_credit" if mode == "credit" else "paid_mock",
                "lead": full}
        if trial_note:
            resp["trial_credit_note"] = trial_note
        return resp

    if mock_confirm:
        # 仅本地联调用：模拟"已支付"，返回完整 lead（credit 抵扣已先行记账）
        return _paid("mock")

    if remainder <= 0:
        # 全额 credit 覆盖：直接交付，不走 402
        return _paid("credit")

    # x402 真实链路：返回 402 + paymentRequirements（抵扣后的余款）。
    # payTo 未配置（LEADS_PAY_TO）时为占位模式，不产生真实支付。
    resp = x402mod.payment_required_response(lead_id, remainder)
    if credit_used > 0:
        resp["credit_applied_usd"] = credit_used
        resp["message"] = (f"已抵扣 {credit_used} USD credit，"
                           f"剩余 {remainder} USD 待支付。" + resp["message"])
    if trial_note:
        resp["trial_credit_note"] = trial_note
    con.close()
    return resp


def credit_balance_impl(buyer_id: str = "default") -> dict:
    con = sqlite3.connect(DB_PATH)
    bal = _credit_balance(con, buyer_id)
    claim = con.execute(
        "SELECT code, expires_at FROM trial_claims WHERE buyer_id = ?",
        (buyer_id,)).fetchone()
    con.close()
    resp = {"buyer_id": buyer_id,
            "balance_usd": bal,
            "note": "reward/refund credit 永久有效；trial credit 领取后 30 天有效"}
    if claim:
        resp["trial"] = {"channel_code": claim[0], "expires_at": claim[1]}
    return resp


def report_conversion_impl(lead_id: str, outcome: str, reason: str = "",
                           reason_code: str = "", evidence: str = "",
                           buyer_id: str = "default", contacted_at: str = "") -> dict:
    """回传 v1（原因码 schema 已定死）：reason_code 按 outcome 分组校验；
    other_* 必须带 evidence；旧调用（仅 reason 自由文本）继续兼容，richness=basic。
    contacted_at=买家实际触达时间（ISO，可空），为品类衰减曲线留数据。"""
    allowed = set(REASON_CODES)
    if outcome not in allowed:
        return {"status": "error",
                "message": f"outcome 非法，应为 {sorted(allowed)} 之一"}
    if not any(l["id"] == lead_id for l in LEADS):
        return {"status": "error", "message": f"未找到 lead {lead_id}"}
    code = (reason_code or "").strip()
    if code:
        if code not in REASON_CODES[outcome]:
            return {"status": "error",
                    "message": (f"reason_code '{code}' 不属于 outcome={outcome}，"
                                f"应为 {sorted(REASON_CODES[outcome])} 之一")}
        if code.startswith("other_") and not (evidence or "").strip():
            return {"status": "error",
                    "message": f"reason_code={code} 必须填写 evidence 说明具体情形"}
    richness = _report_richness(outcome, code, evidence)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO conversions (lead_id, outcome, reason, created_at,"
        " buyer_id, reason_code, evidence, richness, contacted_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (lead_id, outcome, reason, _now_iso(),
         buyer_id, code, evidence, richness, contacted_at),
    )

    resp = {"status": "recorded", "lead_id": lead_id, "outcome": outcome,
            "reason_code": code, "richness": richness}
    if outcome == "bad_lead":
        resp["sla"] = ("已受理坏单申报：24h 内自动退 credit 或换一条同类目等值 lead。"
                       "坏单率按周统计，≥5% 触发类目下架整顿。")
    elif outcome == "converted":
        # 返利按 richness 分档（2026-10-23 拍板）：冷启动期 rich 25%/coded 15%/basic 7%，
        # 累计 converted 满 100 次后转常态 rich 10%/coded 7%/basic 3%。
        # （计数在本次 INSERT 之后查、含本次，<= 保证第 1–100 次享冷启动档）
        rate = _rebate_rate(richness, _converted_count(con))
        lead = next((l for l in LEADS if l["id"] == lead_id), None)
        price = _price_of(lead) if lead else 0
        credit = round((price or 0) * rate, 2)
        if credit > 0:
            con.execute(
                "INSERT INTO buyer_credits (buyer_id, amount_usd, reason, source, created_at)"
                " VALUES (?,?,?,?,?)",
                (buyer_id, credit, f"converted:{lead_id}", "reward", _now_iso()),
            )
        resp["reward"] = (f"感谢回传！已返还 {credit} USD credit"
                         f"（成交价的 {int(rate * 100)}%），下次购买可抵扣。")
    con.commit()
    con.close()
    return resp


# ================= MCP tool 注册（薄封装） =================

@mcp.tool()
def search_leads(category: str = "", min_qa_score: float = 60.0,
                 limit: int = 10, buyer_id: str = "default") -> str:
    """搜索在售 lead 摘要（免费）。category 可选：ai_automation_intent / funding_hiring_signal / devtool_signal。每次搜索会落日志（需求图谱/零结果缺口分析），零结果同样记录。"""
    return json.dumps(search_leads_impl(category, min_qa_score, limit, buyer_id),
                      ensure_ascii=False, indent=2)


@mcp.tool()
def buy_lead(lead_id: str, mock_confirm: bool = False,
             trial_code: str = "", buyer_id: str = "default") -> str:
    """购买 lead。trial_code 传渠道码（MOLTBOOK-10/SALUTE-10/IH-10）领取并抵扣 $5 试吃 credit：每身份限领一次，仅非独家、qa 60–75 档可抵扣，不足部分走 x402。当前返回 x402 402 待支付占位（真实结算待接）；mock_confirm=True 仅本地联调模拟已支付。"""
    return json.dumps(buy_lead_impl(lead_id, mock_confirm, trial_code, buyer_id),
                      ensure_ascii=False, indent=2)


@mcp.tool()
def credit_balance(buyer_id: str = "default") -> str:
    """查询买家 credit 余额（含试吃 credit 渠道与有效期）。"""
    return json.dumps(credit_balance_impl(buyer_id), ensure_ascii=False, indent=2)


@mcp.tool()
def report_conversion(lead_id: str, outcome: str, reason: str = "",
                      reason_code: str = "", evidence: str = "",
                      buyer_id: str = "default", contacted_at: str = "") -> str:
    """回传转化结果（schema v1）。outcome：converted / no_response / bad_lead。
    reason_code 结构化原因码（按 outcome 分组，见回传响应里的枚举或 docs/conversion-api.md）；
    evidence 一句证据；contacted_at 实际触达时间（ISO）。带原因码+证据的回传 richness=rich，
    是全网成交 ground truth 的原料；仅 outcome 的旧式回传仍兼容。"""
    return json.dumps(report_conversion_impl(lead_id, outcome, reason, reason_code,
                                             evidence, buyer_id, contacted_at),
                      ensure_ascii=False, indent=2)


if __name__ == "__main__":
    init_db()
    mcp.run()
