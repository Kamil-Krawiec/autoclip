import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from autoclip import library, paths
from autoclip.api import thumbnails
from autoclip.db import store
from autoclip.db.models import Clip, Job, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def fixture(initialised_db):
    file = paths.media_dir() / "source" / "source.mp4"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"synthetic")
    source = Source(id="source", type="upload", path=str(file), duration_s=20)
    store.create_source(source)
    store.create_job(Job(id="job", source_id=source.id, status="done"))
    store.replace_clips("job", [Clip(id="clip", job_id="job", start_s=10, end_s=14)])
    app = FastAPI()
    app.include_router(thumbnails.router, prefix="/api")
    return TestClient(app), file, source


@pytest.fixture
def renders(monkeypatch):
    calls = []

    def render(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"jpeg" + str(len(calls)).encode())

    monkeypatch.setattr(thumbnails.subprocess, "run", render)
    return calls


def test_cache_bounds_source_and_isolation(fixture, renders):
    client, file, _ = fixture
    assert client.get("/api/clips/missing/thumbnail").status_code == 404
    first = client.get("/api/clips/clip/thumbnail")
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    command, options = renders[0]
    assert command[command.index("-ss") + 1] == "12.000000000"
    assert command[command.index("-i") + 1] == str(file)
    assert options["timeout"] == 12
    assert client.get("/api/clips/clip/thumbnail").content == first.content
    assert len(renders) == 1
    store.update_clip("clip", start_s=11)
    assert client.get("/api/clips/clip/thumbnail").content != first.content
    assert renders[-1][0][renders[-1][0].index("-ss") + 1] == "12.500000000"
    file.write_bytes(b"changed source")
    assert client.get("/api/clips/clip/thumbnail").status_code == 200
    assert len(renders) == 3
    assert len(list((paths.root() / "thumbnails").rglob("*.jpg"))) == 1
    assert not (paths.work_dir() / "job").exists()
    store.update_clip("clip", end_s=21)
    assert client.get("/api/clips/clip/thumbnail").status_code == 422


def test_busy_and_failed_render_cleanup(fixture, monkeypatch, renders):
    client, _, _ = fixture
    thumbnails._RENDER_SLOT.acquire()
    try:
        response = client.get("/api/clips/clip/thumbnail")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "2"
        assert renders == []
    finally:
        thumbnails._RENDER_SLOT.release()

    def timeout(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 12)

    monkeypatch.setattr(thumbnails.subprocess, "run", timeout)
    assert client.get("/api/clips/clip/thumbnail").status_code == 504
    assert not list((paths.root() / "thumbnails").rglob("*.jpg"))
    assert not list((paths.root() / "thumbnails").rglob("render-*"))
    assert thumbnails._RENDER_SLOT.acquire(blocking=False)
    thumbnails._RENDER_SLOT.release()


def test_source_and_cache_symlinks(fixture, tmp_path, renders):
    client, file, _ = fixture
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private")
    file.unlink()
    file.symlink_to(outside)
    assert client.get("/api/clips/clip/thumbnail").status_code == 410
    file.unlink()
    file.write_bytes(b"restored")
    (paths.root() / "thumbnails").symlink_to(tmp_path, target_is_directory=True)
    assert client.get("/api/clips/clip/thumbnail").status_code == 410
    assert renders == []


def test_library_source_revalidated_and_audio_fallback(fixture, tmp_path, monkeypatch, renders):
    client, _, source = fixture
    root = tmp_path / "library"
    root.mkdir()
    file = root / "video.mp4"
    file.write_bytes(b"library")
    monkeypatch.setattr(library, "LIBRARY_ROOT", root)
    monkeypatch.setattr(store, "get_source", lambda _: replace(source, library_path="video.mp4"))
    assert client.get("/api/clips/clip/thumbnail").status_code == 200
    file.unlink()
    file.symlink_to(tmp_path / "absent")
    assert client.get("/api/clips/clip/thumbnail").status_code == 410
    monkeypatch.setattr(store, "get_source", lambda _: replace(source, has_video=False))
    assert client.get("/api/clips/clip/thumbnail").status_code == 404
    assert len(renders) == 1


@pytest.mark.slow
def test_real_ffmpeg_uses_nonzero_in_clip_frame(fixture):
    client, file, _ = fixture
    # First ten seconds red, next ten blue. Clip 10..14 must sample blue.
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=64x36:r=2:d=10",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=64x36:r=2:d=10",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-threads",
            "1",
            "-y",
            str(file),
        ],
        check=True,
        timeout=20,
    )
    response = client.get("/api/clips/clip/thumbnail")
    assert response.status_code == 200
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-vf",
            "scale=1:1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        input=response.content,
        capture_output=True,
        check=True,
        timeout=10,
    )
    red, green, blue = result.stdout
    assert blue > 200 and red < 20 and green < 20
