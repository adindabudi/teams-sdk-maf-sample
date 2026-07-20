from dataclasses import dataclass

import httpx
import pytest

from agent_app.file_attachments import (
    TEAMS_FILE_DOWNLOAD_INFO,
    FileTooLargeError,
    FileUploadError,
    TeamsFileDownloader,
)


@dataclass
class Attachment:
    content_type: str = TEAMS_FILE_DOWNLOAD_INFO
    name: str = "report.txt"
    content: object = None

    def __post_init__(self) -> None:
        if self.content is None:
            self.content = {
                "downloadUrl": "https://download.example.invalid/report.txt"
            }


async def test_downloader_fetches_bounded_https_file() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"hello world",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    downloader = TeamsFileDownloader(client=client, max_bytes=32)

    downloaded = await downloader.download(Attachment())

    assert downloaded.name == "report.txt"
    assert downloaded.media_type == "text/plain"
    assert downloaded.content == b"hello world"
    assert [str(request.url) for request in requests] == [
        "https://download.example.invalid/report.txt"
    ]
    await downloader.close()
    assert client.is_closed is True


@pytest.mark.parametrize(
    "attachment",
    [
        Attachment(content_type="application/octet-stream"),
        Attachment(name="../report.txt"),
        Attachment(name="folder\\report.txt"),
        Attachment(content={"downloadUrl": "http://download.example.invalid/file"}),
        Attachment(content={}),
    ],
)
async def test_downloader_rejects_unsafe_metadata_before_network(
    attachment: Attachment,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"should not be read")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    downloader = TeamsFileDownloader(client=client, max_bytes=32)

    with pytest.raises(FileUploadError):
        await downloader.download(attachment)

    assert requests == []
    await downloader.close()


async def test_downloader_rejects_content_length_over_limit() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=b"0123456789abcdef",
            headers={"Content-Length": "16"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    downloader = TeamsFileDownloader(client=client, max_bytes=8)

    with pytest.raises(FileTooLargeError):
        await downloader.download(Attachment())

    await downloader.close()



class ChunkedStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b"123456"
        yield b"78901"


async def test_downloader_stops_chunked_response_over_limit() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            stream=ChunkedStream(),
            headers={"Content-Type": "application/octet-stream"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    downloader = TeamsFileDownloader(client=client, max_bytes=10)

    with pytest.raises(FileTooLargeError):
        await downloader.download(Attachment())

    await downloader.close()


async def test_downloader_rejects_redirect_without_following_it() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            302,
            headers={"Location": "https://other.example.invalid/file"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    downloader = TeamsFileDownloader(client=client, max_bytes=32)

    with pytest.raises(FileUploadError):
        await downloader.download(Attachment())

    await downloader.close()
