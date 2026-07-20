from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image

from agent_app.generated_images import DeterministicStatusImageGenerator

FIXED_TIME = datetime(2026, 7, 20, 6, 30, tzinfo=UTC)


def test_status_image_is_deterministic_bounded_png() -> None:
    generator = DeterministicStatusImageGenerator(clock=lambda: FIXED_TIME)

    generated = generator.generate(engine="maf")
    repeated = generator.generate(engine="maf")

    assert generated == repeated
    assert generated.media_type == "image/png"
    assert (generated.width, generated.height) == (960, 540)
    assert generated.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(generated.content) <= 1_048_576
    assert generated.alt_text == (
        "Generated deployment status sample showing Ready, MAF engine, "
        "and creation time."
    )

    with Image.open(BytesIO(generated.content)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (960, 540)
        colors = image.getcolors(maxcolors=960 * 540)

    assert colors is not None
    assert len(colors) >= 5


@pytest.mark.parametrize(
    "canary",
    [
        "tenant-canary",
        "subscription-canary",
        "user-canary",
        "conversation-canary",
    ],
)
def test_status_image_does_not_include_identifier_canaries(canary: str) -> None:
    generated = DeterministicStatusImageGenerator(
        clock=lambda: FIXED_TIME
    ).generate(engine="maf")

    assert canary.encode() not in generated.content
    assert canary not in generated.alt_text


@pytest.mark.parametrize("engine", ["", "bad/engine", "x" * 33])
def test_status_image_rejects_invalid_engine_labels(engine: str) -> None:
    generator = DeterministicStatusImageGenerator(clock=lambda: FIXED_TIME)

    with pytest.raises(ValueError, match="engine label"):
        generator.generate(engine=engine)
