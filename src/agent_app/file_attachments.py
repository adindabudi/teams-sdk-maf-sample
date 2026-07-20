from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

TEAMS_FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"


class FileUploadError(Exception):
    """A Teams file attachment could not be read safely."""


class FileTooLargeError(FileUploadError):
    """A Teams file attachment exceeded the configured byte limit."""


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    name: str
    media_type: str
    content: bytes


class TeamsFileDownloader:
    def __init__(self, *, client: httpx.AsyncClient, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._client = client
        self._max_bytes = max_bytes

    async def download(self, attachment: object) -> DownloadedFile:
        if getattr(attachment, "content_type", None) != TEAMS_FILE_DOWNLOAD_INFO:
            raise FileUploadError("unsupported attachment type")

        name = _safe_filename(getattr(attachment, "name", None))
        content = _content_mapping(getattr(attachment, "content", None))
        download_url = content.get("downloadUrl", content.get("download_url"))
        if not isinstance(download_url, str) or not _is_safe_download_url(download_url):
            raise FileUploadError("invalid download URL")

        try:
            async with self._client.stream(
                "GET",
                download_url,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise FileUploadError("file download failed")
                _check_content_length(response.headers.get("Content-Length"), self._max_bytes)
                chunks: list[bytes] = []
                byte_count = 0
                async for chunk in response.aiter_bytes():
                    byte_count += len(chunk)
                    if byte_count > self._max_bytes:
                        raise FileTooLargeError("file exceeds configured limit")
                    chunks.append(chunk)
                media_type = _media_type(response.headers.get("Content-Type"))
        except FileUploadError:
            raise
        except httpx.HTTPError as error:
            raise FileUploadError("file download failed") from error

        return DownloadedFile(
            name=name,
            media_type=media_type,
            content=b"".join(chunks),
        )

    async def close(self) -> None:
        await self._client.aclose()


def _safe_filename(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise FileUploadError("invalid file name")
    if "/" in value or "\\" in value or any(ord(character) < 32 for character in value):
        raise FileUploadError("invalid file name")
    return value


def _content_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python", exclude_none=True)
        if isinstance(dumped, Mapping):
            return cast(Mapping[str, Any], dumped)
    raise FileUploadError("invalid attachment metadata")


def _is_safe_download_url(value: str) -> bool:
    if len(value) > 4096:
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    )


def _check_content_length(value: str | None, max_bytes: int) -> None:
    if value is None:
        return
    try:
        content_length = int(value)
    except ValueError as error:
        raise FileUploadError("invalid content length") from error
    if content_length < 0:
        raise FileUploadError("invalid content length")
    if content_length > max_bytes:
        raise FileTooLargeError("file exceeds configured limit")


def _media_type(value: str | None) -> str:
    if value is None:
        return "application/octet-stream"
    media_type = value.partition(";")[0].strip().lower()
    if not media_type or len(media_type) > 128:
        return "application/octet-stream"
    return media_type
