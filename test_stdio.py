#!/usr/bin/env python3
"""走真实 MCP stdio 协议联调：list_tools -> search -> buy(占位) -> buy(mock支付) -> report。"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def show(title, payload_text):
    print(f"\n===== {title} =====")
    try:
        print(json.dumps(json.loads(payload_text), ensure_ascii=False, indent=2)[:1200])
    except Exception:
        print(payload_text[:1200])


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["server.py"], cwd=BASE_DIR)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            assert set(names) == {"search_leads", "buy_lead", "report_conversion",
                                   "credit_balance"}, \
                f"tool 缺失: {names}"

            r = await session.call_tool("search_leads", {"limit": 2})
            show("search_leads", r.content[0].text)
            data = json.loads(r.content[0].text)
            assert data["total"] > 0 and "contact" not in data["leads"][0], \
                "摘要不应含 contact"
            lead_id = data["leads"][0]["id"]

            r = await session.call_tool("buy_lead", {"lead_id": lead_id})
            show("buy_lead（占位）", r.content[0].text)
            data = json.loads(r.content[0].text)
            assert data["status"] == "payment_required" and data["http_status"] == 402
            assert data["payment_requirements"]["network"] == "eip155:8453"

            r = await session.call_tool("buy_lead",
                                        {"lead_id": lead_id, "mock_confirm": True})
            show("buy_lead（mock 支付）", r.content[0].text)
            data = json.loads(r.content[0].text)
            assert data["status"] == "paid_mock" and "contact" in data["lead"]

            r = await session.call_tool("report_conversion",
                                        {"lead_id": lead_id, "outcome": "converted"})
            show("report_conversion", r.content[0].text)
            data = json.loads(r.content[0].text)
            assert data["status"] == "recorded"

            r = await session.call_tool("report_conversion",
                                        {"lead_id": lead_id, "outcome": "bad_lead",
                                         "reason": "测试坏单 SLA 文案"})
            data = json.loads(r.content[0].text)
            assert data["status"] == "recorded" and "sla" in data
            print("\nALL GREEN：mcp_server 本地 stdio 联调通过")


asyncio.run(main())
