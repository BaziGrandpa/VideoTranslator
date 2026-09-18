from pathlib import Path

from app import config


def test_resolve_cli_command_uses_fallback_when_path_lookup_fails(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(config.shutil, "which", lambda _: None)

    assert config.resolve_cli_command("tool", [executable]) == str(executable)
