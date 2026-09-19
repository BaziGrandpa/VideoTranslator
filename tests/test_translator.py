import json
import subprocess
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
    monkeypatch.setattr(translator, "settings", replace(translator.settings, data_dir=tmp_path))
    monkeypatch.setattr(
        translator,
        "_run_command",
        lambda command, *_: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "{}", "")
        ),
    )

    translator.run_codex_with_schema("prompt", tmp_path / "schema.json", "gpt-5.6-luna")

    assert commands[0][commands[0].index("--model") + 1] == "gpt-5.6-luna"
