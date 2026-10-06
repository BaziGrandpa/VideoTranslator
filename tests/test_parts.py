from app.services.parts import build_parts


def make_tokens(minutes: int, words_per_minute: int = 10) -> list[dict]:
    count = minutes * words_per_minute
    interval = 60_000 // words_per_minute
    return [
        {
            "text": "sentence." if index % words_per_minute == words_per_minute - 1 else "word",
            "start_ms": index * interval,
            "end_ms": (index + 1) * interval,
        }
        for index in range(count)
    ]


def test_short_video_is_one_part() -> None:
    assert len(build_parts(make_tokens(39))) == 1


def test_two_hour_video_is_four_balanced_parts() -> None:
    parts = build_parts(make_tokens(120))
    assert len(parts) == 4
    durations = [part["end_ms"] - part["start_ms"] for part in parts]
    assert all(25 * 60_000 <= duration <= 35 * 60_000 for duration in durations)
    assert parts[0]["start_token_id"] == 0
    assert parts[-1]["end_token_id"] == 1199
