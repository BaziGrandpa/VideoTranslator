from __future__ import annotations

import json
import subprocess
from functools import lru_cache

from app.config import command_available, settings


@lru_cache(maxsize=1)
def list_codex_models() -> list[dict[str, str]]:
    if not command_available(settings.codex_cli):
        return []
    completed = subprocess.run(
        [settings.codex_cli, "debug", "models"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=True,
    )
    payload = json.loads(completed.stdout)
    return [
        {
            "id": str(model["slug"]),
            "name": str(model.get("display_name") or model["slug"]),
        }
        for model in payload.get("models", [])
        if isinstance(model, dict) and model.get("slug") and model.get("visibility") == "list"
    ]
