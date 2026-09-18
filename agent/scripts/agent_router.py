#!/usr/bin/env python3
"""Controlled launcher for Google/Grok information-worker tasks.

The default mode is dry-run: write the request and prompt, but do not execute an
external CLI. Add --execute only inside a sandbox or sidecar runtime.

The Google worker is Antigravity CLI (`agy`) only. API-key and legacy Gemini
CLI fallbacks are intentionally excluded from the production route.
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
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("APP_DIR", str(PROJECT_DIR)))
AGENT_DIR = APP_DIR / "agent"
RUNS_DIR = AGENT_DIR / "runs"
SCHEMA_PATH = AGENT_DIR / "schemas" / "worker_evidence.schema.json"

BLOCKED_PATTERNS = (
    "/app/secrets",
    "runtime_env",
    "gmail_token",
    "wechat_app_secret",
    "ibkr password",
    "placeorder",
    "cancelorder",
    "transmit=true",
    "docker rm",
    "docker stop",
    "sudo",
    "curl | bash",
    "curl -fsSL",
    "wget | bash",
)

ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "AGY_MODEL",
    "AGY_EFFORT",
    "XAI_API_KEY",
    "GROK_CODE_XAI_API_KEY",
}

GROK_DISALLOWED_TOOLS = ",".join(
    (
        "update_goal",
        "create_goal",
        "apply_patch",
        "search_replace",
        "run_terminal_cmd",
        "read_file",
        "list_dir",
        "grep",
        "task",
        "get_task_output",
        "kill_task",
        "ask_user_question",
        "enter_lan_mode",
        "enter_plan_mode",
        "exit_plan_mode",
    )
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str, limit: int = 36) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-").lower()
    return (slug or "task")[:limit]


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in ENV_ALLOWLIST}


def worker_env(worker: str) -> dict[str, str]:
    env = safe_env()
    if worker == "gemini":
        proxy = os.environ.get("AGY_PROXY_URL", "").strip()
        if proxy:
            env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy})
    return env


def guard_task(task: str) -> list[str]:
    lower = task.lower()
    hits = [pattern for pattern in BLOCKED_PATTERNS if pattern in lower]
    return hits


def build_prompt(worker: str, mode: str, symbols: list[str], task: str, max_items: int) -> str:
    symbol_text = ", ".join(symbols) if symbols else "watchlist-selected symbols"
    return f"""You are the {worker} information worker for market-watchdog.

Mode: {mode}
Symbols/entities: {symbol_text}
Task: {task}

You collect public evidence only. You do not make final trade decisions. You do
not place orders, send messages, read secrets, or modify production state.

Return one JSON object compatible with this schema:
{SCHEMA_PATH}

Required JSON shape:
{{
  "version": "0.1",
  "worker": "{worker}",
  "mode": "{mode}",
  "collected_at": "<UTC ISO time>",
  "task": "<short task>",
  "items": [
    {{
      "title": "...",
      "url": "...",
      "source_name": "...",
      "source_tier": "S0|S1|S2|S3",
      "published_at": "...",
      "collected_at": "<UTC ISO time>",
      "symbols": ["..."],
      "entities": ["..."],
      "topics": ["..."],
      "summary": "...",
      "evidence_text": "short non-copyright-infringing excerpt or paraphrase",
      "why_it_matters": "...",
      "limitations": "...",
      "confidence": "low|medium|high",
      "trade_instruction": false
    }}
  ]
}}

Rules:
- Limit to {max_items} items.
- Prefer S0/S1 official or quasi-official sources when possible.
- Label broad media or social signals as S3.
- S3-only findings are early warnings, not high-confidence signals.
- Include limitations and what would confirm or refute the evidence.
- Do not output orders, position sizes, or imperative trade instructions.
"""


def worker_command(worker: str, prompt: str, cwd: Path) -> tuple[list[str], str]:
    if worker == "gemini":
        model = os.environ.get("AGY_MODEL", "gemini-3.7-flash-low").strip() or "gemini-3.7-flash-low"
        effort = os.environ.get("AGY_EFFORT", "low").strip().lower()
        if effort != "low":
            effort = "low"
        return [
            "agy", "--model", model, "--effort", effort, "--mode", "plan",
            "--sandbox", "--output-format", "json", "-p", prompt,
            "--print-timeout", "5m",
        ], "agy"
    if worker == "grok":
        return [
            "grok",
            "--no-subagents",
            "--no-memory",
            "--disallowed-tools",
            GROK_DISALLOWED_TOOLS,
            "--permission-mode",
            "plan",
            "--model",
            "grok-4.5",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--cwd",
            str(cwd),
        ], "grok"
    raise ValueError(f"unknown worker: {worker}")


def run_worker(worker: str, prompt: str, cwd: Path, timeout: int) -> dict[str, Any]:
    cmd, backend = worker_command(worker, prompt, cwd)
    command_name = cmd[0]
    command_path = shutil.which(command_name)
    if not command_path:
        return {
            "status": "cli_missing",
            "worker": worker,
            "backend": backend,
            "command": command_name,
            "ok": False,
            "raw_output": "",
        }

    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=worker_env(worker),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    status = "executed"
    cancelled = False
    try:
        wrapper = json.loads(proc.stdout)
        cancelled = isinstance(wrapper, dict) and str(wrapper.get("stopReason") or "").lower() in {"cancelled", "canceled"}
    except (ValueError, TypeError):
        cancelled = False
    return {
        "status": "cancelled" if cancelled else status,
        "worker": worker,
        "backend": backend,
        "command": cmd[:2],
        "ok": proc.returncode == 0 and not cancelled,
        "returncode": proc.returncode,
        "raw_output": proc.stdout,
    }


def parse_symbols(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["gemini", "grok"], required=True)
    parser.add_argument("--mode", choices=["news", "official_research", "verification", "mixed"], default="news")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--task", required=True)
    parser.add_argument("--max-items", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--execute", action="store_true", help="Actually run the worker CLI.")
    parser.add_argument("--dry-run", action="store_true", help="Write prompt only. This is the default unless --execute is used.")
    args = parser.parse_args()

    blocked = guard_task(args.task)
    ensure_dir(RUNS_DIR)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{args.worker}_{slugify(args.mode)}"
    run_dir = RUNS_DIR / run_id
    workspace = run_dir / "workspace"
    ensure_dir(workspace)

    symbols = parse_symbols(args.symbols)
    prompt = build_prompt(args.worker, args.mode, symbols, args.task, args.max_items)
    request = {
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "worker": args.worker,
        "mode": args.mode,
        "symbols": symbols,
        "task": args.task,
        "max_items": args.max_items,
        "execute": bool(args.execute),
        "blocked_patterns": blocked,
    }
    write_json(run_dir / "request.json", request)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    if blocked:
        result = {
            "status": "refused_by_local_policy",
            "ok": False,
            "blocked_patterns": blocked,
            "raw_output": "",
        }
    elif args.execute:
        try:
            result = run_worker(args.worker, prompt, workspace, args.timeout)
        except subprocess.TimeoutExpired as exc:
            result = {"status": "timeout", "ok": False, "worker": args.worker, "raw_output": str(exc)}
        except Exception as exc:
            result = {"status": "failed_controlled", "ok": False, "worker": args.worker, "error": exc.__class__.__name__, "raw_output": str(exc)}
    else:
        result = {
            "status": "dry_run_prompt_written",
            "ok": True,
            "worker": args.worker,
            "raw_output": "",
        }

    raw_output = str(result.get("raw_output") or "")
    (run_dir / "raw_output.txt").write_text(raw_output, encoding="utf-8")
    result["run_id"] = run_id
    result["updated_at"] = utc_now_iso()
    result["prompt_path"] = str(run_dir / "prompt.txt")
    result["raw_output_path"] = str(run_dir / "raw_output.txt")
    write_json(run_dir / "result.json", result)

    print(
        "AGENT_ROUTER "
        f"status={result['status']} worker={args.worker} execute={str(args.execute).lower()} "
        f"run_dir={run_dir} ok={str(bool(result.get('ok'))).lower()}"
    )
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
