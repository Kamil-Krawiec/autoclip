from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from autoclip import db
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, ClipEdit, Job, Source, new_id
from autoclip.pipeline import ffmpeg
from autoclip.pipeline.framing import resolve_crop_path
from autoclip.pipeline.reframe.croppath import (
    CropKeyframe,
    CropPath,
    CropSegment,
    Strategy,
    centre_crop,
)
from autoclip.pipeline.runner import JobWorkspace
from autoclip.pipeline.transcript import Transcript, Word
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def sample_setup(client):
    source = store.create_source(
        Source(
            id=new_id(),
            type="upload",
            path="fixture.mp4",
            duration_s=30.0,
            width=1920,
            height=1080,
        )
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id))
    clip = Clip(id=new_id(), job_id=job.id, start_s=10.0, end_s=25.0)
    store.replace_clips(job.id, [clip])
    Transcript(words=[Word("Test", 10.5, 12.0), Word("Slices", 15.0, 20.0)]).save(
        JobWorkspace(job.id).transcript
    )
    return source, job, clip


def test_api_patch_slices_persists_and_resets_to_auto(client, sample_setup):
    _, _, clip = sample_setup
    url = f"/api/clips/{clip.id}/edit"

    slices_payload = {
        "framing": {
            "mode": "slices",
            "segments": [
                {"start_s": 10.0, "end_s": 15.0, "framing": {"mode": "fit"}},
                {
                    "start_s": 15.0,
                    "end_s": 25.0,
                    "framing": {
                        "mode": "manual",
                        "center_x": 0.4,
                        "center_y": 0.5,
                        "zoom": 1.5,
                    },
                },
            ],
        }
    }
    res = client.patch(url, json=slices_payload)
    assert res.status_code == 200, res.text
    saved = res.json()["framing"]
    assert saved["mode"] == "slices"
    assert len(saved["segments"]) == 2
    assert saved["segments"][0]["framing"]["mode"] == "fit"

    db.reset_connections()
    edit = store.get_clip_edit(clip.id)
    assert edit is not None and edit.framing is not None
    assert edit.framing["mode"] == "slices"
    assert len(edit.framing["segments"]) == 2

    # Explicit reset to Auto should clear framing back to None / auto
    reset_res = client.patch(url, json={"framing": {"mode": "auto"}})
    assert reset_res.status_code == 200
    assert reset_res.json()["framing"] is None

    db.reset_connections()
    assert store.get_clip_edit(clip.id).framing is None


def test_split_screen_saved_and_resolved_per_slice(client, sample_setup):
    source, job, clip = sample_setup
    slices = {
        "mode": "slices",
        "segments": [
            {"start_s": 10.0, "end_s": 15.0, "framing": {"mode": "auto"}},
            {"start_s": 15.0, "end_s": 25.0, "framing": {"mode": "split"}},
        ],
    }
    response = client.patch(f"/api/clips/{clip.id}/edit", json={"framing": slices})
    assert response.status_code == 200, response.text
    db.reset_connections()
    edit = store.get_clip_edit(clip.id)
    assert edit.framing == slices
    path = resolve_crop_path(source, clip, job, edit, None)
    assert not path.segments[0].split
    assert path.segments[-1].split
    assert CropPath.from_dict(path.to_dict()).segments[-1].split


@pytest.mark.slow
def test_split_screen_render_preserves_both_halves(client, tmp_path: Path):
    source_path = tmp_path / "split-source.mp4"
    subprocess.run(
        [
            ffmpeg.ffmpeg_path(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=640x360:r=5:d=2,drawbox=x=320:y=0:w=320:h=360:color=blue:t=fill",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source_path),
        ],
        check=True,
        capture_output=True,
    )
    source = store.create_source(
        Source(
            id=new_id(), type="upload", path=str(source_path), duration_s=2, width=640, height=360
        )
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    clip = Clip(id=new_id(), job_id=job.id, start_s=0, end_s=2)
    store.replace_clips(job.id, [clip])
    Transcript(words=[Word("Test", 0, 1)]).save(JobWorkspace(job.id).transcript)
    res = client.patch(
        f"/api/clips/{clip.id}/edit", json={"framing": {"mode": "split"}, "burn_captions": False}
    )
    assert res.status_code == 200, res.text
    result = client.post(f"/api/clips/{clip.id}/export", json={})
    assert result.status_code == 201, result.text
    path = Path(store.get_export(result.json()["id"]).path)
    pixels = subprocess.run(
        [
            ffmpeg.ffmpeg_path(),
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=108:192",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout

    def pixel(x, y):
        offset = (y * 108 + x) * 3
        return pixels[offset : offset + 3]

    assert pixel(54, 48)[0] > 200 and pixel(54, 48)[2] < 30
    assert pixel(54, 144)[2] > 200 and pixel(54, 144)[0] < 30


def test_invalid_slices_framing_rejected(client, sample_setup):
    _, _, clip = sample_setup
    url = f"/api/clips/{clip.id}/edit"
    invalid_cases = [
        {
            "mode": "slices",
            "segments": [
                {
                    "start_s": 10.0,
                    "end_s": 20.0,
                    "framing": {
                        "mode": "slices",
                        "segments": [{"start_s": 10.0, "end_s": 20.0, "framing": {"mode": "fit"}}],
                    },
                }
            ],
        },
        {
            "mode": "slices",
            "segments": [
                {"start_s": 10.0, "end_s": 14.0, "framing": {"mode": "fit"}},
                {"start_s": 15.0, "end_s": 20.0, "framing": {"mode": "auto"}},
            ],
        },
        {
            "mode": "slices",
            "segments": [
                {"start_s": 10.0, "end_s": 16.0, "framing": {"mode": "fit"}},
                {"start_s": 15.0, "end_s": 20.0, "framing": {"mode": "auto"}},
            ],
        },
        {
            "mode": "slices",
            "segments": [
                {"start_s": 15.0, "end_s": 20.0, "framing": {"mode": "auto"}},
                {"start_s": 10.0, "end_s": 15.0, "framing": {"mode": "fit"}},
            ],
        },
        {
            "mode": "slices",
            "segments": [{"start_s": 15.0, "end_s": 15.0, "framing": {"mode": "auto"}}],
        },
        {
            "mode": "slices",
            "segments": [{"start_s": -1.0, "end_s": 10.0, "framing": {"mode": "fit"}}],
        },
        {
            "mode": "slices",
            "segments": [{"start_s": 20.0, "end_s": 35.0, "framing": {"mode": "fit"}}],
        },
        {
            "mode": "slices",
            "segments": [
                {"start_s": "10.0", "end_s": 20.0, "framing": {"mode": "fit"}},
            ],
        },
        # Exceeds max 64 segments
        {
            "mode": "slices",
            "segments": [
                {
                    "start_s": 10.0 + i * 0.1,
                    "end_s": 10.0 + (i + 1) * 0.1,
                    "framing": {"mode": "fit"},
                }
                for i in range(65)
            ],
        },
    ]
    for invalid in invalid_cases:
        res = client.patch(url, json={"framing": invalid})
        assert res.status_code in (400, 422)

    # Raw JSON non-finite testing without relying on httpx float serialization
    for non_finite in ("Infinity", "-Infinity", "NaN"):
        raw = (
            '{"framing":{"mode":"slices","segments":'
            f'[{{"start_s":10.0,"end_s":{non_finite},"framing":{{"mode":"fit"}}}}]]}}'
        )
        res = client.patch(url, content=raw, headers={"Content-Type": "application/json"})
        assert res.status_code in (400, 422)


def test_trim_and_reexpand_preserves_stored_slices(client, sample_setup):
    source, job, clip = sample_setup
    url_edit = f"/api/clips/{clip.id}/edit"
    url_clip = f"/api/clips/{clip.id}"

    slices = {
        "mode": "slices",
        "segments": [
            {"start_s": 10.0, "end_s": 15.0, "framing": {"mode": "fit"}},
            {
                "start_s": 15.0,
                "end_s": 20.0,
                "framing": {"mode": "manual", "center_x": 0.5, "center_y": 0.5, "zoom": 1.0},
            },
            {"start_s": 20.0, "end_s": 25.0, "framing": {"mode": "auto"}},
        ],
    }
    assert client.patch(url_edit, json={"framing": slices}).status_code == 200

    # Trim clip to 14.0 .. 18.0 (cuts head 10..14 and tail 18..25)
    trim_res = client.patch(url_clip, json={"start_s": 14.0, "end_s": 18.0})
    assert trim_res.status_code == 200

    # Verify saved ClipEdit retains all original slices exactly
    db.reset_connections()
    edit = store.get_clip_edit(clip.id)
    assert edit.framing == slices

    # Re-expand clip beyond original slices: 8.0 .. 28.0
    expand_res = client.patch(url_clip, json={"start_s": 8.0, "end_s": 28.0})
    assert expand_res.status_code == 200

    # Slices are still preserved in DB
    db.reset_connections()
    assert store.get_clip_edit(clip.id).framing == slices

    # When resolved for expanded clip 8..28, head (8..10) and tail (25..28) must fall back to Auto
    expanded_clip = store.get_clip(clip.id)
    cached = centre_crop(source.width, source.height, expanded_clip.duration_s)
    resolved = resolve_crop_path(source, expanded_clip, job, edit, cached)
    assert resolved.duration_s == pytest.approx(20.0)
    assert len(resolved.segments) >= 3
    assert resolved.segments[0].start_s == pytest.approx(0.0)
    assert resolved.segments[0].end_s == pytest.approx(2.0)
    assert not resolved.segments[0].fit  # Fallback head is Auto


def test_clip_independence_and_no_mutation(client, sample_setup):
    source, job, clip1 = sample_setup
    clip2 = Clip(id=new_id(), job_id=job.id, start_s=5.0, end_s=15.0)
    store.replace_clips(job.id, [clip1, clip2])

    slices1 = {
        "mode": "slices",
        "segments": [{"start_s": 10.0, "end_s": 25.0, "framing": {"mode": "fit"}}],
    }
    assert client.patch(f"/api/clips/{clip1.id}/edit", json={"framing": slices1}).status_code == 200

    # clip2 edit should remain untouched
    edit2 = store.get_clip_edit(clip2.id)
    assert edit2 is None or edit2.framing is None


def test_all_auto_boundaries_retained_and_interpolates_keyframes(sample_setup):
    source, job, clip = sample_setup
    # Authored slices where all segments have mode: auto
    framing = {
        "mode": "slices",
        "segments": [
            {"start_s": 10.0, "end_s": 15.0, "framing": {"mode": "auto"}},
            {"start_s": 15.0, "end_s": 25.0, "framing": {"mode": "auto"}},
        ],
    }
    edit = ClipEdit(clip_id=clip.id, framing=framing)

    # Create dynamic cached CropPath spanning 10..25s (15s duration) with panning keyframes
    cached = CropPath(
        source_width=1920,
        source_height=1080,
        segments=[
            CropSegment(
                start_s=0.0,
                end_s=15.0,
                width=608,
                height=1080,
                keyframes=[
                    CropKeyframe(t=0.0, x=100.0, y=0.0),
                    CropKeyframe(t=15.0, x=400.0, y=0.0),
                ],
                strategy=Strategy.TRACK,
            )
        ],
    )
    cached_before = cached.to_dict()
    resolved = resolve_crop_path(source, clip, job, edit, cached)

    # Must not mutate original cached object
    assert cached.to_dict() == cached_before
    # All-auto boundaries must NOT be normalized away into a single segment
    assert len(resolved.segments) == 2
    assert resolved.segments[0].start_s == pytest.approx(0.0)
    assert resolved.segments[0].end_s == pytest.approx(5.0)
    assert resolved.segments[1].start_s == pytest.approx(5.0)
    assert resolved.segments[1].end_s == pytest.approx(15.0)

    # Boundary keyframe at t=5.0s should be interpolated:
    # slope = (400 - 100) / 15 = 20 px/s => at t=5s, x = 100 + 5*20 = 200.0
    last_kf_seg0 = resolved.segments[0].keyframes[-1]
    first_kf_seg1 = resolved.segments[1].keyframes[0]
    assert last_kf_seg0.t == pytest.approx(5.0)
    assert last_kf_seg0.x == pytest.approx(200.0)
    assert first_kf_seg1.t == pytest.approx(5.0)
    assert first_kf_seg1.x == pytest.approx(200.0)


@pytest.mark.slow
def test_real_render_synthetic_slices(client, tmp_path: Path):
    # Source thirds make Fit, right-side Manual, and centre Auto visibly distinct.
    source_path = tmp_path / "synthetic_25s.mp4"
    subprocess.run(
        [
            ffmpeg.ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                "color=c=red:s=640x360:r=12:d=25,"
                "drawbox=x=213:y=0:w=214:h=360:color=green:t=fill,"
                "drawbox=x=427:y=0:w=213:h=360:color=blue:t=fill"
            ),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source_path),
        ],
        check=True,
        capture_output=True,
    )
    source = store.create_source(
        Source(
            id=new_id(),
            type="upload",
            path=str(source_path),
            duration_s=25.0,
            width=640,
            height=360,
        )
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    clip = Clip(id=new_id(), job_id=job.id, start_s=10.0, end_s=25.0, end_word=0)
    store.replace_clips(job.id, [clip])
    Transcript(words=[Word("Test", 10.0, 25.0)]).save(JobWorkspace(job.id).transcript)

    slices = {
        "mode": "slices",
        "segments": [
            {"start_s": 10.0, "end_s": 14.0, "framing": {"mode": "fit"}},
            {
                "start_s": 14.0,
                "end_s": 20.0,
                "framing": {"mode": "manual", "center_x": 0.9, "center_y": 0.5, "zoom": 1.2},
            },
            {"start_s": 20.0, "end_s": 25.0, "framing": {"mode": "auto"}},
        ],
    }
    patch_res = client.patch(
        f"/api/clips/{clip.id}/edit", json={"framing": slices, "ratio": "9:16"}
    )
    assert patch_res.status_code == 200, patch_res.text

    export_res = client.post(f"/api/clips/{clip.id}/export", json={})
    assert export_res.status_code == 201
    record = store.get_export(export_res.json()["id"])
    out_path = Path(record.path)
    assert out_path.exists()

    probe_cmd = [
        ffmpeg.ffmpeg_path(),
        "-v",
        "error",
        "-i",
        str(out_path),
        "-f",
        "null",
        "-",
    ]
    decode_check = subprocess.run(probe_cmd, capture_output=True)
    assert decode_check.returncode == 0

    info = ffmpeg.probe(out_path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration_s == pytest.approx(15.0, abs=0.5)
    assert info.has_audio

    def frame_at(seconds: float) -> bytes:
        return subprocess.run(
            [
                ffmpeg.ffmpeg_path(),
                "-v",
                "error",
                "-ss",
                str(seconds),
                "-i",
                str(out_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=108:192",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-an",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout

    def pixel(frame: bytes, x: int, y: int) -> tuple[int, int, int]:
        offset = (y * 108 + x) * 3
        return frame[offset], frame[offset + 1], frame[offset + 2]

    def close(actual, expected) -> bool:
        return all(abs(a - b) <= 20 for a, b in zip(actual, expected, strict=True))

    fit = frame_at(2.0)
    assert close(pixel(fit, 5, 96), (255, 0, 0))
    assert close(pixel(fit, 103, 96), (0, 0, 255))

    manual = frame_at(7.0)
    assert close(pixel(manual, 54, 96), (0, 0, 255))

    auto = frame_at(12.0)
    assert close(pixel(auto, 54, 96), (0, 128, 0))
