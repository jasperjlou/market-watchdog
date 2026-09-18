#!/usr/bin/env python3
"""Receive owner-only Telegram questions and create isolated Codex jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import wechat_ai_message_poller as shared


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_PATH = AGENT_DIR / "state" / "telegram_ai_gateway_state.json"
LATEST_ANSWER_PATH = AGENT_DIR / "state" / "telegram_ai_latest_answer.json"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "TELEGRAM_CHAT_ID"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:20]


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_state(payload: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, STATE_PATH)
    STATE_PATH.chmod(0o600)


def api_call(token: str, method: str, payload: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"telegram_http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("telegram_network_error") from exc
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise RuntimeError("telegram_invalid_json") from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        code = parsed.get("error_code") if isinstance(parsed, dict) else "unknown"
        raise RuntimeError(f"telegram_api_{code}")
    return parsed.get("result")


def send_reply(token: str, chat_id: str, text: str) -> None:
    api_call(token, "sendMessage", {"chat_id": chat_id, "text": text[:4096]})


def latest_answer() -> str:
    payload = load_json(LATEST_ANSWER_PATH, {})
    body = str(payload.get("body") or "").strip() if isinstance(payload, dict) else ""
    return body[:4096] if body else "还没有新的分析结果。你可以直接发送股票代码或持仓问题。"


def run_once(limit: int = 10) -> dict[str, Any]:
    token = str(os.environ.get(TOKEN_ENV) or "").strip()
    owner_chat_id = str(os.environ.get(CHAT_ID_ENV) or "").strip()
    base = {
        "version": "1.0",
        "updated_at": utc_now_iso(),
        "owner_hash": "chat_" + stable_hash(owner_chat_id) if owner_chat_id else "",
        "actual_broker_writes": False,
        "credential_values_stored": False,
    }
    if not token or not owner_chat_id:
        payload = {**base, "status": "disabled_missing_env", "received": 0, "accepted": 0}
        write_state(payload)
        return payload

    state = load_json(STATE_PATH, {})
    next_offset = state.get("next_offset") if isinstance(state, dict) else None
    try:
        webhook = api_call(token, "getWebhookInfo", {}) if next_offset is None else {}
        if isinstance(webhook, dict) and str(webhook.get("url") or ""):
            payload = {**base, "status": "blocked_webhook_active", "received": 0, "accepted": 0}
            write_state(payload)
            return payload
        if next_offset is None:
            pending = api_call(token, "getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": ["message"]})
            updates = pending if isinstance(pending, list) else []
            next_offset = max((int(item.get("update_id", -1)) for item in updates if isinstance(item, dict)), default=-1) + 1
            payload = {**base, "status": "bootstrapped", "next_offset": next_offset, "received": 0, "accepted": 0}
            write_state(payload)
            return payload

        raw_updates = api_call(
            token,
            "getUpdates",
            {"offset": int(next_offset), "limit": max(1, min(limit, 100)), "timeout": 1, "allowed_updates": ["message"]},
        )
        updates = raw_updates if isinstance(raw_updates, list) else []
        accepted = 0
        ignored = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = int(update.get("update_id", -1))
            next_offset = max(int(next_offset), update_id + 1)
            message = update.get("message") if isinstance(update.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            incoming_chat_id = str(chat.get("id") or "")
            text = shared.sanitize_question(str(message.get("text") or ""))
            if incoming_chat_id != owner_chat_id or not text:
                ignored += 1
                continue
            lowered = text.lower().strip()
            if lowered in {"/start", "/help", "帮助", "help"}:
                send_reply(token, owner_chat_id, "直接发送股票代码、走势问题或持仓减持问题；Telegram 提问会在 Telegram 自动回复。不会自动下单。")
            elif lowered in {"结果", "上次结果", "上次结论", "result", "last"}:
                send_reply(token, owner_chat_id, latest_answer())
            else:
                config = shared.load_config()
                intent = shared.classify_intent(text, config)
                shared.write_trigger(
                    config,
                    {
                        "message_id": "tg_" + stable_hash(update_id),
                        "received_at": utc_now_iso(),
                        "message": text,
                        "source_channel": "telegram",
                    },
                    intent,
                    dry_run=False,
                )
                send_reply(token, owner_chat_id, "已收到，正在结合 Moomoo 持仓、价格趋势和相关新闻分析；结果会自动回复到这里，不会自动下单。")
            accepted += 1
        payload = {
            **base,
            "status": "ok",
            "next_offset": int(next_offset),
            "received": len(updates),
            "accepted": accepted,
            "ignored": ignored,
        }
    except RuntimeError as exc:
        payload = {
            **base,
            "status": "error",
            "next_offset": int(next_offset or 0),
            "received": 0,
            "accepted": 0,
            "error": str(exc)[:80],
        }
    write_state(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    payload = run_once(args.limit)
    print(
        "TELEGRAM_AI_POLLER "
        f"status={payload['status']} received={payload['received']} accepted={payload['accepted']} "
        "broker_writes=false secrets_logged=false"
    )
    return 0 if payload["status"] in {"ok", "bootstrapped", "disabled_missing_env"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
