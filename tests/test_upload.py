"""Upload boundaries use temporary media and never call paid providers."""

import asyncio
import errno
import io
from pathlib import Path

import pytest
from autoclip import paths
from autoclip.api import sources
from autoclip.app import create_app
from autoclip.db import store
from autoclip.pipeline import ingest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


def test_forged_media_is_rejected_without_artifacts(client):
    response = client.post("/api/sources/upload", files={"file": ("fake.mp4", b"not media")})
    assert response.status_code == 422
    assert store.list_sources() == []
    assert list(paths.media_dir().iterdir()) == []


@pytest.mark.parametrize("length", [None, "1", "10000"])
async def test_body_limit_precedes_spooling(monkeypatch, length):
    monkeypatch.setattr(sources, "MAX_UPLOAD_BYTES", 64)
    app = create_app()
    body = (
        b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x.mp4"\r\n\r\n'
        + b"x" * 100
    )
    received = iter([body[:40], body[40:]])
    sent = []

    async def receive():
        return {"type": "http.request", "body": next(received), "more_body": True}

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"multipart/form-data; boundary=boundary")]
    if length:
        headers.append((b"content-length", length.encode()))
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "POST",
            "path": "/api/sources/upload",
            "query_string": b"",
            "headers": headers,
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
            "root_path": "",
        },
        receive,
        send,
    )
    assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 413


def test_spooling_disk_full_returns_507(client, monkeypatch):
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", full)
    response = client.post("/api/sources/upload", files={"file": ("x.mp4", b"media")})
    assert response.status_code == 507


async def test_read_error_closes_upload_and_staging(monkeypatch, tmp_path):
    file = UploadFile(io.BytesIO(b"media"), filename="x.mp4")
    monkeypatch.setattr(sources.tempfile, "tempdir", str(tmp_path))

    def failed_read(*args):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(file.file, "read", failed_read)
    with pytest.raises(Exception) as error:
        await sources.upload_source(file)
    assert error.value.status_code == 507
    assert file.file.closed
    assert not list(tmp_path.glob("*.mp4"))


@pytest.mark.parametrize("ending", ["disconnect", "truncated", "limit"])
async def test_interrupted_parser_closes_spools(monkeypatch, ending):
    import tempfile

    spools = []

    def spool(*args, **kwargs):
        file = tempfile.SpooledTemporaryFile(*args, **kwargs)  # noqa: SIM115
        spools.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", spool)
    monkeypatch.setattr(sources, "MAX_UPLOAD_BYTES", 256)
    head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.mp4"\r\n\r\n'
    messages = [{"type": "http.request", "body": head + b"x", "more_body": True}]
    if ending == "disconnect":
        messages.append({"type": "http.disconnect"})
    else:
        messages.append(
            {
                "type": "http.request",
                "body": b"x" * 300 if ending == "limit" else b"",
                "more_body": False,
            }
        )
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await create_app()(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "POST",
            "path": "/api/sources/upload",
            "query_string": b"",
            "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
            "root_path": "",
        },
        receive,
        send,
    )
    assert spools and all(file.closed for file in spools)
    status = next(m for m in sent if m["type"] == "http.response.start")["status"]
    assert status == {"disconnect": 400, "truncated": 422, "limit": 413}[ending]


def test_store_failure_removes_ingested_media(client, monkeypatch):
    from autoclip.db.models import Source

    def ingest_file(path, **kwargs):
        target = paths.source_media_dir("test") / "source.mp4"
        target.parent.mkdir()
        path.rename(target)
        return Source(id="test", type="upload", path=str(target), duration_s=1)

    def failed_store(source):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(ingest, "ingest_file", ingest_file)
    monkeypatch.setattr(store, "create_source", failed_store)
    response = client.post("/api/sources/upload", files={"file": ("x.mp4", b"media")})
    assert response.status_code == 507
    assert list(paths.media_dir().iterdir()) == []


async def test_repeated_cancel_waits_for_worker_and_removes_media(monkeypatch, tmp_path):
    import threading

    from autoclip.db.models import Source

    started = threading.Event()
    finish = threading.Event()
    target = tmp_path / "owned" / "source.mp4"

    def ingest_file(path, **kwargs):
        target.parent.mkdir()
        path.rename(target)
        started.set()
        assert finish.wait(5)
        return Source(id="test", type="upload", path=str(target), duration_s=1)

    monkeypatch.setattr(ingest, "ingest_file", ingest_file)
    file = UploadFile(io.BytesIO(b"media"), filename="x.mp4")
    task = asyncio.create_task(sources.upload_source(file))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not file.file.closed
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert file.file.closed
    assert not target.parent.exists()


def test_staging_write_error_closes_upload_and_removes_staging(client, monkeypatch, tmp_path):
    real_tempfile = sources.tempfile.NamedTemporaryFile
    staged = []

    def staging(*args, **kwargs):
        file = real_tempfile(*args, dir=tmp_path, **kwargs)
        staged.append(Path(file.name))

        def full(*args):
            raise OSError(errno.ENOSPC, "disk full")

        file.write = full
        return file

    monkeypatch.setattr(sources.tempfile, "NamedTemporaryFile", staging)
    response = client.post("/api/sources/upload", files={"file": ("x.mp4", b"media")})
    assert response.status_code == 507
    assert staged and all(not file.exists() for file in staged)


def test_openapi_keeps_upload_file_contract(client):
    schema = client.get("/openapi.json").json()
    body = schema["paths"]["/api/sources/upload"]["post"]["requestBody"]
    assert body["required"]
    ref = body["content"]["multipart/form-data"]["schema"]["$ref"].split("/")[-1]
    file = schema["components"]["schemas"][ref]["properties"]["file"]
    assert (
        file.get("format") == "binary" or file.get("contentMediaType") == "application/octet-stream"
    )


@pytest.mark.parametrize(
    "headers", [{}, {"content-type": "text/plain"}, {"content-type": "multipart/form-data"}]
)
def test_missing_or_invalid_multipart_is_client_error(client, headers):
    response = client.post("/api/sources/upload", content=b"not multipart", headers=headers)
    assert response.status_code in (400, 415)
