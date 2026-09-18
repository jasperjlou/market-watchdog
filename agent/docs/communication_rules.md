# Communication Rules

Gmail, Telegram, WhatsApp, and WeChat are gated communication channels.
The active combined alert route is Gmail + Telegram when Telegram credentials
are complete. WhatsApp and WeChat remain available but do not enter the combined
route merely because their runtime gates exist.

## What Enabled Means

- The system can check whether the channel is ready.
- Codex can enqueue a reviewed message.
- A human-visible status file records what happened.
- Real sending requires explicit gates.

Enabled does not mean workers may send messages by themselves.

## Gmail

Default: draft or gated send.

Real send requires:

- `ALLOW_GMAIL_SEND=1`
- `MARKET_WATCHDOG_GMAIL_ALLOW_SEND=1`
- `communication_gateway.py --dispatch --allow-send`

Preferred behavior is draft/review first. Gmail sends must not include raw
secrets, actual broker API calls, or unreviewed worker output.

## WeChat

Default: callback replies and gated official send.

Real official-account send requires:

- `ALLOW_WECHAT_OFFICIAL_SEND=1`
- `MARKET_WATCHDOG_WECHAT_ALLOW_SEND=1`
- `communication_gateway.py --dispatch --allow-send`

WeChat official custom-service sending may fail if the user has not interacted
recently or the response count/window is exhausted. The startup checklist should
surface that as a manual action.

## Telegram

Default: gated official Bot API send. L2/L3/L4 urgent alerts use the same compact
three-line Chinese body as Gmail. Daily briefs remain Gmail-only.

Real Telegram send requires:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ALLOW_TELEGRAM_SEND=1`
- `MARKET_WATCHDOG_TELEGRAM_ALLOW_SEND=1`
- `communication_gateway.py --dispatch --allow-send`

The recipient must first open the bot and press Start/send `/start`; otherwise
the bot cannot initiate a private conversation.

## Allowed Message Kinds

- startup manual action
- system health
- authorization needed
- L2/L3/L4 market review
- portfolio hit
- portfolio snapshot
- trade recommendation
- order ticket draft
- cancel/replace suggestion
- daily brief
- error alert

## Blocked

- actual broker write API calls
- `placeOrder`, `cancelOrder`, `modifyOrder`, or `transmit=true`
- raw credentials or token values
- unreviewed worker output

## Message Style

Self-facing alerts should be ultra-concise. If a safety tag is needed, use only:

```text
人工确认
```
