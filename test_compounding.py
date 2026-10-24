#!/usr/bin/env python3
"""复利数据资产三件套测试（2026-10-22）：
① 回传原因码 schema v1（枚举校验 / other_* 必须 evidence / 旧调用兼容 / richness 分级）
② search 日志 + 零结果记录
③ converted 返利记到回传的 buyer_id 名下
用临时 DB，不碰真实 conversions.db。"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

# 临时 DB 隔离
tmp = tempfile.mkdtemp()
server.DB_PATH = os.path.join(tmp, "test.db")
server.init_db()

lead_id = server.LEADS[0]["id"]
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ①-a 合法 rich 回传
r = server.report_conversion_impl(lead_id, "converted", reason_code="deal_closed",
                                  evidence="签了年付合同", buyer_id="buyerA",
                                  contacted_at="2026-10-22T10:00:00+08:00")
check("rich converted 受理", r["status"] == "recorded" and r["richness"] == "rich")

# ①-b coded（有码无证据）
r = server.report_conversion_impl(lead_id, "no_response", reason_code="no_reply")
check("coded 回传 richness=coded", r["status"] == "recorded" and r["richness"] == "coded")

# ①-c 旧式调用兼容（仅 reason 自由文本）
r = server.report_conversion_impl(lead_id, "bad_lead", reason="联系不上")
check("旧式回传兼容 richness=basic", r["status"] == "recorded" and r["richness"] == "basic")

# ①-d 跨组原因码被拒（converted 不能用 bad_lead 的码）
r = server.report_conversion_impl(lead_id, "converted", reason_code="wrong_info")
check("跨组 reason_code 被拒", r["status"] == "error")

# ①-e other_* 无 evidence 被拒
r = server.report_conversion_impl(lead_id, "bad_lead", reason_code="other_bad")
check("other_* 无 evidence 被拒", r["status"] == "error")
r = server.report_conversion_impl(lead_id, "bad_lead", reason_code="other_bad",
                                  evidence="对方说项目半年前就黄了但帖子还在招人")
check("other_* 带 evidence 受理", r["status"] == "recorded" and r["richness"] == "rich")

# ①-f 非法 outcome / 不存在的 lead
check("非法 outcome 被拒", server.report_conversion_impl(lead_id, "maybe")["status"] == "error")
check("不存在 lead 被拒", server.report_conversion_impl("nope", "converted")["status"] == "error")

# 落库核对
con = sqlite3.connect(server.DB_PATH)
rows = con.execute("SELECT outcome, reason_code, richness, buyer_id FROM conversions ORDER BY id").fetchall()
check("落库 4 行", len(rows) == 4)
check("首行字段正确", rows[0] == ("converted", "deal_closed", "rich", "buyerA"))

# ③ converted 返利记到 buyerA（rich 档冷启动 25%）
bal = con.execute("SELECT COALESCE(SUM(amount_usd),0) FROM buyer_credits WHERE buyer_id='buyerA'").fetchone()[0]
check("返利记入回传 buyer_id", bal > 0)

# ③-b 返利分档（2026-10-23 拍板）：冷启动 rich 25% / coded 15% / basic 7%
price = server._price_of(server.LEADS[0])
for buyer, kw, rate in (("tierRich", {"reason_code": "pilot_started", "evidence": "付费试点已启动"}, 0.25),
                        ("tierCoded", {"reason_code": "meeting_booked"}, 0.15),
                        ("tierBasic", {}, 0.07)):
    server.report_conversion_impl(lead_id, "converted", buyer_id=buyer, **kw)
    got = con.execute("SELECT COALESCE(SUM(amount_usd),0) FROM buyer_credits WHERE buyer_id=?",
                      (buyer,)).fetchone()[0]
    check(f"分档返利 {buyer}={rate}", abs(got - round(price * rate, 2)) < 0.011)
check("分档函数常态档", server._rebate_rate("rich", 101) == 0.10
      and server._rebate_rate("coded", 101) == 0.07
      and server._rebate_rate("basic", 200) == 0.03)

# ② search 日志 + 零结果
server.search_leads_impl(category="ai_automation_intent", buyer_id="buyerA")
server.search_leads_impl(category="no_such_category_xyz", buyer_id="buyerB")
logs = con.execute("SELECT buyer_id, category, zero_result FROM search_logs ORDER BY id").fetchall()
check("search 日志 2 行", len(logs) == 2)
check("零结果行标记", logs[1][1] == "no_such_category_xyz" and logs[1][2] == 1)
gaps = con.execute("SELECT category, COUNT(*) FROM search_logs WHERE zero_result=1 GROUP BY category").fetchall()
check("零结果可聚合出缺口表", gaps == [("no_such_category_xyz", 1)])
con.close()

print("\n%d FAIL" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
