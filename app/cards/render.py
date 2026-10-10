from __future__ import annotations

from pathlib import Path
from typing import Any

from app.cards.visual import fade_factor, render_card_png


def prepare_card_images(
    directory: Path, cards: list[dict[str, Any]], width: int, height: int,
    timestamp_ms: int | None = None,
) -> list[dict[str, Any]]:
    prepared = []
    for index, card in enumerate(cards):
        opacity = fade_factor(card, timestamp_ms) if timestamp_ms is not None else 1.0
        if opacity <= 0:
            continue
        image, geometry = render_card_png(card, width, height, opacity)
        path = directory / f"commentary-card-{index}.png"
        path.write_bytes(image)
        prepared.append({"path": path, "geometry": geometry, "card": card})
    return prepared


def append_card_filters(
    base_graph: str, prepared: list[dict[str, Any]], *, exact_frame: bool = False
) -> str:
    if not prepared:
        return base_graph
    graph = base_graph.removesuffix("[outv]") + "[card-base]"
    previous = "card-base"
    for index, item in enumerate(prepared, start=1):
        card = item["card"]
        geometry = item["geometry"]
        overlay_input = f"[{index}:v]"
        if not exact_frame:
            style = card["style"]
            filters = ["fps=30", "format=rgba"]
            if style["fade_in_ms"]:
                filters.append(
                    f"fade=t=in:st={card['start_ms'] / 1000:.3f}:"
                    f"d={style['fade_in_ms'] / 1000:.3f}:alpha=1"
                )
            if style["fade_out_ms"]:
                filters.append(
                    f"fade=t=out:st={(card['end_ms'] - style['fade_out_ms']) / 1000:.3f}:"
                    f"d={style['fade_out_ms'] / 1000:.3f}:alpha=1"
                )
            graph += f";{overlay_input}{','.join(filters)}[card-image-{index}]"
            overlay_input = f"[card-image-{index}]"
        output = "outv" if index == len(prepared) else f"card-layer-{index}"
        enable = (
            ""
            if exact_frame else
            f":enable='gte(t,{card['start_ms'] / 1000:.3f})*"
            f"lt(t,{card['end_ms'] / 1000:.3f})'"
        )
        graph += (
            f";[{previous}]{overlay_input}overlay=x={geometry['x']}:y={geometry['y']}:"
            f"format=auto:eof_action=repeat:shortest=1{enable}[{output}]"
        )
        previous = output
    return graph
