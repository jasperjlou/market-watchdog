from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "scripts" / "communication_gateway.py"


def load_module():
    spec = importlib.util.spec_from_file_location("communication_gateway", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_successful_channel_is_recorded_as_partial_delivery() -> None:
    module = load_module()
    results = {
        "gmail": {"ok": True},
        "whatsapp": {"ok": False, "detail": "missing_meta_credentials"},
    }

    outcome = module.classify_dispatch_outcome(results, allow_send=True, dry_run=False)

    assert outcome == "sent_partial"


def test_all_failed_channels_remain_failed() -> None:
    module = load_module()
    results = {"gmail": {"ok": False}, "whatsapp": {"ok": False}}

    assert module.classify_dispatch_outcome(results, allow_send=True, dry_run=False) == "failed"


def test_compact_market_messages_keep_their_exact_body_contract() -> None:
    module = load_module()
    policy = {"append_safety_tag": True, "required_disclaimer": "人工确认"}
    alert = "相关事件说明：异动待确认\n价格趋势：短线偏强\n操作建议：不追高"
    daily = "今日回顾：平稳\n只提供提醒，系统不自动下单。"
    weekly = "本周回顾：平稳\n只提供提醒，系统不自动下单。"

    assert module.append_safety_tag(alert, policy, "market_review_L2") == alert
    assert module.append_safety_tag(daily, policy, "daily_brief") == daily
    assert module.append_safety_tag(weekly, policy, "weekly_brief") == weekly
    assert module.append_safety_tag("普通消息", policy, "authorization_needed").endswith("人工确认")


def test_partial_delivery_is_archived_without_resending_successful_channel(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent"
    failed = tmp_path / "failed"
    for path in (outbox, sent, failed):
        path.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"append_safety_tag":false,"blocked_content":[],"combined_channels":["gmail","whatsapp"],'
        '"channels":{"gmail":{"enabled":true},"whatsapp":{"enabled":true}}}',
        encoding="utf-8",
    )
    message = outbox / "message.json"
    message.write_text(
        '{"status":"queued","channel":"both","subject":"health","body":"degraded"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "OUTBOX_DIR", outbox)
    monkeypatch.setattr(module, "SENT_DIR", sent)
    monkeypatch.setattr(module, "FAILED_DIR", failed)
    monkeypatch.setattr(module, "POLICY_PATH", policy)
    monkeypatch.setattr(module, "RUNTIME_ENV_PATH", tmp_path / "missing-env")
    monkeypatch.setattr(module, "run_cmd", lambda cmd, timeout=60: {"ok": "gmail_real_sender" in " ".join(cmd)})
    monkeypatch.setattr(module.os, "environ", {
        "MARKET_WATCHDOG_GMAIL_ALLOW_SEND": "1",
        "MARKET_WATCHDOG_WHATSAPP_ALLOW_SEND": "1",
        "ALLOW_WHATSAPP_SEND": "1",
    })

    module.dispatch_one(message, allow_send=True, dry_run=False)

    archived = module.load_json(sent / "message.json", {})
    assert archived["status"] == "sent_partial"
    assert archived["dispatch"]["delivered_channels"] == ["gmail"]
    assert archived["dispatch"]["failed_channels"] == ["whatsapp"]
    assert not message.exists()


def test_combined_route_uses_gmail_only_until_whatsapp_is_ready(monkeypatch) -> None:
    module = load_module()
    policy = {
        "combined_channels": ["gmail", "whatsapp"],
        "channels": {
            "gmail": {"enabled": True},
            "whatsapp": {"enabled": False, "auto_enable_when_ready": True},
            "wechat": {"enabled": False},
        },
    }
    monkeypatch.setattr(module.os, "environ", {})

    assert module.resolve_channels("both", policy) == ["gmail"]


def test_combined_route_auto_enables_whatsapp_only_with_all_credentials_and_gates(monkeypatch) -> None:
    module = load_module()
    policy = {
        "combined_channels": ["gmail", "whatsapp"],
        "channels": {
            "gmail": {"enabled": True},
            "whatsapp": {"enabled": False, "auto_enable_when_ready": True},
        },
    }
    monkeypatch.setattr(
        module.os,
        "environ",
        {key: "1" for key in module.WHATSAPP_REQUIRED_ENV},
    )

    assert module.resolve_channels("both", policy) == ["gmail", "whatsapp"]


def test_gmail_auto_enables_only_with_token_recipient_and_both_gates(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    token_path = tmp_path / "gmail_token.json"
    policy = {
        "channels": {
            "gmail": {"enabled": False, "auto_enable_when_ready": True},
        },
    }
    monkeypatch.setattr(module, "DEFAULT_GMAIL_TOKEN_PATH", token_path)
    monkeypatch.setattr(module.os, "environ", {})

    assert module.channel_enabled(policy, "gmail") is False

    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        module.os,
        "environ",
        {
            "GMAIL_DEFAULT_TO": "receiver@example.com",
            "ALLOW_GMAIL_SEND": "1",
            "MARKET_WATCHDOG_GMAIL_ALLOW_SEND": "1",
        },
    )

    assert module.channel_enabled(policy, "gmail") is True


def test_wechat_auto_enables_only_with_credentials_and_both_gates(monkeypatch) -> None:
    module = load_module()
    policy = {
        "channels": {
            "wechat": {"enabled": False, "auto_enable_when_ready": True},
        },
    }
    monkeypatch.setattr(module.os, "environ", {})
    assert module.channel_enabled(policy, "wechat") is False

    monkeypatch.setattr(
        module.os,
        "environ",
        {
            "WECHAT_APP_ID": "present",
            "WECHAT_APP_SECRET": "present",
            "WECHAT_OPENID": "present",
            "ALLOW_WECHAT_OFFICIAL_SEND": "1",
            "MARKET_WATCHDOG_WECHAT_ALLOW_SEND": "1",
        },
    )
    assert module.channel_enabled(policy, "wechat") is True


def test_specific_disabled_channel_is_not_resolved(monkeypatch) -> None:
    module = load_module()
    policy = {"channels": {"gmail": {"enabled": False, "auto_enable_when_ready": True}}}
    monkeypatch.setattr(module.os, "environ", {})

    assert module.resolve_channels("gmail", policy) == []


def test_unavailable_channel_defers_message_without_losing_it(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent"
    failed = tmp_path / "failed"
    for path in (outbox, sent, failed):
        path.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({
            "append_safety_tag": False,
            "blocked_content": [],
            "channels": {"gmail": {"enabled": False, "auto_enable_when_ready": True}},
        }),
        encoding="utf-8",
    )
    message = outbox / "message.json"
    message.write_text(
        '{"status":"queued","channel":"gmail","subject":"test","body":"body"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "OUTBOX_DIR", outbox)
    monkeypatch.setattr(module, "SENT_DIR", sent)
    monkeypatch.setattr(module, "FAILED_DIR", failed)
    monkeypatch.setattr(module, "POLICY_PATH", policy)
    monkeypatch.setattr(module, "RUNTIME_ENV_PATH", tmp_path / "missing-env")
    monkeypatch.setattr(module.os, "environ", {})

    result = module.dispatch_one(message, allow_send=True, dry_run=False)

    retained = module.load_json(message, {})
    assert result["deferred"] is True
    assert retained["status"] == "queued"
    assert retained["dispatch"]["detail"] == "no_ready_channels"
    assert not list(sent.iterdir())
    assert not list(failed.iterdir())


def test_public_policy_keeps_all_external_channels_disabled() -> None:
    policy_path = AGENT_ROOT / "config" / "communication_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))

    assert policy["communication_enabled"] is False
    assert policy["auto_external_send_allowed"] is False
    assert policy["combined_channels"] == ["telegram"]
    assert policy["message_style"]["routing"]["daily_brief"] == "gmail"
    assert policy["message_style"]["routing"]["weekly_brief"] == "gmail"
    assert policy["message_style"]["routing"]["market_review_L2"] == "telegram"
    assert policy["channel_roles"]["telegram"]["interactive"] == "same_channel_direct_reply"
    assert policy["channel_roles"]["wechat"]["async_result_command"] == "结果"
    for channel in ("gmail", "wechat", "whatsapp", "telegram"):
        assert policy["channels"][channel]["enabled"] is False
        assert policy["channels"][channel]["auto_enable_when_ready"] is False
        assert policy["channels"][channel]["activation_state"] == "disabled_in_public_template"


def test_enqueue_enforces_report_and_urgent_channel_roles(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps({
            "append_safety_tag": False,
            "blocked_content": [],
            "allowed_message_kinds": ["daily_brief", "weekly_brief", "market_review_L2", "wechat_ai_reply"],
            "combined_channels": ["telegram"],
            "channels": {
                "gmail": {"allowed_kinds": ["daily_brief", "weekly_brief"]},
                "telegram": {"allowed_kinds": ["market_review_L2", "wechat_ai_reply"]},
            },
            "message_style": {"routing": {
                "daily_brief": "gmail",
                "weekly_brief": "gmail",
                "market_review_L2": "telegram",
            }},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "POLICY_PATH", policy_path)
    monkeypatch.setattr(module, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(module, "SENT_DIR", tmp_path / "sent")
    monkeypatch.setattr(module, "FAILED_DIR", tmp_path / "failed")

    daily = module.enqueue("telegram", "daily_brief", "日报", "正文", "normal")
    urgent = module.enqueue("gmail", "market_review_L2", "预警", "正文", "high")
    reply = module.enqueue("telegram", "wechat_ai_reply", "问答", "正文", "normal")

    assert daily["status"] == "queued" and daily["channel"] == "gmail"
    assert urgent["status"] == "queued" and urgent["channel"] == "telegram"
    assert reply["status"] == "queued" and reply["channel"] == "telegram"


def test_public_risk_policy_keeps_money_actions_disabled() -> None:
    risk_policy = (AGENT_ROOT.parent / "config" / "risk_controls.yaml").read_text(encoding="utf-8")

    assert "live_order_allowed: false" in risk_policy
    assert "real_money_action_allowed: false" in risk_policy
    assert "broker_connect_allowed: false" in risk_policy
    assert "gmail_send_allowed: false" in risk_policy
    assert "wechat_real_send_allowed: false" in risk_policy


def test_wechat_45015_opens_backoff_and_new_inbound_message_clears_it(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    channel_state = tmp_path / "wechat_channel_state.json"
    callback_state = tmp_path / "callback_state.json"
    monkeypatch.setattr(module, "WECHAT_CHANNEL_STATE_PATH", channel_state)
    monkeypatch.setattr(module, "WECHAT_CALLBACK_STATE_PATH", callback_state)
    now = datetime(2026, 7, 14, 22, 0, tzinfo=timezone.utc)

    module.record_wechat_result(
        {"ok": False, "output": "wechat_send_refused errcode=45015 errmsg=response out of time limit"},
        now=now,
    )
    blocked = module.wechat_send_allowed(now=now)
    assert blocked["allowed"] is False
    assert blocked["reason"] == "response_window_closed"

    callback_state.write_text(
        '{"updated_at":1784067000,"last_type":"xml","last_message_preview":"hello"}',
        encoding="utf-8",
    )
    reopened = module.wechat_send_allowed(now=datetime.fromtimestamp(1784067001, tz=timezone.utc))
    assert reopened["allowed"] is True
    assert reopened["reason"] == "recent_inbound_message"


def test_wechat_48001_permanently_suppresses_proactive_send(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    channel_state = tmp_path / "wechat_channel_state.json"
    monkeypatch.setattr(module, "WECHAT_CHANNEL_STATE_PATH", channel_state)
    now = datetime(2026, 8, 22, 22, 30, tzinfo=timezone.utc)

    module.record_wechat_result(
        {"ok": False, "output": "wechat_send_refused errcode=48001 errmsg=api unauthorized"},
        now=now,
    )

    blocked = module.wechat_send_allowed(now=now)
    assert blocked == {"allowed": False, "reason": "api_unauthorized", "error_code": 48001}

    for key in module.WECHAT_REQUIRED_ENV:
        monkeypatch.setenv(key, "1")
    assert module.wechat_runtime_ready() is False
