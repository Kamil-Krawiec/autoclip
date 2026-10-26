from __future__ import annotations

from pathlib import Path

import pytest
from autoclip import library
from autoclip.api import library as library_api
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, ClipEdit, Job, Source
from autoclip.pipeline import ingest
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home):
    with TestClient(create_app()) as test_client:
        yield test_client


def test_browse_is_shallow_sorted_and_filters_extensions(tmp_path, monkeypatch, client):
    root = tmp_path / "media"
    root.mkdir()
    (root / "z.mp4").write_bytes(b"video")
    (root / "a.txt").write_text("ignored")
    (root / "folder").mkdir()
    (root / "folder" / "inside.mp4").write_bytes(b"nested")
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)

    response = client.get("/api/library")
    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "directory": "",
        "truncated": False,
        "entries": [
            {"path": "folder", "name": "folder", "is_dir": True, "size_bytes": None},
            {"path": "z.mp4", "name": "z.mp4", "is_dir": False, "size_bytes": 5},
        ],
    }


def test_missing_mount_and_invalid_paths(tmp_path, monkeypatch, client):
    root = tmp_path / "absent"
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    assert client.get("/api/library").json() == {
        "available": False,
        "directory": "",
        "entries": [],
        "truncated": False,
    }
    assert client.get("/api/library", params={"directory": "missing"}).status_code == 404
    assert client.get("/api/library", params={"directory": "../"}).status_code == 404


def test_rejects_symlink_escape_and_revalidates_after_import(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not media")
    (root / "link.mp4").symlink_to(outside)
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    with pytest.raises(library.LibraryError):
        library.resolve_library_path("../outside.mp4")
    with pytest.raises(library.LibraryError):
        library.resolve_library_path("link.mp4")

    original = root / "source.mp4"
    original.write_bytes(b"still here")
    source = Source(id="library-test", type="upload", path=str(original), library_path="source.mp4")
    assert library.media_path(source) == original
    original.unlink()
    original.symlink_to(outside)
    with pytest.raises(library.LibraryError):
        library.media_path(source)
    assert outside.read_bytes() == b"not media"


def test_import_registers_reference_without_copy(tmp_path, monkeypatch, client):
    root = tmp_path / "media"
    root.mkdir()
    source_file = root / "synthetic.mp4"
    source_file.write_bytes(b"fixture bytes")
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    from types import SimpleNamespace

    monkeypatch.setattr(
        ingest,
        "_probe_and_validate",
        lambda _path: SimpleNamespace(
            duration_s=2.0,
            width=16,
            height=9,
            fps=25.0,
            has_audio=True,
            has_video=True,
        ),
    )

    response = client.post("/api/library/import", json={"path": "synthetic.mp4"})
    assert response.status_code == 201, response.text
    item = store.get_source(response.json()["id"])
    assert item is not None
    assert item.library_path == "synthetic.mp4"
    assert item.path == str(source_file)
    assert source_file.read_bytes() == b"fixture bytes"


def _make_media(path: Path, *, audio: bool) -> None:
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is not installed in this test image")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if audio:
        command += [
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-shortest",
        ]
        command += ["-c:v", "mpeg4", "-c:a", "aac"]
    else:
        command += ["-f", "lavfi", "-i", "color=c=blue:s=160x90:d=1", "-c:v", "mpeg4"]
    subprocess.run([*command, str(path)], check=True)


def test_real_media_import_and_no_audio_rejection(tmp_path, monkeypatch, client):
    root = tmp_path / "media"
    root.mkdir()
    media = root / "real.mp4"
    silent = root / "silent.mp4"
    _make_media(media, audio=True)
    _make_media(silent, audio=False)
    original_hash = media.read_bytes()
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)

    response = client.post("/api/library/import", json={"path": media.name})
    assert response.status_code == 201, response.text
    registered = store.get_source(response.json()["id"])
    assert registered and registered.library_path == media.name
    assert media.read_bytes() == original_hash
    assert client.post("/api/library/import", json={"path": silent.name}).status_code == 422
    assert silent.exists()


def test_entry_cap_and_permission_errors(tmp_path, monkeypatch, client):
    root = tmp_path / "media"
    root.mkdir()
    for index in range(1001):
        (root / f"{index:04}.mp4").touch()
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    response = client.get("/api/library")
    assert response.status_code == 200
    assert response.json()["truncated"] is True
    assert len(response.json()["entries"]) == 1000

    def denied(_path):
        raise PermissionError

    monkeypatch.setattr(library_api.os, "scandir", denied)
    assert client.get("/api/library").status_code == 403


def test_v2_migration_preserves_existing_rows():
    import sqlite3

    from autoclip.db import schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema._migration_v1(conn)
    schema._migration_v2(conn)
    conn.execute(
        "INSERT INTO sources (id, type, path, title, created_at) "
        "VALUES ('s', 'upload', '/old.mp4', 'old', 'now')"
    )
    conn.execute(
        "INSERT INTO jobs (id, source_id, status, created_at, updated_at) "
        "VALUES ('j', 's', 'done', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO clips (id, job_id, start_s, end_s, created_at) VALUES ('c', 'j', 0, 1, 'now')"
    )
    conn.execute(
        "INSERT INTO clip_edits (clip_id, caption_style, ratio, burn_captions, updated_at) "
        "VALUES ('c', 'clean_lower', '1:1', 0, 'now')"
    )
    conn.execute(
        "INSERT INTO exports (id, clip_id, path, created_at) "
        "VALUES ('e', 'c', '/export.mp4', 'now')"
    )
    schema._migration_v3(conn)
    source_row = conn.execute("SELECT path, library_path FROM sources WHERE id='s'").fetchone()
    assert tuple(source_row) == ("/old.mp4", None)
    assert conn.execute("SELECT id FROM jobs").fetchone()[0] == "j"
    assert tuple(
        conn.execute("SELECT caption_style, ratio, burn_captions FROM clip_edits").fetchone()
    ) == ("clean_lower", "1:1", 0)
    assert conn.execute("SELECT path FROM exports").fetchone()[0] == "/export.mp4"
    conn.close()


def test_missing_library_source_blocks_job_and_media(tmp_path, monkeypatch, client):
    root = tmp_path / "media"
    root.mkdir()
    media = root / "gone.mp4"
    media.write_bytes(b"temporary")
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    source = Source(id="gone-source", type="upload", path=str(media), library_path="gone.mp4")
    store.create_source(source)
    job_id = "gone-job"
    store.create_job(Job(id=job_id, source_id=source.id))
    media.unlink()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private")
    media.symlink_to(outside)
    assert client.post("/api/jobs", json={"source_id": source.id}).status_code == 410
    assert client.get(f"/api/jobs/{job_id}/media").status_code == 410
    clip = Clip(id="gone-clip", job_id=job_id, start_s=0, end_s=1)
    store.replace_clips(job_id, [clip])
    store.upsert_clip_edit(ClipEdit(clip_id=clip.id, edited_words=[]))
    assert client.post(f"/api/clips/{clip.id}/export", json={}).status_code == 410
    assert outside.read_bytes() == b"private"


def test_library_source_can_be_exported_without_copying(tmp_path, monkeypatch, client):
    root = tmp_path / "media"
    root.mkdir()
    media = root / "export.mp4"
    _make_media(media, audio=True)
    before = media.read_bytes()
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    from autoclip.pipeline import ingest

    info = ingest._probe_and_validate(media)
    source = store.create_source(
        Source(
            id="export-source",
            type="upload",
            path=str(media),
            library_path=media.name,
            duration_s=info.duration_s,
            width=info.width,
            height=info.height,
            fps=info.fps,
            has_audio=info.has_audio,
            has_video=info.has_video,
        )
    )
    job = store.create_job(Job(id="export-job", source_id=source.id))
    clip = Clip(id="export-clip", job_id=job.id, start_s=0, end_s=min(1, info.duration_s))
    store.replace_clips(job.id, [clip])
    store.upsert_clip_edit(
        ClipEdit(clip_id=clip.id, edited_words=[], ratio="16:9", burn_captions=False)
    )

    response = client.post(f"/api/clips/{clip.id}/export", json={"ratio": "16:9"})
    assert response.status_code == 201, response.text
    exports = store.list_exports(clip.id)
    assert len(exports) == 1 and Path(exports[0].path).is_file()
    assert media.read_bytes() == before


def test_import_permission_error_is_forbidden(tmp_path, monkeypatch, client):
    root = tmp_path / "media"
    root.mkdir()
    media = root / "private.mp4"
    media.write_bytes(b"unreadable")
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    original_open = Path.open

    def denied(path, *args, **kwargs):
        if path == media:
            raise PermissionError("denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    response = client.post("/api/library/import", json={"path": media.name})
    assert response.status_code == 403
    assert store.list_sources() == []


def test_unreadable_mount_is_forbidden(tmp_path, monkeypatch, client):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    original_resolve = Path.resolve

    def denied(path, *args, **kwargs):
        if path == root:
            raise PermissionError("private host path")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", denied)
    response = client.get("/api/library")
    assert response.status_code == 403
    assert "private host path" not in response.text
