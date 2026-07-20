import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Literal, Protocol

from PIL import Image, ImageDraw, ImageFont

WIDTH = 960
HEIGHT = 540
_ENGINE_LABEL = re.compile(r"[A-Za-z0-9 ._-]{1,32}")


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    content: bytes
    media_type: Literal["image/png"]
    width: int
    height: int
    alt_text: str


class ImageGenerator(Protocol):
    def generate(self, *, engine: str) -> GeneratedImage: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeterministicStatusImageGenerator:
    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock

    def generate(self, *, engine: str) -> GeneratedImage:
        if _ENGINE_LABEL.fullmatch(engine) is None:
            raise ValueError("engine label must contain 1-32 safe characters")

        generated_at = self._clock()
        if generated_at.utcoffset() is None:
            raise ValueError("image generation clock must return an aware datetime")
        timestamp = generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        engine_label = engine.upper()

        image = Image.new("RGB", (WIDTH, HEIGHT), "#F5F6F2")
        draw = ImageDraw.Draw(image)
        eyebrow_font = ImageFont.load_default(size=20)
        status_font = ImageFont.load_default(size=72)
        label_font = ImageFont.load_default(size=18)
        value_font = ImageFont.load_default(size=34)
        footer_font = ImageFont.load_default(size=17)

        draw.rounded_rectangle(
            (40, 40, WIDTH - 40, HEIGHT - 40),
            radius=28,
            fill="#FFFFFF",
            outline="#D8DAD4",
            width=2,
        )
        draw.text(
            (82, 78),
            "DEPLOYMENT SNAPSHOT",
            font=eyebrow_font,
            fill="#343734",
        )
        draw.rounded_rectangle(
            (80, 128, 414, 284),
            radius=22,
            fill="#E3F3E8",
        )
        draw.ellipse((110, 165, 142, 197), fill="#18864B")
        draw.text((166, 154), "READY", font=status_font, fill="#145C37")

        draw.rounded_rectangle(
            (448, 128, 878, 284),
            radius=22,
            fill="#E6F0FA",
        )
        draw.text((482, 158), "ENGINE", font=label_font, fill="#315B7D")
        draw.text((482, 198), engine_label, font=value_font, fill="#173E5C")

        draw.line((80, 330, 878, 330), fill="#D8DAD4", width=2)
        draw.text((82, 366), "GENERATED", font=label_font, fill="#6B5A27")
        draw.text((82, 404), timestamp, font=value_font, fill="#4B421F")
        draw.rounded_rectangle(
            (708, 372, 878, 438),
            radius=18,
            fill="#F8EBC5",
        )
        draw.text((742, 393), "PRIVATE", font=eyebrow_font, fill="#66521D")
        draw.text(
            (82, 472),
            "Deterministic sample image - no user or tenant data",
            font=footer_font,
            fill="#626660",
        )

        output = BytesIO()
        image.save(output, format="PNG", compress_level=9)
        return GeneratedImage(
            content=output.getvalue(),
            media_type="image/png",
            width=WIDTH,
            height=HEIGHT,
            alt_text=(
                "Generated deployment status sample showing Ready, "
                f"{engine_label} engine, and creation time."
            ),
        )
