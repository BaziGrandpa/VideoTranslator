from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import command_available, settings
from app.db import connect, transaction
from app.repository import Repository, now_iso
from app.services.publication_cover import SIZES, default_design
from app.services.translator import _run_command, activate_cli_call, deactivate_cli_call

logger = logging.getLogger(__name__)


def _prompt(ratio: str, destination: Path) -> str:
    width, height = SIZES[ratio]
    if ratio == "16x9":
        visual_direction = (
            "Image 1 is a 16:9 layout guide whose central region contains the approved 4:3 "
            "background exactly; its stretched left and right bands are placeholders to "
            "outpaint naturally. Image 2 is that approved 4:3 background and is the PRIMARY "
            "visual source. Image 3 is the original reference, only for checking identity. "
            "Keep the complete 4:3 composition in the center visually unchanged while completing "
            "the extended left and right areas. Create a 16:9 companion of the same instant: "
            "keep the same person and facial features, hairstyle, clothes, pose, objects, "
            "object presence or absence, setting, light, colors, and style. Do not invent a "
            "microphone or any other object that is absent in Image 1; do not remove an "
            "object present there. Extend the existing scene to the left and right; keep the "
            "central subjects and objects in their existing positions. The result should be "
            "immediately recognizable as the same image, with only the canvas extended sideways. "
        )
    else:
        visual_direction = (
            "This 4:3 image is the MASTER composition for the recommendation feed. "
            "The attached image is the original edit target, not merely a subject description. "
            "Preserve its recognizable subject, visual content, colors, and artistic style "
            "as closely as possible. Make this composition visually complete on its own. "
        )
    return (
        "$imagegen\n"
        "Edit the attached image(s) to create ONE new, text-free video cover background. "
        f"{visual_direction}"
        "Use the image generation tool; a programmatic crop, resize, blur, or paint-over alone "
        "does not fulfill this task. Remove existing captions, lettering, logos, "
        "UI, and distracting visual clutter. Recompose or extend the scene so its subject or "
        "visual focus sits on the right, with clear space on the left for a large title. "
        "Do not add any new text, title, text shadow, gradient mask, or final cover overlays. "
        f"Create a {ratio.replace('x', ':')} landscape image, ideally {width}x{height} pixels. "
        f"Save the generated bitmap as a PNG at exactly this absolute path: {destination}. "
        "If the image tool saves elsewhere first, copy its output to that path. "
        "Verify the PNG exists before finishing. Do not edit any other project files."
    )


def _run_codex(references: tuple[Path, ...], destination: Path, ratio: str, model: str) -> None:
    if not command_available(settings.codex_cli):
        raise RuntimeError(f"Codex CLI not found: {settings.codex_cli}")
    prompt = _prompt(ratio, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    (destination.parent / f"prompt-{ratio}.txt").write_text(prompt, encoding="utf-8")
    command = [
        settings.codex_cli, "exec", "--ephemeral", "--sandbox", "workspace-write",
        "--skip-git-repo-check", "--color", "never", "-C", str(destination.parent),
    ]
    for reference in references:
        command.extend(["--image", str(reference)])
    if model:
        command.extend(["--model", model])
    if settings.codex_reasoning_effort:
        command.extend([
            "--config", f'model_reasoning_effort="{settings.codex_reasoning_effort}"'
        ])
    command.append("-")
    token = activate_cli_call("codex", stage=f"publication-background-{ratio}")
    try:
        completed = _run_command(command, prompt)
    finally:
        deactivate_cli_call(token)
    (destination.parent / f"codex-{ratio}.txt").write_text(
        completed.stdout[-20000:] + "\n\nSTDERR:\n" + completed.stderr[-10000:],
        encoding="utf-8",
    )


def _validate_and_normalize(path: Path, ratio: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Codex did not write the {ratio.replace('x', ':')} background PNG")
    try:
        with Image.open(path) as source:
            source.load()
            if source.format != "PNG":
                raise ValueError("Codex output must be a PNG")
            width, height = source.size
            target_width, target_height = SIZES[ratio]
            if width * height > 25_000_000:
                raise ValueError("Codex output is too large")
            if width < 640 or height < 360:
                raise ValueError("Codex output is too small")
            if abs(width / height - target_width / target_height) > 0.08:
                raise ValueError(f"Codex output does not have a {ratio.replace('x', ':')} ratio")
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid {ratio.replace('x', ':')} generated background: {exc}") from exc
    if image.size != (target_width, target_height):
        image = ImageOps.fit(image, (target_width, target_height), Image.Resampling.LANCZOS)
    temporary = path.with_name(f".validated-{uuid.uuid4().hex}.png")
    image.save(temporary, format="PNG")
    temporary.replace(path)


def _make_companion_guide(master: Path, destination: Path) -> None:
    """Give imagegen the full 4:3 scene with room to outpaint horizontally."""
    width, height = SIZES["16x9"]
    with Image.open(master) as source:
        image = source.convert("RGB")
    if image.size != SIZES["4x3"]:
        raise ValueError("The 4:3 master has an unexpected size")
    center_width = round(image.width * height / image.height)
    center = image.resize((center_width, height), Image.Resampling.LANCZOS)
    offset = (width - center_width) // 2
    guide = Image.new("RGB", (width, height))
    guide.paste(center, (offset, 0))
    guide.paste(center.crop((0, 0, 1, height)).resize((offset, height)), (0, 0))
    right = width - offset - center_width
    guide.paste(
        center.crop((center_width - 1, 0, center_width, height)).resize((right, height)),
        (offset + center_width, 0),
    )
    guide.save(destination, format="PNG")


def _preserve_master_center(master: Path, companion: Path) -> None:
    """Keep the full 4:3 composition while retaining model-generated side outpainting."""
    with Image.open(master) as source, Image.open(companion) as generated:
        center_width = round(source.width * SIZES["16x9"][1] / source.height)
        center = source.convert("RGB").resize(
            (center_width, SIZES["16x9"][1]), Image.Resampling.LANCZOS
        )
        result = generated.convert("RGB")
    offset = (result.width - center.width) // 2
    feather = 28
    mask = Image.new("L", center.size, 255)
    pixels = mask.load()
    for x in range(feather):
        opacity = round(255 * x / feather)
        for y in range(center.height):
            pixels[x, y] = opacity
            pixels[center.width - 1 - x, y] = opacity
    result.paste(center, (offset, 0), mask)
    temporary = companion.with_name(f".matched-{uuid.uuid4().hex}.png")
    result.save(temporary, format="PNG")
    temporary.replace(companion)


def generate_publication_backgrounds(project_id: str, repository: Repository) -> None:
    with connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT * FROM publication_workspaces WHERE project_id = ?", (project_id,)
        ).fetchone()
    if row is None or row["background_status"] not in {"queued", "running"}:
        return
    reference = Path(row["reference_path"]) if row["reference_path"] else None
    if reference is None or not reference.is_file():
        raise RuntimeError("Upload a reference image before generating backgrounds")
    generation_id = row["background_generation_id"]
    revision = row["reference_revision"]
    with transaction(repository.database_path) as connection:
        connection.execute(
            "UPDATE publication_workspaces SET background_status = 'running', "
            "background_error = NULL, updated_at = ? WHERE project_id = ? "
            "AND background_generation_id = ?",
            (now_iso(), project_id, generation_id),
        )
    project = repository.get_project(project_id)
    model = str((project or {}).get("codex_model") or settings.codex_model)
    run_dir = settings.data_dir / "projects" / project_id / "publication" / f"generation-{generation_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    generated: dict[str, Path] = {}
    for ratio in ("4x3", "16x9"):
        path = run_dir / f"base-{ratio}.png"
        path.unlink(missing_ok=True)
        if ratio == "4x3":
            references = (reference,)
        else:
            guide = run_dir / "guide-16x9.png"
            _make_companion_guide(generated["4x3"], guide)
            references = (guide, generated["4x3"], reference)
        _run_codex(references, path, ratio, model)
        _validate_and_normalize(path, ratio)
        if ratio == "16x9":
            _preserve_master_center(generated["4x3"], path)
        generated[ratio] = path
    with transaction(repository.database_path) as connection:
        current = connection.execute(
            "SELECT background_generation_id, reference_revision FROM publication_workspaces "
            "WHERE project_id = ?", (project_id,),
        ).fetchone()
        if (current is None or current["background_generation_id"] != generation_id
                or current["reference_revision"] != revision):
            raise RuntimeError("The reference image changed during generation; retry")
        for ratio, path in generated.items():
            existing = connection.execute(
                "SELECT design_json FROM publication_covers WHERE project_id = ? AND ratio = ?",
                (project_id, ratio),
            ).fetchone()
            if existing is None:
                design = json.dumps(default_design(ratio))
            else:
                design = existing["design_json"]
            connection.execute(
                """INSERT INTO publication_covers
                   (project_id, ratio, image_path, design_json, revision, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(project_id, ratio) DO UPDATE SET
                   image_path = excluded.image_path, revision = publication_covers.revision + 1,
                   updated_at = excluded.updated_at""",
                (project_id, ratio, str(path), design, now_iso()),
            )
        connection.execute(
            "UPDATE publication_workspaces SET background_status = 'completed', "
            "background_error = NULL, updated_at = ? WHERE project_id = ?",
            (now_iso(), project_id),
        )
    logger.info("Generated both publication backgrounds for %s", project_id)


def mark_background_failed(project_id: str, repository: Repository, error: str) -> None:
    with transaction(repository.database_path) as connection:
        connection.execute(
            "UPDATE publication_workspaces SET background_status = 'failed', "
            "background_error = ?, updated_at = ? WHERE project_id = ? "
            "AND background_status IN ('queued', 'running')",
            (error, now_iso(), project_id),
        )
