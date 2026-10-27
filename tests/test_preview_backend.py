from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path

import pytest
from autoclip import paths
from autoclip.api import clips as clips_api
from autoclip.api.schemas import PreviewRequestIn
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, Job, Source, new_id
from autoclip.pipeline import captions, export
from autoclip.pipeline.reframe.croppath import CropKeyframe, CropPath, CropSegment, centre_crop
from autoclip.pipeline.runner import JobWorkspace
from autoclip.pipeline.transcript import Transcript, Word
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    with TestClient(create_app()) as value:
        yield value


@pytest.fixture
def seeded_clip(client, tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"fake-mp4")
    source = store.create_source(
        Source(
            id=new_id(), type="upload", path=str(media), duration_s=20.0, width=1920, height=1080
        )
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    clip = Clip(
        id=new_id(),
        job_id=job.id,
        start_s=2.0,
        end_s=8.0,
        start_word=0,
        end_word=1,
        status="candidate",
    )
    store.replace_clips(job.id, [clip])
    Transcript(words=[Word("Hello", 2.2, 3.0), Word("world", 3.1, 4.5)]).save(
        JobWorkspace(job.id).transcript
    )
    return clip, source


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"start_s": -0.1, "duration_s": 2.0},
        {"start_s": 0.0, "duration_s": 0.0},
        {"start_s": 0.0, "duration_s": -1.0},
        {"start_s": 0.0, "duration_s": 5.01},
        {"start_s": True, "duration_s": 2.0},
        {"start_s": 0.0, "duration_s": "2.0"},
        {"start_s": 0.0, "duration_s": 2.0, "extra": 1},
    ],
)
def test_argon_preview_schema_rejects_invalid_inputs(client, seeded_clip, bad_payload):
    clip, _ = seeded_clip
    resp = client.post(f"/api/clips/{clip.id}/preview", json=bad_payload)
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_num", [math.nan, math.inf, -math.inf])
def test_argon_preview_schema_rejects_non_finite_numbers(bad_num):
    from autoclip.api.schemas import PreviewRequestIn

    with pytest.raises(ValueError):
        PreviewRequestIn(start_s=bad_num, duration_s=1.0)
    with pytest.raises(ValueError):
        PreviewRequestIn(start_s=0.0, duration_s=bad_num)


def test_argon_filtergraph_post_ass_seek_preserves_default_export():
    style = captions.get_style("bold_pop")
    path = CropPath(
        1920,
        1080,
        [
            CropSegment(0.0, 2.0, 608, 1080, [CropKeyframe(0.0, 100.0, 0.0)]),
            CropSegment(2.0, 6.0, 1920, 1080, [CropKeyframe(2.0, 0.0, 0.0)], fit=True),
        ],
    )
    base_req = export.ExportRequest(
        source=Path("/tmp/in.mp4"),
        destination=Path("/tmp/out.mp4"),
        start_s=2.0,
        end_s=8.0,
        crop_path=path,
        words=[Word("test", 2.5, 5.5)],
        style=style,
        ratio="9:16",
        burn_captions=True,
    )
    default_fg = export.build_video_filtergraph(base_req, subtitle_name="captions.ass")
    assert default_fg.endswith("[vcat]ass=filename=captions.ass:fontsdir=fonts[vout]")
    assert "vass" not in default_fg

    default_af = export.build_audio_filtergraph(export.ExportSettings(), base_req)
    assert "atrim" not in default_af

    preview_req = export.ExportRequest(
        source=base_req.source,
        destination=base_req.destination,
        start_s=2.0,
        end_s=8.0,
        crop_path=path,
        words=base_req.words,
        style=style,
        ratio="9:16",
        burn_captions=True,
        output_start_s=1.5,
        output_duration_s=2.5,
    )
    preview_fg = export.build_video_filtergraph(preview_req, subtitle_name="captions.ass")
    assert (
        "[vcat]ass=filename=captions.ass:fontsdir=fonts[vass];"
        "[vass]trim=start=1.5000:end=4.0000,setpts=PTS-STARTPTS[vout]"
    ) in preview_fg

    preview_af = export.build_audio_filtergraph(export.ExportSettings(), preview_req)
    assert preview_af.endswith(",atrim=start=1.5000:end=4.0000,asetpts=PTS-STARTPTS")

    unburned_req = export.ExportRequest(
        source=base_req.source,
        destination=base_req.destination,
        start_s=2.0,
        end_s=8.0,
        crop_path=centre_crop(1920, 1080, 6.0),
        words=[],
        style=style,
        ratio="9:16",
        burn_captions=False,
        output_start_s=1.0,
        output_duration_s=2.0,
    )
    unburned_fg = export.build_video_filtergraph(unburned_req, subtitle_name=None)
    assert unburned_fg.endswith("[v0]trim=start=1.0000:end=3.0000,setpts=PTS-STARTPTS[vout]")


def test_argon_preview_clamps_duration_leaves_db_untouched_and_cleans_temp(
    client, seeded_clip, monkeypatch
):
    clip, _ = seeded_clip
    client.patch(
        f"/api/clips/{clip.id}/edit",
        json={
            "ratio": "1:1",
            "burn_captions": False,
            "framing": {
                "mode": "slices",
                "segments": [
                    {"start_s": 2.0, "end_s": 5.0, "framing": {"mode": "fit"}},
                    {
                        "start_s": 5.0,
                        "end_s": 8.0,
                        "framing": {
                            "mode": "manual",
                            "center_x": 0.5,
                            "center_y": 0.5,
                            "zoom": 2.0,
                        },
                    },
                ],
            },
        },
    )
    captured: list[export.ExportRequest] = []

    def fake_export(request: export.ExportRequest, *, work_dir: Path, settings=None, **kwargs):
        captured.append(request)
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        request.destination.write_bytes(b"mp4-preview-bytes")
        return request.destination

    monkeypatch.setattr(export, "export_clip", fake_export)

    out_Of_bounds = client.post(
        f"/api/clips/{clip.id}/preview", json={"start_s": 6.0, "duration_s": 1.0}
    )
    assert out_Of_bounds.status_code == 400

    resp = client.post(f"/api/clips/{clip.id}/preview", json={"start_s": 4.5, "duration_s": 5.0})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("video/mp4")
    assert resp.content == b"mp4-preview-bytes"

    assert len(captured) == 1
    req = captured[0]
    assert req.output_start_s == pytest.approx(4.5)
    assert req.output_duration_s == pytest.approx(1.5)
    assert req.ratio == "1:1"
    assert req.burn_captions is False
    assert len(req.crop_path.segments) == 2
    assert req.crop_path.segments[0].fit is True
    assert req.crop_path.segments[1].fit is False

    assert store.list_exports(clip.id) == []
    assert store.get_clip(clip.id).status == "candidate"
    previews_dir = paths.exports_dir() / "_previews"
    assert not previews_dir.exists() or list(previews_dir.iterdir()) == []


def test_argon_preview_single_flight_409_and_error_cleanup(client, seeded_clip, monkeypatch):
    clip, _ = seeded_clip
    entered = threading.Event()
    release = threading.Event()

    def blocking_export(request: export.ExportRequest, *, work_dir: Path, **kwargs):
        entered.set()
        release.wait(timeout=5.0)
        raise export.ExportError("Simulated ffmpeg failure")

    monkeypatch.setattr(export, "export_clip", blocking_export)

    results: list[int] = []
    t = threading.Thread(
        target=lambda: results.append(
            client.post(
                f"/api/clips/{clip.id}/preview", json={"start_s": 0.0, "duration_s": 1.0}
            ).status_code
        )
    )
    t.start()
    assert entered.wait(timeout=5.0)

    busy = client.post(f"/api/clips/{clip.id}/preview", json={"start_s": 0.0, "duration_s": 1.0})
    assert busy.status_code == 409

    release.set()
    t.join(timeout=5.0)
    assert results == [500]

    previews_dir = paths.exports_dir() / "_previews"
    assert not previews_dir.exists() or list(previews_dir.iterdir()) == []


def _preview_lock_is_free() -> bool:
    acquired = clips_api._PREVIEW_LOCK.acquire(blocking=False)
    if acquired:
        clips_api._PREVIEW_LOCK.release()
    return acquired


def test_preview_timeout_cancels_worker_releases_lock_and_cleans_temp(
    client, seeded_clip, monkeypatch
):
    clip, _ = seeded_clip
    monkeypatch.setattr(clips_api, "_PREVIEW_TIMEOUT_S", 0.05)
    entered = threading.Event()
    cancel_seen = threading.Event()
    allow_success = threading.Event()

    def fake_export(request, *, work_dir, settings=None, cancelled, **kwargs):
        entered.set()
        if allow_success.is_set():
            request.destination.parent.mkdir(parents=True, exist_ok=True)
            request.destination.write_bytes(b"preview-bytes")
            return request.destination
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cancelled():
                cancel_seen.set()
                raise export.ExportError("cancelled")
            time.sleep(0.005)
        raise AssertionError("preview export did not observe cancellation")

    monkeypatch.setattr(export, "export_clip", fake_export)
    status_before = store.get_clip(clip.id).status

    async def first_attempt():
        with pytest.raises(HTTPException) as exc_info:
            await clips_api.preview_clip(clip.id, PreviewRequestIn(start_s=0.0, duration_s=1.0))
        return exc_info.value

    error = asyncio.run(first_attempt())
    assert error.status_code == 504
    assert entered.is_set()
    assert cancel_seen.wait(timeout=5.0)
    assert _preview_lock_is_free()
    previews_dir = paths.exports_dir() / "_previews"
    assert not previews_dir.exists() or list(previews_dir.iterdir()) == []
    assert store.list_exports(clip.id) == []
    assert store.get_clip(clip.id).status == status_before

    allow_success.set()
    monkeypatch.setattr(clips_api, "_PREVIEW_TIMEOUT_S", 5.0)

    async def followup():
        response = await clips_api.preview_clip(
            clip.id, PreviewRequestIn(start_s=0.0, duration_s=1.0)
        )
        path = Path(response.path)
        assert path.is_file()
        assert path.read_bytes() == b"preview-bytes"
        if response.background is not None:
            await response.background()
        return path

    followup_path = asyncio.run(followup())
    assert not followup_path.exists()
    assert _preview_lock_is_free()


def test_preview_cancellation_keeps_lock_until_worker_exits(client, seeded_clip, monkeypatch):
    clip, _ = seeded_clip
    entered = threading.Event()
    cancel_seen = threading.Event()
    release_exit = threading.Event()
    worker_exited = threading.Event()

    def fake_export(request, *, work_dir, settings=None, cancelled, **kwargs):
        entered.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cancelled():
                cancel_seen.set()
                if not release_exit.wait(timeout=5.0):
                    raise AssertionError("preview worker was never released")
                worker_exited.set()
                raise export.ExportError("cancelled")
            time.sleep(0.005)
        raise AssertionError("preview export did not observe cancellation")

    monkeypatch.setattr(export, "export_clip", fake_export)
    status_before = store.get_clip(clip.id).status

    async def scenario():
        task = asyncio.create_task(
            clips_api.preview_clip(clip.id, PreviewRequestIn(start_s=0.0, duration_s=1.0))
        )
        assert await asyncio.to_thread(entered.wait, 5.0)
        previews_dir = paths.exports_dir() / "_previews"
        assert any(previews_dir.glob("preview-*"))

        assert task.cancel() is True
        assert await asyncio.to_thread(cancel_seen.wait, 5.0)
        assert task.cancel() is True

        assert not task.done()
        assert not _preview_lock_is_free()
        assert any(previews_dir.glob("preview-*"))

        release_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker_exited.is_set()
        return previews_dir

    previews_dir = asyncio.run(scenario())
    assert _preview_lock_is_free()
    assert not previews_dir.exists() or list(previews_dir.iterdir()) == []
    assert store.list_exports(clip.id) == []
    assert store.get_clip(clip.id).status == status_before


def test_argon_preview_response_send_failure_cleans_up_temp_dir(client, seeded_clip, monkeypatch):
    clip, _ = seeded_clip

    def fake_export(request: export.ExportRequest, *, work_dir: Path, settings=None, **kwargs):
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        request.destination.write_bytes(b"mp4-preview-bytes")
        return request.destination

    monkeypatch.setattr(export, "export_clip", fake_export)

    async def scenario():
        response = await clips_api.preview_clip(
            clip.id, PreviewRequestIn(start_s=0.0, duration_s=1.0)
        )
        parent_dir = Path(response.path).parent
        assert parent_dir.exists()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/preview",
            "headers": [],
        }

        async def fake_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def failing_send(message):
            raise RuntimeError("Simulated client disconnect during send")

        with pytest.raises(RuntimeError, match="Simulated client disconnect"):
            await response(scope, fake_receive, failing_send)

        assert not parent_dir.exists()

    asyncio.run(scenario())
    previews_dir = paths.exports_dir() / "_previews"
    assert not previews_dir.exists() or list(previews_dir.iterdir()) == []
    assert _preview_lock_is_free()
