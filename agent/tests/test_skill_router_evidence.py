from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "skill_router.py"


def load_module():
    spec = importlib.util.spec_from_file_location("skill_router_evidence", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_batch_collects_google_and_grok_without_losing_partial_results(monkeypatch, tmp_path: Path) -> None:
    module = load_module()

    def fake_run(worker: str, prompt: str, cwd: Path, timeout: int) -> dict:
        if worker == "gemini":
            return {"worker": worker, "ok": True, "status": "executed", "raw_output": "{}"}
        raise RuntimeError("temporary provider failure")

    monkeypatch.setattr(module, "run_worker", fake_run)
    results = module.run_worker_batch(["gemini", "grok"], "prompt", tmp_path, 30)

    assert [item["worker"] for item in results] == ["gemini", "grok"]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["status"] == "failed_controlled"


def test_normalizes_json_wrapped_worker_evidence() -> None:
    module = load_module()
    evidence = {
        "version": "1.0", "worker": "grok", "mode": "news", "collected_at": "2026-07-14T18:00:00Z",
        "items": [{
            "title": "SK hynix updates HBM capacity", "url": "https://issuer.example/hbm",
            "source_name": "issuer", "source_tier": "S0", "published_at": "2026-07-14T17:00:00Z",
            "symbols": ["skhy"], "topics": ["HBM"], "summary": "Capacity update",
            "direction": "bullish", "impact_horizon": "2_6w", "confidence": "high",
        }],
    }
    raw = json.dumps({"response": "```json\n" + json.dumps(evidence) + "\n```"})

    result = module.normalize_worker_evidence(raw, "grok", "proactive news")

    assert result["items"][0]["symbols"] == ["SKHY"]
    assert result["items"][0]["direction"] == "bullish"
    assert result["items"][0]["trade_instruction"] is False


def test_combined_evidence_deduplicates_same_url() -> None:
    module = load_module()
    payload = module.normalize_worker_evidence(json.dumps({"items": [{"title": "A", "url": "https://example/a", "source_name": "x", "source_tier": "S1", "symbols": ["MU"], "summary": "x", "confidence": "medium"}]}), "gemini", "news")
    combined = module.combine_worker_evidence([payload, payload], "news")
    assert len(combined["items"]) == 1
    assert combined["worker"] == "codex"


def test_prompt_requires_forward_direction_and_direct_urls() -> None:
    module = load_module()
    prompt = module.build_prompt("proactive memory news", "SKHY,MU", [])
    assert "do not wait for a price anomaly" not in prompt.lower()
    assert "direct URL" in prompt
    assert '"direction": "bullish|bearish|neutral|mixed"' in prompt
    assert '"impact_horizon"' in prompt


def test_cancelled_worker_output_is_not_treated_as_success(monkeypatch, tmp_path: Path) -> None:
    module = load_module()

    class Result:
        returncode = 0
        stdout = json.dumps({"text": "starting research", "stopReason": "Cancelled"})

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    result = module.run_worker("grok", "prompt", tmp_path, 30)
    assert result["ok"] is False
    assert result["status"] == "cancelled"


def test_broken_agy_does_not_retry_another_google_backend(monkeypatch, tmp_path: Path) -> None:
    module = load_module()

    class Failed:
        returncode = 1
        stdout = "agy failed"

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[0])
        return Failed()

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_worker("gemini", "prompt", tmp_path, 30)
    assert calls == ["agy"]
    assert result["ok"] is False
    assert result["backend"] == "agy"
    assert result["status"] == "executed"


def test_api_key_does_not_override_antigravity_cli(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-key")
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps({"items": []})

    def fake_run(command, **kwargs):
        calls.append(command)
        assert "GEMINI_API_KEY" not in kwargs["env"]
        return Result()

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/bin/agy")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_worker("gemini", "bounded prompt", tmp_path, 30)

    assert result["ok"] is True
    assert result["backend"] == "agy"
    assert [command[0] for command in calls] == ["agy"]


def test_only_gemini_worker_receives_dedicated_proxy(monkeypatch) -> None:
    module = load_module()
    proxy = "socks5h://127.0.0.1:40000"
    monkeypatch.setenv("AGY_PROXY_URL", proxy)

    gemini_env = module.worker_env("gemini")
    grok_env = module.worker_env("grok")

    assert gemini_env["HTTPS_PROXY"] == proxy
    assert gemini_env["ALL_PROXY"] == proxy
    assert "HTTPS_PROXY" not in grok_env
    assert "ALL_PROXY" not in grok_env


def test_interaction_output_text_handles_direct_and_nested_payloads() -> None:
    module = load_module()
    assert module.interaction_output_text({"output_text": '{"items": []}'}) == '{"items": []}'
    assert module.interaction_output_text({"outputs": [{"content": [{"text": '{"items": []}'}]}]}) == '{"items": []}'
