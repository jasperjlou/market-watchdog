#!/usr/bin/env python3
"""Poll WeChat official-account inbox and create AI trigger tasks.

WeChat official accounts deliver inbound messages by callback, not by a pull API.
The callback receiver appends messages to `/app/state/wechat_official_inbox.jsonl`;
this poller turns new messages into Codex/worker trigger files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("APP_DIR", str(PROJECT_DIR)))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
CONFIG_PATH = AGENT_DIR / "config" / "wechat_ai_gateway.json"
WATCHLIST_PATH = APP_DIR / "config" / "watchlist.yaml"
SCAN_POLICY_PATH = APP_DIR / "config" / "scan_policy.yaml"
SYMBOL_ALIASES_PATH = APP_DIR / "config" / "symbol_query_aliases.yaml"
STATE_DIR = AGENT_DIR / "state"
AUTH_REQUESTS_PATH = STATE_DIR / "trade_authorization_requests.jsonl"
OUTLOOK_PATH = STATE_DIR / "trend_outlook_latest.json"
NEWS_PATH = STATE_DIR / "proactive_news_evidence.json"
PORTFOLIO_PATH = STATE_DIR / "moomoo_portfolio_snapshot_latest.json"


PREFIXED_TICKER_RE = re.compile(
    r"(?<![A-Z0-9])(?:\$|US[.:])([A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,2})?)(?![A-Z0-9])"
)
SPECIAL_TICKER_RE = re.compile(r"(?<![A-Z0-9])(\^[A-Z0-9]{1,6}|[A-Z]{1,8}=[A-Z])(?![A-Z0-9])")
PLAIN_TICKER_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,2})?)(?![A-Z0-9])")
DEFAULT_IGNORED_TOKENS = {
    "AI", "API", "CPI", "CURRENT", "ETF", "FOMC", "GDP", "IPO", "NEWS", "PRICE", "SEC", "THE", "USD",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload if payload is not None else default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_bridge_json(path: Path, payload: Any) -> None:
    """Atomically publish a non-secret job into the isolated Codex bridge."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o660)
    os.replace(temporary, path)
    path.chmod(0o660)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_config() -> dict[str, Any]:
    return load_json(CONFIG_PATH, {})


def app_path(value: Any, fallback: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if raw == "/app":
        return APP_DIR
    if raw.startswith("/app/"):
        return APP_DIR / raw[len("/app/"):]
    return Path(raw)


def load_inbox(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(limit * 5, 50):]:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def load_callback_state_item(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path, {})
    if not isinstance(payload, dict):
        return []
    message = str(payload.get("last_message_preview") or "").strip()
    last_type = str(payload.get("last_type") or "")
    updated_at = str(payload.get("updated_at") or "")
    if not message or message in {"GET_VERIFY", "ECHO_OK"} or last_type == "verify":
        return []
    return [
        {
            "version": "0.1",
            "message_id": "wx_state_" + stable_id(updated_at, last_type, message),
            "received_at": updated_at,
            "source": "wechat_official_callback_state",
            "user_hash": "",
            "msg_type": last_type,
            "message": message,
            "status": "new",
            "state_fallback": True,
        }
    ]


def normalize_symbol(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().upper()


def query_config() -> dict[str, Any]:
    payload = load_yaml(SYMBOL_ALIASES_PATH, {})
    return payload if isinstance(payload, dict) else {}


def ordered_known_symbols(config: dict[str, Any]) -> list[str]:
    symbols: list[str] = []

    def add(value: Any) -> None:
        symbol = normalize_symbol(value)
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    watchlist = load_yaml(WATCHLIST_PATH, {})
    for item in watchlist.get("items", []) if isinstance(watchlist, dict) else []:
        if isinstance(item, dict):
            add(item.get("symbol"))

    scan_policy = load_yaml(SCAN_POLICY_PATH, {})
    if isinstance(scan_policy, dict):
        for tier in (scan_policy.get("tiers") or {}).values():
            if isinstance(tier, dict):
                for symbol in tier.get("symbols", []):
                    add(symbol)
        for basket in (scan_policy.get("theme_baskets") or {}).values():
            if isinstance(basket, dict):
                for symbol in basket.get("symbols", []):
                    add(symbol)

    for symbol in (config.get("symbol_aliases") or {}):
        add(symbol)

    portfolio = load_json(PORTFOLIO_PATH, {})
    for item in portfolio.get("positions", []) if isinstance(portfolio, dict) else []:
        if isinstance(item, dict):
            add(item.get("symbol"))

    outlook = load_json(OUTLOOK_PATH, {})
    for item in outlook.get("items", []) if isinstance(outlook, dict) else []:
        if isinstance(item, dict):
            add(item.get("symbol"))
    return symbols


def alias_match_span(text: str, alias: Any) -> tuple[int, int] | None:
    haystack = unicodedata.normalize("NFKC", text).casefold()
    needle = unicodedata.normalize("NFKC", str(alias or "")).casefold().strip()
    if not needle:
        return None
    start = 0
    while True:
        position = haystack.find(needle, start)
        if position < 0:
            return None
        end = position + len(needle)
        before = haystack[position - 1] if position else ""
        after = haystack[end] if end < len(haystack) else ""
        first = needle[0]
        last = needle[-1]
        before_ok = not (first.isascii() and first.isalnum() and before.isascii() and before.isalnum())
        after_ok = not (last.isascii() and last.isalnum() and after.isascii() and after.isalnum())
        if before_ok and after_ok:
            return position, end
        start = position + 1


def alias_position(text: str, alias: Any) -> int | None:
    match = alias_match_span(text, alias)
    return match[0] if match else None


def symbol_alias_events(text: str, config: dict[str, Any]) -> list[tuple[int, int, str]]:
    aliases_by_symbol: dict[str, list[str]] = {}
    watchlist = load_yaml(WATCHLIST_PATH, {})
    for item in watchlist.get("items", []) if isinstance(watchlist, dict) else []:
        if not isinstance(item, dict):
            continue
        symbol = normalize_symbol(item.get("symbol"))
        name = str(item.get("name") or "").strip()
        if symbol and name:
            aliases_by_symbol.setdefault(symbol, []).append(name)
    for raw_symbol, raw_aliases in (config.get("symbol_aliases") or {}).items():
        symbol = normalize_symbol(raw_symbol)
        aliases = raw_aliases if isinstance(raw_aliases, list) else [raw_aliases]
        aliases_by_symbol.setdefault(symbol, []).extend(str(value) for value in aliases if value)

    events: list[tuple[int, int, str]] = []
    for symbol, aliases in aliases_by_symbol.items():
        matches = [match for alias in aliases if (match := alias_match_span(text, alias)) is not None]
        if matches:
            start, end = min(matches, key=lambda item: (item[0], -(item[1] - item[0])))
            events.append((start, end, symbol))
    return sorted(events, key=lambda item: (item[0], item[2]))


def theme_baskets() -> dict[str, list[str]]:
    payload = load_yaml(SCAN_POLICY_PATH, {})
    baskets: dict[str, list[str]] = {}
    for name, item in (payload.get("theme_baskets") or {}).items() if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        symbols = [normalize_symbol(value) for value in item.get("symbols", [])]
        baskets[str(name)] = [symbol for symbol in symbols if symbol]
    return baskets


def symbols_from_text(text: str) -> list[str]:
    config = query_config()
    maximum = max(1, min(int(config.get("max_results") or 12), 24))
    known = set(ordered_known_symbols(config))
    ignored = DEFAULT_IGNORED_TOKENS | {
        normalize_symbol(value) for value in config.get("ignored_tokens", []) if value
    }
    upper = unicodedata.normalize("NFKC", text).upper()
    explicit_events: list[tuple[int, int, str]] = []

    for match in PREFIXED_TICKER_RE.finditer(upper):
        explicit_events.append((match.start(), match.end(), normalize_symbol(match.group(1))))
    for match in SPECIAL_TICKER_RE.finditer(upper):
        symbol = normalize_symbol(match.group(1))
        if symbol in known:
            explicit_events.append((match.start(), match.end(), symbol))
    for match in PLAIN_TICKER_RE.finditer(upper):
        symbol = normalize_symbol(match.group(1))
        if symbol in known and symbol not in ignored:
            explicit_events.append((match.start(), match.end(), symbol))
    alias_events = symbol_alias_events(text, config)
    explicit_events.extend(alias_events)

    resolved: list[str] = []

    def add(symbol: Any) -> None:
        normalized = normalize_symbol(symbol)
        if normalized and normalized not in resolved and len(resolved) < maximum:
            resolved.append(normalized)

    for _, _, symbol in sorted(explicit_events, key=lambda item: item[0]):
        add(symbol)

    baskets = theme_baskets()
    relationship_terms = config.get("relationship_terms") or []
    relationship_requested = any(alias_position(text, term) is not None for term in relationship_terms)
    theme_events: list[tuple[int, int, str]] = []
    for name, raw_aliases in (config.get("theme_aliases") or {}).items():
        aliases = raw_aliases if isinstance(raw_aliases, list) else [raw_aliases]
        matches = [match for alias in aliases if (match := alias_match_span(text, alias)) is not None]
        if matches and str(name) in baskets:
            start, end = min(matches, key=lambda item: (item[0], -(item[1] - item[0])))
            nested_in_company_name = any(
                start >= alias_start and end <= alias_end for alias_start, alias_end, _ in alias_events
            )
            if not nested_in_company_name or relationship_requested:
                theme_events.append((start, end, str(name)))
    for _, _, name in sorted(theme_events, key=lambda item: item[0]):
        for symbol in baskets[name]:
            add(symbol)

    if relationship_requested and resolved:
        seed_symbols = list(resolved)
        memberships: list[tuple[int, int, list[str]]] = []
        basket_order = {name: index for index, name in enumerate(baskets)}
        for symbol in seed_symbols:
            for name, basket_symbols in baskets.items():
                if symbol in basket_symbols:
                    memberships.append((len(basket_symbols), basket_order[name], basket_symbols))
        for _, _, basket_symbols in sorted(memberships):
            for symbol in basket_symbols:
                add(symbol)
    return resolved[:maximum]


def contains_any(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower or term in text for term in terms)


def classify_intent(text: str, config: dict[str, Any]) -> str:
    intents = config.get("intents") if isinstance(config.get("intents"), dict) else {}
    if contains_any(text, list(intents.get("trade_authorization") or [])):
        return "trade_authorization"
    if contains_any(text, list(intents.get("trade_draft") or [])):
        return "trade_draft"
    return "market_question"


def trigger_for_intent(intent: str, text: str) -> str:
    lower = text.lower()
    if intent in {"trade_draft", "trade_authorization"}:
        return "trade_authorization_request" if intent == "trade_authorization" else "wechat_message"
    if any(term in lower for term in ["sec", "filing", "10-k", "10-q", "8-k", "official", "earnings"]):
        return "official_filing_or_earnings"
    if any(term in lower for term in ["news", "breaking", "x.com", "twitter", "reddit", "rumor"]):
        return "breaking_news"
    return "wechat_message"


def task_for_message(item: dict[str, Any], intent: str) -> str:
    text = sanitize_question(str(item.get("message") or ""))
    source_channel = str(item.get("source_channel") or "wechat").strip().lower()
    source_label = "Telegram" if source_channel == "telegram" else "WeChat"
    prefix = {
        "market_question": f"{source_label} user market question. Answer with concise market/news/position-aware analysis.",
        "trade_draft": f"{source_label} user asks for trade/order/cancel/replace draft. Produce concise recommendation and non-transmitting order ticket only.",
        "trade_authorization": f"{source_label} user may be authorizing a trade. Build authorization record, risk precheck, and non-transmitting order draft. Do not call broker write APIs.",
    }.get(intent, f"{source_label} user message.")
    return (
        f"{prefix}\n"
        f"Message: {text}\n"
        "Use current portfolio/open-order snapshot if available. "
        "Allowed: recommendation, order ticket draft, cancel/replace suggestion. "
        "Forbidden: placeOrder/cancelOrder/modifyOrder/transmit=true."
    )


def sanitize_question(text: str, limit: int = 1200) -> str:
    cleaned = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    return cleaned.strip()[:limit]


def safe_portfolio_context() -> dict[str, Any]:
    payload = load_json(PORTFOLIO_PATH, {})
    positions = []
    allowed = (
        "symbol", "position", "quantity", "average_cost", "avg_cost", "market_price",
        "market_value", "unrealized_pnl", "unrealized_pnl_pct", "currency",
    )
    for item in payload.get("positions", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        safe = {key: item.get(key) for key in allowed if item.get(key) is not None}
        if safe.get("symbol"):
            positions.append(safe)
        if len(positions) >= 60:
            break
    return {
        "available": bool(isinstance(payload, dict) and payload.get("account_data_available") is True),
        "provider": "moomoo",
        "positions_count": len(positions),
        "positions": positions,
        "as_of": (
            payload.get("updated_at") or payload.get("captured_at") or payload.get("collected_at")
        ) if isinstance(payload, dict) else None,
    }


def safe_market_context(symbols: list[str]) -> dict[str, Any]:
    wanted = {symbol.upper() for symbol in symbols}
    outlook_payload = load_json(OUTLOOK_PATH, {})
    raw_outlook = outlook_payload.get("items", []) if isinstance(outlook_payload, dict) else []
    ranked = sorted(
        (item for item in raw_outlook if isinstance(item, dict)),
        key=lambda item: float(item.get("risk_score") or 0),
        reverse=True,
    )
    selected = [item for item in ranked if not wanted or str(item.get("symbol") or "").upper() in wanted][:12]
    outlook_keys = (
        "symbol", "category", "risk_score", "confidence_label", "short_term", "swing",
        "conclusion", "invalidation", "return_1d_pct", "return_5d_pct", "return_20d_pct",
        "current_price", "last_close", "support", "resistance", "flags", "portfolio",
        "risk_reasons", "data_source", "delayed",
    )
    outlook = [{key: item.get(key) for key in outlook_keys if item.get(key) is not None} for item in selected]

    news_payload = load_json(NEWS_PATH, {})
    news = []
    news_keys = (
        "title", "url", "source_name", "source_tier", "published_at", "symbols",
        "summary", "direction", "impact_horizon", "confidence", "limitations",
    )
    for item in news_payload.get("items", []) if isinstance(news_payload, dict) else []:
        if not isinstance(item, dict):
            continue
        item_symbols = {str(value).upper() for value in item.get("symbols", [])}
        if wanted and not wanted.intersection(item_symbols):
            continue
        news.append({key: item.get(key) for key in news_keys if item.get(key) is not None})
        if len(news) >= 10:
            break
    return {
        "requested_symbols": sorted(wanted),
        "trend_as_of": (
            outlook_payload.get("updated_at") or outlook_payload.get("generated_at") or outlook_payload.get("price_snapshot_at")
        ) if isinstance(outlook_payload, dict) else None,
        "trend_items": outlook,
        "news_as_of": news_payload.get("updated_at") if isinstance(news_payload, dict) else None,
        "news_items": news,
    }


def write_trigger(config: dict[str, Any], item: dict[str, Any], intent: str, dry_run: bool) -> dict[str, Any]:
    query_dir = app_path(config.get("query_inbox_path"), Path("/var/lib/market-watchdog/codex-queries/inbox"))
    message = sanitize_question(str(item.get("message") or ""))
    message_id = str(item.get("message_id") or stable_id(message, item.get("received_at")))
    symbols = symbols_from_text(message)
    request_type = "portfolio_advice" if intent in {"trade_draft", "trade_authorization"} else "market_question"
    source_channel = str(item.get("source_channel") or "wechat").strip().lower()
    if source_channel not in {"wechat", "telegram"}:
        source_channel = "wechat"
    job_id = "wechat_" + stable_id(message_id, intent)
    payload = {
        "version": "1.0",
        "job_id": job_id,
        "created_at": utc_now_iso(),
        "source": "telegram_bot" if source_channel == "telegram" else "wechat_official",
        "source_message_id": message_id,
        "request_type": request_type,
        "intent": intent,
        "question": message,
        "symbols": symbols,
        "portfolio_context": safe_portfolio_context(),
        "market_context": safe_market_context(symbols),
        "delivery": {
            "source_channel": source_channel,
            "primary_channel": source_channel,
            "reply_mode": "direct" if source_channel == "telegram" else "passive_result_cache",
            "email_for_urgent": False,
        },
        "model": str(config.get("model") or "gpt-5.5"),
        "actual_broker_writes_allowed": False,
        "human_review_required": True,
        "contains_credentials": False,
    }
    path = query_dir / f"{job_id}.json"
    if not dry_run:
        write_bridge_json(path, payload)
    if intent == "trade_authorization" and not dry_run:
        append_jsonl(
            AUTH_REQUESTS_PATH,
            {
                "version": "0.1",
                "created_at": utc_now_iso(),
                "source_message_id": message_id,
                "source_channel": source_channel,
                "symbols": symbols,
                "message_preview": message[:240],
                "status": "draft_only_broker_write_disabled",
                "actual_broker_writes_allowed": False,
            },
        )
    return {"path": str(path), "payload": payload}


def run_once(limit: int, dry_run: bool) -> dict[str, Any]:
    config = load_config()
    inbox_path = app_path(config.get("inbox_path"), APP_DIR / "state" / "wechat_official_inbox.jsonl")
    state_path = app_path(config.get("processed_state_path"), STATE_DIR / "wechat_ai_gateway_state.json")
    state = load_json(state_path, {"processed_ids": []})
    processed_ids = set(state.get("processed_ids") if isinstance(state.get("processed_ids"), list) else [])
    new_items = []
    results = []
    callback_state_path = app_path(config.get("callback_state_path"), APP_DIR / "state" / "wechat_official_callback_state.json")
    incoming_items = load_inbox(inbox_path, limit=limit) + load_callback_state_item(callback_state_path)
    for item in incoming_items:
        message_id = str(item.get("message_id") or stable_id(item.get("message"), item.get("received_at")))
        if message_id in processed_ids:
            continue
        message = str(item.get("message") or "").strip()
        if not message or message == "GET_VERIFY":
            processed_ids.add(message_id)
            continue
        intent = classify_intent(message, config)
        result = write_trigger(config, {**item, "message_id": message_id}, intent, dry_run=dry_run)
        results.append({"message_id": message_id, "intent": intent, **result})
        new_items.append(message_id)
        processed_ids.add(message_id)
        if len(new_items) >= limit:
            break
    payload = {
        "version": "0.1",
        "updated_at": utc_now_iso(),
        "inbox_path": str(inbox_path),
        "dry_run": dry_run,
        "new_count": len(new_items),
        "results": results,
        "processed_ids": sorted(processed_ids)[-1000:],
        "actual_broker_writes_allowed": False,
    }
    if not dry_run:
        write_json(state_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    payload = run_once(limit=args.limit, dry_run=args.dry_run)
    print(
        "WECHAT_AI_POLLER "
        f"new={payload['new_count']} dry_run={str(payload['dry_run']).lower()} "
        f"broker_write=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
