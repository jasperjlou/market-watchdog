#!/usr/bin/env python3
"""Startup authorization and external-call checklist for market-watchdog."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("APP_DIR", str(PROJECT_DIR)))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
STATUS_PATH = STATE_DIR / "authorization_status.json"
RUNTIME_ENV_PATH = APP_DIR / "secrets" / "runtime_env.sh"
SYSTEMD_RUNTIME_ENV_PATH = Path("/etc/market-watchdog/runtime.env")
SYSTEMD_CHANNEL_ENV_PATHS = (
    Path("/etc/market-watchdog/google.env"),
    Path("/etc/market-watchdog/meta.env"),
    Path("/etc/market-watchdog/wechat.env"),
    Path("/etc/market-watchdog/telegram.env"),
)
GMAIL_PROFILE_PATH = STATE_DIR / "external_authorizations" / "gmail_connector_profile.json"
AI_ORCHESTRATION_PATH = AGENT_DIR / "config" / "ai_orchestration.json"
RUNTIME_ISOLATION_PATH = AGENT_DIR / "config" / "runtime_isolation.json"
MARKET_DATA_ROUTER_PATH = AGENT_DIR / "scripts" / "market_data_router.py"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_runtime_env() -> dict[str, bool]:
    presence: dict[str, bool] = {}
    for path in (RUNTIME_ENV_PATH, SYSTEMD_RUNTIME_ENV_PATH, *SYSTEMD_CHANNEL_ENV_PATHS):
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
                if key and key not in os.environ:
                    os.environ[key] = value
                presence[key] = bool(value)
    return presence


def runtime_env_present() -> bool:
    return RUNTIME_ENV_PATH.exists() or SYSTEMD_RUNTIME_ENV_PATH.exists() or any(path.exists() for path in SYSTEMD_CHANNEL_ENV_PATHS)


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket()
    sock.settimeout(2)
    try:
        sock.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        sock.close()


def run_status(cmd: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(APP_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode == 0, proc.stdout[-1200:]
    except Exception as exc:
        return False, exc.__class__.__name__


def proc_contains(needle: str) -> bool:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "args"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0 and needle in proc.stdout
    except Exception:
        return False


def ibkr_check() -> dict[str, Any]:
    port_ok = port_open(int(os.environ.get("IBKR_READONLY_PORT", "4001")))
    readonly_ok = False
    detail = ""
    if port_ok:
        code = (
            "from ib_insync import IB\n"
            "ib=IB()\n"
            "try:\n"
            "    ib.connect('127.0.0.1', 4001, clientId=971, timeout=6, readonly=True)\n"
            "    ib.reqCurrentTime(); print('ok', len(ib.managedAccounts()))\n"
            "finally:\n"
            "    ib.disconnect() if ib.isConnected() else None\n"
        )
        readonly_ok, detail = run_status([sys.executable, "-c", code], timeout=12)
    return {
        "id": "ibkr_gateway",
        "label": "IBKR Gateway API",
        "status": "ready" if port_ok and readonly_ok else "needs_manual_action",
        "ready": bool(port_ok and readonly_ok),
        "external_call": true_false(True),
        "manual_action": "" if port_ok and readonly_ok else "Open noVNC and complete IBKR login / 2FA, then keep API port 4001 connected.",
        "checks": {"port_4001": port_ok, "readonly_handshake": readonly_ok},
        "detail": detail[:240],
    }


def true_false(value: bool) -> bool:
    return bool(value)


def gmail_check(runtime_presence: dict[str, bool]) -> dict[str, Any]:
    token_path = Path(os.environ.get("GMAIL_TOKEN", "/app/secrets/gmail_token.json"))
    if str(token_path) and not token_path.is_absolute():
        token_path = Path("/app/secrets/gmail_token.json")
    credentials_path = Path("/app/secrets/gmail_credentials.json")
    profile = load_json(GMAIL_PROFILE_PATH, {})
    token_valid = False
    detail = ""
    if token_path.exists():
        code = (
            "import sys\n"
            "from google.oauth2.credentials import Credentials\n"
            "from google.auth.transport.requests import Request\n"
            "p=sys.argv[1]\n"
            "scopes=['https://www.googleapis.com/auth/gmail.compose']\n"
            "c=Credentials.from_authorized_user_file(p, scopes)\n"
            "if c.expired and c.refresh_token:\n"
            "    c.refresh(Request())\n"
            "print('valid', bool(c.valid))\n"
        )
        token_valid, detail = run_status([sys.executable, "-c", code, str(token_path)], timeout=20)
    gate_send = os.environ.get("ALLOW_GMAIL_SEND") == "1" and os.environ.get("MARKET_WATCHDOG_GMAIL_ALLOW_SEND") == "1"
    recipient_ready = bool(os.environ.get("GMAIL_DEFAULT_TO")) or runtime_presence.get("GMAIL_DEFAULT_TO", False)
    ready = token_path.exists() and token_valid and recipient_ready and gate_send
    action = ""
    if not token_path.exists() or not token_valid:
        action = "Refresh Gmail OAuth token for container sender."
    elif not recipient_ready:
        action = "Set GMAIL_DEFAULT_TO for the project Gmail sender."
    elif not gate_send:
        action = "Gmail is ready for drafts/status. Set ALLOW_GMAIL_SEND=1 and MARKET_WATCHDOG_GMAIL_ALLOW_SEND=1 only when real sending is intended."
    return {
        "id": "gmail",
        "label": "Gmail connector and sender",
        "status": "ready_gated" if ready else "needs_manual_action",
        "ready": bool(ready),
        "external_call": True,
        "manual_action": action,
        "checks": {
            "codex_connector_connected": profile.get("status") == "connected",
            "profile_email_present": bool(profile.get("email")),
            "credentials_file_present": credentials_path.exists(),
            "token_file_present": token_path.exists(),
            "token_valid_or_refreshable": token_valid,
            "real_send_gate_enabled": gate_send,
            "runtime_env_present": runtime_env_present(),
            "runtime_keys_present": {
                "ALLOW_GMAIL_SEND": bool(os.environ.get("ALLOW_GMAIL_SEND")) or runtime_presence.get("ALLOW_GMAIL_SEND", False),
                "MARKET_WATCHDOG_GMAIL_ALLOW_SEND": bool(os.environ.get("MARKET_WATCHDOG_GMAIL_ALLOW_SEND")) or runtime_presence.get("MARKET_WATCHDOG_GMAIL_ALLOW_SEND", False),
                "GMAIL_DEFAULT_TO": recipient_ready,
            }
        },
        "detail": detail[:240],
    }


def wechat_check(runtime_presence: dict[str, bool]) -> dict[str, Any]:
    callback_port_ok = port_open(8788)
    callback_proc_ok = proc_contains("wechat_official_callback_receiver.py")
    sender_state = load_json(APP_DIR / "state" / "wechat_official_sender_state.json", {})
    callback_state = load_json(APP_DIR / "state" / "wechat_official_callback_state.json", {})
    channel_state = load_json(APP_DIR / "agent" / "state" / "wechat_channel_state.json", {})
    env_keys = ["WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_OPENID", "ALLOW_WECHAT_OFFICIAL_SEND", "MARKET_WATCHDOG_WECHAT_ALLOW_SEND", "WECHAT_CALLBACK_TOKEN"]
    env_present = {key: bool(os.environ.get(key)) or runtime_presence.get(key, False) for key in env_keys}
    send_gate = os.environ.get("ALLOW_WECHAT_OFFICIAL_SEND") == "1" and os.environ.get("MARKET_WATCHDOG_WECHAT_ALLOW_SEND") == "1"
    sender_ready = all(env_present[k] for k in ["WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_OPENID"]) and send_gate
    api_unauthorized = channel_state.get("last_error_code") == 48001 and channel_state.get("status") == "api_unauthorized"
    known_issue = ""
    if api_unauthorized:
        known_issue = "WeChat inbound callback is ready, but this account lacks proactive customer-message API permission. Proactive sends are disabled without retry."
    elif "45047" in str(sender_state.get("last_detail", "")):
        known_issue = "WeChat official sending previously hit response-count/window limit. Message the official account or wait for the permitted response window before expecting proactive send success."
    manual_action = ""
    if not callback_port_ok or not callback_proc_ok:
        manual_action = "Start WeChat callback receiver and verify public callback routing."
    elif not env_present.get("WECHAT_CALLBACK_TOKEN"):
        manual_action = "Set/confirm WeChat callback token in runtime env or token path."
    elif not sender_ready:
        manual_action = "WeChat callback is ready. For real official send, confirm app_id/app_secret/openid and enable both send gates."
    elif known_issue and not api_unauthorized:
        manual_action = known_issue
    status = "needs_manual_action"
    if callback_port_ok and callback_proc_ok:
        status = "ready_inbound_only" if api_unauthorized else "ready_gated"
    return {
        "id": "wechat",
        "label": "WeChat callback and official sender",
        "status": status,
        "ready": bool(callback_port_ok and callback_proc_ok),
        "external_call": True,
        "manual_action": manual_action,
        "checks": {
            "callback_process": callback_proc_ok,
            "callback_port_8788": callback_port_ok,
            "callback_token_present": env_present.get("WECHAT_CALLBACK_TOKEN", False),
            "official_sender_env_present": {
                "WECHAT_APP_ID": env_present.get("WECHAT_APP_ID", False),
                "WECHAT_APP_SECRET": env_present.get("WECHAT_APP_SECRET", False),
                "WECHAT_OPENID": env_present.get("WECHAT_OPENID", False)
            },
            "real_send_gate_enabled": send_gate,
            "official_send_authorized": not api_unauthorized,
            "last_sent": sender_state.get("last_sent") is True,
            "last_status": sender_state.get("last_status", ""),
            "callback_last_type": callback_state.get("last_type", "")
        },
        "detail": known_issue,
    }


def whatsapp_check(runtime_presence: dict[str, bool]) -> dict[str, Any]:
    sender_state = load_json(APP_DIR / "state" / "whatsapp_cloud_sender_state.json", {})
    credential_keys = [
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_RECIPIENT",
        "WHATSAPP_TEMPLATE_NAME",
    ]
    gate_keys = ["ALLOW_WHATSAPP_SEND", "MARKET_WATCHDOG_WHATSAPP_ALLOW_SEND"]
    env_present = {
        key: bool(os.environ.get(key)) or runtime_presence.get(key, False)
        for key in [*credential_keys, *gate_keys]
    }
    credentials_ready = all(env_present[key] for key in credential_keys)
    gates_ready = all(os.environ.get(key) == "1" for key in gate_keys)
    ready = bool(credentials_ready and gates_ready)
    if not credentials_ready:
        action = "Create/connect a Meta WhatsApp Business Cloud API sender, then set the token, phone-number ID, recipient and approved Chinese template."
    elif not gates_ready:
        action = "WhatsApp credentials are present. Enable both WhatsApp send gates only after the approved template is confirmed."
    else:
        action = ""
    return {
        "id": "whatsapp",
        "label": "WhatsApp Business Cloud API sender",
        "status": "ready_gated" if ready else "needs_manual_action",
        "ready": ready,
        "external_call": True,
        "manual_action": action,
        "checks": {
            "credential_fields_present": {key: env_present[key] for key in credential_keys},
            "approved_template_name_present": env_present["WHATSAPP_TEMPLATE_NAME"],
            "real_send_gates_enabled": gates_ready,
            "official_cloud_api_only": True,
            "last_sent": sender_state.get("last_sent") is True,
            "last_status": sender_state.get("last_status", ""),
        },
        "detail": "Outbound template notifications only; no WhatsApp Web or personal-account automation.",
    }


def telegram_check(runtime_presence: dict[str, bool]) -> dict[str, Any]:
    sender_state = load_json(APP_DIR / "state" / "telegram_bot_sender_state.json", {})
    credential_keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    gate_keys = ["ALLOW_TELEGRAM_SEND", "MARKET_WATCHDOG_TELEGRAM_ALLOW_SEND"]
    env_present = {
        key: bool(os.environ.get(key)) or runtime_presence.get(key, False)
        for key in [*credential_keys, *gate_keys]
    }
    credentials_ready = all(env_present[key] for key in credential_keys)
    gates_ready = all(os.environ.get(key) == "1" for key in gate_keys)
    ready = bool(credentials_ready and gates_ready)
    if not credentials_ready:
        action = "Create a Telegram bot with BotFather, start the bot from the recipient chat, then set its bot token and chat ID."
    elif not gates_ready:
        action = "Telegram credentials are present. Enable both Telegram send gates to permit real notifications."
    else:
        action = ""
    return {
        "id": "telegram",
        "label": "Telegram Bot API sender",
        "status": "ready_gated" if ready else "needs_manual_action",
        "ready": ready,
        "external_call": True,
        "manual_action": action,
        "checks": {
            "credential_fields_present": {key: env_present[key] for key in credential_keys},
            "real_send_gates_enabled": gates_ready,
            "official_bot_api_only": True,
            "last_sent": sender_state.get("last_sent") is True,
            "last_status": sender_state.get("last_status", ""),
        },
        "detail": "Official Bot API only; no Telegram client/session automation.",
    }


def cli_check(
    command: str,
    token_paths: list[str],
    *,
    probe_args: list[str] | None = None,
    reject_phrases: list[str] | None = None,
) -> dict[str, Any]:
    binary = command.split()[0]
    available = bool(shutil_which(binary))
    token_present = any(Path(path).exists() for path in token_paths)
    probe_ok = bool(available and token_present)
    if probe_ok and probe_args:
        command_ok, output = run_status(probe_args, timeout=20)
        lowered = output.lower()
        rejected = any(phrase.lower() in lowered for phrase in (reject_phrases or []))
        probe_ok = bool(command_ok and not rejected)
    ready = bool(available and token_present and probe_ok)
    return {
        "available": available,
        "token_present": token_present,
        "probe_ok": probe_ok,
        "ready": ready,
        "manual_action": "" if ready else f"Run login/auth for {binary} if worker execution is needed."
    }


def shutil_which(binary: str) -> str | None:
    from shutil import which

    return which(binary)


def worker_checks() -> list[dict[str, Any]]:
    orchestration = load_json(AI_ORCHESTRATION_PATH, {})
    agent_profiles = orchestration.get("agents", {}) if isinstance(orchestration, dict) else {}
    antigravity_enabled = agent_profiles.get("google_antigravity", {}).get("enabled", True)
    grok_enabled = agent_profiles.get("grok", {}).get("enabled", True)
    disabled_cli = {
        "available": False,
        "token_present": False,
        "probe_ok": False,
        "ready": False,
        "manual_action": "",
    }
    antigravity_token = Path(os.environ.get("HOME", "/var/lib/market-watchdog/antigravity/home")) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    agy = cli_check(
        "agy",
        [str(antigravity_token)],
        probe_args=["agy", "models"],
        reject_phrases=["not logged", "not authenticated"],
    ) if antigravity_enabled else dict(disabled_cli)
    grok = cli_check(
        "grok",
        ["/root/.grok/auth.json", "/root/.config/grok/config.json"],
        probe_args=["grok", "models"],
        reject_phrases=["not logged", "not authenticated"],
    ) if grok_enabled else dict(disabled_cli)
    google_receipt = load_json(STATE_DIR / "provider_receipts" / "google_gemini.json", {})
    google_cli_receipt_ready = bool(
        google_receipt.get("status") == "completed"
        and google_receipt.get("provider") == "google_antigravity_cli"
        and google_receipt.get("subscription_baseline_only") is True
        and google_receipt.get("ai_credits_enabled") is False
    )
    google_ready = bool(agy["ready"] and google_cli_receipt_ready)
    return [
        {
            "id": "antigravity",
            "label": "Google Antigravity worker",
            "status": "disabled_by_config" if not antigravity_enabled else ("ready_gated" if google_ready else "needs_manual_action"),
            "ready": True if not antigravity_enabled else google_ready,
            "external_call": True,
            "manual_action": "" if not antigravity_enabled else (
                "" if google_ready else "Authenticate Antigravity CLI, keep AI Credits off, run one harmless CLI provider test, and retain its completed receipt."
            ),
            "checks": {
                "configured_enabled": antigravity_enabled,
                "cli_available": agy["available"],
                "cli_token_present": agy["token_present"],
                "cli_auth_probe": agy["probe_ok"],
                "cli_receipt_completed": google_cli_receipt_ready,
                "subscription_baseline_only": google_receipt.get("subscription_baseline_only") is True,
                "ai_credits_enabled": google_receipt.get("ai_credits_enabled") is True
            }
        },
        {
            "id": "grok",
            "label": "Grok worker",
            "status": "disabled_by_config" if not grok_enabled else ("ready_gated" if grok["ready"] else "needs_manual_action"),
            "ready": True if not grok_enabled else grok["ready"],
            "external_call": True,
            "manual_action": "" if not grok_enabled else grok["manual_action"],
            "checks": {"configured_enabled": grok_enabled, "cli_available": grok["available"], "token_present": grok["token_present"], "auth_probe": grok["probe_ok"]}
        }
    ]


def optional_sidecars() -> list[dict[str, Any]]:
    registry = load_json(AGENT_DIR / "integrations" / "registry.json", {"integrations": []})
    items = []
    for item in registry.get("integrations", []):
        if not isinstance(item, dict):
            continue
        items.append({
            "id": item.get("id"),
            "label": item.get("name"),
            "status": item.get("status"),
            "ready": item.get("status") in {"registered_reference_only", "registered_optional_sidecar"},
            "external_call": False,
            "manual_action": "",
            "optional_action": "Optional sidecar. Start only when you intentionally need this service.",
            "checks": {"registered": True, "execution_mode": item.get("execution_mode")}
        })
    return items


def startup_readiness(checks: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    by_id = {str(item.get("id")): item for item in checks}
    required = [str(item) for item in policy.get("startup_required_checks", ["gmail", "grok"])]
    optional = [str(item) for item in policy.get("startup_optional_checks", ["ibkr_gateway", "antigravity", "whatsapp", "wechat", "telegram"])]
    required_failed = [item_id for item_id in required if not bool(by_id.get(item_id, {}).get("ready"))]
    optional_failed = [item_id for item_id in optional if item_id in by_id and not bool(by_id[item_id].get("ready"))]
    return {
        "ready": not required_failed,
        "required_checks": required,
        "optional_checks": optional,
        "required_failed": required_failed,
        "optional_failed": optional_failed,
    }


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def quant_toolchain_check() -> dict[str, Any]:
    modules = {
        "yfinance": module_available("yfinance"),
        "openbb": module_available("openbb"),
        "financetoolkit": module_available("financetoolkit"),
        "vectorbt": module_available("vectorbt"),
    }
    yfinance_ready = modules["yfinance"]
    optional_missing = [name for name in ("openbb", "financetoolkit", "vectorbt") if not modules[name]]
    manual_action = ""
    if not yfinance_ready:
        manual_action = "Install yfinance in the core container before relying on K-line/news reaction checks."
    return {
        "id": "quant_toolchain",
        "label": "Quant market-data and validation modules",
        "status": "ready_optional_missing" if yfinance_ready and optional_missing else ("ready" if yfinance_ready else "needs_manual_action"),
        "ready": bool(yfinance_ready),
        "external_call": False,
        "manual_action": manual_action,
        "optional_action": "" if not optional_missing else "Optional quant sidecars missing: " + ", ".join(optional_missing) + ". Install only when a deep-check/backtest job needs them.",
        "checks": {
            "core_yfinance_present": modules["yfinance"],
            "optional_openbb_present": modules["openbb"],
            "optional_financetoolkit_present": modules["financetoolkit"],
            "optional_vectorbt_present": modules["vectorbt"],
            "policy_config_present": (AGENT_DIR / "config" / "quant_toolchain.json").exists()
        }
    }


def market_data_check() -> dict[str, Any]:
    ok, output = run_status([sys.executable, str(MARKET_DATA_ROUTER_PATH), "--status", "--json"], timeout=15)
    try:
        payload = json.loads(output) if ok else {}
    except (TypeError, ValueError):
        payload = {}
    providers = payload.get("providers", {}) if isinstance(payload, dict) else {}
    moomoo = providers.get("moomoo", {}) if isinstance(providers, dict) else {}
    ibkr = providers.get("ibkr", {}) if isinstance(providers, dict) else {}
    yfinance = providers.get("yfinance", {}) if isinstance(providers, dict) else {}
    fallback_ready = bool(yfinance.get("module_present"))
    preferred_ready = bool(moomoo.get("module_present") and moomoo.get("port_open"))
    ibkr_ready = bool(ibkr.get("module_present") and ibkr.get("port_open"))
    ready = bool(ok and fallback_ready)
    optional = []
    if not preferred_ready:
        optional.append("Moomoo client is prepared; start/login OpenD on the private Docker network when account access is available.")
    if not ibkr_ready:
        optional.append("IBKR quote fallback is offline until Gateway login/2FA and port 4001 are available.")
    return {
        "id": "market_data_router",
        "label": "Read-only market-data provider chain",
        "status": "ready" if ready and preferred_ready else ("ready_with_fallback" if ready else "needs_manual_action"),
        "ready": ready,
        "external_call": False,
        "manual_action": "" if ready else "Restore the yfinance fallback before enabling scheduled market scans.",
        "optional_action": " ".join(optional),
        "checks": {
            "router_status_ok": ok,
            "moomoo_module_present": bool(moomoo.get("module_present")),
            "moomoo_opend_port_open": bool(moomoo.get("port_open")),
            "ibkr_module_present": bool(ibkr.get("module_present")),
            "ibkr_gateway_port_open": bool(ibkr.get("port_open")),
            "yfinance_fallback_present": fallback_ready,
            "readonly": payload.get("readonly") is True,
        },
    }
def runner_check() -> dict[str, Any]:
    status = load_json(STATE_DIR / "system_status.json", {})
    intervals = status.get("intervals") if isinstance(status, dict) else {}
    portfolio_loaded = isinstance(intervals, dict) and "portfolio_sec" in intervals
    hit_loaded = isinstance(intervals, dict) and "hit_kline_sec" in intervals
    wechat_ai_loaded = isinstance(intervals, dict) and "wechat_ai_sec" in intervals
    core_scan_loaded = isinstance(intervals, dict) and "core_scan_sec" in intervals
    theme_scan_loaded = isinstance(intervals, dict) and "theme_scan_sec" in intervals
    context_scan_loaded = isinstance(intervals, dict) and "context_scan_sec" in intervals
    pid_present = bool(status.get("pid")) if isinstance(status, dict) else False
    ready = bool(pid_present and portfolio_loaded and hit_loaded and wechat_ai_loaded and core_scan_loaded and theme_scan_loaded and context_scan_loaded)
    return {
        "id": "system_runner_news_reaction_loops",
        "label": "System runner portfolio and hit-symbol loops",
        "status": "ready" if ready else "needs_manual_action",
        "ready": ready,
        "external_call": False,
        "manual_action": "" if ready else "Restart system_runner with layered core/theme/context scans plus the portfolio, hit-symbol, and WeChat AI loops.",
        "checks": {
            "runner_pid_present": pid_present,
            "portfolio_interval_loaded": portfolio_loaded,
            "hit_kline_interval_loaded": hit_loaded,
            "wechat_ai_interval_loaded": wechat_ai_loaded,
            "core_scan_interval_loaded": core_scan_loaded,
            "theme_scan_interval_loaded": theme_scan_loaded,
            "context_scan_interval_loaded": context_scan_loaded,
        }
    }


def build_status() -> dict[str, Any]:
    runtime_presence = load_runtime_env()
    checks = [ibkr_check(), gmail_check(runtime_presence), telegram_check(runtime_presence), whatsapp_check(runtime_presence), wechat_check(runtime_presence), *worker_checks(), quant_toolchain_check(), market_data_check(), runner_check(), *optional_sidecars()]
    isolation_policy = load_json(RUNTIME_ISOLATION_PATH, {})
    readiness = startup_readiness(checks, isolation_policy)
    optional_ids = set(readiness["optional_checks"])
    manual_actions = []
    optional_actions = []
    for item in checks:
        action = item.get("manual_action")
        if action:
            target = optional_actions if item.get("id") in optional_ids else manual_actions
            target.append({"id": item.get("id"), "action": action, "status": item.get("status")})
        optional_action = item.get("optional_action")
        if optional_action:
            optional_actions.append({"id": item.get("id"), "action": optional_action, "status": item.get("status")})
    payload = {
        "version": "0.2",
        "checked_at": utc_now_iso(),
        "runtime_env_present": runtime_env_present(),
        "startup_ready": readiness["ready"],
        "startup_required_checks": readiness["required_checks"],
        "startup_optional_checks": readiness["optional_checks"],
        "startup_required_failed": readiness["required_failed"],
        "startup_optional_failed": readiness["optional_failed"],
        "real_send_gated": True,
        "secret_values_logged": False,
        "checks": checks,
        "manual_actions": manual_actions,
        "optional_actions": optional_actions,
    }
    write_json(STATUS_PATH, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = build_status()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"AUTHORIZATION_CHECK startup_ready={str(payload['startup_ready']).lower()} manual_actions={len(payload['manual_actions'])} status={STATUS_PATH}")
        for item in payload["manual_actions"]:
            print(f"- {item['id']}: {item['action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
