#!/usr/bin/env python3
"""验证 402 响应带 extensions.bazaar 且结构完整（发现层收录，2026-10-23）。
直接调 x402.payment_required_response，不起 MCP。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x402

r = x402.payment_required_response("lead_test_001", 1.25)

# 原有字段不破坏
assert r["status"] == "payment_required" and r["http_status"] == 402, r
assert r["payment_requirements"]["scheme"] == "exact"
assert r["payment_requirements"]["maxAmountRequired"] == str(int(1.25 * 10 ** 6))

# extensions.bazaar 存在且结构完整
b = r["extensions"]["bazaar"]
info, schema = b["info"], b["schema"]
inp, out = info["input"], info["output"]
assert inp["type"] == "mcp_tool_call" and inp["method"] == "buy_lead", inp
assert inp["serviceName"] == "Leads Plaza" and inp["serviceDescription"]
assert "lead-generation" in inp["categories"]
assert inp["resource"] == "lead:lead_test_001"
assert inp["required"] == ["lead_id"] and "lead_id" in inp["params"]
assert "mock_confirm" in inp["params"] and "buyer_id" in inp["params"]
ex = out["example"]
assert ex["lead"]["id"] == "lead_test_001" and "contact" in ex["lead"], ex
assert schema["type"] == "object" and schema["required"] == ["info"]
assert schema["properties"]["info"]["required"] == ["input", "output"]

# 动态字段随 lead 变化
r2 = x402.payment_required_response("L-999", 0.5)
assert r2["extensions"]["bazaar"]["info"]["input"]["resource"] == "lead:L-999"

print("ALL GREEN：extensions.bazaar 结构完整，原有 402 字段完好")
