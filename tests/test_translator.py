import json
import logging
import subprocess
import sys
from dataclasses import replace

from app.services import translator


def test_grok_reads_structured_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(translator, "settings", replace(translator.settings, data_dir=tmp_path))
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("{}", encoding="utf-8")
    output = {"structuredOutput": {"sentences": [{"source_sentence_id": 1, "chinese_sentence": "你好"}]}}
    completed = subprocess.CompletedProcess([], 0, json.dumps(output), "")
    monkeypatch.setattr(translator, "_run_command", lambda *args, **kwargs: completed)

    assert translator.run_grok_with_schema("prompt", schema_path) == output["structuredOutput"]
    assert not list((tmp_path / "cli-workspace").glob("subtitle-prompt-*.txt"))


def test_codex_schema_call_uses_selected_model(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        translator, "settings",
        replace(translator.settings, data_dir=tmp_path, codex_reasoning_effort="high"),
    )
    monkeypatch.setattr(
        translator,
        "_run_command",
        lambda command, *_: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "{}", "")
        ),
    )

    translator.run_codex_with_schema("prompt", tmp_path / "schema.json", "gpt-5.6-luna")
    translator.run_codex_text("prompt", "gpt-5.6-luna")

    assert len(commands) == 2
    for command in commands:
        assert command[command.index("--model") + 1] == "gpt-5.6-luna"
        assert command[command.index("--config") + 1] == 'model_reasoning_effort="high"'


def test_cli_call_label_names_the_pass() -> None:
    assert translator.format_cli_call(
        "grok", stage="context", requested_model="cli-default"
    ) == "Pass 0 via grok (cli-default)"
    assert translator.format_cli_call(
        "codex",
        stage="reference-translation",
        part=[1, 2],
        requested_model="gpt-5.6-terra",
        reasoning_effort="high",
    ) == "Pass 1 via codex (part 1/2, gpt-5.6-terra, effort high)"
    assert translator.format_cli_call(
        "codex", stage="punctuation-restoration", part=[1, 1],
        requested_model="gpt-5.6-terra", reasoning_effort="high",
    ) == "Punctuation Pass via codex (part 1/1, gpt-5.6-terra, effort high)"


def test_cli_wait_status_is_logged_until_the_child_returns(monkeypatch, tmp_path, caplog) -> None:
    monkeypatch.setattr(
        translator,
        "settings",
        replace(translator.settings, data_dir=tmp_path, translation_timeout_seconds=5),
    )
    monkeypatch.setattr(translator, "CLI_STATUS_INTERVAL_SECONDS", 0.2)
    token = translator.activate_cli_call("grok", stage="context", requested_model="cli-default")
    try:
        with caplog.at_level(logging.INFO, logger="app.services.translator"):
            completed = translator._run_command(
                [sys.executable, "-c", "import time; time.sleep(0.7); print('ok')"]
            )
    finally:
        translator.deactivate_cli_call(token)

    assert completed.stdout.strip() == "ok"
    assert "CLI call started: Pass 0 via grok (cli-default)" in caplog.text
    assert "child process still running" in caplog.text
    assert "CLI call finished: Pass 0 via grok (cli-default)" in caplog.text


def test_cli_call_passes_stdin_to_the_child(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        translator,
        "settings",
        replace(translator.settings, data_dir=tmp_path, translation_timeout_seconds=5),
    )
    completed = translator._run_command(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
        stdin="hello from prompt",
    )
    assert completed.stdout.strip() == "hello from prompt"
