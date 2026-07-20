from io import BytesIO

import pytest
from PIL import Image

from agent_app.file_attachments import DownloadedFile
from agent_app.image_inputs import ImageInputError, normalize_image


def encoded_image(
    *,
    image_format: str,
    size: tuple[int, int] = (80, 40),
    mode: str = "RGB",
    color: object = (20, 80, 160),
    exif: Image.Exif | None = None,
) -> bytes:
    image = Image.new(mode, size, color)
    output = BytesIO()
    save_options: dict[str, object] = {}
    if exif is not None:
        save_options["exif"] = exif
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def downloaded(*, name: str, media_type: str, content: bytes) -> DownloadedFile:
    return DownloadedFile(name=name, media_type=media_type, content=content)


def test_normalizes_jpeg_resizes_and_strips_metadata() -> None:
    exif = Image.Exif()
    exif[0x010E] = "private description"
    source = encoded_image(
        image_format="JPEG",
        size=(1600, 800),
        exif=exif,
    )

    normalized = normalize_image(
        downloaded(name="photo.jpg", media_type="image/jpeg", content=source)
    )

    assert normalized.media_type == "image/jpeg"
    assert (normalized.width, normalized.height) == (1024, 512)
    assert normalized.content.startswith(b"\xff\xd8\xff")
    assert len(normalized.content) <= 1_048_576
    with Image.open(BytesIO(normalized.content)) as image:
        assert image.mode == "RGB"
        assert image.size == (1024, 512)
        assert len(image.getexif()) == 0
        assert image.info.get("icc_profile") is None
        assert image.info.get("comment") is None


def test_normalizes_rgba_png_to_metadata_free_jpeg() -> None:
    source = encoded_image(
        image_format="PNG",
        mode="RGBA",
        color=(20, 80, 160, 100),
    )

    normalized = normalize_image(
        downloaded(name="diagram.png", media_type="image/png", content=source)
    )

    assert normalized.media_type == "image/jpeg"
    assert (normalized.width, normalized.height) == (80, 40)
    with Image.open(BytesIO(normalized.content)) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"


def test_applies_exif_orientation_before_resizing() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6
    source = encoded_image(
        image_format="JPEG",
        size=(80, 40),
        exif=exif,
    )

    normalized = normalize_image(
        downloaded(name="rotated.jpg", media_type="image/jpeg", content=source)
    )

    assert (normalized.width, normalized.height) == (40, 80)


@pytest.mark.parametrize(
    ("media_type", "content", "expected_code"),
    [
        ("application/pdf", b"%PDF-1.7", "invalid_type"),
        ("image/jpeg", encoded_image(image_format="PNG"), "invalid_image"),
        ("image/png", encoded_image(image_format="JPEG"), "invalid_image"),
    ],
)
def test_rejects_unsupported_or_spoofed_media(
    media_type: str,
    content: bytes,
    expected_code: str,
) -> None:
    with pytest.raises(ImageInputError) as error:
        normalize_image(downloaded(name="upload.bin", media_type=media_type, content=content))

    assert error.value.code == expected_code


def test_rejects_corrupt_image_without_leaking_decoder_detail() -> None:
    with pytest.raises(ImageInputError, match="could not be processed safely") as error:
        normalize_image(
            downloaded(
                name="corrupt.jpg",
                media_type="image/jpeg",
                content=b"\xff\xd8\xffnot-a-real-jpeg",
            )
        )

    assert error.value.code == "invalid_image"
    assert "decoder" not in str(error.value)


def test_rejects_animated_png() -> None:
    first = Image.new("RGB", (20, 20), "red")
    second = Image.new("RGB", (20, 20), "blue")
    output = BytesIO()
    first.save(
        output,
        format="PNG",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )

    with pytest.raises(ImageInputError) as error:
        normalize_image(
            downloaded(
                name="animated.png",
                media_type="image/png",
                content=output.getvalue(),
            )
        )

    assert error.value.code == "invalid_image"


def test_rejects_dimensions_above_twenty_megapixels() -> None:
    source = encoded_image(
        image_format="PNG",
        size=(5000, 5000),
        mode="1",
        color=1,
    )

    with pytest.raises(ImageInputError) as error:
        normalize_image(
            downloaded(name="huge.png", media_type="image/png", content=source)
        )

    assert error.value.code == "dimensions_too_large"


def test_rejects_normalized_output_above_configured_cap() -> None:
    source = encoded_image(image_format="JPEG")

    with pytest.raises(ImageInputError) as error:
        normalize_image(
            downloaded(name="photo.jpg", media_type="image/jpeg", content=source),
            max_output_bytes=64,
        )

    assert error.value.code == "normalized_too_large"
