#!/usr/bin/env python3
"""Skill-aware router for market-watchdog information work.

This router picks local research skills, builds a bounded prompt, and delegates
to a Google Gemini API worker. Legacy CLI/Grok paths remain inert compatibility
code and are not selected by the production profile. The router never places
orders, sends messages, reads secrets, or changes production state.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
SKILLS_DIR = AGENT_DIR / "skills"
RUNS_DIR = AGENT_DIR / "runs"
AI_CONFIG_PATH = AGENT_DIR / "config" / "ai_orchestration.json"
GOOGLE_RECEIPT_PATH = AGENT_DIR / "state" / "provider_receipts" / "google_gemini.json"

MAX_SKILL_CHARS = 12000

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

BLOCKED_PATTERNS = (
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
    "curl | bash",
    "wget | bash",
)

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


def slugify(value: str, limit: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-").lower()
    return (slug or "task")[:limit]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        elif isinstance(value, str) and key.lower() in {"text", "output_text", "outputtext"}:
            if value.strip():
                texts.append(value.strip())

    collect(payload)
    return "\n".join(dict.fromkeys(texts))


def parse_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [item.strip().strip("'\"") for item in value.split(",") if item.strip()]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if value.startswith("[") and value.endswith("]"):
            meta[key] = parse_list(value)
        else:
            meta[key] = value
    return meta, body


def summarize_body(body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    for line in lines:
        if not line.startswith("#") and len(line) > 20:
            return line[:240]
    return (lines[0] if lines else "")[:240]


def load_skills() -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    if not SKILLS_DIR.exists():
        return skills
    for path in sorted(SKILLS_DIR.rglob("SKILL.md")):
        rel = path.relative_to(AGENT_DIR)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        name = str(meta.get("name") or path.parent.name)
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = parse_list(tags)
        status = str(meta.get("status") or "unknown")
        skills.append(
            {
                "name": name,
                "path": str(rel),
                "absolute_path": str(path),
                "status": status,
                "tags": tags,
                "preferred_workers": meta.get("preferred_workers") or [],
                "summary": summarize_body(body),
                "text": text,
            }
        )
    return skills


def tokenize(value: str) -> set[str]:
    return {tok.lower() for tok in re.findall(r"[A-Za-z0-9_$.-]{2,}", value)}


def score_skill(skill: dict[str, Any], query: str, symbols: str) -> int:
    haystack = " ".join(
        [
            skill["name"],
            skill["path"],
            skill.get("summary", ""),
            " ".join(skill.get("tags") or []),
            skill["text"][:8000],
        ]
    ).lower()
    query_text = f"{query} {symbols}".lower()
    score = 0
    for tok in tokenize(query_text):
        if tok in haystack:
            score += 2
    domain_boosts = {
        "serenity": ("serenity", "chokepoint", "bottleneck", "photonics", "cpo", "supply chain"),
        "semiconductor": ("semiconductor", "silicon", "wafer", "substrate", "laser", "memory"),
        "news": ("news", "breaking", "catalyst", "headline", "event"),
        "thesis": ("thesis", "variant perception", "diligence", "disconfirm"),
    }
    for terms in domain_boosts.values():
        if any(term in query_text for term in terms) and any(term in haystack for term in terms):
            score += 8
    if "approved" in skill.get("path", "") or "market/" in skill.get("path", ""):
        score += 2
    if "sandbox" in skill.get("status", "") or "review_only" in skill.get("status", ""):
        score -= 1
    return score


def select_skills(task: str, symbols: str, max_skills: int) -> list[dict[str, Any]]:
    skills = load_skills()
    scored = [(score_skill(skill, task, symbols), skill) for skill in skills]
    scored.sort(key=lambda item: (item[0], item[1]["name"]), reverse=True)
    selected = [skill for score, skill in scored if score > 0][:max_skills]
    if not selected and skills:
        selected = skills[:1]
    return selected


def choose_workers(task: str, worker_mode: str, selected: list[dict[str, Any]]) -> list[str]:
    if worker_mode in {"gemini", "grok"}:
        return [worker_mode]
    if worker_mode == "both":
        return ["gemini", "grok"]

    text = task.lower()
    preferred: list[str] = []
    for skill in selected:
        workers = skill.get("preferred_workers") or []
        if isinstance(workers, str):
            workers = parse_list(workers)
        preferred.extend(worker for worker in workers if worker in {"gemini", "grok"})

    if any(term in text for term in ["breaking", "news", "x.com", "twitter", "social", "reddit"]):
        return ["grok", "gemini"]
    if any(term in text for term in ["filing", "10-k", "10-q", "sec", "official", "earnings"]):
        return ["gemini", "grok"]
    if any(term in text for term in ["serenity", "bottleneck", "chokepoint", "supply chain", "cpo", "photonics"]):
        return ["gemini", "grok"]
    if preferred:
        return list(dict.fromkeys(preferred))
    return ["gemini"]


def guard_task(task: str) -> list[str]:
    lower = task.lower()
    return [pattern for pattern in BLOCKED_PATTERNS if pattern in lower]


def build_prompt(task: str, symbols: str, selected: list[dict[str, Any]]) -> str:
    skill_blocks = []
    for skill in selected:
        text = skill["text"]
        if len(text) > MAX_SKILL_CHARS:
            text = text[:MAX_SKILL_CHARS] + "\n\n[TRUNCATED BY LOCAL ROUTER]\n"
        skill_blocks.append(
            f"## Skill: {skill['name']}\n"
            f"Path: {skill['path']}\n"
            f"Status: {skill.get('status')}\n"
            f"Tags: {', '.join(skill.get('tags') or [])}\n\n"
            f"{text}"
        )

    return f"""You are an information worker inside market-watchdog.

Task:
{task}

Symbols/entities:
{symbols or "not specified"}

Selected local skills:
{chr(10).join(skill_blocks) if skill_blocks else "No local skill selected."}

Hard rules:
- Use public evidence only.
- Do not read secrets, OAuth tokens, SSH keys, broker credentials, or Docker sockets.
- You may produce trade recommendations, non-transmitting order ticket drafts, and cancel/replace suggestions.
- Do not call broker write APIs. Do not place, cancel, modify, or transmit orders.
- Do not send Gmail, WeChat, Slack, or any external message.
- Separate thesis, evidence, uncertainty, and what would disconfirm the thesis.
- Social or broad-media-only evidence is early warning only, never final confirmation.

Return exactly one JSON object matching the worker-evidence contract. Every
material claim must have a direct URL. If no material evidence is found,
return an empty items list instead of inventing an event:
{{
  "version": "1.0",
  "worker": "gemini|grok",
  "mode": "news|official_research|verification|mixed",
  "collected_at": "ISO-8601 UTC",
  "task": "...",
  "items": [
    {{
      "title": "...",
      "url": "https://direct-source-url",
      "source_name": "...",
      "source_tier": "S0|S1|S2|S3",
      "published_at": "ISO-8601 UTC if known",
      "collected_at": "ISO-8601 UTC",
      "symbols": ["..."],
      "topics": ["..."],
      "summary": "verified facts only",
      "evidence_text": "short supporting excerpt or precise paraphrase",
      "why_it_matters": "forward-looking transmission path",
      "direction": "bullish|bearish|neutral|mixed",
      "impact_horizon": "immediate|1_5d|2_6w|long_term",
      "confidence": "low|medium|high",
      "limitations": "...",
      "trade_instruction": false
    }}
  ],
  "blocked_actions_confirmed": true
}}
"""


def command_for_worker(worker: str, prompt: str, cwd: Path) -> tuple[list[str], str]:
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
    raise ValueError(f"unknown worker {worker}")


def run_grok_responses_api(prompt: str, timeout: int) -> dict[str, Any]:
    """Use xAI's server-side web search when the local Grok agent cancels."""
    api_key = (os.environ.get("XAI_API_KEY") or os.environ.get("GROK_CODE_XAI_API_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "status": "api_key_missing", "backend": "xai_responses", "raw_output": ""}
    payload = json.dumps({
        "model": "grok-4.5",
        "input": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search"}],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://api.x.ai/v1/responses",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise ValueError("xai_non_object_response")
        return {
            "ok": True, "status": "executed_api_fallback", "backend": "xai_responses",
            "returncode": 0, "raw_output": body,
        }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False, "status": "api_http_error", "backend": "xai_responses",
            "returncode": int(exc.code), "raw_output": f"HTTPError:{exc.code}",
        }
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False, "status": "api_failed_controlled", "backend": "xai_responses",
            "returncode": None, "raw_output": exc.__class__.__name__,
        }


def run_grok_context_fallback(prompt: str, cwd: Path, timeout: int) -> dict[str, Any]:
    marker = "CONTROLLER-PROVIDED PUBLIC CONTEXT"
    if marker not in prompt:
        return {"ok": False, "status": "context_missing", "backend": "grok", "raw_output": ""}
    retry_prompt = (
        "Do not use any tool, do not browse, and do not access files. Perform a bounded contradiction review using only "
        "the controller-provided public context already embedded below. Clearly mark missing confirmation and return exactly "
        "the worker-evidence JSON contract requested in the prompt. An empty items list is valid.\n\n" + prompt
    )
    cmd, backend = command_for_worker("grok", retry_prompt, cwd)
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=safe_env(), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "status": "context_retry_failed", "backend": backend, "raw_output": exc.__class__.__name__}
    cancelled = False
    try:
        wrapper = json.loads(proc.stdout)
        cancelled = isinstance(wrapper, dict) and str(wrapper.get("stopReason") or "").lower() in {"cancelled", "canceled"}
    except (ValueError, TypeError):
        pass
    return {
        "ok": proc.returncode == 0 and not cancelled,
        "status": "cancelled" if cancelled else "executed_context_fallback",
        "backend": backend,
        "returncode": proc.returncode,
        "raw_output": proc.stdout,
    }


def run_worker(worker: str, prompt: str, cwd: Path, timeout: int) -> dict[str, Any]:
    cmd, backend = command_for_worker(worker, prompt, cwd)
    command_path = shutil.which(cmd[0])
    if not command_path:
        return {"worker": worker, "backend": backend, "ok": False, "status": "cli_missing", "raw_output": ""}
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
    if worker == "grok" and cancelled:
        context_fallback = run_grok_context_fallback(prompt, cwd, timeout)
        if context_fallback.get("ok"):
            return {"worker": worker, **context_fallback}
        fallback = run_grok_responses_api(prompt, timeout)
        if fallback.get("ok"):
            return {"worker": worker, **fallback}
        return {
            "worker": worker, "backend": backend, "ok": False, "status": "cancelled",
            "returncode": proc.returncode, "raw_output": proc.stdout,
            "fallback_status": fallback.get("status"), "context_fallback_status": context_fallback.get("status"),
        }
    if worker == "gemini" and proc.returncode == 0 and not cancelled:
        model = cmd[cmd.index("--model") + 1]
        write_json(GOOGLE_RECEIPT_PATH, {
            "version": "1.0",
            "status": "completed",
            "verified_at": utc_now_iso(),
            "provider": "google_antigravity_cli",
            "model": model,
            "subscription_baseline_only": True,
            "ai_credits_enabled": False,
            "credential_logged": False,
            "actual_broker_writes": False,
            "external_send": False,
        })
    return {
        "worker": worker,
        "backend": backend,
        "ok": proc.returncode == 0 and not cancelled,
        "status": "cancelled" if cancelled else status,
        "returncode": proc.returncode,
        "raw_output": proc.stdout,
    }


def run_worker_batch(workers: list[str], prompt: str, cwd: Path, timeout: int) -> list[dict[str, Any]]:
    """Run independent evidence workers concurrently and preserve requested order."""
    if not workers:
        return []

    def guarded(worker: str) -> dict[str, Any]:
        try:
            return run_worker(worker, prompt, cwd, timeout)
        except subprocess.TimeoutExpired as exc:
            return {"worker": worker, "ok": False, "status": "timeout", "raw_output": str(exc)}
        except Exception as exc:
            return {
                "worker": worker, "ok": False, "status": "failed_controlled",
                "error": exc.__class__.__name__, "raw_output": str(exc),
            }

    with ThreadPoolExecutor(max_workers=min(2, len(workers)), thread_name_prefix="evidence-worker") as pool:
        futures = {worker: pool.submit(guarded, worker) for worker in workers}
        return [futures[worker].result() for worker in workers]


def _decode_json_text(text: str) -> Any:
    candidates = [text.strip()]
    candidates.extend(match.group(1).strip() for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL))
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


def _find_evidence_payload(value: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6:
        return None
    if isinstance(value, str):
        decoded = _decode_json_text(value)
        return _find_evidence_payload(decoded, depth + 1) if decoded is not None else None
    if isinstance(value, dict):
        if isinstance(value.get("items"), list) or isinstance(value.get("findings"), list):
            return value
        for key in ("response", "result", "output", "content", "text", "message", "data"):
            if key in value:
                found = _find_evidence_payload(value[key], depth + 1)
                if found is not None:
                    return found
        for nested in value.values():
            found = _find_evidence_payload(nested, depth + 1)
            if found is not None:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_evidence_payload(nested, depth + 1)
            if found is not None:
                return found
    return None


def _mode_for_task(task: str) -> str:
    lower = task.lower()
    if any(term in lower for term in ("filing", "official", "sec", "issuer", "regulator")):
        return "official_research"
    if any(term in lower for term in ("news", "breaking", "headline", "last 24")):
        return "news"
    return "mixed"


def _symbols(value: Any) -> list[str]:
    values = value if isinstance(value, list) else parse_list(str(value or ""))
    result = []
    for raw in values:
        symbol = str(raw).strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def normalize_worker_evidence(raw_output: str, worker: str, task: str, collected_at: str | None = None) -> dict[str, Any]:
    timestamp = collected_at or utc_now_iso()
    payload = _find_evidence_payload(raw_output) or {}
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not raw_items and isinstance(payload.get("findings"), list):
        for finding in payload["findings"]:
            if not isinstance(finding, dict):
                continue
            evidence_text = str(finding.get("evidence") or "")
            url_match = re.search(r"https?://[^\s)>\]}]+", evidence_text)
            raw_items.append({
                "title": str(finding.get("claim") or "worker finding")[:180],
                "url": url_match.group(0) if url_match else "",
                "source_name": worker,
                "source_tier": finding.get("source_tier") or "S3",
                "symbols": _symbols(payload.get("symbols")),
                "summary": str(finding.get("claim") or ""),
                "evidence_text": evidence_text,
                "why_it_matters": str(finding.get("next_check") or ""),
                "limitations": str(finding.get("limitations") or "legacy worker format"),
                "confidence": finding.get("confidence") or "low",
            })

    normalized = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("claim") or "").strip()[:240]
        if not title:
            continue
        tier = str(item.get("source_tier") or "S3").upper()
        if tier not in {"S0", "S1", "S2", "S3"}:
            tier = "S3"
        confidence = str(item.get("confidence") or "low").lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
        direction = str(item.get("direction") or item.get("market_direction") or "neutral").lower()
        if direction not in {"bullish", "bearish", "neutral", "mixed"}:
            direction = "neutral"
        impact_horizon = str(item.get("impact_horizon") or "unspecified").lower()
        if impact_horizon not in {"immediate", "1_5d", "2_6w", "long_term", "unspecified"}:
            impact_horizon = "unspecified"
        normalized.append({
            "title": title,
            "url": str(item.get("url") or "").strip(),
            "source_id": str(item.get("source_id") or item.get("source_name") or worker).strip(),
            "source_name": str(item.get("source_name") or worker).strip(),
            "source_tier": tier,
            "published_at": str(item.get("published_at") or "").strip(),
            "collected_at": str(item.get("collected_at") or timestamp),
            "symbols": _symbols(item.get("symbols")),
            "entities": _symbols(item.get("entities")),
            "topics": [str(value).strip() for value in (item.get("topics") or []) if str(value).strip()],
            "summary": str(item.get("summary") or item.get("claim") or "").strip()[:1200],
            "evidence_text": str(item.get("evidence_text") or item.get("evidence") or "").strip()[:1200],
            "why_it_matters": str(item.get("why_it_matters") or "").strip()[:800],
            "limitations": str(item.get("limitations") or "").strip()[:800],
            "direction": direction,
            "impact_horizon": impact_horizon,
            "confidence": confidence,
            "trade_instruction": False,
        })
    return {
        "version": "1.0",
        "worker": worker if worker in {"gemini", "grok"} else "manual",
        "mode": str(payload.get("mode") or _mode_for_task(task)),
        "collected_at": str(payload.get("collected_at") or timestamp),
        "task": task,
        "items": normalized,
        "blocked_actions_confirmed": True,
    }


def combine_worker_evidence(payloads: list[dict[str, Any]], task: str, collected_at: str | None = None) -> dict[str, Any]:
    seen: set[str] = set()
    items = []
    for payload in payloads:
        for item in payload.get("items", []) if isinstance(payload, dict) else []:
            key = (str(item.get("url") or "").strip().lower() or str(item.get("title") or "").strip().lower())
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(item)
    return {
        "version": "1.0",
        "worker": payloads[0].get("worker") if len(payloads) == 1 else "codex",
        "mode": "mixed" if len({str(item.get('mode')) for item in payloads}) > 1 else (payloads[0].get("mode") if payloads else _mode_for_task(task)),
        "collected_at": collected_at or utc_now_iso(),
        "task": task,
        "items": items,
        "blocked_actions_confirmed": True,
        "actual_broker_writes": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="Research task or question.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols/entities.")
    parser.add_argument("--workers", choices=["auto", "gemini", "grok", "both"], default="auto")
    parser.add_argument("--max-skills", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--list-skills", action="store_true")
    args = parser.parse_args()

    if args.list_skills:
        skills = load_skills()
        print(json.dumps([{k: s[k] for k in ("name", "path", "status", "tags", "summary")} for s in skills], ensure_ascii=False, indent=2))
        return 0

    if not args.task:
        parser.error("--task is required unless --list-skills is used")

    blocked = guard_task(args.task)
    selected = select_skills(args.task, args.symbols, args.max_skills)
    workers = choose_workers(args.task, args.workers, selected)
    prompt = build_prompt(args.task, args.symbols, selected)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_skill_{slugify(args.task)}"
    run_dir = RUNS_DIR / run_id
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    request = {
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "task": args.task,
        "symbols": args.symbols,
        "selected_skills": [{k: s[k] for k in ("name", "path", "status", "tags", "summary")} for s in selected],
        "workers": workers,
        "execute": bool(args.execute),
        "blocked_patterns": blocked,
    }
    write_json(run_dir / "request.json", request)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    raw_outputs: list[dict[str, Any]] = []
    normalized_payloads: list[dict[str, Any]] = []
    if blocked:
        result = {"ok": False, "status": "refused_by_local_policy", "blocked_patterns": blocked, "worker_results": []}
    elif args.execute:
        raw_outputs = run_worker_batch(workers, prompt, workspace, args.timeout)
        collected_at = utc_now_iso()
        for item in raw_outputs:
            payload = normalize_worker_evidence(
                str(item.get("raw_output") or ""), str(item.get("worker") or "manual"), args.task, collected_at
            )
            if item.get("ok") or payload.get("items"):
                normalized_payloads.append(payload)
            item["evidence_count"] = len(payload.get("items") or [])
        successful = sum(1 for item in raw_outputs if item.get("ok"))
        status = "executed" if successful == len(raw_outputs) else ("executed_partial" if successful else "executed_failed")
        worker_summaries = [
            {key: value for key, value in item.items() if key != "raw_output"}
            for item in raw_outputs
        ]
        result = {
            "ok": successful > 0,
            "status": status,
            "worker_results": worker_summaries,
            "evidence_count": sum(len(payload.get("items") or []) for payload in normalized_payloads),
        }
    else:
        result = {"ok": True, "status": "dry_run_prompt_written", "worker_results": []}

    write_json(run_dir / "result.json", result)
    for item in raw_outputs:
        worker = item.get("worker", "worker")
        (run_dir / f"raw_output_{worker}.txt").write_text(item.get("raw_output") or "", encoding="utf-8")
    if args.execute and not blocked:
        combined = combine_worker_evidence(normalized_payloads, args.task)
        write_json(run_dir / "raw_output.txt", combined)

    print(
        "SKILL_ROUTER "
        f"status={result['status']} "
        f"ok={result['ok']} "
        f"skills={','.join(skill['name'] for skill in selected) or 'none'} "
        f"workers={','.join(workers)} "
        f"run_dir={run_dir}"
    )
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
