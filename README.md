# Leads Plaza MCP Server

An MCP server that lets AI agents **search, buy, and report on sales leads** — with x402 micropayments (USDC on Base) and a conversion-feedback loop that drives QA scoring and dynamic pricing.

## 5-minute quickstart

```bash
git clone https://github.com/YOUR-ORG/leads-plaza-mcp-server.git
cd leads-plaza-mcp-server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python test_stdio.py   # runs a full self-check over real MCP stdio
```

Then register it with your agent framework.

**Claude Code** (`~/.claude.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "leads-plaza": {
      "command": "/path/to/leads-plaza-mcp-server/.venv/bin/python",
      "args": ["server.py"],
      "cwd": "/path/to/leads-plaza-mcp-server",
      "env": {
        "LEADS_NETWORK": "base-sepolia",
        "LEADS_PAY_TO": "0xYourSellerAddress"
      }
    }
  }
}
```

**Cursor** (Settings → MCP → Add server, same command/args/env as above).

No `LEADS_PAY_TO`? The server still runs — `buy_lead` returns a placeholder 402
explaining that no real payment will happen. Set `mock_confirm=true` to simulate
a paid purchase locally.

## Tools

| Tool | What it does | Cost |
|---|---|---|
| `search_leads(category?, min_qa_score?, limit?)` | Browse lead summaries filtered by category and QA score. Summaries never include contact info. | Free |
| `buy_lead(lead_id, mock_confirm?)` | Purchase a lead. Returns an x402 `402 payment_required` with `paymentRequirements` (EIP-3009 USDC). With `mock_confirm=true`, simulates payment locally and returns the full lead. | Pay-per-lead (USDC) |
| `report_conversion(lead_id, outcome, reason?)` | Report what happened: `converted` / `no_response` / `bad_lead`. `bad_lead` triggers the SLA (refund credit or replacement within 24h). `converted` earns a 7% credit rebate. Reports feed back into QA scoring and pricing. | Free |

Categories: `ai_automation_intent`, `funding_hiring_signal`, `devtool_signal`.

## Network status

> **Testnet only (October 2026).** The x402 flow is live on **Base Sepolia**
> (`eip155:84532`, `LEADS_NETWORK=base-sepolia`). **Mainnet is not open yet** —
> do not send real funds. When mainnet opens, set `LEADS_NETWORK=base`
> (default) and a real `LEADS_PAY_TO` seller address.

## Architecture

```
Agent / framework (Claude Code, Cursor, …)
        │  MCP stdio
        ▼
  server.py  ── search_leads / buy_lead / report_conversion
     │                │                  │
     │                │                  ▼
     │                │           conversions.db (sqlite)
     │                │           orders · conversions · buyer_credits
     │                ▼
     │          x402.py ── 402 paymentRequirements (exact/USDC)
     │                     verify/settle via Coinbase facilitator
     ▼
  pricing.py ── price = base × freshness × qa_coef × cvr × exclusive
  mock_data.py ── 12 fictional seed leads (used when no scored data present)
```

Data fallback: on startup the server looks for scored QA leads next to the
repo (`../data/qa/scored_leads_*.jsonl` in a full deployment); when absent it
gracefully falls back to the bundled `mock_data.py` fixtures. No real personal
data is ever shipped — mock contacts use `*.local` placeholder domains.

## Local verification

```bash
python test_stdio.py
```

Runs list_tools → search → buy (402 placeholder) → buy (mock paid) →
report_conversion over the real MCP stdio protocol. All green means the loop works.

## License

MIT — see [LICENSE](LICENSE).
