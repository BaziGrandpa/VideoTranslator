from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.cards.model import CardPayload
from app.cards.repository import CardRepository
from app.cards.visual import render_card_png
from app.services.renderer import probe_video


class ProjectLookup(Protocol):
    database_path: Path | None

    def get_project(self, project_id: str) -> dict | None: ...


def create_router(project_repository: Callable[[], ProjectLookup]) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/cards", tags=["cards"])

    def context(project_id: str, editing: bool = False) -> tuple[dict, CardRepository]:
        repository = project_repository()
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if editing and project["stage"] in {"render_queued", "rendering", "render_cancelling"}:
            raise HTTPException(status_code=409, detail="Cards are locked during rendering")
        return project, CardRepository(repository.database_path)

    def dimensions(project: dict) -> tuple[int, int]:
        video_path = Path(str(project.get("video_path") or ""))
        if not video_path.is_file():
            raise HTTPException(status_code=409, detail="The project has no downloaded video")
        return probe_video(video_path)

    def check(project: dict, payload: CardPayload) -> tuple[int, int]:
        if project.get("duration_ms") and payload.end_ms > int(project["duration_ms"]):
            raise HTTPException(status_code=422, detail="Card ends after the video")
        size = dimensions(project)
        try:
            render_card_png(payload, *size)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return size

    def visual(card: dict | CardPayload, size: tuple[int, int]) -> Response:
        try:
            content, geometry = render_card_png(card, *size)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return Response(
            content=content,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "X-Card-X": str(geometry["x"]),
                "X-Card-Y": str(geometry["y"]),
                "X-Card-Width": str(geometry["width"]),
                "X-Card-Height": str(geometry["height"]),
            },
        )

    @router.get("")
    def list_cards(project_id: str) -> list[dict]:
        _, cards = context(project_id)
        return cards.list(project_id)

    @router.get("/default-style")
    def default_style(project_id: str, response: Response) -> dict:
        _, cards = context(project_id)
        response.headers["Cache-Control"] = "no-store"
        return cards.default_style()

    @router.post("", status_code=201)
    def create_card(project_id: str, payload: CardPayload) -> dict:
        project, cards = context(project_id, editing=True)
        check(project, payload)
        try:
            return cards.create(project_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/visual")
    def preview_visual(project_id: str, payload: CardPayload) -> Response:
        project, _ = context(project_id)
        return visual(payload, check(project, payload))

    @router.get("/{card_id}/visual")
    def card_visual(project_id: str, card_id: int) -> Response:
        project, cards = context(project_id)
        card = cards.get(project_id, card_id)
        if card is None:
            raise HTTPException(status_code=404, detail="Card not found")
        return visual(card, dimensions(project))

    @router.put("/{card_id}")
    def update_card(project_id: str, card_id: int, payload: CardPayload) -> dict:
        project, cards = context(project_id, editing=True)
        check(project, payload)
        try:
            return cards.update(project_id, card_id, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="Card not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.delete("/{card_id}")
    def delete_card(project_id: str, card_id: int) -> dict:
        _, cards = context(project_id, editing=True)
        try:
            cards.delete(project_id, card_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Card not found") from None
        return {"ok": True}

    return router
