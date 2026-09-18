import json
import subprocess
from dataclasses import replace

import pytest

from app.services import translator


def test_translate_prefers_codex(monkeypatch) -> None:
    calls: list[str] = []
    progress: list[tuple[int, int]] = []

    def fake_codex(_: str) -> dict:
        calls.append("codex")
        return {"translations": [{"id": 0, "text": "Translated greeting"}]}

    monkeypatch.setattr(
        translator,
        "settings",
        replace(translator.settings, translation_providers=("codex", "grok")),
    )
    monkeypatch.setattr(translator, "command_available", lambda _: True)
    monkeypatch.setattr(translator, "_run_codex", fake_codex)
    monkeypatch.setattr(
        translator,
        "_run_grok",
        lambda _: calls.append("grok") or {"translations": []},
    )

    result = translator.translate_segments(
        [{"source_text": "Hello"}],
        "zh-CN",
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert calls == ["codex"]
    assert progress == [(1, 1)]
    assert result[0]["translated_text"] == "Translated greeting"


def test_translate_falls_back_to_grok(monkeypatch) -> None:
    calls: list[str] = []

    def failed_codex(_: str) -> dict:
        calls.append("codex")
        raise RuntimeError("temporary failure")

    def working_grok(_: str) -> dict:
        calls.append("grok")
        return {"translations": [{"id": 0, "text": "Fallback succeeded"}]}

    monkeypatch.setattr(
        translator,
        "settings",
        replace(translator.settings, translation_providers=("codex", "grok")),
    )
    monkeypatch.setattr(translator, "command_available", lambda _: True)
    monkeypatch.setattr(translator, "_run_codex", failed_codex)
    monkeypatch.setattr(translator, "_run_grok", working_grok)

    result = translator.translate_segments([{"source_text": "Fallback"}], "zh-CN")

    assert calls == ["codex", "grok"]
    assert result[0]["translated_text"] == "Fallback succeeded"


def test_rejects_incomplete_cli_result(monkeypatch) -> None:
    monkeypatch.setattr(
        translator,
        "settings",
        replace(translator.settings, translation_providers=("codex",)),
    )
    monkeypatch.setattr(translator, "command_available", lambda _: True)
    monkeypatch.setattr(translator, "_run_codex", lambda _: {"translations": []})

    with pytest.raises(RuntimeError, match="Subtitle IDs do not match"):
        translator.translate_segments([{"source_text": "Hello"}], "zh-CN")


def test_grok_reads_structured_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(translator, "settings", replace(translator.settings, data_dir=tmp_path))
    output = {"structuredOutput": {"translations": [{"id": 0, "text": "Translated greeting"}]}}
    completed = subprocess.CompletedProcess([], 0, json.dumps(output), "")
    monkeypatch.setattr(translator, "_run_command", lambda *args, **kwargs: completed)

    assert translator._run_grok("prompt") == output["structuredOutput"]
    assert not list((tmp_path / "cli-workspace").glob("subtitle-prompt-*.txt"))


def test_codex_schema_call_uses_selected_model(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(translator, "settings", replace(translator.settings, data_dir=tmp_path))
    monkeypatch.setattr(
        translator,
        "_run_command",
        lambda command, *_: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "{}", "")
        ),
    )

    translator._run_codex_with_schema("prompt", tmp_path / "schema.json", "gpt-5.6-luna")

    assert commands[0][commands[0].index("--model") + 1] == "gpt-5.6-luna"
