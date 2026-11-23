"""A local file served in place that grows while it's being sent: exactly
the bytes it had when it was stat'ed go out, matching the Content-Length."""

import anyio
from app.api import archive as archive_api


def _scope() -> dict:
    return {
        "type": "http",
        "method": "GET",
        "headers": [],
        "asgi": {"spec_version": "2.4"},
    }


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _serve(response) -> tuple[dict, bytes]:
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and len(messages) == 2:
            # The log grows after the first chunk went out.
            with open(response.path, "ab") as handle:
                handle.write(b"appended while sending\n" * 100)

    anyio.run(response, _scope(), _receive, send)
    start = messages[0]
    body = b"".join(m["body"] for m in messages[1:])
    assert messages[-1]["more_body"] is False
    return dict(start["headers"]), body


def test_a_growing_file_is_sent_as_it_was_when_stated(tmp_path):
    path = tmp_path / "app.log"
    original = b"".join(f"line {i}\n".encode() for i in range(200))
    path.write_bytes(original)

    response = archive_api._SnapshotFileResponse(path, media_type="text/plain")
    response.chunk_size = 64

    headers, body = _serve(response)

    assert headers[b"content-length"] == str(len(original)).encode()
    assert body == original
    assert path.stat().st_size > len(original)


def test_an_empty_file_sends_an_empty_body(tmp_path):
    path = tmp_path / "empty.log"
    path.write_bytes(b"")

    headers, body = _serve(archive_api._SnapshotFileResponse(path, media_type="text/plain"))

    assert headers[b"content-length"] == b"0"
    assert body == b""


def test_open_serves_a_local_file_through_the_snapshot_response():
    # The viewer's open and the download both use it, so neither aborts on a
    # file that grows mid-send.
    assert issubclass(archive_api._FinishingFileResponse, archive_api._SnapshotFileResponse)
