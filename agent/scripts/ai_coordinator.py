#!/usr/bin/env python3
"""Coordinate Codex, Google/Antigravity, and Grok for market-watchdog.

This is not a daemon and it never calls broker write APIs. It can coordinate
AI workers to produce trade recommendations and non-transmitting order drafts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("APP_DIR", str(PROJECT_DIR)))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
CONFIG_PATH = AGENT_DIR / "config" / "ai_orchestration.json"
BUS_DIR = AGENT_DIR / "state" / "ai_bus"
STATE_DIR = AGENT_DIR / "state"
RUNS_DIR = AGENT_DIR / "runs"
REVIEWS_DIR = BUS_DIR / "reviews"
SAFE_ENV_NAMES = {
    "PATH", "HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL",
    "TZ", "TMPDIR", "AGY_MODEL", "AGY_EFFORT",
}

BLOCKED_TERMS = (
    "/app/secrets",
    "/root/.ssh",
    "oauth-token",
    "gmail_token",
    "wechat_app_secret",
    "ibkr password",
    "placeorder",
    "cancelorder",
    "transmit=true",
    "docker rm",
    "docker stop",
    "sudo",
)


def antigravity_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in SAFE_ENV_NAMES}
    proxy = os.environ.get("AGY_PROXY_URL", "").strip()
    if proxy:
        env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy})
    return env


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str, limit: int = 48) -> str:
    out = []
    for char in value.lower():
        if char.isalnum() or char in "._-":
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    return (slug or "task")[:limit]


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_symbols(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def controller_public_context(symbols: list[str]) -> str:
    wanted = {str(item).upper() for item in symbols if str(item).strip()}
    outlook_payload = load_json_file(STATE_DIR / "trend_outlook_latest.json", {})
    news_payload = load_json_file(STATE_DIR / "proactive_news_evidence.json", {})
    outlook = []
    for item in outlook_payload.get("items", []) if isinstance(outlook_payload, dict) else []:
        if not isinstance(item, dict) or str(item.get("symbol") or "").upper() not in wanted:
            continue
        outlook.append({key: item.get(key) for key in (
            "symbol", "category", "risk_score", "confidence_label", "short_term", "swing",
            "conclusion", "invalidation", "return_1d_pct", "flags",
        )})
    news = []
    for item in news_payload.get("items", []) if isinstance(news_payload, dict) else []:
        if not isinstance(item, dict):
            continue
        item_symbols = {str(value).upper() for value in (item.get("symbols") or [])}
        if wanted and not wanted.intersection(item_symbols):
            continue
        news.append({key: item.get(key) for key in (
            "title", "url", "source_name", "source_tier", "published_at", "symbols",
            "summary", "evidence_text", "direction", "impact_horizon", "confidence", "limitations",
        )})
        if len(news) >= 8:
            break
    if not outlook and not news:
        return ""
    payload = {"symbols": sorted(wanted), "trend_context": outlook, "public_news_context": news}
    return json.dumps(payload, ensure_ascii=False, indent=2)[:16000]


def guard_task(task: str) -> list[str]:
    lower = task.lower()
    return [term for term in BLOCKED_TERMS if term in lower]


def enabled_workers(config: dict[str, Any], requested: list[str]) -> list[str]:
    profiles = config.get("agents", {})
    selected = [
        worker
        for worker in dict.fromkeys(requested)
        if profiles.get(worker, {}).get("enabled", True)
    ]
    if selected:
        return selected
    return [
        worker
        for worker in ("grok", "google_antigravity")
        if profiles.get(worker, {}).get("enabled", True)
    ][:1]


def choose_workers(config: dict[str, Any], trigger: str, task: str) -> list[str]:
    task_lower = task.lower()
    # Price-only L1 events are discovery work, not an external-warning review.
    # One fresh-news worker is enough here; L2 warning reviews still use the full
    # multi-model path after independent confirmation exists.
    if "l1 price-only anomaly" in task_lower:
        return []
    event = config["activation_policy"]["event_triggers"].get(trigger)
    if event:
        workers = []
        first = event.get("first_worker")
        confirm = event.get("confirm_worker")
        if first in {"grok", "google_antigravity"}:
            workers.append(first)
        if confirm in {"grok", "google_antigravity"} and confirm not in workers:
            workers.append(confirm)
        if confirm == "auto":
            if any(term in task_lower for term in ["x.com", "twitter", "social", "breaking", "rumor", "news"]):
                workers.append("grok")
            if any(term in task_lower for term in ["filing", "official", "earnings", "sec", "supply chain", "bottleneck"]):
                workers.append("google_antigravity")
        if workers:
            return enabled_workers(config, workers)

    if any(term in task_lower for term in ["x.com", "twitter", "social", "breaking", "rumor", "news"]):
        return enabled_workers(config, ["grok", "google_antigravity"])
    if any(term in task_lower for term in ["filing", "official", "earnings", "sec", "supply chain", "bottleneck", "serenity"]):
        return enabled_workers(config, ["google_antigravity", "grok"])
    return enabled_workers(config, ["google_antigravity"])


def worker_alias_for_skill_router(workers: list[str]) -> str:
    mapped = ["gemini" if item == "google_antigravity" else item for item in workers]
    if set(mapped) == {"gemini", "grok"}:
        return "both"
    if mapped == ["grok"]:
        return "grok"
    if mapped == ["gemini"]:
        return "gemini"
    return "both"


def make_message(
    run_id: str,
    sender: str,
    recipient: str,
    message_type: str,
    status: str,
    task: str,
    symbols: list[str],
    parent_message_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message_id = f"{run_id}_{message_type}_{recipient}_{len(task)}"
    return {
        "version": "0.1",
        "message_id": message_id,
        "run_id": run_id,
        "parent_message_id": parent_message_id,
        "created_at": utc_now_iso(),
        "sender": sender,
        "recipient": recipient,
        "message_type": message_type,
        "status": status,
        "symbols": symbols,
        "topic": payload.get("topic", "") if payload else "",
        "task": task,
        "requested_skill_ids": payload.get("requested_skill_ids", []) if payload else [],
        "source_tier": "not_applicable",
        "confidence": "not_applicable",
        "evidence_refs": [],
        "payload": payload or {},
        "next_actions": [],
        "blocked_actions_confirmed": True,
    }


def build_plan(config: dict[str, Any], trigger: str, task: str, symbols: list[str]) -> dict[str, Any]:
    workers = choose_workers(config, trigger, task)
    agent_profiles = config["agents"]
    return {
        "created_at": utc_now_iso(),
        "trigger": trigger,
        "task": task,
        "symbols": symbols,
        "controller": "codex",
        "workers": workers,
        "activation": {
            worker: agent_profiles[worker]["activation"] for worker in workers if worker in agent_profiles
        },
        "communication": config["communication"],
        "quorum_policy": config["quorum_policy"],
        "sequence": [
            "codex_prepare_task_envelope",
            "dispatch_information_workers",
            "wait_for_worker_outputs_or_timeout",
            "codex_validate_sources_and_contradictions",
            "codex_merge_kline_context",
            "codex_write_human_review_brief_or_refusal",
        ],
    }


def execute_skill_router(task: str, symbols: str, workers: list[str], timeout: int) -> dict[str, Any]:
    router = AGENT_DIR / "scripts" / "skill_router.py"
    cmd = [
        sys.executable,
        str(router),
        "--task",
        task,
        "--symbols",
        symbols,
        "--workers",
        worker_alias_for_skill_router(workers),
        "--max-skills",
        "2",
        "--execute",
        "--timeout",
        str(timeout),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(AGENT_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=timeout + 30,
    )
    return {
        "command": cmd[:2] + ["..."],
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "output": proc.stdout,
    }


def codex_review_schema() -> dict[str, Any]:
    string = {"type": "string", "minLength": 8}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "event", "summary", "evidence_summary", "recommendation", "invalidation",
            "next_check", "tomorrow_outlook", "future_outlook", "confidence", "citations",
        ],
        "properties": {
            "event": string,
            "summary": string,
            "evidence_summary": {"type": "array", "items": {"type": "string", "minLength": 4}, "minItems": 1},
            "recommendation": string,
            "invalidation": string,
            "next_check": string,
            "tomorrow_outlook": string,
            "future_outlook": string,
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }


def run_codex_review_model(model: str, prompt: str, workspace: Path, timeout: int) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    schema_path = workspace / "warning_review.schema.json"
    output_path = workspace / "warning_review.output.json"
    write_json(schema_path, codex_review_schema())
    output_path.unlink(missing_ok=True)
    binary = "/root/.local/bin/codex" if Path("/root/.local/bin/codex").exists() else (shutil.which("codex") or "codex")
    command = [
        binary, "-a", "never", "-s", "read-only", "-m", model,
        "-c", 'model_reasoning_effort="medium"', "exec", "--skip-git-repo-check",
        "--ephemeral", "--color", "never", "-C", str(workspace),
        "--output-schema", str(schema_path), "--output-last-message", str(output_path), "-",
    ]
    env = {key: value for key, value in os.environ.items() if key in SAFE_ENV_NAMES}
    try:
        proc = subprocess.run(
            command, cwd=str(workspace), env=env, input=prompt, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": exc.__class__.__name__}
    if proc.returncode != 0 or not output_path.exists():
        lower = proc.stdout.lower()
        category = "authentication" if any(value in lower for value in ("unauthorized", "login", "authentication", "401")) else (
            "model_unavailable" if any(value in lower for value in ("model not found", "was not found", "unsupported model")) else "provider_error"
        )
        return {"ok": False, "error": category, "output_tail": proc.stdout[-500:]}
    payload = load_json_file(output_path, {})
    return {"ok": isinstance(payload, dict) and bool(payload), "payload": payload, "error": None}


def run_codex_review_chain(
    models: list[str], prompt: str, workspace: Path, *, timeout: int,
    runner: Callable[[str, str, Path, int], dict[str, Any]] = run_codex_review_model,
) -> dict[str, Any]:
    attempts = []
    for model in models:
        result = runner(model, prompt, workspace, timeout)
        attempts.append({"model": model, "ok": bool(result.get("ok")), "error": result.get("error")})
        if result.get("ok"):
            return {"ok": True, "model": model, "payload": result.get("payload", {}), "attempts": attempts}
    return {"ok": False, "model": None, "payload": {}, "attempts": attempts}


def interaction_output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text") or payload.get("outputText")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    texts: list[str] = []

    def collect(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and key.lower() in {
            "text", "output_text", "outputtext", "response", "result", "content", "message",
        } and value.strip():
            texts.append(value.strip())

    collect(payload)
    return "\n".join(dict.fromkeys(texts))


def run_google_review_model(model: str, prompt: str, workspace: Path, timeout: int) -> dict[str, Any]:
    """Use the subscription-backed Antigravity CLI as the final reviewer."""
    del workspace
    binary = shutil.which("agy") or "agy"
    command = [
        binary, "--model", model, "--effort", "low", "--mode", "plan",
        "--sandbox", "--output-format", "json", "-p", prompt,
        "--print-timeout", "5m",
    ]
    env = antigravity_env()
    try:
        proc = subprocess.run(
            command, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False, timeout=timeout,
        )
        if proc.returncode != 0:
            lower = proc.stdout.lower()
            category = "authentication" if any(value in lower for value in ("unauthorized", "login", "authentication", "401")) else (
                "quota_or_rate_limit" if any(value in lower for value in ("quota", "rate limit", "usage limit")) else "provider_error"
            )
            return {"ok": False, "error": category, "returncode": proc.returncode}
        decoded = json.loads(proc.stdout)
        if isinstance(decoded, dict) and all(key in decoded for key in codex_review_schema()["required"]):
            parsed = decoded
        else:
            output = interaction_output_text(decoded)
            parsed = json.loads(output)
        if not isinstance(parsed, dict):
            raise ValueError("google_review_non_object")
        receipt = AGENT_DIR / "state" / "provider_receipts" / "google_gemini.json"
        write_json(receipt, {
            "version": "1.0", "status": "completed", "verified_at": utc_now_iso(),
            "provider": "google_antigravity_cli", "model": model,
            "subscription_baseline_only": True, "ai_credits_enabled": False,
            "credential_logged": False, "actual_broker_writes": False, "external_send": False,
        })
        return {"ok": True, "payload": parsed, "error": None}
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": exc.__class__.__name__}


def load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def skill_run_dir(execution: dict[str, Any]) -> Path | None:
    match = re.search(r"run_dir=([^\s]+)", str(execution.get("output") or ""))
    return Path(match.group(1)) if match else None


def worker_model_status(result: dict[str, Any], worker: str) -> str:
    for item in result.get("worker_results", []) if isinstance(result, dict) else []:
        if str(item.get("worker")) == worker:
            backend = str(item.get("backend") or worker)
            return f"{backend}:{'ok' if item.get('ok') else item.get('status', 'failed')}"
    return "not_run"


def build_warning_review(
    config: dict[str, Any], correlation_id: str, task: str, symbols: list[str],
    execution: dict[str, Any], run_dir: Path, timeout: int,
) -> dict[str, Any]:
    routed_dir = skill_run_dir(execution)
    worker_result = load_json_file(routed_dir / "result.json", {}) if routed_dir else {}
    evidence = load_json_file(routed_dir / "raw_output.txt", {"items": []}) if routed_dir else {"items": []}
    evidence_text = json.dumps(evidence, ensure_ascii=False)[:40000]
    prompt = (
        "You are the Codex controller and final market-risk reviewer. Review the two bounded worker outputs below. "
        "Do not invent a catalyst or certainty. Separate verified evidence from inference. Produce actionable but "
        "non-executing guidance for a human, with an invalidation condition and next check. Recommendations may include "
        "hold/watch/reduce/protect/wait, but never place or authorize an order. Write like a concise institutional client "
        "alert: facts first, price implication second, and conditional action last. All string values except URLs, tickers, "
        "and fixed enum values must use natural Simplified Chinese（简体中文）. Keep event within 80 Chinese characters, "
        "summary within 110, and recommendation within 130. Tomorrow and future outlook fields are internal inputs for the "
        "daily report only; do not fold them into the immediate alert recommendation. Return only the requested JSON.\n\n"
        f"Symbols: {','.join(symbols)}\nTask: {task}\nWorker evidence: {evidence_text}"
    )
    codex_profile = config.get("agents", {}).get("codex", {})
    google_profile = config.get("agents", {}).get("google_antigravity", {})
    google_only = not bool(codex_profile.get("enabled", True))
    if google_only:
        google_models = google_profile.get("models", {}) if isinstance(google_profile.get("models"), dict) else {}
        models = [str(google_models.get("deep") or "gemini-3.7-flash-low"), str(google_models.get("research") or "gemini-3.7-flash-low")]
        review_runner = run_google_review_model
        reviewer_provider = "google_antigravity_cli"
    else:
        models = [str(codex_profile.get("model") or "gpt-5.6"), *[str(item) for item in codex_profile.get("fallback_models", ["gpt-5.5"])]]
        review_runner = run_codex_review_model
        reviewer_provider = "codex"
    models = list(dict.fromkeys(model for model in models if model))
    reviewer = run_codex_review_chain(
        models, prompt, run_dir / "final_review", timeout=timeout, runner=review_runner,
    )
    successful_workers = sum(1 for item in worker_result.get("worker_results", []) if item.get("ok"))
    synthesis = reviewer.get("payload") if reviewer.get("ok") else {}
    required = ("event", "summary", "recommendation", "invalidation", "next_check")
    substantive = (
        isinstance(synthesis, dict)
        and all(len(str(synthesis.get(key) or "").strip()) >= 8 for key in required)
        and all(len(re.findall(r"[\u4e00-\u9fff]", str(synthesis.get(key) or ""))) >= 4 for key in ("event", "summary", "recommendation"))
        and bool(synthesis.get("evidence_summary"))
        and bool(synthesis.get("citations"))
    )
    required_worker_count = 1 if google_only else 2
    status = "complete" if substantive and successful_workers >= required_worker_count else (
        "complete_with_fallback" if substantive else "partial"
    )
    return {
        "version": "1.0",
        "correlation_id": correlation_id,
        "created_at": utc_now_iso(),
        "status": status,
        "symbols": symbols,
        "models": {
            "google": worker_model_status(worker_result, "gemini"),
            "grok": worker_model_status(worker_result, "grok"),
            "codex": "disabled_google_only" if google_only else str(reviewer.get("model") or "unavailable"),
            "final_reviewer": str(reviewer.get("model") or "unavailable"),
        },
        "worker_success_count": successful_workers,
        "worker_evidence_count": int(worker_result.get("evidence_count") or 0),
        "reviewer_provider": reviewer_provider,
        "review_attempts": reviewer.get("attempts", []),
        "codex_attempts": [] if google_only else reviewer.get("attempts", []),
        "synthesis": synthesis,
        "external_send_performed": False,
        "actual_broker_writes": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capabilities", action="store_true", help="Print AI capability and activation policy.")
    parser.add_argument("--task", help="Task to coordinate.")
    parser.add_argument("--symbols", default="")
    parser.add_argument(
        "--trigger",
        default="user_research_request",
        choices=[
            "breaking_news",
            "official_filing_or_earnings",
            "price_volume_anomaly",
            "portfolio_hit",
            "wechat_message",
            "trade_authorization_request",
            "user_research_request",
        ],
    )
    parser.add_argument("--execute", action="store_true", help="Actually invoke skill_router workers.")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--correlation-id", default="")
    args = parser.parse_args()

    config = load_config()
    if args.capabilities:
        print(json.dumps({"agents": config["agents"], "activation_policy": config["activation_policy"]}, ensure_ascii=False, indent=2))
        return 0

    if not args.task:
        parser.error("--task is required unless --capabilities is used")

    blocked = guard_task(args.task)
    symbols = parse_symbols(args.symbols)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_coord_{slugify(args.trigger)}"
    run_dir = RUNS_DIR / run_id
    bus_run_dir = BUS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    bus_run_dir.mkdir(parents=True, exist_ok=True)

    plan = build_plan(config, args.trigger, args.task, symbols)
    plan["run_id"] = run_id
    plan["blocked_patterns"] = blocked
    write_json(run_dir / "plan.json", plan)
    write_json(bus_run_dir / "plan.json", plan)

    task_message = make_message(
        run_id,
        sender="codex",
        recipient="all",
        message_type="task",
        status="refused_by_policy" if blocked else "queued",
        task=args.task,
        symbols=symbols,
        payload={"trigger": args.trigger, "workers": plan["workers"]},
    )
    write_json(bus_run_dir / "000_task.json", task_message)

    for index, worker in enumerate(plan["workers"], start=1):
        msg = make_message(
            run_id,
            sender="codex",
            recipient=worker,
            message_type="task",
            status="refused_by_policy" if blocked else "queued",
            task=args.task,
            symbols=symbols,
            parent_message_id=task_message["message_id"],
            payload={"trigger": args.trigger, "worker_role": config["agents"][worker]["role"]},
        )
        write_json(bus_run_dir / f"{index:03d}_{worker}_task.json", msg)

    result: dict[str, Any] = {
        "run_id": run_id,
        "ok": not blocked,
        "status": "refused_by_local_policy" if blocked else "planned",
        "workers": plan["workers"],
        "blocked_patterns": blocked,
    }

    if blocked:
        pass
    elif args.execute and not plan["workers"]:
        result["status"] = "paused_no_enabled_workers"
    elif args.execute:
        worker_task = args.task
        public_context = controller_public_context(symbols)
        if public_context:
            worker_task += (
                "\n\nCONTROLLER-PROVIDED PUBLIC CONTEXT (do not treat as independently verified; "
                "validate contradictions and limitations):\n" + public_context
            )
        execution = execute_skill_router(worker_task, args.symbols, plan["workers"], args.timeout)
        result["execution"] = execution
        result["status"] = "executed"
        result["ok"] = bool(execution["ok"])
        if args.correlation_id:
            review = build_warning_review(config, args.correlation_id, args.task, symbols, execution, run_dir, args.timeout)
            REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
            review_path = REVIEWS_DIR / f"{args.correlation_id}.json"
            write_json(review_path, review)
            result["warning_review"] = {"status": review["status"], "path": str(review_path), "models": review["models"]}

    fusion_msg = make_message(
        run_id,
        sender="codex",
        recipient="codex",
        message_type="fusion",
        status="needs_human_review" if result["ok"] else "refused_by_policy",
        task=args.task,
        symbols=symbols,
        parent_message_id=task_message["message_id"],
        payload={"result_status": result["status"], "next_gate": "codex_human_review"},
    )
    write_json(bus_run_dir / "999_codex_fusion_gate.json", fusion_msg)
    write_json(run_dir / "result.json", result)

    print(
        "AI_COORDINATOR "
        f"status={result['status']} "
        f"ok={result['ok']} "
        f"workers={','.join(plan['workers'])} "
        f"run_dir={run_dir} "
        f"bus_dir={bus_run_dir}"
    )
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
