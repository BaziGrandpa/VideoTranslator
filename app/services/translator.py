from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings


def run_codex_with_schema(
    prompt: str, schema_path: Path, model: str | None = None
) -> dict[str, Any]:
    command = [
        settings.codex_cli,
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        str(schema_path),
        "--color",
        "never",
    ]
    selected_model = settings.codex_model if model is None else model
    if selected_model:
        command.extend(["--model", selected_model])
    if settings.codex_reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{settings.codex_reasoning_effort}"'])
    command.append("-")
    completed = _run_command(command, prompt)
    return json.loads(completed.stdout)


def run_codex_text(prompt: str, model: str | None = None) -> str:
    command = [
        settings.codex_cli,
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--color",
        "never",
    ]
    selected_model = settings.codex_model if model is None else model
    if selected_model:
        command.extend(["--model", selected_model])
    if settings.codex_reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{settings.codex_reasoning_effort}"'])
    command.append("-")
    return _run_command(command, prompt).stdout.strip()


def run_grok_with_schema(prompt: str, schema_path: Path) -> dict[str, Any]:
    schema = schema_path.read_text(encoding="utf-8")
    cli_workspace = settings.data_dir / "cli-workspace"
    cli_workspace.mkdir(parents=True, exist_ok=True)
    prompt_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="subtitle-prompt-",
            dir=cli_workspace,
            encoding="utf-8",
            delete=False,
        ) as prompt_file:
            prompt_file.write(prompt)
            prompt_path = Path(prompt_file.name)
        command = [
            settings.grok_cli,
            "--prompt-file",
            str(prompt_path),
            "--json-schema",
            schema,
            "--no-subagents",
            "--disable-web-search",
            "--permission-mode",
            "plan",
            "--max-turns",
            "1",
            "--verbatim",
            "--cwd",
            str(cli_workspace),
        ]
        if settings.grok_model:
            command.extend(["--model", settings.grok_model])
        completed = _run_command(command)
        envelope = json.loads(completed.stdout)
        structured = envelope.get("structuredOutput")
        if isinstance(structured, dict):
            return structured
        text = envelope.get("text")
        if isinstance(text, str):
            return json.loads(text)
        raise RuntimeError("Grok CLI did not return structuredOutput")
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)


def run_grok_text(prompt: str) -> str:
    cli_workspace = settings.data_dir / "cli-workspace"
    cli_workspace.mkdir(parents=True, exist_ok=True)
    prompt_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="translation-prompt-",
            dir=cli_workspace,
            encoding="utf-8",
            delete=False,
        ) as prompt_file:
            prompt_file.write(prompt)
            prompt_path = Path(prompt_file.name)
        command = [
            settings.grok_cli,
            "--prompt-file",
            str(prompt_path),
            "--output-format",
            "json",
            "--no-subagents",
            "--disable-web-search",
            "--permission-mode",
            "plan",
            "--max-turns",
            "1",
            "--verbatim",
            "--cwd",
            str(cli_workspace),
        ]
        if settings.grok_model:
            command.extend(["--model", settings.grok_model])
        envelope = json.loads(_run_command(command).stdout)
        text = envelope.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Grok CLI did not return translated text")
        return text.strip()
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)


def _run_command(command: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    cli_workspace = settings.data_dir / "cli-workspace"
    cli_workspace.mkdir(parents=True, exist_ok=True)
    try:
        return subprocess.run(
            command,
            input=stdin,
            cwd=cli_workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.translation_timeout_seconds,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "Unknown error").strip()
        raise RuntimeError(detail[-2000:]) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"CLI call timed out after {settings.translation_timeout_seconds} seconds"
        ) from exc
