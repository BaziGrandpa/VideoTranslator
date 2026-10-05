from pathlib import Path

from app.services.caption_tokens import reconstruct_vtt_tokens
from app.services.subtitle_text import (
    display_translated_text,
    normalize_source_text,
    normalize_translated_text,
)
from app.services.subtitles import (
    ms_to_ass,
    ms_to_srt,
    parse_vtt,
    validate_segments,
    write_ass,
    write_srt,
)


def test_timestamp_formatting() -> None:
    assert ms_to_srt(3_723_456) == "01:02:03,456"
    assert ms_to_ass(3_723_456) == "1:02:03.45"


def test_normalizes_chinese_stored_text_and_stage_directions() -> None:
    assert normalize_translated_text("你自卑的地方，\n在很多人眼里可能很美。") == (
        "你自卑的地方，在很多人眼里可能很美。"
    )
    assert normalize_translated_text("因为我是个小个子女生，") == "因为我是个小个子女生，"
    assert normalize_translated_text("因为他们觉得我很可爱。\n［哼笑］") == ("因为他们觉得我很可爱。")
    assert normalize_translated_text("真的吗？") == "真的吗？"
    assert normalize_translated_text("我和 Bonnie Blue 去找 Katie。") == (
        "我和Bonnie Blue去找Katie。"
    )
    assert normalize_translated_text("全新的 45A Ferrari，真的很酷。") == (
        "全新的45A Ferrari，真的很酷。"
    )
    assert normalize_translated_text("——今天开始——") == "今天开始——"
    assert normalize_translated_text("—今天——开始。") == "今天——开始。"
    assert normalize_source_text("because I'm cute. >> [snorts] >>") == "because I'm cute."


def test_display_removes_only_terminal_layout_marks() -> None:
    assert display_translated_text("他们会看你。") == "他们会看你"
    assert display_translated_text("他说：“看你。”") == "他说：“看你”"
    assert display_translated_text("这样，") == "这样"
    assert display_translated_text("真的？") == "真的？"
    assert display_translated_text("他们会看你。然后离开。") == "他们会看你。然后离开"


def test_parse_vtt_removes_markup_and_duplicate(tmp_path: Path) -> None:
    source = tmp_path / "captions.vtt"
    source.write_text(
        """WEBVTT

00:00:01.000 --> 00:00:02.000
<c.colorE5E5E5>Hello &amp; welcome</c>

00:00:02.000 --> 00:00:03.000
<c>Hello &amp; welcome</c>

00:00:03.000 --> 00:00:04.250
Next sentence.
""",
        encoding="utf-8",
    )
    assert parse_vtt(source) == [
        {"start_ms": 1000, "end_ms": 3000, "source_text": "Hello & welcome"},
        {"start_ms": 3000, "end_ms": 4250, "source_text": "Next sentence."},
    ]


def test_validate_segments_reports_overlap_and_missing_translation() -> None:
    problems = validate_segments(
        [
            {
                "start_ms": 0,
                "end_ms": 2000,
                "source_text": "First",
                "translated_text": "Translated first",
            },
            {
                "start_ms": 1900,
                "end_ms": 2500,
                "source_text": "Second",
                "translated_text": "",
            },
        ]
    )
    assert any("empty translation" in item for item in problems)
    assert any("overlap" in item for item in problems)


def test_write_ass_contains_bilingual_events(tmp_path: Path) -> None:
    path = tmp_path / "subtitles.ass"
    write_ass(
        path,
        [
            {
                "start_ms": 1000,
                "end_ms": 2500,
                "source_text": "Hello",
                "translated_text": "Hello translated",
            }
        ],
        "Microsoft YaHei",
        True,
    )
    content = path.read_text(encoding="utf-8-sig")
    assert "Style: Chinese,Microsoft YaHei" in content
    assert ",Chinese,,0,0,0,,Hello translated" in content
    assert ",English,,0,0,0,,Hello" in content


def test_ass_and_srt_hide_terminal_marks_without_changing_input(tmp_path: Path) -> None:
    segments = [
        {
            "start_ms": 0,
            "end_ms": 1000,
            "source_text": "Look at you.",
            "translated_text": "看你。",
        }
    ]
    write_ass(tmp_path / "subtitles.ass", segments)
    write_srt(tmp_path / "subtitles.srt", segments, "translated_text")
    assert ",Chinese,,0,0,0,,看你\n" in (tmp_path / "subtitles.ass").read_text(encoding="utf-8-sig")
    assert "\n看你\n" in (tmp_path / "subtitles.srt").read_text(encoding="utf-8-sig")
    assert segments[0]["translated_text"] == "看你。"


def test_reconstructs_youtube_rolling_captions_without_duplicate_words(tmp_path: Path) -> None:
    source = tmp_path / "rolling.vtt"
    source.write_text(
        """WEBVTT

00:00:00.760 --> 00:00:03.750
 
Many<00:00:01.040><c> people</c><00:00:01.400><c> are</c><00:00:01.560><c> thinking</c>

00:00:03.750 --> 00:00:03.760
Many people are thinking
 

00:00:03.760 --> 00:00:06.550
Many people are thinking
clearly.<00:00:04.200><c> They</c><00:00:04.600><c> continue.</c>
""",
        encoding="utf-8",
    )

    tokens = reconstruct_vtt_tokens(source, "youtube_auto")

    assert [token["text"] for token in tokens] == [
        "Many",
        "people",
        "are",
        "thinking",
        "clearly.",
        "They",
        "continue.",
    ]
    assert tokens[0]["start_ms"] == 760
    assert tokens[4]["start_ms"] == 3760
