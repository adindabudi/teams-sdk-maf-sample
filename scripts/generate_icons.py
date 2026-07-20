import struct
import zlib
from collections.abc import Callable
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


PixelFunction = Callable[[int, int], tuple[int, int, int, int]]


def png(width: int, height: int, pixel: PixelFunction) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixel(x, y))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"".join(
        [
            PNG_SIGNATURE,
            chunk(b"IHDR", header),
            chunk(b"IDAT", zlib.compress(rows)),
            chunk(b"IEND", b""),
        ]
    )


def color_pixel(x: int, y: int) -> tuple[int, int, int, int]:
    inset = 36 <= x < 156 and 36 <= y < 156
    stripe = 78 <= x < 114 or 78 <= y < 114
    return (255, 255, 255, 255) if inset and stripe else (15, 108, 189, 255)


def outline_pixel(x: int, y: int) -> tuple[int, int, int, int]:
    outer = 5 <= x < 27 and 5 <= y < 27
    inner = 8 <= x < 24 and 8 <= y < 24
    return (255, 255, 255, 255) if outer and not inner else (0, 0, 0, 0)


def main() -> None:
    output = Path("appPackage")
    output.mkdir(exist_ok=True)
    (output / "color.png").write_bytes(png(192, 192, color_pixel))
    (output / "outline.png").write_bytes(png(32, 32, outline_pixel))


if __name__ == "__main__":
    main()
