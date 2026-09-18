from __future__ import annotations

import importlib.util
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "authorization_check.py"


def load_module():
    spec = importlib.util.spec_from_file_location("authorization_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_env_loader_accepts_systemd_environment_file(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    legacy = tmp_path / "missing-runtime_env.sh"
    systemd_env = tmp_path / "runtime.env"
    systemd_env.write_text(
        "ALLOW_GMAIL_SEND=1\nMARKET_WATCHDOG_GMAIL_ALLOW_SEND=1\nGMAIL_DEFAULT_TO=present\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "RUNTIME_ENV_PATH", legacy)
    monkeypatch.setattr(module, "SYSTEMD_RUNTIME_ENV_PATH", systemd_env, raising=False)
    monkeypatch.delenv("ALLOW_GMAIL_SEND", raising=False)

    presence = module.load_runtime_env()

    assert presence["ALLOW_GMAIL_SEND"] is True
    assert presence["MARKET_WATCHDOG_GMAIL_ALLOW_SEND"] is True
    assert presence["GMAIL_DEFAULT_TO"] is True
    assert module.runtime_env_present() is True


def test_gmail_project_oauth_is_ready_without_codex_connector_profile(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    token = tmp_path / "gmail_token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GMAIL_TOKEN", str(token))
    monkeypatch.setenv("GMAIL_DEFAULT_TO", "present")
    monkeypatch.setenv("ALLOW_GMAIL_SEND", "1")
    monkeypatch.setenv("MARKET_WATCHDOG_GMAIL_ALLOW_SEND", "1")
    monkeypatch.setattr(module, "run_status", lambda *_args, **_kwargs: (True, "valid True"))
    monkeypatch.setattr(module, "GMAIL_PROFILE_PATH", tmp_path / "missing-profile.json")

    result = module.gmail_check({})

    assert result["ready"] is True
    assert result["status"] == "ready_gated"
    assert result["manual_action"] == ""
    assert result["checks"]["codex_connector_connected"] is False
    assert result["checks"]["runtime_keys_present"]["GMAIL_DEFAULT_TO"] is True


def test_cli_probe_rejects_stale_token_when_provider_reports_not_authenticated(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    token = tmp_path / "auth.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "shutil_which", lambda _: "/usr/local/bin/grok")
    monkeypatch.setattr(module, "run_status", lambda *_args, **_kwargs: (True, "You are not authenticated."))

    result = module.cli_check(
        "grok",
        [str(token)],
        probe_args=["grok", "models"],
        reject_phrases=["not authenticated"],
    )

    assert result["token_present"] is True
    assert result["probe_ok"] is False
    assert result["ready"] is False


def test_cli_probe_accepts_authenticated_model_listing(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    token = tmp_path / "auth.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "shutil_which", lambda _: "/usr/local/bin/grok")
    monkeypatch.setattr(module, "run_status", lambda *_args, **_kwargs: (True, "Available models: grok-build"))

    result = module.cli_check(
        "grok",
        [str(token)],
        probe_args=["grok", "models"],
        reject_phrases=["not authenticated"],
    )

    assert result["probe_ok"] is True
    assert result["ready"] is True


def test_disabled_worker_provider_does_not_block_startup_readiness(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    config_path = tmp_path / "ai_orchestration.json"
    config_path.write_text(
        '{"agents":{"google_antigravity":{"enabled":false},"grok":{"enabled":true}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "AI_ORCHESTRATION_PATH", config_path)

    def fake_cli_check(command, *_args, **_kwargs):
        ready = command == "grok"
        return {
            "available": True,
            "token_present": ready,
            "probe_ok": ready,
            "ready": ready,
            "manual_action": "login required" if not ready else "",
        }

    monkeypatch.setattr(module, "cli_check", fake_cli_check)

    checks = {item["id"]: item for item in module.worker_checks()}

    assert checks["antigravity"]["status"] == "disabled_by_config"
    assert checks["antigravity"]["ready"] is True
    assert checks["antigravity"]["manual_action"] == ""
    assert checks["antigravity"]["checks"]["configured_enabled"] is False
    assert checks["grok"]["status"] == "ready_gated"


def test_enabled_antigravity_uses_runtime_home_and_requires_cli_receipt(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    config_path = tmp_path / "ai_orchestration.json"
    config_path.write_text(
        '{"agents":{"google_antigravity":{"enabled":true},"grok":{"enabled":false}}}',
        encoding="utf-8",
    )
    home = tmp_path / "service-home"
    token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("{}", encoding="utf-8")
    state = tmp_path / "state"
    receipt = state / "provider_receipts" / "google_gemini.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        '{"status":"completed","provider":"google_antigravity_cli","subscription_baseline_only":true,"ai_credits_enabled":false}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "AI_ORCHESTRATION_PATH", config_path)
    monkeypatch.setattr(module, "STATE_DIR", state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-count")
    seen_tokens = []

    def fake_cli_check(command, token_paths, **_kwargs):
        seen_tokens.extend(token_paths)
        ready = command == "agy" and str(token) in token_paths
        return {
            "available": ready, "token_present": ready, "probe_ok": ready,
            "ready": ready, "manual_action": "" if ready else "login required",
        }

    monkeypatch.setattr(module, "cli_check", fake_cli_check)

    checks = {item["id"]: item for item in module.worker_checks()}

    assert str(token) in seen_tokens
    assert checks["antigravity"]["ready"] is True
    assert checks["antigravity"]["checks"]["cli_receipt_completed"] is True
    assert "gemini_api_key_present" not in checks["antigravity"]["checks"]


def test_api_key_alone_cannot_make_antigravity_ready(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    config_path = tmp_path / "ai_orchestration.json"
    config_path.write_text(
        '{"agents":{"google_antigravity":{"enabled":true},"grok":{"enabled":false}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "AI_ORCHESTRATION_PATH", config_path)
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("GEMINI_API_KEY", "api-key-must-be-ignored")
    monkeypatch.setattr(
        module,
        "cli_check",
        lambda *_args, **_kwargs: {
            "available": False, "token_present": False, "probe_ok": False,
            "ready": False, "manual_action": "login required",
        },
    )

    checks = {item["id"]: item for item in module.worker_checks()}

    assert checks["antigravity"]["ready"] is False
    assert "Antigravity CLI" in checks["antigravity"]["manual_action"]


def test_optional_ibkr_and_whatsapp_do_not_block_startup_readiness() -> None:
    module = load_module()
    checks = [
        {"id": "ibkr_gateway", "ready": False},
        {"id": "gmail", "ready": True},
        {"id": "whatsapp", "ready": False},
        {"id": "grok", "ready": True},
    ]
    policy = {
        "startup_required_checks": ["gmail", "grok"],
        "startup_optional_checks": ["ibkr_gateway", "whatsapp"],
    }

    result = module.startup_readiness(checks, policy)

    assert result["ready"] is True
    assert result["required_failed"] == []
    assert result["optional_failed"] == ["ibkr_gateway", "whatsapp"]


def test_whatsapp_requires_credentials_and_both_send_gates(monkeypatch) -> None:
    module = load_module()
    keys = [
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_RECIPIENT",
        "WHATSAPP_TEMPLATE_NAME",
        "ALLOW_WHATSAPP_SEND",
        "MARKET_WATCHDOG_WHATSAPP_ALLOW_SEND",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    missing = module.whatsapp_check({})
    assert missing["ready"] is False
    assert missing["checks"]["official_cloud_api_only"] is True

    for key in keys:
        monkeypatch.setenv(key, "1")
    ready = module.whatsapp_check({key: True for key in keys})
    assert ready["ready"] is True
    assert ready["status"] == "ready_gated"


def test_wechat_48001_is_reported_as_stable_inbound_only(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    (tmp_path / "state").mkdir()
    (tmp_path / "agent" / "state").mkdir(parents=True)
    (tmp_path / "agent" / "state" / "wechat_channel_state.json").write_text(
        '{"status":"api_unauthorized","last_error_code":48001}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "APP_DIR", tmp_path)
    monkeypatch.setattr(module, "port_open", lambda _port: True)
    monkeypatch.setattr(module, "proc_contains", lambda _value: True)
    for key in (
        "WECHAT_APP_ID",
        "WECHAT_APP_SECRET",
        "WECHAT_OPENID",
        "WECHAT_CALLBACK_TOKEN",
        "ALLOW_WECHAT_OFFICIAL_SEND",
        "MARKET_WATCHDOG_WECHAT_ALLOW_SEND",
    ):
        monkeypatch.setenv(key, "1")

    result = module.wechat_check({})

    assert result["ready"] is True
    assert result["status"] == "ready_inbound_only"
    assert result["manual_action"] == ""
    assert result["checks"]["official_send_authorized"] is False


def test_market_data_chain_is_ready_when_yfinance_fallback_exists(monkeypatch) -> None:
    module = load_module()
    payload = {
        "readonly": True,
        "providers": {
            "moomoo": {"module_present": True, "port_open": False},
            "ibkr": {"module_present": True, "port_open": False},
            "yfinance": {"module_present": True},
        },
    }
    monkeypatch.setattr(module, "run_status", lambda *_args, **_kwargs: (True, __import__("json").dumps(payload)))

    result = module.market_data_check()

    assert result["ready"] is True
    assert result["status"] == "ready_with_fallback"
    assert result["checks"]["readonly"] is True
