from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings
from app.services.subtitle_quality import PROMPT_VERSION


def record_event(directory: Path | None, kind: str, **fields: Any) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    name = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{kind}-{uuid4().hex[:8]}.json"
    payload = {
        "timestamp": now.isoformat(),
        "prompt_version": PROMPT_VERSION,
        "kind": kind,
        **fields,
    }
    (directory / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def call_metadata(provider: str, codex_model: str | None) -> dict:
    return {
        "provider": provider,
        "requested_model": (
            (settings.codex_model if codex_model is None else codex_model)
            if provider == "codex"
            else settings.grok_model
        )
        or "cli-default",
        "reasoning_effort": settings.codex_reasoning_effort if provider == "codex" else None,
    }
