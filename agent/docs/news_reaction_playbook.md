# News Reaction Playbook

Updated: 2026-06-07

Goal: latest news -> related tickers -> position/open-order impact -> trade recommendation -> non-transmitting order/cancel/replace draft -> high-frequency follow-up.

## L Levels

| Level | Meaning | Push | Action |
| --- | --- | --- | --- |
| L0 | Queue/noise | No | Store, dedupe, map only |
| L1 | Fast skim | No | Worker follow-up if useful |
| L2 | Actionable alert | Yes | Concise alert + stance |
| L3 | Urgent decision | Yes | Trade recommendation + invalidation + possible order draft |
| L4 | Holding/open-order hit or execution-ready draft | Yes | Draft order/cancel/replace + high-frequency monitoring |

Promotion uses two scores:

- `alert_score`: news/source/materiality speed score.
- `decision_score`: alert score plus insight quality, execution readiness, risk gate, protection state, backtestability, data quality, fundamentals, and strategy validation.

## Fast Chain

1. Grok: fastest news/social/contradiction sweep.
2. Google/Antigravity: official source, filing, regulator, long-context check.
3. yfinance: immediate price/K-line/volume/options/news clue.
4. Codex: score, map related names, inspect K-line/portfolio, decide action.
5. Optional sidecars: OpenBB for deep data, FinanceToolkit for fundamentals, vectorbt for strategy validation.

## Concise Alert Format

```text
L4 NVDA
事件: ...
关系: 持仓/direct/二级
影响: bear, ...
建议: trim/hedge/hold/replace
草稿: SELL LMT qty... transmit=false
失效: ...
下查: 60s
```

## Trigger Rules

- Current holding or open order hit: upgrade one level; L2/L3 becomes L4.
- S3/social only: cap at L1 until official source or price resonance appears.
- S0 official plus price resonance: minimum L2.
- S0 official plus holding hit: minimum L3, often L4.
- Stale timestamp or contradiction: downgrade one level until resolved.
- Macro calendar alone is not an alert; surprise plus price resonance is.
- Risk halted: downgrade L3/L4 and block exposure-increasing drafts.
- Protection lock active: allow wait/hold/cancel-risk-reducing drafts only.
- L3/L4 must have fresh market data or be marked conditional.
- Fundamental-driven L3/L4 should run FinanceToolkit/OpenBB-style deep check when available.
- Reusable strategy rules need vectorbt-style validation before becoming persistent automation.

## Quant Tool Routing

| Tool | Path | Trigger | Output |
| --- | --- | --- | --- |
| yfinance | core hot path | L2+, holding hit, open-order hit, scheduled anomaly scan | price/K-line/volume/options/news clue |
| OpenBB | optional sidecar | L3/L4 deep scan, broad market scan, provider cross-check | richer market/macro/screener context |
| FinanceToolkit | optional sidecar | earnings, margin, balance-sheet, valuation, capital-structure events | fundamental score |
| vectorbt | optional sidecar | persistent rule, strategy-derived alert, post-alert audit | validation score |
| awesome-quant | reference only | future module discovery | candidate list |

## IBKR State

- Refresh read-only portfolio snapshot every 3600 seconds.
- State path: `/app/agent/state/ibkr_portfolio_snapshot_latest.json`.
- Include positions and open orders.
- No broker write calls.

## High-Frequency Follow-Up

- L4 or holding/open-order hit writes `/app/agent/state/hit_symbols.json`.
- Runner refreshes active hit symbols every 60 seconds.
- Default expiry: 90 minutes.
- Extend if a new L2+ event arrives.

## Source Map

Primary source map is stored in `/app/agent/config/global_financial_sources.json`.
It covers:

- global equity and volatility
- rates and central banks
- FX and capital flows
- macro calendar
- credit and liquidity
- commodities and energy
- regulatory enforcement and exchange/broker rules
- issuer filings and corporate actions
- AI/semis and export control
- government contracts, defense, space, spectrum
- logistics, shipping, geopolitical chokepoints

## Execution Boundary

Allowed: recommendation, order ticket draft, cancel/replace suggestion.

Forbidden: `placeOrder`, `cancelOrder`, `modifyOrder`, `transmit=true`, secret values.

## WeChat AI Gateway

- Official account callback appends inbound messages to `/app/state/wechat_official_inbox.jsonl`.
- Poller: `/app/agent/scripts/wechat_ai_message_poller.py --once`.
- Runner interval after restart: 30 seconds.
- Trade authorization messages create an authorization record and AI trigger only.
- Current broker write state: disabled.
