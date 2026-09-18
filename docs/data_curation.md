# Internal AI data curation

`/app/scripts/data_curator.py` organizes bounded, non-sensitive runtime records.
It runs in the `Asia/Shanghai` timezone with this schedule:

- Daily at 03:30: try `gemini-3.5-flash-lite`, then `gemini-3.5-flash`, with low reasoning effort.
- Sunday at 04:00: try `gemini-3.6-flash`, then `gemini-3.5-flash`, with high reasoning effort.

Google Gemini is the only AI provider. Codex and Grok are not runtime fallbacks.
If Google is unavailable because of quota, authentication, model availability,
timeout, or network failure, curation stops without deleting source records.
The curator remains disabled until a harmless live Gemini request produces a
completed provider receipt.

The curator excludes paths and names associated with secrets, credentials,
IBKR, portfolios, accounts, Gmail, and WeChat. It cannot send messages or call
broker APIs.

Deletion is two-stage:

1. The model output must be valid JSON and echo the exact manifest digest.
2. A curation receipt records a content-hash fingerprint for each
   archive-eligible record.

`runtime_maintenance.py` archives a record only when its current fingerprint
matches a completed receipt. Changed or uncurated records are deferred. New
archives are verified and receive a hash manifest with source bytes, archive
bytes, and compression ratio; only verified archives older than 90 days are
permanently removed.
