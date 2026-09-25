from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)
CLI_STATUS_INTERVAL_SECONDS = 15
_cli_call_label: ContextVar[str] = ContextVar("cli_call_label", default="CLI call")

_STAGE_NAMES = {
    "punctuation-restoration": "Punctuation Pass",
    "context": "Pass 0",
    "reference-translation": "Pass 1",
    "part-sentence-alignment": "Pass 2",
    "pass3-1-boundary-assignment": "Pass 3-1",
    "pass3-2-chinese-splitting": "Pass 3-2",
}


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


def format_cli_call(provider: str, **metadata: Any) -> str:
    """Describe the pass that is about to block on a local CLI."""
    stage = str(metadata.get("stage") or "translation")
    name = _STAGE_NAMES.get(stage, stage)
    details: list[str] = []
    part = metadata.get("part")
    if isinstance(part, (list, tuple)) and len(part) == 2:
        details.append(f"part {part[0]}/{part[1]}")
    chunk = metadata.get("chunk")
    if chunk:
        details.append(f"chunk {chunk}")
    model = metadata.get("requested_model")
    if model:
        details.append(str(model))
    effort = metadata.get("reasoning_effort")
    if effort:
        details.append(f"effort {effort}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{name} via {provider}{suffix}"


def activate_cli_call(provider: str, **metadata: Any) -> Any:
    return _cli_call_label.set(format_cli_call(provider, **metadata))


def deactivate_cli_call(token: Any) -> None:
    _cli_call_label.reset(token)


def _read_pipe(pipe: Any, sink: list[str]) -> None:
    try:
        sink.append(pipe.read())
    finally:
        pipe.close()


def _write_stdin(pipe: Any, data: str) -> None:
    try:
        pipe.write(data)
    finally:
        pipe.close()


def _run_command(command: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    cli_workspace = settings.data_dir / "cli-workspace"
    cli_workspace.mkdir(parents=True, exist_ok=True)
    label = _cli_call_label.get()
    timeout = settings.translation_timeout_seconds
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cli_workspace,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    threads = [
        threading.Thread(target=_read_pipe, args=(process.stdout, stdout_parts), daemon=True),
        threading.Thread(target=_read_pipe, args=(process.stderr, stderr_parts), daemon=True),
    ]
    if stdin is not None and process.stdin is not None:
        threads.append(
            threading.Thread(target=_write_stdin, args=(process.stdin, stdin), daemon=True)
        )
    for thread in threads:
        thread.start()

    logger.info("CLI call started: %s; waiting for the child process (limit %ss)", label, timeout)
    started = time.monotonic()
    next_status = started + CLI_STATUS_INTERVAL_SECONDS
    timed_out = False
    while process.poll() is None:
        now = time.monotonic()
        if now - started >= timeout:
            timed_out = True
            process.kill()
            break
        if now >= next_status:
            elapsed = int(now - started)
            logger.info(
                "CLI waiting: %s; child process still running, %ss elapsed, limit %ss",
                label,
                elapsed,
                timeout,
            )
            next_status = now + CLI_STATUS_INTERVAL_SECONDS
        time.sleep(0.2)
    for thread in threads:
        thread.join(timeout=5)
    stdout = stdout_parts[0] if stdout_parts else ""
    stderr = stderr_parts[0] if stderr_parts else ""
    elapsed = time.monotonic() - started
    if timed_out:
        logger.error("CLI call timed out: %s after %.0fs", label, elapsed)
        raise RuntimeError(f"CLI call timed out after {timeout} seconds")
    if process.returncode != 0:
        detail = (stderr or stdout or "Unknown error").strip()
        logger.error("CLI call failed: %s after %.0fs", label, elapsed)
        raise RuntimeError(detail[-2000:])
    logger.info("CLI call finished: %s in %.0fs", label, elapsed)
    return subprocess.CompletedProcess(command, process.returncode or 0, stdout, stderr)
