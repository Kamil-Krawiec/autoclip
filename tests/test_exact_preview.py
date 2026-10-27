from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from autoclip import db
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, Job, Source, new_id
from autoclip.pipeline import export, ffmpeg
from autoclip.pipeline.export import ExportRequest
from autoclip.pipeline.runner import JobWorkspace
from autoclip.pipeline.transcript import Transcript, Word
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def clip_and_source(client, tmp_path: Path):
    source_file = tmp_path / "test_source.bin"
    source_file.write_bytes(b"dummy source video content")
    source = store.create_source(
        Source(
            id=new_id(),
            type="upload",
            path=str(source_file),
            duration_s=10.0,
            width=320,
            height=180,
        )
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    clip = Clip(id=new_id(), job_id=job.id, start_s=2.0, end_s=7.0, end_word=1)
    store.replace_clips(job.id, [clip])

    transcript = Transcript(
        words=[
            Word("Witaj", 2.2, 3.5),
            Word("Świecie", 3.6, 5.0),
        ]
    )
    transcript.save(JobWorkspace(job.id).transcript)
    return clip, source, source_file


@pytest.mark.parametrize(
    "payload,expected_status",
    [
        ({"start_s": -0.1, "duration_s": 2.0}, 422),
        ({"start_s": 0.0, "duration_s": 0.0}, 422),
        ({"start_s": 0.0, "duration_s": 5.1}, 422),
        ({"start_s": "0.0", "duration_s": 2.0}, 422),
        ({"start_s": 5.5, "duration_s": 1.0}, 400),
    ],
)
def test_preview_invalid_requests_rejected(client, clip_and_source, payload, expected_status):
    clip, _, _ = clip_and_source
    res = client.post(f"/api/clips/{clip.id}/preview", json=payload)
    assert res.status_code == expected_status


def test_preview_does_not_mutate_db_or_create_export(
    client, clip_and_source, monkeypatch, tmp_path
):
    clip, _, _ = clip_and_source

    captured_requests = []

    import autoclip.api.clips as api_clips_module

    def mock_export_clip(req: ExportRequest, *args, **kwargs):
        captured_requests.append(req)
        req.destination.parent.mkdir(parents=True, exist_ok=True)
        req.destination.write_bytes(b"mocked mp4 bytes")
        return req.destination

    monkeypatch.setattr(api_clips_module.export_module, "export_clip", mock_export_clip)

    status_before = store.get_clip(clip.id).status
    exports_before = store.list_exports(clip.id)

    res = client.post(f"/api/clips/{clip.id}/preview", json={"start_s": 1.0, "duration_s": 2.0})
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert len(res.content) > 0

    db.reset_connections()
    assert store.get_clip(clip.id).status == status_before
    assert store.list_exports(clip.id) == exports_before
    assert len(captured_requests) == 1


def test_preview_uses_full_words_and_style_matching_export(client, clip_and_source, monkeypatch):
    clip, _, _ = clip_and_source
    client.patch(
        f"/api/clips/{clip.id}/edit",
        json={
            "caption_style": "karaoke_lower",
            "caption_overrides": {"position_x": 0.8},
            "ratio": "1:1",
        },
    )

    captured = []
    import autoclip.api.clips as api_clips_module

    def mock_export(req: ExportRequest, *args, **kwargs):
        captured.append(req)
        req.destination.parent.mkdir(parents=True, exist_ok=True)
        req.destination.write_bytes(b"mocked mp4 bytes")
        return req.destination

    monkeypatch.setattr(api_clips_module.export_module, "export_clip", mock_export)

    res = client.post(f"/api/clips/{clip.id}/preview", json={"start_s": 1.5, "duration_s": 2.0})
    assert res.status_code == 200
    req = captured[0]

    assert len(req.words) == 2
    assert req.words[0].text == "Witaj"
    assert req.words[1].text == "Świecie"
    assert req.style.key == "karaoke_lower"
    assert req.style.position_x == 0.8
    assert req.ratio == "1:1"


def test_preview_clamps_duration_to_remaining_clip(client, clip_and_source, monkeypatch):
    clip, _, _ = clip_and_source
    captured = []
    import autoclip.api.clips as api_clips_module

    def mock_export(req: ExportRequest, *args, **kwargs):
        captured.append(req)
        req.destination.parent.mkdir(parents=True, exist_ok=True)
        req.destination.write_bytes(b"mocked mp4 bytes")
        return req.destination

    monkeypatch.setattr(api_clips_module.export_module, "export_clip", mock_export)

    # Clip is 5.0s long. At start_s=4.0, remaining is 1.0s. Output duration must be clamped to 1.0s.
    res = client.post(f"/api/clips/{clip.id}/preview", json={"start_s": 4.0, "duration_s": 3.0})
    assert res.status_code == 200
    assert len(captured) == 1
    assert getattr(captured[0], "output_duration_s", None) == pytest.approx(1.0)


def test_preview_source_missing_and_render_failure_cleanup(client, clip_and_source, monkeypatch):
    clip, source, source_file = clip_and_source

    import autoclip.api.clips as api_clips_module

    temp_workspace_dir = None

    def failing_export(req: ExportRequest, work_dir, *args, **kwargs):
        nonlocal temp_workspace_dir
        temp_workspace_dir = work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "leftover.tmp").write_text("data")
        raise export.ExportError("Render failed")

    monkeypatch.setattr(api_clips_module.export_module, "export_clip", failing_export)

    err_res = client.post(f"/api/clips/{clip.id}/preview", json={"start_s": 0.0, "duration_s": 1.0})
    assert err_res.status_code == 500
    if temp_workspace_dir is not None:
        assert not temp_workspace_dir.exists()

    # Source deleted: current production defect raises 500, integrator will fix to 410.
    # We enforce 410 per contract.
    source_file.unlink()
    res_missing = client.post(
        f"/api/clips/{clip.id}/preview", json={"start_s": 0.0, "duration_s": 1.0}
    )
    assert res_missing.status_code == 410


@pytest.mark.slow
def test_real_render_preview_matches_full_export_frames(client, tmp_path: Path):
    # 640x360 source with color gradient and sound, 15s long
    source_file = tmp_path / "preview_src.mp4"
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
            "testsrc=size=640x360:rate=24:duration=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=15",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source_file),
        ],
        check=True,
        capture_output=True,
    )
    source = store.create_source(
        Source(
            id=new_id(),
            type="upload",
            path=str(source_file),
            duration_s=15.0,
            width=640,
            height=360,
        )
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    # Clip 2.0s to 12.0s (10s duration)
    clip = Clip(id=new_id(), job_id=job.id, start_s=2.0, end_s=12.0, end_word=1)
    store.replace_clips(job.id, [clip])

    # Words spanning across preview boundary
    Transcript(
        words=[
            Word("Pierwsze", 2.5, 4.5),
            Word("Drugie", 4.6, 7.0),
        ]
    ).save(JobWorkspace(job.id).transcript)

    url_edit = f"/api/clips/{clip.id}/edit"
    client.patch(
        url_edit,
        json={
            "caption_style": "bold_pop",
            "ratio": "9:16",
            "framing": {
                "mode": "slices",
                "segments": [
                    {"start_s": 2.0, "end_s": 6.0, "framing": {"mode": "fit"}},
                    {
                        "start_s": 6.0,
                        "end_s": 12.0,
                        "framing": {
                            "mode": "manual",
                            "center_x": 0.5,
                            "center_y": 0.5,
                            "zoom": 1.0,
                        },
                    },
                ],
            },
        },
    )

    # 1. Full export
    exp_res = client.post(f"/api/clips/{clip.id}/export", json={})
    assert exp_res.status_code == 201
    exp_record = store.get_export(exp_res.json()["id"])
    full_export_path = exp_record.path

    # Preview begins inside a caption group.
    prev_res = client.post(
        f"/api/clips/{clip.id}/preview", json={"start_s": 3.0, "duration_s": 3.0}
    )
    assert prev_res.status_code == 200
    preview_path = tmp_path / "preview_out.mp4"
    preview_path.write_bytes(prev_res.content)

    # Extract frame at preview offset 1.0s (which corresponds to full-export time 4.0s)
    def extract_raw_frame(path: str | Path, ss: float):
        return subprocess.run(
            [
                ffmpeg.ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{ss:.4f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout

    frame_export = extract_raw_frame(full_export_path, 4.0)
    frame_preview = extract_raw_frame(preview_path, 1.0)
    assert len(frame_export) == len(frame_preview)
    # Compare pixel differences allowing realistic lossy-encoding tolerance
    diffs = [abs(a - b) for a, b in zip(frame_export, frame_preview, strict=True)]
    avg_diff = sum(diffs) / len(diffs)
    assert avg_diff < 5.0
