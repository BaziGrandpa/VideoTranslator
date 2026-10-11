from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CardStyle(BaseModel):
    side: Literal["left", "center", "right"] = "right"
    width_pct: float = Field(default=32, ge=20, le=50)
    font_family: Literal["Microsoft YaHei", "Arial", "SimHei"] = "Microsoft YaHei"
    title_size: int = Field(default=54, ge=20, le=100)
    english_size: int = Field(default=34, ge=18, le=80)
    body_size: int = Field(default=38, ge=18, le=80)
    title_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    english_color: str = Field(default="#DCE6F1", pattern=r"^#[0-9a-fA-F]{6}$")
    body_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    title_bold: bool = True
    english_bold: bool = False
    body_bold: bool = False
    line_spacing: float = Field(default=1.25, ge=1, le=2)
    padding: int = Field(default=30, ge=12, le=70)
    background_opacity: float = Field(default=0.62, ge=0.2, le=0.9)
    fade_in_ms: int = Field(default=800, ge=0, le=5000)
    fade_out_ms: int = Field(default=800, ge=0, le=5000)


class CardPayload(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    chinese_term: str = Field(min_length=1, max_length=160)
    english_term: str = Field(default="", max_length=240)
    explanation: str = Field(min_length=1, max_length=1200)
    style: CardStyle = Field(default_factory=CardStyle)

    @model_validator(mode="after")
    def check_timing_and_content(self) -> CardPayload:
        self.chinese_term = self.chinese_term.strip()
        self.english_term = self.english_term.strip()
        self.explanation = self.explanation.strip()
        if not self.chinese_term or not self.explanation:
            raise ValueError("Chinese term and explanation cannot be empty")
        if self.end_ms <= self.start_ms:
            raise ValueError("Card end time must be after its start time")
        if self.style.fade_in_ms + self.style.fade_out_ms > self.end_ms - self.start_ms:
            raise ValueError("Fade-in and fade-out together exceed the card duration")
        return self
