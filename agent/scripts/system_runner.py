#!/usr/bin/env python3
"""Run the market-watchdog agent system loop with startup authorization checks.

The runner keeps health state fresh, lists manual authorization actions, refreshes
read-only K-line snapshots, runs the Codex fusion gate, checks communication
channels, and processes explicit AI trigger files. It does not place orders,
send messages, or read secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
LOGS_DIR = AGENT_DIR / "logs"
RUNS_DIR = AGENT_DIR / "runs"
TRIGGERS_DIR = STATE_DIR / "ai_bus" / "triggers"
TRIGGER_ARCHIVE_DIR = STATE_DIR / "ai_bus" / "processed_triggers"
STATUS_PATH = STATE_DIR / "system_status.json"
PID_PATH = STATE_DIR / "system_runner.pid"
HEALTH_TRANSITION_PATH = STATE_DIR / "health_transition_state.json"
WATCHLIST_PATH = APP_DIR / "config" / "watchlist.yaml"
SCAN_POLICY_PATH = APP_DIR / "config" / "scan_policy.yaml"
PORTFOLIO_SNAPSHOT_PATH = STATE_DIR / "moomoo_portfolio_snapshot_latest.json"
HIT_SYMBOLS_PATH = STATE_DIR / "hit_symbols.json"
COMMUNICATION_POLICY_PATH = AGENT_DIR / "config" / "communication_policy.json"
COMMUNICATION_STATUS_PATH = STATE_DIR / "communication_status.json"
RUNTIME_ISOLATION_PATH = AGENT_DIR / "config" / "runtime_isolation.json"
OUTLOOK_POLICY_PATH = APP_DIR / "config" / "trend_outlook_policy.yaml"
OUTLOOK_PATH = STATE_DIR / "trend_outlook_latest.json"
DAILY_BRIEF_STATE_PATH = STATE_DIR / "daily_market_brief_state.json"
WEEKLY_BRIEF_STATE_PATH = STATE_DIR / "weekly_market_brief_state.json"

NEWS_RESEARCH_ROTATION = [
    {
        "theme": "semiconductors_memory",
        "symbols": ["SKHY", "MU", "WDC", "STX", "P", "NVDA", "TSM", "AMAT", "SMH"],
        "focus": "HBM, DRAM/NAND pricing, AI memory demand, capacity, earnings, export controls and Korea FX",
        "sources": "issuer IR, SEC EDGAR, BIS/Federal Register, SEMI/SIA and high-trust financial news",
    },
    {
        "theme": "space_aerospace",
        "symbols": ["RKLB", "ASTS", "LUNR", "PL", "RDW", "XAR", "ITA", "BA", "LMT", "NOC", "RTX"],
        "focus": "launches, failures, NASA/FCC decisions, government awards, backlog and funding",
        "sources": "NASA, FCC, SAM.gov, Defense contracts, SEC EDGAR, issuer IR and high-trust news",
    },
    {
        "theme": "gold_macro",
        "symbols": ["GLD", "IAU", "GDX", "GDXJ", "NEM", "AEM", "GC=F", "TLT", "^TNX", "^VIX"],
        "focus": "real yields, USD, inflation, central-bank demand, geopolitics and miner guidance",
        "sources": "Federal Reserve, Treasury, BLS, CME, World Gold Council, issuer IR and high-trust news",
    },
    {
        "theme": "broad_market",
        "symbols": ["SPY", "QQQ", "IWM", "RSP", "HYG", "TLT", "^VIX", "AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA"],
        "focus": "market breadth, rates, credit, volatility, sector rotation and mega-cap catalysts",
        "sources": "Federal Reserve, Treasury, BLS/BEA, SEC EDGAR, Cboe, Nasdaq and high-trust news",
    },
]

PORTS = [
    ("moomoo_opend", "127.0.0.1", 11111),
    ("novnc", "127.0.0.1", 6082),
    ("wechat_callback", "127.0.0.1", 8788),
]

STOP_REQUESTED = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def as_eastern(value: datetime) -> datetime:
    """Convert an aware datetime to US Eastern without requiring tzdata."""
    utc_value = value.astimezone(timezone.utc)
    year = utc_value.year
    march_first = datetime(year, 3, 1, tzinfo=timezone.utc)
    second_sunday = 1 + ((6 - march_first.weekday()) % 7) + 7
    november_first = datetime(year, 11, 1, tzinfo=timezone.utc)
    first_sunday = 1 + ((6 - november_first.weekday()) % 7)
    dst_start = datetime(year, 3, second_sunday, 7, tzinfo=timezone.utc)
    dst_end = datetime(year, 11, first_sunday, 6, tzinfo=timezone.utc)
    hours = -4 if dst_start <= utc_value < dst_end else -5
    return utc_value.astimezone(timezone(timedelta(hours=hours), name="EDT" if hours == -4 else "EST"))


def ensure_dirs() -> None:
    for path in (STATE_DIR, LOGS_DIR, RUNS_DIR, TRIGGERS_DIR, TRIGGER_ARCHIVE_DIR):
        path.mkdir(parents=True, exist_ok=True)


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


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def handle_stop(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(f"SYSTEM_RUNNER stop_signal={signum}", flush=True)


def pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_pid(pid_path: Path) -> None:
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except Exception:
            old_pid = 0
        if old_pid and pid_running(old_pid):
            raise SystemExit(f"system_runner already running pid={old_pid}")
    pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def release_pid(pid_path: Path) -> None:
    try:
        if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_path.unlink()
    except Exception:
        pass


def check_ports() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, host, port in PORTS:
        sock = socket.socket()
        sock.settimeout(2)
        try:
            sock.connect((host, port))
            result[name] = {"ok": True, "port": port}
        except Exception as exc:
            result[name] = {"ok": False, "port": port, "error": exc.__class__.__name__}
        finally:
            sock.close()
    return result


def check_processes() -> dict[str, bool]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "comm,args"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        text = proc.stdout
    except Exception:
        text = ""
    return {
        "ib_gateway_java": "IB Gateway" in text or "ibgateway" in text,
        "wechat_callback": "wechat_official_callback_receiver.py" in text,
        "xvfb": "Xvfb :92" in text,
        "x11vnc": "x11vnc" in text,
        "websockify": "websockify" in text,
    }


def compute_stability(
    ports: dict[str, dict[str, Any]],
    processes: dict[str, bool],
    policy: dict[str, Any],
) -> dict[str, Any]:
    required_ports = {str(item) for item in policy.get("required_ports", ["wechat_callback"])}
    required_processes = {str(item) for item in policy.get("required_processes", ["wechat_callback"])}
    required_failures = [
        *[f"port:{name}" for name in sorted(required_ports) if not bool(ports.get(name, {}).get("ok"))],
        *[f"process:{name}" for name in sorted(required_processes) if not bool(processes.get(name))],
    ]
    optional_failures = [
        *[f"port:{name}" for name, item in ports.items() if name not in required_ports and not bool(item.get("ok"))],
        *[f"process:{name}" for name, ok in processes.items() if name not in required_processes and not bool(ok)],
    ]
    return {
        "stable": not required_failures,
        "required_failures": required_failures,
        "optional_failures": sorted(optional_failures),
    }


def health_snapshot(current: dict[str, Any]) -> dict[str, Any]:
    if current.get("startup_ready") is None:
        return {"health": "unknown", "signature": "unknown", "reasons": []}
    health = "healthy" if bool(current.get("stable")) and bool(current.get("startup_ready")) else "degraded"
    # Authorization reminders are operational metadata, not core outages.  Treating
    # them as health failures created a self-reinforcing stream of user alerts when
    # Gmail or WeChat authorization briefly changed state.
    reasons = sorted({str(item) for item in current.get("required_service_failures", [])})
    signature = json.dumps({"health": health, "reasons": reasons}, ensure_ascii=True, sort_keys=True)
    return {"health": health, "signature": signature, "reasons": reasons}


def health_transition(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = health_snapshot(current)
    if snapshot["health"] == "unknown":
        return None
    previous_health = str(previous.get("health") or "")
    if not previous_health:
        return None if snapshot["health"] == "healthy" else {"event": "degraded", **snapshot}
    if previous.get("signature") == snapshot["signature"]:
        return None
    return {
        "event": "recovered" if snapshot["health"] == "healthy" else "degraded",
        **snapshot,
    }


def queue_health_transition(payload: dict[str, Any]) -> dict[str, Any] | None:
    previous = load_json(HEALTH_TRANSITION_PATH, {})
    previous = previous if isinstance(previous, dict) else {}
    snapshot = health_snapshot(payload)
    if snapshot["health"] == "unknown":
        return None
    transition = health_transition(previous, payload)
    state = {
        **snapshot,
        "updated_at": utc_now_iso(),
        "last_event": transition.get("event") if transition else previous.get("last_event"),
    }
    if transition:
        state["last_event_at"] = utc_now_iso()
        if os.environ.get("MARKET_WATCHDOG_SYSTEM_HEALTH_SEND") == "1":
            subject = "【系统状态】市场监控恢复" if transition["event"] == "recovered" else "【系统状态】市场监控异常"
            reason_text = "、".join(transition["reasons"][:4]) or "核心服务已恢复"
            command = [
                sys.executable,
                str(AGENT_DIR / "scripts" / "communication_gateway.py"),
                "--enqueue",
                "--channel", "gmail",
                "--kind", "system_health",
                "--priority", "normal" if transition["event"] == "recovered" else "high",
                "--subject", subject,
                "--body", f"核心监控状态：{reason_text}。",
            ]
            queued = run_command("health_transition_enqueue", command, timeout=30)
            state["last_enqueue_ok"] = bool(queued.get("ok"))
            transition["enqueue"] = queued
            transition["external_notification"] = "explicitly_enabled"
        else:
            state["last_enqueue_ok"] = None
            transition["external_notification"] = "suppressed_internal_only"
    write_json(HEALTH_TRANSITION_PATH, state)
    return transition


def watchlist_symbols(max_symbols: int) -> list[str]:
    try:
        import yaml  # type: ignore
    except Exception:
        return []
    if not WATCHLIST_PATH.exists():
        return []
    data = yaml.safe_load(WATCHLIST_PATH.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else []
    symbols: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("enabled") is not True:
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:max_symbols]


def scan_tier_symbols(tier: str, max_symbols: int = 80) -> list[str]:
    policy = load_yaml(SCAN_POLICY_PATH)
    tier_config = policy.get("tiers", {}).get(tier, {}) if isinstance(policy, dict) else {}
    values = tier_config.get("symbols", []) if isinstance(tier_config, dict) else []
    symbols: list[str] = []
    for value in values if isinstance(values, list) else []:
        symbol = str(value).strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:max_symbols]


def coordinator_symbols(max_symbols: int, *, slot: int | None = None) -> list[str]:
    policy = load_yaml(SCAN_POLICY_PATH)
    config = policy.get("coordinator", {}) if isinstance(policy, dict) else {}
    core_limit = max(1, int(config.get("core_symbols_each_run", 12)))
    rotating_limit = max(0, int(config.get("rotating_theme_symbols_each_run", 12)))
    total_limit = min(max_symbols, max(1, int(config.get("maximum_symbols_per_run", max_symbols))))
    core = scan_tier_symbols("core", core_limit)
    rotating = scan_tier_symbols("themes", 200) + scan_tier_symbols("context", 200)
    rotating = [symbol for symbol in rotating if symbol not in core]
    if rotating and rotating_limit:
        slot = slot if slot is not None else int(time.time() // 900)
        start = (slot * rotating_limit) % len(rotating)
        selected = [rotating[(start + index) % len(rotating)] for index in range(min(rotating_limit, len(rotating)))]
    else:
        selected = []
    return (core + selected)[:total_limit]


def proactive_research_plan(slot: int | None = None) -> dict[str, str]:
    slot = slot if slot is not None else int(time.time() // 1800)
    item = NEWS_RESEARCH_ROTATION[slot % len(NEWS_RESEARCH_ROTATION)]
    symbols = ",".join(item["symbols"])
    task = (
        f"Proactive {item['theme']} outlook research; do not wait for a price anomaly. "
        f"Review fresh events from the last 24 hours and material context from the last 7 days for {symbols}. "
        f"Focus on {item['focus']}. Prioritize {item['sources']}. "
        "For every material item provide a direct URL, published time, source tier, symbols/topics, "
        "bullish/bearish/neutral/mixed direction, immediate/1_5d/2_6w/long_term impact horizon, "
        "transmission path, uncertainty and disconfirming evidence. Return an empty evidence list when nothing material is found. "
        "Evidence collection only: do not send externally and do not perform broker writes."
    )
    return {"theme": str(item["theme"]), "symbols": symbols, "task": task}


def coordinator_delay_seconds(base_interval: int, now: datetime | None = None) -> int:
    policy = load_yaml(OUTLOOK_POLICY_PATH)
    news_policy = policy.get("news", {}) if isinstance(policy, dict) else {}
    local = as_eastern(now or datetime.now(timezone.utc))
    market_window = local.weekday() < 5 and 8 <= local.hour < 18
    configured = int(
        news_policy.get(
            "market_hours_research_interval_seconds" if market_window else "off_hours_research_interval_seconds",
            1800 if market_window else 7200,
        )
    )
    return max(max(1, base_interval), configured)


def news_feed_delay_seconds(base_interval: int, now: datetime | None = None) -> int:
    policy = load_yaml(OUTLOOK_POLICY_PATH)
    news_policy = policy.get("news", {}) if isinstance(policy, dict) else {}
    local = as_eastern(now or datetime.now(timezone.utc))
    market_window = local.weekday() < 5 and 8 <= local.hour < 18
    configured = int(
        news_policy.get(
            "market_hours_feed_interval_seconds" if market_window else "off_hours_feed_interval_seconds",
            900 if market_window else 3600,
        )
    )
    return max(max(1, base_interval), configured)


def daily_brief_enqueue_enabled(
    policy: dict[str, Any],
    *,
    channel_status: dict[str, Any] | None = None,
) -> bool:
    channels = policy.get("channels") if isinstance(policy.get("channels"), dict) else {}
    gmail = channels.get("gmail") if isinstance(channels.get("gmail"), dict) else {}
    if not bool(policy.get("communication_enabled")):
        return False
    if bool(gmail.get("enabled")):
        return True
    if gmail.get("auto_enable_when_ready") is not True:
        return False
    status = channel_status if isinstance(channel_status, dict) else load_json(COMMUNICATION_STATUS_PATH, {})
    activation = status.get("channel_activation") if isinstance(status, dict) else {}
    return bool(activation.get("gmail")) if isinstance(activation, dict) else False


def daily_brief_due(
    now: datetime,
    policy: dict[str, Any],
    state: dict[str, Any],
    *,
    delivery_enabled: bool = True,
) -> bool:
    local = as_eastern(now)
    config = policy.get("daily_brief", {}) if isinstance(policy, dict) else {}
    if config.get("enabled", True) is False:
        return False
    if config.get("weekdays_only", True) and local.weekday() >= 5:
        return False
    hour = int(config.get("hour_et", 16))
    minute = int(config.get("minute_et", 20))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < scheduled or local.hour >= 20:
        return False
    date = local.date().isoformat()
    if str(state.get("last_queued_market_date") or "") == date:
        return False
    if not delivery_enabled and str(state.get("last_generated_market_date") or "") == date:
        return False
    return True


def market_week(value: datetime) -> str:
    iso = as_eastern(value).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weekly_brief_due(
    now: datetime,
    policy: dict[str, Any],
    state: dict[str, Any],
    *,
    delivery_enabled: bool = True,
) -> bool:
    local = as_eastern(now)
    config = policy.get("weekly_brief", {}) if isinstance(policy, dict) else {}
    if config.get("enabled", True) is False:
        return False
    weekday = min(6, max(0, int(config.get("weekday_et", 4))))
    if local.weekday() < weekday:
        return False
    scheduled_date = local.date() - timedelta(days=local.weekday() - weekday)
    scheduled = local.replace(
        year=scheduled_date.year,
        month=scheduled_date.month,
        day=scheduled_date.day,
        hour=int(config.get("hour_et", 17)),
        minute=int(config.get("minute_et", 10)),
        second=0,
        microsecond=0,
    )
    if local < scheduled:
        return False
    week = market_week(now)
    if str(state.get("last_queued_week") or "") == week:
        return False
    if not delivery_enabled and str(state.get("last_generated_week") or "") == week:
        return False
    return True


def run_command(name: str, cmd: list[str], timeout: int) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AGENT_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return {
            "name": name,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "duration_sec": round(time.time() - started, 2),
            "output_tail": proc.stdout[-2000:],
            "ran_at": utc_now_iso(),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "ok": False,
            "returncode": None,
            "duration_sec": round(time.time() - started, 2),
            "output_tail": str(exc)[-2000:],
            "ran_at": utc_now_iso(),
            "error": "TimeoutExpired",
        }


def run_scan_tier(tier: str, symbols: str = "", max_symbols: int = 80) -> dict[str, Any]:
    selected = symbols or ",".join(scan_tier_symbols(tier, max_symbols))
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scripts" / "kline_snapshot.py"),
        "--tier",
        tier,
        "--symbols",
        selected,
        "--max-symbols",
        str(max_symbols),
    ]
    result = run_command(f"market_scan_{tier}", cmd, timeout=240)
    state = load_json(STATE_DIR / "kline_snapshot_latest.json", {})
    result["provider_status"] = state.get("provider_status", {}) if isinstance(state, dict) else {}
    return result


def run_market_signal_engine() -> dict[str, Any]:
    cmd = [sys.executable, str(AGENT_DIR / "scripts" / "market_signal_engine.py"), "--enqueue"]
    result = run_command("market_signal_engine", cmd, timeout=45)
    payload = load_json(STATE_DIR / "market_signals_latest.json", {})
    result["payload"] = payload if isinstance(payload, dict) else {}
    return result


def run_proactive_news_collector() -> dict[str, Any]:
    cmd = [
        sys.executable, str(AGENT_DIR / "scripts" / "proactive_news_collector.py"),
        "--queries-per-run", "2", "--max-records", "8", "--lookback-days", "7",
    ]
    result = run_command("proactive_news_collector", cmd, timeout=120)
    payload = load_json(STATE_DIR / "proactive_news_latest.json", {})
    result["payload"] = payload if isinstance(payload, dict) else {}
    return result


def run_trend_outlook() -> dict[str, Any]:
    cmd = [sys.executable, str(AGENT_DIR / "scripts" / "trend_outlook_engine.py")]
    result = run_command("trend_outlook_engine", cmd, timeout=60)
    payload = load_json(OUTLOOK_PATH, {})
    result["payload"] = {
        "generated_at": payload.get("generated_at"),
        "category_counts": payload.get("category_counts", {}),
        "news_event_count": payload.get("news_event_count", 0),
    } if isinstance(payload, dict) else {}
    return result


def run_market_emergency_monitor() -> dict[str, Any]:
    cmd = [sys.executable, str(AGENT_DIR / "scripts" / "market_emergency_monitor.py"), "--enqueue"]
    result = run_command("market_emergency_monitor", cmd, timeout=45)
    payload = load_json(STATE_DIR / "market_emergency_latest.json", {})
    result["payload"] = payload if isinstance(payload, dict) else {}
    return result


def run_daily_market_brief(*, enqueue: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, str(AGENT_DIR / "scripts" / "daily_market_brief.py")]
    if enqueue:
        cmd.append("--enqueue")
    return run_command("daily_market_brief", cmd, timeout=45)


def run_weekly_market_brief(*, enqueue: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, str(AGENT_DIR / "scripts" / "daily_market_brief.py"), "--weekly"]
    if enqueue:
        cmd.append("--enqueue")
    return run_command("weekly_market_brief", cmd, timeout=45)


def active_hit_symbols(limit: int) -> list[str]:
    payload = load_json(HIT_SYMBOLS_PATH, {})
    now = datetime.now(timezone.utc)
    items = payload.get("items") if isinstance(payload, dict) else []
    symbols: list[str] = []
    if not isinstance(items, list):
        return symbols
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in symbols:
            continue
        expires_at_raw = str(item.get("expires_at") or "").strip()
        if expires_at_raw:
            try:
                expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
                if expires_at < now:
                    continue
            except Exception:
                pass
        symbols.append(symbol)
        if len(symbols) >= limit:
            break
    return symbols


def run_hit_kline(max_symbols: int) -> dict[str, Any]:
    symbols = active_hit_symbols(max_symbols)
    if not symbols:
        return {"name": "hit_kline_snapshot", "ok": True, "skipped": True, "reason": "no_active_hit_symbols", "ran_at": utc_now_iso()}
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scripts" / "kline_snapshot.py"),
        "--symbols",
        ",".join(symbols),
        "--period",
        "5d",
        "--interval",
        "5m",
        "--max-symbols",
        str(max_symbols),
        "--output",
        str(STATE_DIR / "kline_hit_symbols_latest.json"),
        "--replace-output",
        "--tier",
        "active_hit",
    ]
    return run_command("hit_kline_snapshot", cmd, timeout=120)


def run_portfolio_snapshot() -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scripts" / "moomoo_portfolio_snapshot.py"),
        "--output",
        str(PORTFOLIO_SNAPSHOT_PATH),
    ]
    result = run_command("moomoo_portfolio_snapshot", cmd, timeout=45)
    payload = load_json(PORTFOLIO_SNAPSHOT_PATH, {})
    result["payload"] = payload if isinstance(payload, dict) else {}
    return result


def run_wechat_ai_poller() -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scripts" / "wechat_ai_message_poller.py"),
        "--once",
    ]
    return run_command("wechat_ai_message_poller", cmd, timeout=45)


def run_telegram_ai_poller() -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scripts" / "telegram_ai_message_poller.py"),
        "--once",
    ]
    return run_command("telegram_ai_message_poller", cmd, timeout=45)


def run_wechat_ai_result_dispatcher() -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scripts" / "wechat_ai_result_dispatcher.py"),
        "--once",
    ]
    result = run_command("wechat_ai_result_dispatcher", cmd, timeout=45)
    result["payload"] = load_json(STATE_DIR / "wechat_ai_result_dispatcher_state.json", {})
    return result


def run_fusion() -> dict[str, Any]:
    cmd = [sys.executable, str(AGENT_DIR / "scripts" / "codex_fusion_gate.py"), "--dry-run"]
    return run_command("codex_fusion_gate", cmd, timeout=60)


def run_market_alert_dispatcher() -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scripts" / "market_alert_dispatcher.py"),
        "--min-level", "L2",
    ]
    return run_command("market_alert_dispatcher", cmd, timeout=45)


def run_authorization_check() -> dict[str, Any]:
    cmd = [sys.executable, str(AGENT_DIR / "scripts" / "authorization_check.py"), "--json"]
    result = run_command("authorization_check", cmd, timeout=45)
    payload = load_json(STATE_DIR / "authorization_status.json", {})
    result["payload"] = payload if isinstance(payload, dict) else {}
    return result


def run_communication_status() -> dict[str, Any]:
    gateway = str(AGENT_DIR / "scripts" / "communication_gateway.py")
    policy = load_json(COMMUNICATION_POLICY_PATH, {})
    auto_dispatch: dict[str, Any] | None = None
    if bool(policy.get("auto_external_send_allowed")):
        limit = max(1, min(int(policy.get("auto_send_batch_limit", 5)), 20))
        auto_dispatch = run_command(
            "communication_gateway_dispatch",
            [sys.executable, gateway, "--dispatch", "--allow-send", "--limit", str(limit)],
            timeout=120,
        )
    result = run_command("communication_gateway_status", [sys.executable, gateway, "--status"], timeout=45)
    payload = load_json(STATE_DIR / "communication_status.json", {})
    result["payload"] = payload if isinstance(payload, dict) else {}
    if auto_dispatch is not None:
        result["auto_dispatch"] = auto_dispatch
    return result


def run_coordinator(
    trigger: str, task: str, symbols: str, execute_workers: bool, timeout: int,
    correlation_id: str = "",
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scripts" / "ai_coordinator.py"),
        "--trigger",
        trigger,
        "--symbols",
        symbols,
        "--task",
        task,
        "--timeout",
        str(timeout),
    ]
    if execute_workers:
        cmd.append("--execute")
    if correlation_id:
        cmd.extend(["--correlation-id", correlation_id])
    return run_command("ai_coordinator", cmd, timeout=timeout + 60)


def run_warning_review_dispatcher() -> dict[str, Any]:
    cmd = [sys.executable, str(AGENT_DIR / "scripts" / "warning_review_dispatcher.py")]
    result = run_command("warning_review_dispatcher", cmd, timeout=45)
    payload = load_json(STATE_DIR / "market_emergency_state.json", {})
    result["warning_chamber_count"] = len(payload.get("warning_chamber", {})) if isinstance(payload, dict) else 0
    return result


def process_triggers(default_execute_workers: bool, timeout: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted(TRIGGERS_DIR.glob("*.json")):
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        trigger = str(payload.get("trigger") or "user_research_request")
        task = str(payload.get("task") or "").strip()
        if not task:
            continue
        symbols = str(payload.get("symbols") or "")
        execute_workers = bool(payload.get("execute_workers", default_execute_workers))
        correlation_id = str(payload.get("correlation_id") or "")
        result = run_coordinator(trigger, task, symbols, execute_workers, timeout, correlation_id)
        result["trigger_file"] = str(path)
        result["correlation_id"] = correlation_id
        result["workflow"] = str(payload.get("workflow") or "")
        results.append(result)
        archive_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{path.name}"
        shutil.move(str(path), str(TRIGGER_ARCHIVE_DIR / archive_name))
    return results


def manual_actions_from_last(last: dict[str, Any]) -> list[dict[str, Any]]:
    authorization = last.get("authorization", {})
    payload = authorization.get("payload") if isinstance(authorization, dict) else {}
    actions = payload.get("manual_actions") if isinstance(payload, dict) else []
    return actions if isinstance(actions, list) else []


def status_payload(started_at: str, args: argparse.Namespace, last: dict[str, Any]) -> dict[str, Any]:
    ports = check_ports()
    processes = check_processes()
    isolation = compute_stability(ports, processes, load_json(RUNTIME_ISOLATION_PATH, {}))
    manual_actions = manual_actions_from_last(last)
    authorization_payload = (last.get("authorization") or {}).get("payload", {}) if isinstance(last.get("authorization"), dict) else {}
    communication_payload = (last.get("communication") or {}).get("payload", {}) if isinstance(last.get("communication"), dict) else {}
    return {
        "version": "0.4",
        "status": "running",
        "mode": args.mode,
        "pid": os.getpid(),
        "started_at": started_at,
        "updated_at": utc_now_iso(),
        "readonly": True,
        "auto_trade_allowed": False,
        "auto_external_send_allowed": bool(communication_payload.get("auto_external_send_allowed")),
        "communication_enabled": bool(communication_payload.get("communication_enabled")),
        "execute_workers": bool(args.execute_workers),
        "intervals": {
            "health_sec": args.health_interval,
            "authorization_sec": args.authorization_interval,
            "communication_sec": args.communication_interval,
            "kline_sec": args.core_scan_interval,
            "core_scan_sec": args.core_scan_interval,
            "theme_scan_sec": args.theme_scan_interval,
            "context_scan_sec": args.context_scan_interval,
            "portfolio_sec": args.portfolio_interval,
            "hit_kline_sec": args.hit_kline_interval,
            "wechat_ai_sec": args.wechat_ai_interval,
            "telegram_ai_sec": args.telegram_ai_interval,
            "fusion_sec": args.fusion_interval,
            "coordinator_sec": args.coordinator_interval,
            "trend_outlook_sec": args.outlook_interval,
            "emergency_sec": args.emergency_interval,
            "daily_brief_check_sec": args.daily_brief_check_interval,
            "news_feed_floor_sec": args.news_feed_interval,
        },
        "ports": ports,
        "processes": processes,
        "stable": isolation["stable"],
        "required_service_failures": isolation["required_failures"],
        "optional_service_failures": isolation["optional_failures"],
        "startup_ready": authorization_payload.get("startup_ready"),
        "manual_action_count": len(manual_actions),
        "manual_actions": manual_actions,
        "trigger_dir": str(TRIGGERS_DIR),
        "portfolio_snapshot_path": str(PORTFOLIO_SNAPSHOT_PATH),
        "hit_symbols": active_hit_symbols(args.max_symbols),
        "communication_outbox_count": communication_payload.get("outbox_count"),
        "last": last,
    }


def run_once(args: argparse.Namespace, started_at: str) -> dict[str, Any]:
    last: dict[str, Any] = {}
    last["authorization"] = run_authorization_check()
    last["communication"] = run_communication_status()
    last["portfolio"] = run_portfolio_snapshot()
    last["market_scans"] = {
        "core": run_scan_tier("core", args.symbols, args.max_symbols),
        "themes": run_scan_tier("themes", max_symbols=args.max_symbols),
        "context": run_scan_tier("context", max_symbols=args.max_symbols),
    }
    last["kline"] = last["market_scans"]["core"]
    last["market_signals"] = run_market_signal_engine()
    last["hit_kline"] = run_hit_kline(args.max_symbols)
    last["wechat_ai"] = run_wechat_ai_poller()
    last["telegram_ai"] = run_telegram_ai_poller()
    last["wechat_ai_results"] = run_wechat_ai_result_dispatcher()
    if int((last["wechat_ai_results"].get("payload") or {}).get("external_queued") or 0) > 0:
        last["communication"] = run_communication_status()
    last["proactive_news"] = run_proactive_news_collector()
    last["fusion"] = run_fusion()
    last["market_alerts"] = run_market_alert_dispatcher()
    last["trend_outlook"] = run_trend_outlook()
    last["market_emergency"] = run_market_emergency_monitor()
    emergency_payload = last["market_emergency"].get("payload", {})
    if int(emergency_payload.get("queued_count") or 0) > 0:
        last["communication"] = run_communication_status()
    communication_policy = load_json(COMMUNICATION_POLICY_PATH, {})
    enqueue_daily_brief = daily_brief_enqueue_enabled(communication_policy if isinstance(communication_policy, dict) else {})
    if daily_brief_due(
        datetime.now(timezone.utc),
        load_yaml(OUTLOOK_POLICY_PATH),
        load_json(DAILY_BRIEF_STATE_PATH, {}),
        delivery_enabled=enqueue_daily_brief,
    ):
        last["daily_brief"] = run_daily_market_brief(enqueue=enqueue_daily_brief)
        if enqueue_daily_brief:
            last["communication"] = run_communication_status()
    if weekly_brief_due(
        datetime.now(timezone.utc),
        load_yaml(OUTLOOK_POLICY_PATH),
        load_json(WEEKLY_BRIEF_STATE_PATH, {}),
        delivery_enabled=enqueue_daily_brief,
    ):
        last["weekly_brief"] = run_weekly_market_brief(enqueue=enqueue_daily_brief)
        if enqueue_daily_brief:
            last["communication"] = run_communication_status()
    last["triggers"] = process_triggers(args.execute_workers, args.worker_timeout)
    if any(item.get("workflow") == "warning_review" for item in last["triggers"]):
        last["fusion"] = run_fusion()
        last["trend_outlook"] = run_trend_outlook()
        last["warning_reviews"] = run_warning_review_dispatcher()
        last["communication"] = run_communication_status()
    payload = status_payload(started_at, args, last)
    write_json(STATUS_PATH, payload)
    queue_health_transition(payload)
    return payload


def run_loop(args: argparse.Namespace) -> None:
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    ensure_dirs()
    acquire_pid(PID_PATH)
    started_at = utc_now_iso()
    last: dict[str, Any] = {}
    next_health = 0.0
    next_authorization = 0.0
    next_communication = 0.0
    next_scans = {"core": 0.0, "themes": 0.0, "context": 0.0}
    next_portfolio = 0.0
    next_hit_kline = 0.0
    next_wechat_ai = 0.0
    next_telegram_ai = 0.0
    next_fusion = 0.0
    next_news_feed = 0.0
    next_outlook = 0.0
    next_emergency = 0.0
    next_daily_brief_check = 0.0
    next_heartbeat_log = 0.0
    next_coord = time.time() + coordinator_delay_seconds(args.coordinator_interval) if args.coordinator_interval > 0 else 0.0

    try:
        while not STOP_REQUESTED:
            now = time.time()
            if now >= next_health:
                payload = status_payload(started_at, args, last)
                write_json(STATUS_PATH, payload)
                if now >= next_heartbeat_log:
                    print(
                        "SYSTEM_RUNNER "
                        f"heartbeat stable={payload['stable']} startup_ready={payload.get('startup_ready')} "
                        f"manual_actions={payload.get('manual_action_count')} updated_at={payload['updated_at']}",
                        flush=True,
                    )
                    next_heartbeat_log = now + args.log_heartbeat_interval
                next_health = now + args.health_interval

            if args.authorization_interval > 0 and now >= next_authorization:
                last["authorization"] = run_authorization_check()
                next_authorization = now + args.authorization_interval

            if args.communication_interval > 0 and now >= next_communication:
                last["communication"] = run_communication_status()
                next_communication = now + args.communication_interval

            scanned = False
            scan_intervals = {
                "core": args.core_scan_interval,
                "themes": args.theme_scan_interval,
                "context": args.context_scan_interval,
            }
            market_scans = last.setdefault("market_scans", {})
            for tier, interval_seconds in scan_intervals.items():
                if interval_seconds > 0 and now >= next_scans[tier]:
                    selected = args.symbols if tier == "core" else ""
                    market_scans[tier] = run_scan_tier(tier, selected, args.max_symbols)
                    if tier == "core":
                        last["kline"] = market_scans[tier]
                    next_scans[tier] = now + interval_seconds
                    scanned = True
            if scanned:
                last["market_signals"] = run_market_signal_engine()

            if args.portfolio_interval > 0 and now >= next_portfolio:
                last["portfolio"] = run_portfolio_snapshot()
                next_portfolio = now + args.portfolio_interval

            if args.hit_kline_interval > 0 and now >= next_hit_kline:
                last["hit_kline"] = run_hit_kline(args.max_symbols)
                next_hit_kline = now + args.hit_kline_interval

            if args.wechat_ai_interval > 0 and now >= next_wechat_ai:
                last["wechat_ai"] = run_wechat_ai_poller()
                last["wechat_ai_results"] = run_wechat_ai_result_dispatcher()
                if int((last["wechat_ai_results"].get("payload") or {}).get("external_queued") or 0) > 0:
                    last["communication"] = run_communication_status()
                next_wechat_ai = now + args.wechat_ai_interval

            if args.telegram_ai_interval > 0 and now >= next_telegram_ai:
                last["telegram_ai"] = run_telegram_ai_poller()
                next_telegram_ai = now + args.telegram_ai_interval

            if args.news_feed_interval > 0 and now >= next_news_feed:
                last["proactive_news"] = run_proactive_news_collector()
                if int((last["proactive_news"].get("payload") or {}).get("new_count") or 0) > 0:
                    last["fusion"] = run_fusion()
                    last["trend_outlook"] = run_trend_outlook()
                next_news_feed = now + news_feed_delay_seconds(args.news_feed_interval)

            if args.fusion_interval > 0 and now >= next_fusion:
                last["fusion"] = run_fusion()
                last["market_alerts"] = run_market_alert_dispatcher()
                next_fusion = now + args.fusion_interval

            if args.outlook_interval > 0 and now >= next_outlook:
                last["trend_outlook"] = run_trend_outlook()
                next_outlook = now + args.outlook_interval

            if args.emergency_interval > 0 and now >= next_emergency:
                last["market_emergency"] = run_market_emergency_monitor()
                emergency_payload = last["market_emergency"].get("payload", {})
                if int(emergency_payload.get("queued_count") or 0) > 0:
                    last["communication"] = run_communication_status()
                next_emergency = now + args.emergency_interval

            if args.daily_brief_check_interval > 0 and now >= next_daily_brief_check:
                communication_policy = load_json(COMMUNICATION_POLICY_PATH, {})
                enqueue_daily_brief = daily_brief_enqueue_enabled(
                    communication_policy if isinstance(communication_policy, dict) else {}
                )
                if daily_brief_due(
                    datetime.now(timezone.utc),
                    load_yaml(OUTLOOK_POLICY_PATH),
                    load_json(DAILY_BRIEF_STATE_PATH, {}),
                    delivery_enabled=enqueue_daily_brief,
                ):
                    last["trend_outlook"] = run_trend_outlook()
                    last["daily_brief"] = run_daily_market_brief(enqueue=enqueue_daily_brief)
                    if enqueue_daily_brief and last["daily_brief"].get("ok"):
                        last["communication"] = run_communication_status()
                if weekly_brief_due(
                    datetime.now(timezone.utc),
                    load_yaml(OUTLOOK_POLICY_PATH),
                    load_json(WEEKLY_BRIEF_STATE_PATH, {}),
                    delivery_enabled=enqueue_daily_brief,
                ):
                    last["trend_outlook"] = run_trend_outlook()
                    last["weekly_brief"] = run_weekly_market_brief(enqueue=enqueue_daily_brief)
                    if enqueue_daily_brief and last["weekly_brief"].get("ok"):
                        last["communication"] = run_communication_status()
                next_daily_brief_check = now + args.daily_brief_check_interval

            trigger_results = process_triggers(args.execute_workers, args.worker_timeout)
            if trigger_results:
                last["triggers"] = trigger_results
                if any(item.get("workflow") == "warning_review" for item in trigger_results):
                    last["fusion"] = run_fusion()
                    last["trend_outlook"] = run_trend_outlook()
                    last["warning_reviews"] = run_warning_review_dispatcher()
                    if last["warning_reviews"].get("ok"):
                        last["communication"] = run_communication_status()

            if args.coordinator_interval > 0 and now >= next_coord:
                research = proactive_research_plan(int(now // max(1, args.coordinator_interval)))
                symbols = args.symbols or research["symbols"]
                last["coordinator"] = run_coordinator(
                    "user_research_request",
                    research["task"],
                    symbols,
                    args.execute_workers,
                    args.worker_timeout,
                )
                next_coord = now + coordinator_delay_seconds(args.coordinator_interval)

            payload = status_payload(started_at, args, last)
            write_json(STATUS_PATH, payload)
            queue_health_transition(payload)
            time.sleep(max(1, min(args.health_interval, 10)))
    finally:
        payload = status_payload(started_at, args, last)
        payload["status"] = "stopped"
        payload["stopped_at"] = utc_now_iso()
        write_json(STATUS_PATH, payload)
        release_pid(PID_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--mode", default="safe_observe")
    parser.add_argument("--health-interval", type=int, default=60)
    parser.add_argument("--authorization-interval", type=int, default=600)
    parser.add_argument("--communication-interval", type=int, default=300)
    parser.add_argument("--core-scan-interval", type=int, default=300)
    parser.add_argument("--theme-scan-interval", type=int, default=900)
    parser.add_argument("--context-scan-interval", type=int, default=1800)
    parser.add_argument("--log-heartbeat-interval", type=int, default=900)
    parser.add_argument("--portfolio-interval", type=int, default=3600)
    parser.add_argument("--hit-kline-interval", type=int, default=60)
    parser.add_argument("--wechat-ai-interval", type=int, default=30)
    parser.add_argument("--telegram-ai-interval", type=int, default=10)
    parser.add_argument("--fusion-interval", type=int, default=300)
    parser.add_argument("--coordinator-interval", type=int, default=7200)
    parser.add_argument("--news-feed-interval", type=int, default=900)
    parser.add_argument("--outlook-interval", type=int, default=300)
    parser.add_argument("--emergency-interval", type=int, default=60)
    parser.add_argument("--daily-brief-check-interval", type=int, default=60)
    parser.add_argument("--worker-timeout", type=int, default=180)
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--execute-workers", action="store_true")
    args = parser.parse_args()

    ensure_dirs()
    if args.once:
        payload = run_once(args, utc_now_iso())
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("stable") else 2

    run_loop(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
