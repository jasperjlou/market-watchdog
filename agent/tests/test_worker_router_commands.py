from __future__ import annotations

import importlib.util
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = AGENT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_safe_current_grok_command(command: list[str], cwd: Path) -> None:
    assert command[0] == "grok"
    assert "--no-auto-update" not in command
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert "--no-subagents" in command
    assert "--no-memory" in command
    assert "--tools" not in command
    denied = set(command[command.index("--disallowed-tools") + 1].split(","))
    assert {
        "update_goal",
        "create_goal",
        "apply_patch",
        "search_replace",
        "run_terminal_cmd",
        "read_file",
        "list_dir",
        "task",
        "enter_lan_mode",
    } <= denied
    assert "search" not in denied
    assert command[command.index("--model") + 1] == "grok-4.5"
    assert command[command.index("--output-format") + 1] == "json"
    assert command[command.index("--cwd") + 1] == str(cwd)


def assert_safe_antigravity_command(command: list[str], prompt: str) -> None:
    assert command[0] == "agy"
    assert command[command.index("--model") + 1] == "gemini-3.7-flash-low"
    assert command[command.index("--effort") + 1] == "low"
    assert command[command.index("--mode") + 1] == "plan"
    assert "--sandbox" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert command[command.index("--print-timeout") + 1] == "5m"
    assert command[command.index("-p") + 1] == prompt


def test_agent_router_uses_supported_safe_grok_flags(tmp_path: Path) -> None:
    module = load_script("agent_router")

    command, backend = module.worker_command("grok", "safe prompt", tmp_path)

    assert backend == "grok"
    assert command[command.index("-p") + 1] == "safe prompt"
    assert_safe_current_grok_command(command, tmp_path)


def test_skill_router_uses_supported_safe_grok_flags(tmp_path: Path) -> None:
    module = load_script("skill_router")

    command, backend = module.command_for_worker("grok", "safe prompt", tmp_path)

    assert backend == "grok"
    assert command[command.index("-p") + 1] == "safe prompt"
    assert_safe_current_grok_command(command, tmp_path)


def test_agent_router_uses_antigravity_only_with_bounded_flags(monkeypatch, tmp_path: Path) -> None:
    module = load_script("agent_router")
    monkeypatch.setenv("AGY_MODEL", "gemini-3.7-flash-low")
    monkeypatch.setenv("AGY_EFFORT", "low")

    command, backend = module.worker_command("gemini", "safe prompt", tmp_path)

    assert backend == "agy"
    assert "gemini" not in command
    assert_safe_antigravity_command(command, "safe prompt")


def test_skill_router_uses_antigravity_only_with_bounded_flags(monkeypatch, tmp_path: Path) -> None:
    module = load_script("skill_router")
    monkeypatch.setenv("AGY_MODEL", "gemini-3.7-flash-low")
    monkeypatch.setenv("AGY_EFFORT", "low")

    command, backend = module.command_for_worker("gemini", "safe prompt", tmp_path)

    assert backend == "agy"
    assert "gemini" not in command
    assert_safe_antigravity_command(command, "safe prompt")
