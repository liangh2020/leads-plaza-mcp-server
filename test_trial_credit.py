#!/usr/bin/env python3
"""试吃 credit 抵扣逻辑单测（纯函数 + 临时 sqlite，不碰真实 data/conversions.db）。

覆盖 docs/trial-credits.md 实现清单：
- 渠道码校验 / 同一身份限领一次 / credit_balance 查询
- buy_lead 全额抵扣（paid_credit）、部分抵扣+402 余款、独家/qa≥75 不抵扣
- trial credit 30 天过期后不计入余额
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import server  # noqa: E402

tmp = tempfile.mkdtemp(prefix="trial_test_")
server.DB_PATH = os.path.join(tmp, "test.db")
server.init_db()

# 受控测试 leads：t1 试吃档（qa 65，非独家，价 0.40）
# t2 qa 80（不在试吃档），t3 独家（不在试吃档）
def _mk(lid, qa, exclusive=False, category="devtool_signal"):
    return {"id": lid, "category": category, "title": lid,
            "source_url": f"https://x/{lid}", "summary": "",
            "intent_score": 0.8, "qa_score": qa, "age_hours": 1.0,
            "exclusive": exclusive, "category_cvr": None,
            "contact": {"type": "post", "value": f"https://x/{lid}"}}

server.LEADS = [_mk("t1", 65.0), _mk("t2", 80.0), _mk("t3", 65.0, exclusive=True)]
P1 = server._price_of(server.LEADS[0])
assert P1 == 0.4, f"测试前置失败：t1 价格应为 0.40，实际 {P1}"

con = sqlite3.connect(server.DB_PATH)

# 1. 有效渠道码领取 → 余额 5.0
ok, msg = server._claim_trial_credit(con, "buyerA", "MOLTBOOK-10")
con.commit()
assert ok, msg
assert server._credit_balance(con, "buyerA") == 5.0
print("1. 领取成功 OK:", msg)

# 2. 同一身份重复领取被拦（换渠道码也不行），余额不变
ok, msg = server._claim_trial_credit(con, "buyerA", "SALUTE-10")
assert not ok and "限领一次" in msg, msg
assert server._credit_balance(con, "buyerA") == 5.0
print("2. 防薅（同一身份限领一次）OK")

# 3. 无效渠道码
ok, msg = server._claim_trial_credit(con, "buyerB", "NOPE-99")
assert not ok and "渠道码无效" in msg, msg
print("3. 无效渠道码拒绝 OK")

# 4. 另一身份可领另一渠道码（来源统计）
ok, _ = server._claim_trial_credit(con, "buyerB", "SALUTE-10")
con.commit()
assert ok and server._credit_balance(con, "buyerB") == 5.0
row = con.execute(
    "SELECT code FROM trial_claims WHERE buyer_id='buyerB'").fetchone()
assert row[0] == "SALUTE-10"
print("4. 多身份/渠道码独立统计 OK")

# 5. 全额抵扣：buy_lead 真实链路 → paid_credit，不走 402
r = server.buy_lead_impl("t1", trial_code="IH-10", buyer_id="buyerC")
assert r["status"] == "paid_credit", r
assert r["lead"]["payment"]["credit_used_usd"] == P1, r
assert server._credit_balance(sqlite3.connect(server.DB_PATH), "buyerC") == round(5.0 - P1, 2)
assert "contact" in r["lead"]
print(f"5. 全额 credit 支付 OK（抵扣 {P1}，余 {round(5.0 - P1, 2)}）")

# 6. 部分抵扣：余额 0.30 < 价格 0.40 → 抵扣 0.30 + 402 余款 0.10
con.execute(
    "INSERT INTO buyer_credits (buyer_id, amount_usd, reason, source, created_at)"
    " VALUES ('buyerD', 5.0, 'trial:MANUAL', 'trial', '')")
con.execute(
    "INSERT INTO buyer_credits (buyer_id, amount_usd, reason, source, created_at)"
    " VALUES ('buyerD', -4.7, 'buy:t0', 'consume', '')")
con.commit()
assert server._credit_balance(con, "buyerD") == 0.3
r = server.buy_lead_impl("t1", buyer_id="buyerD")
assert r["status"] == "payment_required" and r["http_status"] == 402, r
assert r["credit_applied_usd"] == 0.3, r
assert r["payment_requirements"]["price_usd"] == 0.1, r["payment_requirements"]
assert server._credit_balance(sqlite3.connect(server.DB_PATH), "buyerD") == 0.0
print("6. 部分抵扣 + 402 余款 OK（抵扣 0.30，402 金额 0.10）")

# 7. qa≥75 的 lead 不在试吃范围：不扣 credit，走原 402 全价
r = server.buy_lead_impl("t2", buyer_id="buyerA")
assert r["status"] == "payment_required", r
assert "credit_applied_usd" not in r, r
assert server._credit_balance(sqlite3.connect(server.DB_PATH), "buyerA") == 5.0
print("7. qa≥75 不抵扣 OK")

# 8. 独家 lead 不在试吃范围
r = server.buy_lead_impl("t3", buyer_id="buyerA")
assert r["status"] == "payment_required" and "credit_applied_usd" not in r, r
assert server._credit_balance(sqlite3.connect(server.DB_PATH), "buyerA") == 5.0
print("8. 独家 lead 不抵扣 OK")

# 9. 过期 trial credit 不计入余额
past = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
con.execute(
    "INSERT INTO buyer_credits (buyer_id, amount_usd, reason, source, expires_at, created_at)"
    " VALUES ('buyerE', 5.0, 'trial:OLD', 'trial', ?, '')", (past,))
con.commit()
assert server._credit_balance(con, "buyerE") == 0.0
print("9. 30 天过期失效 OK")

# 10. mock 链路：credit 抵扣先行记账，返回 paid_mock
r = server.buy_lead_impl("t1", mock_confirm=True, buyer_id="buyerA")
assert r["status"] == "paid_mock", r
assert r["lead"]["payment"]["credit_used_usd"] == P1, r
assert server._credit_balance(sqlite3.connect(server.DB_PATH), "buyerA") == round(5.0 - P1, 2)
print("10. mock 链路抵扣记账 OK")

# 11. buy_lead 传无效渠道码 → 明确报错，不扣款不交付
r = server.buy_lead_impl("t1", trial_code="BAD", buyer_id="buyerB")
assert r["status"] == "error" and "渠道码无效" in r["message"], r
print("11. 无效渠道码 buy_lead 报错 OK")

# 12. credit_balance_impl：含渠道与有效期
info = server.credit_balance_impl("buyerA")
assert info["balance_usd"] == round(5.0 - P1, 2), info
assert info["trial"]["channel_code"] == "MOLTBOOK-10", info
print("12. credit_balance 查询 OK:", info)

con.close()
print("\nALL GREEN：试吃 credit 12/12 通过")
