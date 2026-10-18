---
name: leads-plaza
description: Buy qualified sales leads with x402 micropayments and report conversion outcomes back. Use when the user wants to find prospects, buy lead contact info, or close the loop on an outreach.
---

# Leads Plaza — agent skill

You have three MCP tools from the `leads-plaza` server. Together they form one
loop: **find → buy → report back**. Always run the full loop; the report step
is what keeps lead quality (and your future prices) honest.

## 1. Find leads — `search_leads`

```json
{ "category": "ai_automation_intent", "min_qa_score": 60, "limit": 10 }
```

- `category` (optional): `ai_automation_intent` (people asking for AI/automation
  help — highest intent), `funding_hiring_signal` (funded/hiring companies),
  `devtool_signal` (developer-tool traction).
- `min_qa_score` (optional, default 60): QA floor. Only leads ≥ 60 are listed
  for sale. Higher = fewer, better.
- Summaries are **free** and never include contact info. Pick by `qa_score`,
  `freshness` (leads decay — fresher is better), and `price_usd`.

If the user describes a need in plain words ("find me AI automation buyers"),
map it to a category and search. Show the top 3–5 with price before buying.

## 2. Buy a lead — `buy_lead`

```json
{ "lead_id": "LD-20261013-0001" }
```

- Without payment this returns **HTTP 402** with `paymentRequirements`
  (exact-scheme USDC on Base). Sign with the user's wallet (EIP-3009
  `transferWithAuthorization`) and retry with the `X-PAYMENT` header —
  or let the x402 client middleware handle it.
- On testnet (`LEADS_NETWORK=base-sepolia`) this costs test USDC and is safe
  to try. On mainnet it spends real money — **always confirm the price with
  the user before paying**.
- `mock_confirm: true` simulates payment locally (no chain interaction).
  Useful for demos, never for real purchases.
- The paid response contains the full lead including `contact`. Treat contact
  details as the user's private data — never log or repeat them unnecessarily.

## 3. Report back — `report_conversion`

```json
{ "lead_id": "LD-20261013-0001", "outcome": "converted", "reason": "" }
```

- `outcome`: `converted` (you closed / got a reply), `no_response`
  (outreach sent, nothing back), `bad_lead` (wrong info, dead contact —
  triggers the SLA: refund credit or replacement within 24h).
- `converted` earns a **7% credit rebate** toward the next purchase.
- This is not optional politeness: reports feed the QA score and dynamic
  pricing. Reporting `bad_lead` protects every future buyer, including you.

## Golden-path script (copy-paste flow)

1. `search_leads` with the user's category → show top picks + prices.
2. User picks one → `buy_lead` → confirm price → pay (or mock).
3. Do the outreach using the returned `contact`.
4. `report_conversion` with the real outcome — same day if possible.

## Guardrails

- Never buy without the user's explicit go-ahead on mainnet.
- Never fabricate a `report_conversion` outcome — report what actually happened.
- If `buy_lead` returns `not_for_sale`, the lead failed QA; pick another.
- If the server is unconfigured (`"configured": false` in the 402), payments
  are placeholders — say so instead of pretending a purchase happened.
