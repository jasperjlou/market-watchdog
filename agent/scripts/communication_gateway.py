#!/usr/bin/env python3
"""Gated report, urgent-alert, and interactive-reply communication gateway."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
OUTBOX_DIR = STATE_DIR / "outbox"
SENT_DIR = STATE_DIR / "outbox_sent"
FAILED_DIR = STATE_DIR / "outbox_failed"
POLICY_PATH = AGENT_DIR / "config" / "communication_policy.json"
STATUS_PATH = STATE_DIR / "communication_status.json"
RUNTIME_ENV_PATH = APP_DIR / "secrets" / "runtime_env.sh"
SYSTEMD_ENV_PATHS = (
    Path("/etc/market-watchdog/runtime.env"),
    Path("/etc/market-watchdog/google.env"),
    Path("/etc/market-watchdog/meta.env"),
    Path("/etc/market-watchdog/wechat.env"),
    Path("/etc/market-watchdog/telegram.env"),
)
DEFAULT_GMAIL_TOKEN_PATH = APP_DIR / "secrets" / "gmail_token.json"
WECHAT_CHANNEL_STATE_PATH = STATE_DIR / "wechat_channel_state.json"
WECHAT_CALLBACK_STATE_PATH = APP_DIR / "state" / "wechat_official_callback_state.json"
GMAIL_REQUIRED_ENV = (
    "GMAIL_DEFAULT_TO",
    "ALLOW_GMAIL_SEND",
    "MARKET_WATCHDOG_GMAIL_ALLOW_SEND",
)
WECHAT_REQUIRED_ENV = (
    "WECHAT_APP_ID",
    "WECHAT_APP_SECRET",
    "WECHAT_OPENID",
    "ALLOW_WECHAT_OFFICIAL_SEND",
    "MARKET_WATCHDOG_WECHAT_ALLOW_SEND",
)
WHATSAPP_REQUIRED_ENV = (
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_RECIPIENT",
    "WHATSAPP_TEMPLATE_NAME",
    "ALLOW_WHATSAPP_SEND",
    "MARKET_WATCHDOG_WHATSAPP_ALLOW_SEND",
)
TELEGRAM_REQUIRED_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ALLOW_TELEGRAM_SEND",
    "MARKET_WATCHDOG_TELEGRAM_ALLOW_SEND",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def ensure_dirs() -> None:
    for path in (OUTBOX_DIR, SENT_DIR, FAILED_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_runtime_env() -> None:
    for path in (RUNTIME_ENV_PATH, *SYSTEMD_ENV_PATHS):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            try:
                parts = shlex.split(line)
            except ValueError:
                continue
            for part in parts:
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                os.environ.setdefault(key, value)


def whatsapp_runtime_ready() -> bool:
    credential_keys = WHATSAPP_REQUIRED_ENV[:4]
    gate_keys = WHATSAPP_REQUIRED_ENV[4:]
    return all(bool(os.environ.get(key)) for key in credential_keys) and all(
        os.environ.get(key) == "1" for key in gate_keys
    )


def telegram_runtime_ready() -> bool:
    credential_keys = TELEGRAM_REQUIRED_ENV[:2]
    gate_keys = TELEGRAM_REQUIRED_ENV[2:]
    return all(bool(os.environ.get(key)) for key in credential_keys) and all(
        os.environ.get(key) == "1" for key in gate_keys
    )


def gmail_runtime_ready() -> bool:
    token_path = Path(os.environ.get("GMAIL_TOKEN", str(DEFAULT_GMAIL_TOKEN_PATH)))
    return token_path.is_file() and bool(os.environ.get(GMAIL_REQUIRED_ENV[0])) and all(
        os.environ.get(key) == "1" for key in GMAIL_REQUIRED_ENV[1:]
    )


def wechat_runtime_ready() -> bool:
    credential_keys = WECHAT_REQUIRED_ENV[:3]
    gate_keys = WECHAT_REQUIRED_ENV[3:]
    credentials_ready = all(bool(os.environ.get(key)) for key in credential_keys) and all(
        os.environ.get(key) == "1" for key in gate_keys
    )
    state = load_json(WECHAT_CHANNEL_STATE_PATH, {})
    api_authorized = not (
        isinstance(state, dict)
        and state.get("status") == "api_unauthorized"
        and state.get("last_error_code") == 48001
    )
    return credentials_ready and api_authorized


def channel_runtime_ready(channel: str) -> bool:
    if channel == "gmail":
        return gmail_runtime_ready()
    if channel == "wechat":
        return wechat_runtime_ready()
    if channel == "whatsapp":
        return whatsapp_runtime_ready()
    if channel == "telegram":
        return telegram_runtime_ready()
    return False


def channel_enabled(policy: dict[str, Any], channel: str) -> bool:
    channels = policy.get("channels", {}) if isinstance(policy, dict) else {}
    config = channels.get(channel, {}) if isinstance(channels, dict) else {}
    if not isinstance(config, dict):
        config = {}
    if config.get("enabled") is True:
        return True
    if config.get("auto_enable_when_ready") is True:
        return channel_runtime_ready(channel)
    if channel in {"gmail", "wechat"} and "enabled" not in config:
        return True
    return False


def resolve_channels(requested: str, policy: dict[str, Any]) -> list[str]:
    if requested != "both":
        return [requested] if channel_enabled(policy, requested) else []
    raw = policy.get("combined_channels", ["telegram"])
    if not isinstance(raw, list):
        raw = ["telegram"]
    result: list[str] = []
    for item in raw:
        channel = str(item)
        if channel not in {"gmail", "wechat", "whatsapp", "telegram"} or channel in result:
            continue
        if channel_enabled(policy, channel):
            result.append(channel)
    return result


def blocked_content(text: str, policy: dict[str, Any]) -> list[str]:
    lower = text.lower()
    hits = []
    for item in policy.get("blocked_content", []):
        token = str(item).lower()
        if token in lower:
            hits.append(str(item))
    hard_patterns = ["placeorder", "cancelorder", "modifyorder", "transmit=true", "/app/secrets", "access_token", "app_secret"]
    for token in hard_patterns:
        if token in lower:
            hits.append(token)
    return sorted(set(hits))


def append_safety_tag(body: str, policy: dict[str, Any], kind: str = "") -> str:
    # These message types already carry their own safety sentence and have a
    # deliberately compact client-facing contract.  Appending another tag turns
    # a three-line alert into five lines and adds no new protection.
    if kind.startswith("market_review_") or kind in {"daily_brief", "weekly_brief"}:
        return body
    if policy.get("append_safety_tag", True) is False:
        return body
    tag = str(policy.get("required_disclaimer") or "人工确认").strip()
    if not tag or tag in body:
        return body
    return body.rstrip() + "\n\n" + tag


def routed_channel(requested: str, kind: str, policy: dict[str, Any]) -> str:
    style = policy.get("message_style") if isinstance(policy.get("message_style"), dict) else {}
    routing = style.get("routing") if isinstance(style.get("routing"), dict) else {}
    configured = str(routing.get(kind) or "").strip().lower()
    if configured == "internal_only":
        return ""
    if configured in {"gmail", "wechat", "whatsapp", "telegram", "both"}:
        return configured
    return requested


def channel_kind_violations(channel: str, kind: str, policy: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    globally_allowed = policy.get("allowed_message_kinds")
    if isinstance(globally_allowed, list) and kind not in {str(value) for value in globally_allowed}:
        violations.append("message_kind_not_allowed")
    targets = policy.get("combined_channels", []) if channel == "both" else [channel]
    channels = policy.get("channels") if isinstance(policy.get("channels"), dict) else {}
    for target in targets if isinstance(targets, list) else []:
        config = channels.get(str(target)) if isinstance(channels.get(str(target)), dict) else {}
        if "allowed_kinds" not in config:
            continue
        allowed = config.get("allowed_kinds")
        if not isinstance(allowed, list) or kind not in {str(value) for value in allowed}:
            violations.append(f"channel_kind_not_allowed:{target}")
    return sorted(set(violations))


def enqueue(channel: str, kind: str, subject: str, body: str, priority: str, source_run_id: str = "") -> dict[str, Any]:
    ensure_dirs()
    policy = load_json(POLICY_PATH, {})
    requested_channel = channel
    channel = routed_channel(channel, kind, policy)
    body = append_safety_tag(body, policy, kind)
    hits = blocked_content(subject + "\n" + body, policy)
    if not channel:
        hits.append("external_route_internal_only")
        channel = "internal_only"
    else:
        hits.extend(channel_kind_violations(channel, kind, policy))
    hits = sorted(set(hits))
    message_id = "out_" + stable_id(channel, kind, subject, body, utc_now_iso())
    payload = {
        "version": "0.1",
        "message_id": message_id,
        "created_at": utc_now_iso(),
        "channel": channel,
        "requested_channel": requested_channel if requested_channel != channel else None,
        "kind": kind,
        "priority": priority,
        "subject": subject,
        "body": body,
        "status": "refused" if hits else "queued",
        "human_review_required": True,
        "no_auto_trade": True,
        "trade_advice_allowed": bool(policy.get("trade_advice_allowed", True)),
        "order_ticket_drafts_allowed": bool(policy.get("order_ticket_drafts_allowed", True)),
        "actual_broker_writes_allowed": bool(policy.get("actual_broker_writes_allowed", False)),
        "source_run_id": source_run_id or None,
        "blocked_content_hits": hits,
        "dispatch": {}
    }
    path = (FAILED_DIR if hits else OUTBOX_DIR) / f"{message_id}.json"
    write_json(path, payload)
    return {"path": str(path), **payload}


def run_cmd(cmd: list[str], timeout: int = 60) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, cwd=str(APP_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": proc.stdout[-2000:]}
    except Exception as exc:
        return {"ok": False, "returncode": None, "output": exc.__class__.__name__}


def parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def callback_received_at() -> datetime | None:
    payload = load_json(WECHAT_CALLBACK_STATE_PATH, {})
    if not isinstance(payload, dict) or str(payload.get("last_type") or "").lower() != "xml":
        return None
    raw = payload.get("updated_at") if isinstance(payload, dict) else None
    try:
        if isinstance(raw, (int, float)) or str(raw).isdigit():
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        pass
    return parse_iso(raw)


def record_wechat_result(result: dict[str, Any], *, now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    output = str(result.get("output") or result.get("detail") or "")
    state = load_json(WECHAT_CHANNEL_STATE_PATH, {})
    if "errcode=48001" in output:
        state.update({
            "status": "api_unauthorized",
            "blocked_at": current.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "blocked_until": None,
            "last_error_code": 48001,
            "manual_action": "Use inbound callback replies, or obtain an Official Account type with customer-message API permission.",
        })
        write_json(WECHAT_CHANNEL_STATE_PATH, state)
    elif "errcode=45015" in output:
        state.update({
            "status": "response_window_closed",
            "blocked_at": current.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "blocked_until": (current + timedelta(hours=48)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "last_error_code": 45015,
            "manual_action": "Send a new message to the official account to reopen the customer-service reply window.",
        })
        write_json(WECHAT_CHANNEL_STATE_PATH, state)
    elif result.get("ok"):
        state.update({"status": "available", "last_success_at": utc_now_iso(), "last_error_code": None})
        write_json(WECHAT_CHANNEL_STATE_PATH, state)


def wechat_send_allowed(*, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    state = load_json(WECHAT_CHANNEL_STATE_PATH, {})
    if (
        isinstance(state, dict)
        and state.get("status") == "api_unauthorized"
        and state.get("last_error_code") == 48001
    ):
        return {"allowed": False, "reason": "api_unauthorized", "error_code": 48001}
    blocked_at = parse_iso(state.get("blocked_at")) if isinstance(state, dict) else None
    inbound_at = callback_received_at()
    if blocked_at and inbound_at and inbound_at > blocked_at:
        state.update({"status": "available", "reopened_at": inbound_at.isoformat().replace("+00:00", "Z")})
        write_json(WECHAT_CHANNEL_STATE_PATH, state)
        return {"allowed": True, "reason": "recent_inbound_message", "inbound_at": inbound_at.isoformat()}
    blocked_until = parse_iso(state.get("blocked_until")) if isinstance(state, dict) else None
    if blocked_until and current < blocked_until:
        return {"allowed": False, "reason": "response_window_closed", "blocked_until": blocked_until.isoformat()}
    return {"allowed": True, "reason": "not_blocked"}


def classify_dispatch_outcome(results: dict[str, dict[str, Any]], allow_send: bool, dry_run: bool) -> str:
    if dry_run or not allow_send:
        return "dry_run"
    successes = sum(1 for item in results.values() if item.get("ok"))
    if successes == len(results) and results:
        return "sent"
    if successes:
        return "sent_partial"
    return "failed"


def status() -> dict[str, Any]:
    ensure_dirs()
    load_runtime_env()
    policy = load_json(POLICY_PATH, {})
    gmail = run_cmd([sys.executable, str(APP_DIR / "scripts" / "gmail_real_sender.py"), "--status"], timeout=20)
    wechat = run_cmd([sys.executable, str(APP_DIR / "scripts" / "wechat_official_sender.py"), "--status"], timeout=20)
    whatsapp = run_cmd([sys.executable, str(APP_DIR / "scripts" / "whatsapp_cloud_sender.py"), "--status"], timeout=20)
    telegram = run_cmd([sys.executable, str(APP_DIR / "scripts" / "telegram_bot_sender.py"), "--status"], timeout=20)
    payload = {
        "version": "0.1",
        "checked_at": utc_now_iso(),
        "communication_enabled": bool(policy.get("communication_enabled")),
        "auto_external_send_allowed": bool(policy.get("auto_external_send_allowed")),
        "worker_direct_send_allowed": bool(policy.get("worker_direct_send_allowed")),
        "outbox_count": len(list(OUTBOX_DIR.glob("*.json"))),
        "sent_count": len(list(SENT_DIR.glob("*.json"))),
        "failed_count": len(list(FAILED_DIR.glob("*.json"))),
        "gmail_status": gmail,
        "wechat_status": wechat,
        "whatsapp_status": whatsapp,
        "telegram_status": telegram,
        "channel_activation": {
            channel: channel_enabled(policy, channel)
            for channel in ("gmail", "wechat", "whatsapp", "telegram")
        },
        "combined_channels": resolve_channels("both", policy),
        "wechat_send_availability": wechat_send_allowed(),
        "secret_values_logged": False
    }
    write_json(STATUS_PATH, payload)
    return payload


def dispatch_one(path: Path, allow_send: bool, dry_run: bool) -> dict[str, Any]:
    load_runtime_env()
    payload = load_json(path, {})
    policy = load_json(POLICY_PATH, {})
    if not isinstance(payload, dict):
        return {"ok": False, "detail": "invalid_json"}
    if payload.get("status") != "queued":
        return {"ok": False, "detail": "not_queued"}
    if blocked_content(str(payload.get("subject", "")) + "\n" + str(payload.get("body", "")), policy):
        payload["status"] = "refused"
        payload["dispatch"] = {"ok": False, "detail": "blocked_content"}
        write_json(FAILED_DIR / path.name, payload)
        path.unlink(missing_ok=True)
        return payload["dispatch"]

    channel = payload.get("channel")
    subject = str(payload.get("subject") or "[market-watchdog]")
    body = str(payload.get("body") or "")
    results: dict[str, Any] = {}

    channels = resolve_channels(str(channel), policy)
    if not channels:
        payload["dispatch"] = {
            "ran_at": utc_now_iso(),
            "allow_send": allow_send,
            "dry_run": dry_run,
            "deferred": True,
            "detail": "no_ready_channels",
            "results": {},
            "delivered_channels": [],
            "failed_channels": [],
        }
        write_json(path, payload)
        return payload["dispatch"]
    for ch in channels:
        if not channel_enabled(policy, ch):
            results[ch] = {"ok": False, "detail": "channel_disabled"}
            continue
        if ch == "gmail":
            cmd = [sys.executable, str(APP_DIR / "scripts" / "gmail_real_sender.py"), "--subject", subject, "--body", body]
            if dry_run or not allow_send:
                cmd.append("--dry-run")
            else:
                if os.environ.get("MARKET_WATCHDOG_GMAIL_ALLOW_SEND") != "1":
                    results[ch] = {"ok": False, "detail": "MARKET_WATCHDOG_GMAIL_ALLOW_SEND_not_1"}
                    continue
                cmd.append("--allow-send")
            results[ch] = run_cmd(cmd, timeout=90)
        elif ch == "wechat":
            availability = wechat_send_allowed()
            if allow_send and not dry_run and not availability["allowed"]:
                results[ch] = {"ok": False, "detail": availability["reason"], "suppressed": True}
                continue
            cmd = [sys.executable, str(APP_DIR / "scripts" / "wechat_official_sender.py"), "--message", body]
            if dry_run or not allow_send:
                cmd.append("--dry-run")
            else:
                if os.environ.get("MARKET_WATCHDOG_WECHAT_ALLOW_SEND") != "1":
                    results[ch] = {"ok": False, "detail": "MARKET_WATCHDOG_WECHAT_ALLOW_SEND_not_1"}
                    continue
                cmd.append("--allow-send")
            results[ch] = run_cmd(cmd, timeout=90)
            if allow_send and not dry_run:
                record_wechat_result(results[ch])
        elif ch == "whatsapp":
            cmd = [sys.executable, str(APP_DIR / "scripts" / "whatsapp_cloud_sender.py"), "--message", body]
            if dry_run or not allow_send:
                cmd.append("--dry-run")
            else:
                if os.environ.get("MARKET_WATCHDOG_WHATSAPP_ALLOW_SEND") != "1":
                    results[ch] = {"ok": False, "detail": "MARKET_WATCHDOG_WHATSAPP_ALLOW_SEND_not_1"}
                    continue
                cmd.append("--allow-send")
            results[ch] = run_cmd(cmd, timeout=90)
        elif ch == "telegram":
            cmd = [sys.executable, str(APP_DIR / "scripts" / "telegram_bot_sender.py"), "--message", body]
            if dry_run or not allow_send:
                cmd.append("--dry-run")
            else:
                if os.environ.get("MARKET_WATCHDOG_TELEGRAM_ALLOW_SEND") != "1":
                    results[ch] = {"ok": False, "detail": "MARKET_WATCHDOG_TELEGRAM_ALLOW_SEND_not_1"}
                    continue
                cmd.append("--allow-send")
            results[ch] = run_cmd(cmd, timeout=90)
        else:
            results[ch] = {"ok": False, "detail": "unknown_channel"}

    payload["status"] = classify_dispatch_outcome(results, allow_send, dry_run)
    payload["dispatch"] = {
        "ran_at": utc_now_iso(),
        "allow_send": allow_send,
        "dry_run": dry_run,
        "results": results,
        "delivered_channels": sorted(ch for ch, item in results.items() if item.get("ok")),
        "failed_channels": sorted(ch for ch, item in results.items() if not item.get("ok")),
    }
    dest = SENT_DIR if payload["status"] in {"sent", "sent_partial", "dry_run"} else FAILED_DIR
    write_json(dest / path.name, payload)
    path.unlink(missing_ok=True)
    return payload["dispatch"]


def dispatch(allow_send: bool, dry_run: bool, limit: int) -> dict[str, Any]:
    ensure_dirs()
    paths = sorted(OUTBOX_DIR.glob("*.json"))[:limit]
    results = [{"file": str(path), "result": dispatch_one(path, allow_send, dry_run)} for path in paths]
    payload = {"version": "0.1", "ran_at": utc_now_iso(), "allow_send": allow_send, "dry_run": dry_run, "count": len(results), "results": results}
    write_json(STATUS_PATH, {**status(), "last_dispatch": payload})
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--allow-send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--channel", choices=["gmail", "wechat", "whatsapp", "telegram", "both"], default="both")
    parser.add_argument("--kind", default="system_health")
    parser.add_argument("--priority", choices=["low", "normal", "high", "urgent"], default="normal")
    parser.add_argument("--subject", default="[market-watchdog] notification")
    parser.add_argument("--body", default="")
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.enqueue:
        result = enqueue(args.channel, args.kind, args.subject, args.body or args.subject, args.priority, args.source_run_id)
        print(f"COMMUNICATION_ENQUEUE status={result['status']} channel={args.channel} path={result['path']} blocked={len(result.get('blocked_content_hits', []))}")
        return 0 if result["status"] == "queued" else 2
    if args.dispatch:
        result = dispatch(args.allow_send, args.dry_run or not args.allow_send, args.limit)
        print(f"COMMUNICATION_DISPATCH count={result['count']} allow_send={str(args.allow_send).lower()} dry_run={str(result['dry_run']).lower()} status={STATUS_PATH}")
        return 0
    payload = status()
    print(f"COMMUNICATION_STATUS enabled={str(payload['communication_enabled']).lower()} outbox={payload['outbox_count']} sent={payload['sent_count']} failed={payload['failed_count']} status={STATUS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
