# Runtime isolation and failure behavior

Core readiness is defined in `agent/config/runtime_isolation.json`.

- The deterministic scanner is the only startup-required path. Moomoo OpenD,
  Gmail, Telegram, WeChat callback, and Google/Codex workers are independently
  gated so one unavailable integration does not stop scanning or retention.
- Moomoo OpenD is the active read-only quote and holdings source on loopback;
  labelled yfinance is the quote fallback. IBKR, VNC, noVNC, Xvfb, x11vnc,
  and websockify are outside the active production path.
- Routine health transitions stay internal. Repeated identical failures do not
  create user alerts.
- A one-channel send failure is recorded as `sent_partial`; a successful
  channel is not incorrectly treated as failed or automatically resent.
- Codex quota, CLI, authentication, model, timeout, and network failures are
  categorized without storing raw provider output. Retry cooldowns prevent a
  tight failure loop.
- Background qualified reviews use the isolated Google Antigravity path.
  Interactive questions use the separate ephemeral Codex `gpt-5.5` identity;
  Grok and background Codex are disabled.
- Gmail is report-only: daily at 16:40 America/New_York and weekly Friday at
  17:10. Deduplicated, substantively reviewed L2/L3/L4 events route only to
  Telegram. Telegram questions reply directly; WeChat questions stay on the
  signed passive path and use `结果` to retrieve asynchronous answers.
- Broker writes remain disabled by application policy. Moomoo account access is
  restricted to account-list and position-list reads; no unlock, order, cancel,
  modify, or transmit path is present.
