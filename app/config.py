from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load a tiny .env subset without adding a runtime dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT_DIR / ".env")


def command_available(command: str) -> bool:
    candidate = Path(command)
    if candidate.is_absolute():
        try:
            return candidate.is_file()
        except OSError:
            return False
    return shutil.which(command) is not None


def resolve_cli_command(command: str, fallback_candidates: list[Path]) -> str:
    candidate = Path(command)
    if candidate.is_absolute():
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            pass
    resolved = shutil.which(command)
    if resolved:
        return resolved
    available_candidates: list[Path] = []
    for fallback in fallback_candidates:
        try:
            if fallback.is_file():
                available_candidates.append(fallback)
            elif "*" in str(fallback):
                available_candidates.extend(
                    path for path in fallback.parent.glob(fallback.name) if path.is_file()
                )
        except OSError:
            continue
    if available_candidates:
        try:
            newest = max(available_candidates, key=lambda path: path.stat().st_mtime)
        except OSError:
            newest = available_candidates[0]
        return str(newest)
    return command


LOCAL_APP_DATA = Path(os.getenv("LOCALAPPDATA", ""))
USER_PROFILE = Path(os.getenv("USERPROFILE", ""))


def resolve_codex_cli() -> str:
    configured = os.getenv("CODEX_CLI", "codex")
    codex_bin = LOCAL_APP_DATA / "OpenAI" / "Codex" / "bin"
    candidates = list(codex_bin.glob("*")) if codex_bin.is_dir() else []
    executables = [directory / "codex.exe" for directory in candidates]
    return resolve_cli_command(configured, executables)


def resolve_grok_cli() -> str:
    configured = os.getenv("GROK_CLI", "grok")
    return resolve_cli_command(configured, [USER_PROFILE / ".grok" / "bin" / "grok.exe"])


@dataclass(frozen=True)
class Settings:
    root_dir: Path = ROOT_DIR
    data_dir: Path = Path(os.getenv("VIDEO_TRANSLATOR_DATA_DIR", ROOT_DIR / "data"))
    database_path: Path = Path(
        os.getenv("VIDEO_TRANSLATOR_DB", ROOT_DIR / "data" / "video_translator_v2.db")
    )
    output_dir: Path = ROOT_DIR / os.getenv("VIDEO_TRANSLATOR_OUTPUT_DIR", "output")
    translation_providers: tuple[str, ...] = tuple(
        item.strip().lower()
        for item in os.getenv("TRANSLATION_PROVIDERS", "codex,grok").split(",")
        if item.strip()
    )
    codex_cli: str = resolve_codex_cli()
    grok_cli: str = resolve_grok_cli()
    codex_model: str = os.getenv("CODEX_MODEL", "")
    codex_reasoning_effort: str = os.getenv("CODEX_REASONING_EFFORT", "high")
    grok_model: str = os.getenv("GROK_MODEL", "")
    translation_timeout_seconds: int = int(os.getenv("TRANSLATION_TIMEOUT_SECONDS", "1800"))
    max_video_height: int = int(os.getenv("MAX_VIDEO_HEIGHT", "1080"))
    whisper_model: str = os.getenv("WHISPER_MODEL", "small.en")
    whisper_device: str = os.getenv("WHISPER_DEVICE", "auto")
    whisper_compute_type: str = os.getenv("WHISPER_COMPUTE_TYPE", "auto")
    ffmpeg_bin: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    ffprobe_bin: str = os.getenv("FFPROBE_BIN", "ffprobe")
    render_video_encoder: str = os.getenv("RENDER_VIDEO_ENCODER", "libx264").strip().lower()
    chinese_subtitle_font: str = os.getenv("CHINESE_SUBTITLE_FONT", "Microsoft YaHei")
    chinese_subtitle_size: int = int(os.getenv("CHINESE_SUBTITLE_SIZE", "46"))
    chinese_subtitle_color: str = os.getenv("CHINESE_SUBTITLE_COLOR", "#FFFFFF")
    chinese_subtitle_outline: float = float(os.getenv("CHINESE_SUBTITLE_OUTLINE", "3"))
    english_subtitle_font: str = os.getenv("ENGLISH_SUBTITLE_FONT", "Arial")
    english_subtitle_size: int = int(os.getenv("ENGLISH_SUBTITLE_SIZE", "28"))
    english_subtitle_color: str = os.getenv("ENGLISH_SUBTITLE_COLOR", "#D8D8D8")
    english_subtitle_outline: float = float(os.getenv("ENGLISH_SUBTITLE_OUTLINE", "2"))
    subtitle_bottom_margin: int = int(os.getenv("SUBTITLE_BOTTOM_MARGIN", "46"))
    subtitle_line_gap: int = int(os.getenv("SUBTITLE_LINE_GAP", "66"))
    subtitle_horizontal_margin: int = int(os.getenv("SUBTITLE_HORIZONTAL_MARGIN", "90"))
    bottom_mask_height: float = float(os.getenv("BOTTOM_MASK_HEIGHT", "0.34"))
    bottom_mask_opacity: float = float(os.getenv("BOTTOM_MASK_OPACITY", "0.72"))
    host: str = os.getenv("APP_HOST", "127.0.0.1")
    port: int = int(os.getenv("APP_PORT", "6866"))

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "cli-workspace").mkdir(parents=True, exist_ok=True)


settings = Settings()
