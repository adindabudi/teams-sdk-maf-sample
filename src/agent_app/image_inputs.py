import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from agent_app.file_attachments import DownloadedFile

MAX_IMAGE_PIXELS = 20_000_000
MAX_LONG_EDGE = 1024
MAX_NORMALIZED_BYTES = 1_048_576
JPEG_SIGNATURE = b"\xff\xd8\xff"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    content: bytes
    media_type: Literal["image/jpeg"]
    width: int
    height: int


class ImageInputError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The image could not be processed safely")


def normalize_image(
    downloaded: DownloadedFile,
    *,
    max_output_bytes: int = MAX_NORMALIZED_BYTES,
) -> NormalizedImage:
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    if downloaded.media_type not in {"image/jpeg", "image/png"}:
        raise ImageInputError("invalid_type")
    if not _signature_matches(downloaded.media_type, downloaded.content):
        raise ImageInputError("invalid_image")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(downloaded.content)) as probe:
                _validate_dimensions(probe.size)
                _validate_single_frame(probe)
                probe.verify()

            with Image.open(BytesIO(downloaded.content)) as source:
                _validate_dimensions(source.size)
                _validate_single_frame(source)
                source.load()
                oriented = ImageOps.exif_transpose(source)
                rgb = _to_rgb(oriented)

        rgb.thumbnail(
            (MAX_LONG_EDGE, MAX_LONG_EDGE),
            resample=Image.Resampling.LANCZOS,
        )
        output = BytesIO()
        rgb.save(
            output,
            format="JPEG",
            quality=85,
            optimize=True,
        )
        content = output.getvalue()
    except ImageInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ImageInputError("dimensions_too_large") from error
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as error:
        raise ImageInputError("invalid_image") from error

    if len(content) > max_output_bytes:
        raise ImageInputError("normalized_too_large")
    return NormalizedImage(
        content=content,
        media_type="image/jpeg",
        width=rgb.width,
        height=rgb.height,
    )


def _signature_matches(media_type: str, content: bytes) -> bool:
    if media_type == "image/jpeg":
        return content.startswith(JPEG_SIGNATURE)
    if media_type == "image/png":
        return content.startswith(PNG_SIGNATURE)
    return False


def _validate_dimensions(size: tuple[int, int]) -> None:
    width, height = size
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise ImageInputError("dimensions_too_large")


def _validate_single_frame(image: Image.Image) -> None:
    if getattr(image, "n_frames", 1) != 1:
        raise ImageInputError("invalid_image")


def _to_rgb(image: Image.Image) -> Image.Image:
    if "A" not in image.getbands():
        return image.convert("RGB")

    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background
