# AI Orchestration Playbook

This project uses three AI roles:

```text
Codex -> controller, analyst, risk gate
Grok -> fast news, social signal, contradiction scan
Antigravity/Gemini -> official source, long context, deep verification
```

## Activation Model

The system is event-first and cadence-second.

### Grok

- Market-hours light scan: every 5 minutes for watchlist/news clusters.
- Off-hours light scan: every 30 minutes.
- Immediate trigger: breaking news, social cluster, X thread, price or volume
  anomaly, user asks for fast external context.
- Cooldown: 5 minutes per symbol or topic.

### Antigravity/Gemini

- Market-hours deep check: every 15 minutes for candidate signals.
- Off-hours deep check: every 60 minutes.
- Immediate trigger: official filing, earnings document, Grok candidate signal,
  Codex disconfirmation request, supply-chain or thesis review.
- Cooldown: 15 minutes per symbol or topic.

### Codex

- Activates on every user task, scheduled cycle, or worker output.
- Runs the skill router, validates evidence, checks contradictions, computes or
  reads K-line context, and writes the review brief.
- Codex is the only role allowed to decide whether a finding is worth promoting
  to a draft source event.
- Codex may output trade recommendations, non-transmitting order drafts, and
  cancel/replace suggestions. Actual broker writes remain disabled.

## Communication Model

The AIs do not chat through secrets or hidden state. They communicate through
auditable artifacts:

```text
/app/agent/runs/<run_id>/
/app/agent/state/ai_bus/<run_id>/
```

Each message follows:

```text
/app/agent/schemas/ai_message.schema.json
```

The normal message flow is:

1. Codex writes the task envelope.
2. Codex dispatches worker envelopes.
3. Grok and/or Antigravity write evidence outputs through `skill_router.py`.
4. Codex writes a fusion envelope.
5. Human review stays required before promotion.

## Trigger Routing

- Breaking news: Grok first, Antigravity confirms, Codex fuses.
- Official filing or earnings: Antigravity first, Grok checks narrative spread,
  Codex fuses.
- Price or volume anomaly: Grok checks news/social, Antigravity checks official
  or high-trust evidence, Codex fuses.
- User research request: Codex picks skill, then routes to the needed workers.

## Quorum

- L0: one low-confidence worker finding is allowed as a note.
- L1: one worker plus Codex review.
- L2: two independent sources, or one worker plus strong price resonance.
- L3: official confirmation plus Codex risk gate.
- L4: current holding/open-order hit, or L3 with fresh trade-ready inputs.
- Trade execution: never automatic from worker output.

## Portfolio And Hit Monitoring

- IBKR read-only snapshot: every 3600 seconds.
- Active hit symbols: every 60 seconds while unexpired.
- L4 writes `/app/agent/state/hit_symbols.json` so the runner can keep watching
  the affected ticker after the first alert.

## Command Examples

Show capabilities:

```bash
python3 /app/agent/scripts/ai_coordinator.py --capabilities
```

Plan a coordination run:

```bash
python3 /app/agent/scripts/ai_coordinator.py \
  --trigger breaking_news \
  --symbols NVDA,MSFT \
  --task "Check whether this AI infrastructure news has a real supply-chain bottleneck impact."
```

Execute a bounded worker run:

```bash
python3 /app/agent/scripts/ai_coordinator.py \
  --trigger user_research_request \
  --symbols NVDA \
  --task "Use Serenity bottleneck framework to test whether NVDA suppliers have hidden chokepoints." \
  --execute
```
