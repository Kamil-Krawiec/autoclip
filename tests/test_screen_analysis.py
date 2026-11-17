from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from autoclip import paths
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, Job, Source
from autoclip.pipeline.reframe.croppath import CropKeyframe, CropPath, CropSegment, Strategy
from fastapi.testclient import TestClient


def _write_synthetic_video(
    path: Path,
    frames: list[np.ndarray],
    *,
    fps: float = 10.0,
) -> Path:
    h, w = frames[0].shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    assert writer.isOpened()
    for frame in frames:
        writer.write(frame)
    writer.release()
    return path


def test_narrow_terminal_pane_recommends_manual_with_roi(tmp_path):
    from autoclip.pipeline.screen_analysis import analyze_clip_screens

    # 1280x720 dark screen with active changing text lines in left narrow pane [x=40..360]
    frames: list[np.ndarray] = []
    for i in range(40):
        img = np.full((720, 1280, 3), 24, dtype=np.uint8)
        cv2.putText(
            img, "$ pytest -q", (60, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (220, 220, 220), 2
        )
        for line in range((i // 3) % 6 + 1):
            cv2.putText(
                img,
                f"test_{line}_{i} PASS",
                (60, 200 + line * 45),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (120, 240, 140),
                2,
            )
        frames.append(img)

    video = _write_synthetic_video(tmp_path / "narrow_term.mp4", frames, fps=10.0)
    res = analyze_clip_screens(video, start_s=0.0, end_s=4.0, ratio="9:16")

    assert res.sampled_frames >= 8
    assert res.recommended_framing["mode"] == "manual"
    assert res.recommended_framing["center_x"] < 0.38
    assert res.segments[0].active_roi is not None
    assert res.segments[0].active_roi["w"] < 0.35


def test_wide_terminal_or_uncertain_static_falls_back_to_fit(tmp_path):
    from autoclip.pipeline.screen_analysis import analyze_clip_screens

    # Wide terminal activity spanning x=80..1200 cannot fit 9:16 crop without clipping
    wide_frames: list[np.ndarray] = []
    for i in range(30):
        img = np.full((720, 1280, 3), 20, dtype=np.uint8)
        y = 120 + ((i * 25) % 480)
        cv2.putText(
            img,
            f"WIDE_LOG_LINE_{i}_COLUMN_LEFT_TO_RIGHT_END_MARKER",
            (80, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (230, 230, 230),
            2,
        )
        wide_frames.append(img)
    wide_video = _write_synthetic_video(tmp_path / "wide_term.mp4", wide_frames, fps=10.0)
    res_wide = analyze_clip_screens(wide_video, start_s=0.0, end_s=3.0, ratio="9:16")
    assert res_wide.recommended_framing == {"mode": "fit"}

    # Completely static / uncertain screen -> must fall back to fit (Q16)
    static_frame = np.full((720, 1280, 3), 32, dtype=np.uint8)
    static_video = _write_synthetic_video(tmp_path / "static.mp4", [static_frame] * 25, fps=10.0)
    res_static = analyze_clip_screens(static_video, start_s=0.0, end_s=2.5, ratio="9:16")
    assert res_static.recommended_framing == {"mode": "fit"}
    assert res_static.overall_confidence < 0.6


def test_vertical_scroll_suppresses_false_cuts_and_detects_scroll(tmp_path):
    from autoclip.pipeline.screen_analysis import analyze_clip_screens

    # Build a tall page and scroll smoothly downward across 5 seconds (50 frames @ 10fps)
    canvas = np.full((2400, 1280, 3), 245, dtype=np.uint8)
    for row in range(30):
        y = 60 + row * 75
        cv2.putText(
            canvas,
            f"Documentation Section {row}: Code & Config Example Line",
            (100, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.95,
            (30, 30, 35),
            2,
        )
        cv2.rectangle(canvas, (100, y + 14), (1140, y + 20), (180, 190, 210), -1)

    frames = [canvas[i * 18 : i * 18 + 720, :, :].copy() for i in range(50)]
    scroll_video = _write_synthetic_video(tmp_path / "scroll.mp4", frames, fps=10.0)
    res = analyze_clip_screens(scroll_video, start_s=0.0, end_s=5.0, ratio="9:16")

    assert len(res.cuts_detected) <= 1
    assert any(seg.is_scroll for seg in res.segments)
    assert res.recommended_framing["mode"] == "fit"


def test_speaker_and_mixed_scene_slices_recommendations(tmp_path):
    from autoclip.pipeline.screen_analysis import analyze_clip_screens

    # 6 seconds @ 10fps:
    # 0..3s: talking head region only (tracked face in center, static background)
    # 3..6s: hard scene cut to narrow terminal on left + speaker PIP on right
    frames: list[np.ndarray] = []
    for i in range(30):
        img = np.full((720, 1280, 3), 60, dtype=np.uint8)
        cv2.circle(img, (640 + (i % 3), 300), 70, (190, 160, 140), -1)
        frames.append(img)
    for i in range(30):
        img = np.full((720, 1280, 3), 18, dtype=np.uint8)
        # Active terminal left pane
        cv2.putText(
            img,
            f"$ run_step_{i}",
            (50, 160 + (i % 5) * 50),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (130, 250, 150),
            2,
        )
        # Speaker PIP box on bottom-right
        cv2.rectangle(img, (920, 420), (1240, 680), (90, 90, 120), -1)
        frames.append(img)

    video = _write_synthetic_video(tmp_path / "mixed.mp4", frames, fps=10.0)
    crop_path = CropPath(
        source_width=1280,
        source_height=720,
        segments=[
            CropSegment(
                start_s=0.0,
                end_s=3.0,
                width=404,
                height=720,
                keyframes=[CropKeyframe(t=0.0, x=438.0, y=0.0)],
                strategy=Strategy.TRACK,
            ),
            CropSegment(
                start_s=3.0,
                end_s=6.0,
                width=404,
                height=720,
                keyframes=[CropKeyframe(t=3.0, x=876.0, y=0.0)],
                strategy=Strategy.TRACK,
            ),
        ],
    )
    res = analyze_clip_screens(video, start_s=0.0, end_s=6.0, ratio="9:16", crop_path=crop_path)
    assert len(res.cuts_detected) >= 1
    assert any(abs(c - 3.0) <= 0.8 for c in res.cuts_detected)
    assert len(res.segments) >= 2
    assert res.segments[0].recommended_mode == "auto"
    assert res.segments[-1].recommended_mode in ("split", "manual", "fit")
    assert res.recommended_framing["mode"] == "slices"


def test_api_analyze_screen_endpoint(autoclip_home, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    from autoclip.api import visual_inspection as vi_router

    app = create_app()
    app.include_router(vi_router.router)

    frames = [np.full((360, 640, 3), 25, dtype=np.uint8) for _ in range(20)]
    for i, f in enumerate(frames):
        cv2.putText(
            f,
            f"log {i}",
            (30, 80 + (i % 4) * 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (230, 230, 230),
            2,
        )

    with TestClient(app) as client:
        media_dir = paths.media_dir() / "s_ana"
        media_dir.mkdir(parents=True)
        vid = _write_synthetic_video(media_dir / "term.mp4", frames, fps=10.0)
        source = store.create_source(
            Source(id="s_ana", type="upload", path=str(vid), duration_s=2.0, width=640, height=360)
        )
        job = store.create_job(Job(id="j_ana", source_id=source.id, status="done"))
        clip = Clip(id="c_ana", job_id=job.id, start_s=0.0, end_s=2.0)
        store.replace_clips(job.id, [clip])

        res = client.post(f"/api/clips/{clip.id}/inspection/analyze-screen", json={})
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["clip_id"] == clip.id
        assert data["recommended_framing"]["mode"] in ("manual", "fit", "split", "slices", "auto")
        assert data["elapsed_ms"] >= 0.0
        assert data["sampled_frames"] >= 4


@pytest.mark.slow
def test_benchmark_60s_1080p_cpu_screen_analysis(tmp_path):
    import subprocess

    from autoclip.pipeline.screen_analysis import analyze_clip_screens

    # Generate a realistic 60s 1080p synthetic clip with FFmpeg (fast)
    vid = tmp_path / "bench_60s_1080p.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30:duration=60",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(vid),
        ],
        check=True,
    )
    t0 = time.perf_counter()
    res = analyze_clip_screens(vid, start_s=0.0, end_s=60.0, ratio="9:16")
    elapsed_s = time.perf_counter() - t0
    assert res.sampled_frames >= 150
    assert elapsed_s < 20.0  # Safety sanity cap; actual measured ms reported in test output
    print(f"BENCHMARK_60S_1080P_ELAPSED_S={elapsed_s:.3f} sampled_frames={res.sampled_frames}")


def test_segment_boundaries_do_not_exceed_precise_source_end():
    from autoclip.pipeline.screen_analysis import _build_segment_hint

    hint = _build_segment_hint(
        seg_clip_start=0.0,
        seg_clip_end=5.99996,
        clip_start_abs=0.0,
        clip_end_abs=5.99996,
        pairs=[],
        src_w=640,
        src_h=360,
        ratio="9:16",
        crop_path=None,
    )
    assert hint.source_end_s <= 5.99996
