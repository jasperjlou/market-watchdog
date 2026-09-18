from __future__ import annotations

import importlib.util
from pathlib import Path
import json


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "ai_coordinator.py"
CONFIG = AGENT_ROOT / "config" / "ai_orchestration.json"


def load_module():
    spec = importlib.util.spec_from_file_location("ai_coordinator", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_with_workers(*, antigravity_enabled: bool, grok_enabled: bool) -> dict:
    return {
        "agents": {
            "google_antigravity": {"enabled": antigravity_enabled},
            "grok": {"enabled": grok_enabled},
        },
        "activation_policy": {
            "event_triggers": {
                "official_filing_or_earnings": {
                    "first_worker": "google_antigravity",
                    "confirm_worker": "grok",
                },
                "user_research_request": {
                    "first_worker": "codex",
                    "confirm_worker": "auto",
                },
            }
        },
    }


def test_public_template_keeps_external_ai_disabled_by_default() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    google = config["agents"]["google_antigravity"]

    assert google["enabled"] is False
    assert google["disabled_reason"] == "disabled_in_public_template"
    assert google["backend"] == "agy"
    assert google["network_route"] == "isolated_local_warp_proxy"
    assert config["agents"]["codex"]["enabled"] is False
    assert config["agents"]["grok"]["enabled"] is False


def test_google_environment_uses_only_the_dedicated_proxy(monkeypatch) -> None:
    module = load_module()
    proxy = "socks5h://127.0.0.1:40000"
    monkeypatch.setenv("AGY_PROXY_URL", proxy)
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.example:9999")
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")

    env = module.antigravity_env()

    assert env["HTTP_PROXY"] == proxy
    assert env["HTTPS_PROXY"] == proxy
    assert env["ALL_PROXY"] == proxy
    assert "AGY_PROXY_URL" not in env
    assert "GEMINI_API_KEY" not in env


def test_coordinator_skips_disabled_provider_but_keeps_enabled_confirmation() -> None:
    module = load_module()
    config = config_with_workers(antigravity_enabled=False, grok_enabled=True)

    workers = module.choose_workers(
        config,
        "official_filing_or_earnings",
        "Review the official filing",
    )

    assert workers == ["grok"]


def test_coordinator_falls_back_to_grok_when_default_provider_is_disabled() -> None:
    module = load_module()
    config = config_with_workers(antigravity_enabled=False, grok_enabled=True)

    workers = module.choose_workers(
        config,
        "user_research_request",
        "Scheduled market-watchdog coordination scan",
    )

    assert workers == ["grok"]


def test_coordinator_preserves_requested_order_when_both_providers_are_enabled() -> None:
    module = load_module()
    config = config_with_workers(antigravity_enabled=True, grok_enabled=True)

    workers = module.choose_workers(
        config,
        "official_filing_or_earnings",
        "Review the official filing",
    )

    assert workers == ["google_antigravity", "grok"]


def test_price_only_l1_uses_no_ai_worker_until_warning_review() -> None:
    module = load_module()
    config = config_with_workers(antigravity_enabled=True, grok_enabled=False)

    workers = module.choose_workers(
        config,
        "price_volume_anomaly",
        "Investigate the L1 price-only anomaly symbol:MU:bullish and find fresh news",
    )

    assert workers == []


def test_confirmed_price_warning_uses_google_when_it_is_the_only_provider() -> None:
    module = load_module()
    config = config_with_workers(antigravity_enabled=True, grok_enabled=False)

    workers = module.choose_workers(
        config,
        "price_volume_anomaly",
        "Urgently investigate MU after a confirmed L2 warning and produce a bounded review",
    )

    assert workers == ["google_antigravity"]


def test_warning_codex_review_falls_back_from_56_to_55(tmp_path: Path) -> None:
    module = load_module()
    calls: list[str] = []

    def runner(model: str, prompt: str, workspace: Path, timeout: int) -> dict:
        calls.append(model)
        if model == "gpt-5.6":
            return {"ok": False, "error": "authentication"}
        return {
            "ok": True,
            "payload": {
                "event": "reviewed event", "summary": "reviewed summary with enough context",
                "evidence_summary": ["official source checked"],
                "recommendation": "wait for confirmation and protect existing position",
                "invalidation": "price loses the confirmed support level",
                "next_check": "next 15 minute snapshot",
                "tomorrow_outlook": "neutral to positive with gap risk",
                "future_outlook": "depends on guidance over two to six weeks",
                "confidence": "medium", "citations": ["https://example.com/source"],
            },
        }

    result = module.run_codex_review_chain(
        ["gpt-5.6", "gpt-5.5"], "prompt", tmp_path, timeout=30, runner=runner,
    )

    assert calls == ["gpt-5.6", "gpt-5.5"]
    assert result["ok"] is True
    assert result["model"] == "gpt-5.5"
    assert result["payload"]["confidence"] == "medium"


def test_google_final_review_uses_antigravity_cli_and_ignores_api_key(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    payload = {
        "event": "已核实事件影响仍需观察",
        "summary": "价格与消息共同显示短期风险抬升",
        "evidence_summary": ["已检查公开来源"],
        "recommendation": "等待下一次确认并保护现有仓位",
        "invalidation": "价格重新站稳确认支撑区域",
        "next_check": "检查下一次十五分钟快照",
        "tomorrow_outlook": "预计震荡并关注缺口风险",
        "future_outlook": "未来取决于指引与需求变化",
        "confidence": "medium",
        "citations": ["https://example.com/source"],
    }
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps({"response": json.dumps(payload, ensure_ascii=False)}, ensure_ascii=False)

    def fake_run(command, **kwargs):
        calls.append(command)
        assert "GEMINI_API_KEY" not in kwargs["env"]
        return Result()

    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/opt/market-watchdog/antigravity/bin/agy")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_google_review_model("gemini-3.7-flash-low", "review prompt", tmp_path, 30)

    assert result["ok"] is True
    assert len(calls) == 1
    command = calls[0]
    assert command[0].endswith("agy")
    assert command[command.index("--model") + 1] == "gemini-3.7-flash-low"
    assert command[command.index("--effort") + 1] == "low"
    assert command[command.index("--mode") + 1] == "plan"
    assert "--sandbox" in command
    assert command[command.index("--output-format") + 1] == "json"
