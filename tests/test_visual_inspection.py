from __future__ import annotations

import base64
import math
import subprocess

import cv2
import numpy as np
import pytest
from autoclip import db, paths
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, Export, Job, Source
from autoclip.pipeline import captions
from autoclip.pipeline import export as export_module
from autoclip.pipeline.runner import JobWorkspace
from autoclip.pipeline.transcript import Transcript, Word
from fastapi.testclient import TestClient


@pytest.fixture
def inspection_app(autoclip_home, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def sample_clip(initialised_db):
    media_dir = paths.media_dir() / "src1"
    media_dir.mkdir(parents=True)
    src_path = media_dir / "video.mp4"
    src_path.write_bytes(b"placeholder")
    source = store.create_source(
        Source(
            id="src1",
            type="upload",
            path=str(src_path),
            duration_s=30.0,
            width=1920,
            height=1080,
        )
    )
    job = store.create_job(Job(id="job1", source_id=source.id, status="done"))
    clip = Clip(
        id="clip1",
        job_id=job.id,
        start_s=10.0,
        end_s=25.0,
        start_word=0,
        end_word=1,
        title="Terminal demo",
    )
    store.replace_clips(job.id, [clip])
    Transcript(
        words=[
            Word("Hello", 10.2, 11.5),
            Word("World", 14.5, 16.0),
        ]
    ).save(JobWorkspace(job.id).transcript)
    return source, job, clip, src_path


# --------------------------------------------------------------------------
# Unit tests: timeline mapping
# --------------------------------------------------------------------------


def test_map_timestamp_retained_clip_source_and_boundaries():
    from autoclip.pipeline.visual_inspection import map_timestamp

    windows = [(0.0, 2.0), (4.0, 8.0), (10.0, 15.0)]
    # Retained timeline (default)
    m0 = map_timestamp(
        0.0, timeline="retained", clip_start_s=10.0, clip_end_s=25.0, windows=windows
    )
    assert (m0.source_t, m0.clip_t, m0.retained_t, m0.in_removed_range, m0.window_index) == (
        10.0,
        0.0,
        0.0,
        False,
        0,
    )

    # Exact seam at retained_t=2.0 moves to start of window 1 (clip_t=4.0)
    m_seam = map_timestamp(
        2.0, timeline="retained", clip_start_s=10.0, clip_end_s=25.0, windows=windows
    )
    assert m_seam.clip_t == pytest.approx(4.0)
    assert m_seam.source_t == pytest.approx(14.0)
    assert m_seam.retained_t == pytest.approx(2.0)
    assert m_seam.window_index == 1
    assert not m_seam.in_removed_range

    # Exact end of retained timeline
    m_end = map_timestamp(
        11.0, timeline="retained", clip_start_s=10.0, clip_end_s=25.0, windows=windows
    )
    assert m_end.clip_t == pytest.approx(15.0)
    assert m_end.source_t == pytest.approx(25.0)
    assert m_end.window_index == 2

    # Clip timeline inside removed range -> retained_t is None, no silent shift
    m_cut = map_timestamp(3.0, timeline="clip", clip_start_s=10.0, clip_end_s=25.0, windows=windows)
    assert m_cut.clip_t == pytest.approx(3.0)
    assert m_cut.source_t == pytest.approx(13.0)
    assert m_cut.retained_t is None
    assert m_cut.in_removed_range is True
    assert m_cut.window_index is None

    # Exact cut start [2.0, 4.0) is in removed range; cut end 4.0 is retained
    m_cut_start = map_timestamp(
        2.0, timeline="clip", clip_start_s=10.0, clip_end_s=25.0, windows=windows
    )
    assert m_cut_start.retained_t is None
    assert m_cut_start.in_removed_range is True

    m_cut_end = map_timestamp(
        4.0, timeline="clip", clip_start_s=10.0, clip_end_s=25.0, windows=windows
    )
    assert m_cut_end.retained_t == pytest.approx(2.0)
    assert m_cut_end.in_removed_range is False
    assert m_cut_end.window_index == 1

    # Source timeline inside cut -> retained_t is None
    m_src_cut = map_timestamp(
        19.0, timeline="source", clip_start_s=10.0, clip_end_s=25.0, windows=windows
    )
    assert m_src_cut.clip_t == pytest.approx(9.0)
    assert m_src_cut.retained_t is None
    assert m_src_cut.in_removed_range is True

    # Trailing cut at clip end [12, 15] -> clip_t=15.0 is removed
    trailing_windows = [(0.0, 12.0)]
    m_trail = map_timestamp(
        15.0, timeline="clip", clip_start_s=10.0, clip_end_s=25.0, windows=trailing_windows
    )
    assert m_trail.retained_t is None
    assert m_trail.in_removed_range is True


def test_map_timestamp_rejects_invalid_and_out_of_bounds():
    from autoclip.pipeline.visual_inspection import map_timestamp

    windows = [(0.0, 5.0)]
    for bad in (-0.1, 5.1, math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            map_timestamp(
                bad, timeline="retained", clip_start_s=10.0, clip_end_s=15.0, windows=windows
            )
    with pytest.raises(ValueError):
        map_timestamp(9.5, timeline="source", clip_start_s=10.0, clip_end_s=15.0, windows=windows)


# --------------------------------------------------------------------------
# Unit tests: revision hashing & export freshness
# --------------------------------------------------------------------------


def test_revision_tracks_all_visual_edits_and_export_freshness(sample_clip):
    from autoclip.pipeline.visual_inspection import (
        assess_export_freshness,
        compute_clip_revision,
        record_export_revision,
    )

    _, _, clip, _ = sample_clip
    rev_base, _ = compute_clip_revision(clip.id)

    # Unrelated clip metadata change should NOT alter render revision
    store.update_clip(clip.id, title="Renamed title", status="kept")
    rev_renamed, _ = compute_clip_revision(clip.id)
    assert rev_renamed == rev_base

    # Visual edits MUST change revision
    store.patch_clip_edit(clip.id, {"framing": {"mode": "fit"}})
    rev_fit, _ = compute_clip_revision(clip.id)
    assert rev_fit != rev_base

    store.patch_clip_edit(clip.id, {"ratio": "1:1"})
    rev_ratio, _ = compute_clip_revision(clip.id)
    assert rev_ratio != rev_fit

    store.patch_clip_edit(clip.id, {"caption_overrides": {"position_y": 0.2}})
    rev_cap, _ = compute_clip_revision(clip.id)
    assert rev_cap != rev_ratio

    store.patch_clip_edit(clip.id, {"removed_ranges": [{"start_s": 12.0, "end_s": 14.0}]})
    rev_cut, _ = compute_clip_revision(clip.id)
    assert rev_cut != rev_cap

    # No exports yet
    fresh0 = assess_export_freshness(clip.id, rev_cut, [])
    assert fresh0["status"] == "no_exports"
    assert fresh0["is_fresh"] is False
    assert fresh0["deterministic"] is True

    # Legacy export without sidecar -> explicit uncertainty
    legacy = Export(id="exp_legacy", clip_id=clip.id, path="/tmp/legacy.mp4", ratio="1:1")
    fresh_legacy = assess_export_freshness(clip.id, rev_cut, [legacy])
    assert fresh_legacy["status"] == "unknown_legacy_export"
    assert fresh_legacy["is_fresh"] is None
    assert fresh_legacy["deterministic"] is False

    # Recorded snapshot revision -> deterministic fresh, then stale after edit
    recorded = Export(id="exp_det", clip_id=clip.id, path="/tmp/det.mp4", ratio="1:1")
    record_export_revision(recorded.id, clip_id=clip.id, revision=rev_cut, ratio="1:1")
    store.create_export(legacy)
    store.create_export(recorded)
    exports = store.list_exports(clip.id)
    assert [e.id for e in exports][:2] == [recorded.id, legacy.id]
    fresh_ok = assess_export_freshness(clip.id, rev_cut, exports)
    assert fresh_ok["status"] == "fresh"
    assert fresh_ok["is_fresh"] is True
    assert fresh_ok["deterministic"] is True

    store.patch_clip_edit(clip.id, {"burn_captions": False})
    rev_noburn, _ = compute_clip_revision(clip.id)
    stale = assess_export_freshness(clip.id, rev_noburn, [recorded, legacy])
    assert stale["status"] == "stale"
    assert stale["is_fresh"] is False
    assert stale["deterministic"] is True


# --------------------------------------------------------------------------
# API contract tests (validation, limits, containment, mocked render)
# --------------------------------------------------------------------------


def test_api_revision_and_validation_limits(inspection_app, sample_clip):
    _, _, clip, _ = sample_clip
    res = inspection_app.get(f"/api/clips/{clip.id}/inspection/revision")
    assert res.status_code == 200
    body = res.json()
    assert body["clip_id"] == clip.id
    assert body["export_freshness"]["status"] == "no_exports"
    assert body["timeline"]["retained_duration_s"] == pytest.approx(15.0)

    # Extra forbidden field, >8 frames, max_width > 720, strict types, NaN
    bad_payloads = [
        {"timestamps": [0.5], "unknown": 1},
        {"timestamps": []},
        {"timestamps": [0.1 * i for i in range(9)]},
        {"timestamps": [0.5], "max_width": 721},
        {"timestamps": [0.5], "max_width": 100},
        {"timestamps": ["0.5"]},
        {"timestamps": [0.5], "max_width": "360"},
        {"timestamps": [0.5], "target": "export"},  # missing export_id
    ]
    for payload in bad_payloads:
        r = inspection_app.post(f"/api/clips/{clip.id}/inspection/frames", json=payload)
        assert r.status_code == 422, f"Expected 422 for {payload}, got {r.status_code}: {r.text}"

    # Contact sheet max 8 tiles & max_width 720
    for payload in [
        {"tile_count": 9},
        {"timestamps": [0.1 * i for i in range(9)]},
        {"max_width": 1080},
    ]:
        r = inspection_app.post(f"/api/clips/{clip.id}/inspection/contact-sheet", json=payload)
        assert r.status_code == 422


def test_api_rejects_symlinks_foreign_exports_and_oversized_payload(
    inspection_app, sample_clip, tmp_path, monkeypatch
):
    _, job, clip, src_path = sample_clip

    # Symlinked source rejected
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"secret")
    src_path.unlink()
    src_path.symlink_to(outside)
    r_sym = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={"timestamps": [0.5], "target": "source"},
    )
    assert r_sym.status_code == 410
    src_path.unlink()
    src_path.write_bytes(b"restored")

    # Export from another clip rejected
    store.replace_clips(
        job.id, [clip, Clip(id="other_clip", job_id=job.id, start_s=0.0, end_s=5.0)]
    )
    other_export = Export(
        id="exp_other",
        clip_id="other_clip",
        path=str(paths.exports_dir() / "other.mp4"),
        ratio="9:16",
    )
    store.create_export(other_export)
    r_foreign = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={"timestamps": [0.5], "target": "export", "export_id": "exp_other"},
    )
    assert r_foreign.status_code in (403, 404)

    # Response size limit (> 4 MB) enforced cleanly
    from autoclip.pipeline import visual_inspection as vi_mod

    huge_img = vi_mod.EncodedImage(
        mime_type="image/png",
        width=720,
        height=1280,
        data=b"x" * 2_500_000,
    )
    monkeypatch.setattr(vi_mod, "extract_source_frame", lambda *a, **kw: huge_img)
    monkeypatch.setattr(vi_mod, "extract_composed_frame", lambda *a, **kw: huge_img)
    r_big = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={"timestamps": [0.5], "target": "both", "format": "png"},
    )
    assert r_big.status_code == 422


# --------------------------------------------------------------------------
# Real FFmpeg render + parity tests
# --------------------------------------------------------------------------


@pytest.mark.slow
def test_real_ffmpeg_frames_contact_sheet_and_export_parity(inspection_app, sample_clip, tmp_path):
    source, job, clip, src_path = sample_clip
    # Clip is [10.0, 25.0] -> use a clean 6s source and update clip to [1.0, 7.0]
    store.update_clip(clip.id, start_s=1.0, end_s=7.0)
    clip = store.get_clip(clip.id)
    Transcript(
        words=[
            Word("FIRST", 1.2, 2.5),
            Word("AFTER", 5.2, 6.6),
        ]
    ).save(JobWorkspace(job.id).transcript)

    # 8s 16:9 video: left half red, right half blue, with audio
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=640x360:r=10:d=8,drawbox=x=320:y=0:w=320:h=360:color=blue:t=fill",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(src_path),
        ],
        check=True,
    )
    # Update source dims to match 640x360
    with db.connect() as conn:
        conn.execute(
            "UPDATE sources SET width=640, height=360, duration_s=8.0 WHERE id=?", (source.id,)
        )
        conn.commit()

    # Slice 0 [1..3] manual on right (blue) half, cut [3..5], slice 1 [5..7] fit
    store.patch_clip_edit(
        clip.id,
        {
            "framing": {
                "mode": "slices",
                "segments": [
                    {
                        "start_s": 1.0,
                        "end_s": 3.0,
                        "framing": {
                            "mode": "manual",
                            "center_x": 0.8,
                            "center_y": 0.5,
                            "zoom": 1.0,
                        },
                    },
                    {"start_s": 3.0, "end_s": 7.0, "framing": {"mode": "fit"}},
                ],
            },
            "removed_ranges": [{"start_s": 3.0, "end_s": 5.0}],
            "caption_style": "bold_pop",
            "ratio": "9:16",
        },
    )

    # Inspect retained_t=0.8 (blue manual + FIRST) and retained_t=2.8 (fit + AFTER at clip_t=4.8)
    r_frames = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={"timestamps": [0.8, 2.8], "timeline": "retained", "target": "both", "max_width": 360},
    )
    assert r_frames.status_code == 200, r_frames.text
    data = r_frames.json()
    assert len(data["frames"]) == 2

    f0 = data["frames"][0]
    assert f0["clip_t"] == pytest.approx(0.8)
    assert f0["retained_t"] == pytest.approx(0.8)
    assert f0["framing_mode"] == "manual"
    src_img0 = cv2.imdecode(
        np.frombuffer(base64.b64decode(f0["source_image"]["data_base64"]), np.uint8),
        cv2.IMREAD_COLOR,
    )
    comp_img0 = cv2.imdecode(
        np.frombuffer(base64.b64decode(f0["composed_image"]["data_base64"]), np.uint8),
        cv2.IMREAD_COLOR,
    )
    # Source image has red on left and blue on right (BGR: red=[:,0:100,2], blue=[:,-100:,0])
    assert src_img0[:, :50, 2].mean() > 180
    assert src_img0[:, -50:, 0].mean() > 180
    # Composed manual right crop is predominantly blue + yellow/white caption pixels
    assert comp_img0[:, :, 0].mean() > 150
    assert (comp_img0[:, :, 2] > 180).sum() > 20  # active yellow bold_pop caption text

    f1 = data["frames"][1]
    assert f1["clip_t"] == pytest.approx(4.8)
    assert f1["retained_t"] == pytest.approx(2.8)
    assert f1["framing_mode"] == "fit"

    # Compare composed frame f1 against real export_clip MP4 at t=2.8
    from autoclip.config import ExportSettings
    from autoclip.pipeline.framing import resolve_crop_path

    clip_now = store.get_clip(clip.id)
    edit_now = store.get_clip_edit(clip.id)
    src_now = store.get_source(source.id)
    crop_path = resolve_crop_path(src_now, clip_now, job, edit_now, None)
    export_mp4 = paths.exports_dir() / "parity.mp4"
    export_module.export_clip(
        export_module.ExportRequest(
            source=src_path,
            destination=export_mp4,
            start_s=clip_now.start_s,
            end_s=clip_now.end_s,
            crop_path=crop_path,
            words=[Word("FIRST", 1.2, 2.5), Word("AFTER", 5.2, 6.6)],
            style=captions.get_style("bold_pop"),
            ratio="9:16",
            removed_ranges=edit_now.removed_ranges,
        ),
        work_dir=tmp_path / "exp_work",
        settings=ExportSettings(),
    )
    export_rec = store.create_export(
        Export(id="exp_parity", clip_id=clip.id, path=str(export_mp4), ratio="9:16")
    )
    r_exp = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={
            "timestamps": [2.8],
            "timeline": "retained",
            "target": "export",
            "export_id": export_rec.id,
            "max_width": 360,
        },
    )
    assert r_exp.status_code == 200, r_exp.text
    exp_b64 = r_exp.json()["frames"][0]["export_image"]["data_base64"]
    exp_img1 = cv2.imdecode(np.frombuffer(base64.b64decode(exp_b64), np.uint8), cv2.IMREAD_COLOR)
    comp_img1 = cv2.imdecode(
        np.frombuffer(base64.b64decode(f1["composed_image"]["data_base64"]), np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert exp_img1.shape == comp_img1.shape
    mean_abs_diff = float(np.abs(exp_img1.astype(np.float32) - comp_img1.astype(np.float32)).mean())
    assert mean_abs_diff < 12.0

    # Contact sheet
    r_cs = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/contact-sheet",
        json={"tile_count": 4, "timeline": "retained", "target": "composed", "max_width": 720},
    )
    assert r_cs.status_code == 200, r_cs.text
    cs = r_cs.json()
    assert len(cs["tiles"]) == 4
    cs_img = cv2.imdecode(
        np.frombuffer(base64.b64decode(cs["image"]["data_base64"]), np.uint8), cv2.IMREAD_COLOR
    )
    assert cs_img.shape[1] <= 720
    for tile in cs["tiles"]:
        x, y, w, h = tile["bbox"]
        assert x >= 0 and y >= 0 and x + w <= cs_img.shape[1] and y + h <= cs_img.shape[0]


def test_frames_raw_nan_and_source_outside_clip_validation(
    inspection_app, sample_clip, monkeypatch
):
    _, _, clip, _ = sample_clip

    # Raw NaN JSON literal must be rejected (422)
    r_nan = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        content=b'{"timestamps": [NaN]}',
        headers={"Content-Type": "application/json"},
    )
    assert r_nan.status_code == 422

    # Stub FFmpeg extractor so unit test checks timeline routing only
    from autoclip.pipeline import visual_inspection as vi_mod

    ok_img = vi_mod.EncodedImage(
        mime_type="image/jpeg",
        width=160,
        height=90,
        data=b"jpegbytes",
    )
    monkeypatch.setattr(vi_mod, "extract_source_frame", lambda *a, **kw: ok_img)

    # source timestamp outside [10.0, 25.0] works for target="source"
    r_src = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={"timestamps": [2.0], "timeline": "source", "target": "source"},
    )
    assert r_src.status_code == 200, r_src.text
    f = r_src.json()["frames"][0]
    assert f["source_t"] == pytest.approx(2.0)
    assert f["retained_t"] is None
    assert f["framing_mode"] is None

    # Same outside-clip source timestamp fails for composed/both
    for tgt in ("composed", "both"):
        r_bad = inspection_app.post(
            f"/api/clips/{clip.id}/inspection/frames",
            json={"timestamps": [2.0], "timeline": "source", "target": tgt},
        )
        assert r_bad.status_code == 422


def test_revision_snapshot_flow_simulating_clips_export(inspection_app, sample_clip, tmp_path):
    _, _, clip, _ = sample_clip
    from autoclip.pipeline import visual_inspection as vi_mod

    # Step 1: Capture snapshot before export starts
    _, _, _, _, _, style, ratio, _, rev_at_start = vi_mod.load_clip_render_context(clip.id)
    exp_path = paths.exports_dir() / "snap_export.mp4"
    exp_path.write_bytes(b"mp4")
    store.create_export(Export(id="exp_snap", clip_id=clip.id, path=str(exp_path), ratio=ratio))
    vi_mod.record_export_revision(
        "exp_snap",
        clip_id=clip.id,
        revision=rev_at_start,
        ratio=ratio,
        style=style.key,
    )

    r_fresh = inspection_app.get(f"/api/clips/{clip.id}/inspection/revision")
    assert r_fresh.status_code == 200
    assert r_fresh.json()["export_freshness"]["status"] == "fresh"

    # Step 2: Subsequent edit invalidates freshness against recorded snapshot
    store.patch_clip_edit(clip.id, {"burn_captions": False})
    r_stale = inspection_app.get(f"/api/clips/{clip.id}/inspection/revision")
    assert r_stale.status_code == 200
    freshness = r_stale.json()["export_freshness"]
    assert freshness["status"] == "stale"
    assert freshness["latest_export"]["recorded_revision"] == rev_at_start
    assert freshness["current_revision"] != rev_at_start


@pytest.mark.slow
def test_real_ffmpeg_split_mode_parity(inspection_app, sample_clip, tmp_path):
    source, job, clip, src_path = sample_clip
    store.update_clip(clip.id, start_s=0.0, end_s=3.0)
    clip = store.get_clip(clip.id)
    Transcript(words=[]).save(JobWorkspace(job.id).transcript)

    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=green:s=640x360:r=10:d=3,drawbox=x=400:y=0:w=240:h=360:color=yellow:t=fill",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(src_path),
        ],
        check=True,
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE sources SET width=640, height=360, duration_s=3.0 WHERE id=?",
            (source.id,),
        )
        conn.commit()

    store.patch_clip_edit(
        clip.id,
        {
            "framing": {
                "mode": "split",
                "panels": [
                    {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0},
                    {"x": 0.65, "y": 0.0, "width": 0.35, "height": 1.0},
                ],
            },
            "burn_captions": False,
            "ratio": "9:16",
        },
    )

    r_comp = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={"timestamps": [1.2], "timeline": "retained", "target": "composed", "max_width": 360},
    )
    assert r_comp.status_code == 200, r_comp.text
    f_comp = r_comp.json()["frames"][0]
    assert f_comp["framing_mode"] == "split"

    from autoclip.config import ExportSettings
    from autoclip.pipeline.framing import resolve_crop_path

    clip_now = store.get_clip(clip.id)
    edit_now = store.get_clip_edit(clip.id)
    src_now = store.get_source(source.id)
    crop_path = resolve_crop_path(src_now, clip_now, job, edit_now, None)
    export_mp4 = paths.exports_dir() / "split_parity.mp4"
    export_module.export_clip(
        export_module.ExportRequest(
            source=src_path,
            destination=export_mp4,
            start_s=clip_now.start_s,
            end_s=clip_now.end_s,
            crop_path=crop_path,
            words=[],
            style=captions.get_style("clean_lower"),
            ratio="9:16",
            burn_captions=False,
        ),
        work_dir=tmp_path / "split_exp",
        settings=ExportSettings(),
    )
    store.create_export(Export(id="exp_split", clip_id=clip.id, path=str(export_mp4), ratio="9:16"))
    r_exp = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={
            "timestamps": [1.2],
            "timeline": "retained",
            "target": "export",
            "export_id": "exp_split",
            "max_width": 360,
        },
    )
    assert r_exp.status_code == 200, r_exp.text
    comp_img = cv2.imdecode(
        np.frombuffer(base64.b64decode(f_comp["composed_image"]["data_base64"]), np.uint8),
        cv2.IMREAD_COLOR,
    )
    exp_img = cv2.imdecode(
        np.frombuffer(
            base64.b64decode(r_exp.json()["frames"][0]["export_image"]["data_base64"]),
            np.uint8,
        ),
        cv2.IMREAD_COLOR,
    )
    assert comp_img.shape == exp_img.shape
    mean_abs_diff = float(np.abs(exp_img.astype(np.float32) - comp_img.astype(np.float32)).mean())
    assert mean_abs_diff < 12.0


def test_export_endpoint_records_snapshot_and_edit_makes_it_stale(
    inspection_app, sample_clip, monkeypatch
):
    _, _, clip, _ = sample_clip

    def fake_render(request, **kwargs):
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        request.destination.write_bytes(b"isolated export")

    monkeypatch.setattr(export_module, "export_clip", fake_render)
    exported = inspection_app.post(f"/api/clips/{clip.id}/export", json={})
    assert exported.status_code == 201, exported.text
    revision = inspection_app.get(f"/api/clips/{clip.id}/inspection/revision")
    assert revision.status_code == 200
    assert revision.json()["export_freshness"]["status"] == "fresh"
    store.patch_clip_edit(clip.id, {"burn_captions": False})
    revision = inspection_app.get(f"/api/clips/{clip.id}/inspection/revision")
    assert revision.json()["export_freshness"]["status"] == "stale"


def test_frame_framing_override_is_transient(inspection_app, sample_clip, monkeypatch):
    from autoclip.pipeline import visual_inspection as vi

    _, _, clip, _ = sample_clip
    seen = []

    def render(**kwargs):
        seen.append(kwargs["crop_path"].segments[0])
        return vi.EncodedImage("image/jpeg", 1, 1, b"frame")

    monkeypatch.setattr(vi, "extract_composed_frame", render)
    url = f"/api/clips/{clip.id}/inspection/frames"
    revision_before = inspection_app.get(f"/api/clips/{clip.id}/inspection/revision").json()
    for framing in (
        {"mode": "auto"},
        {"mode": "fit"},
        {"mode": "manual", "center_x": 0.5, "center_y": 0.5, "zoom": 2.0},
        {"mode": "split"},
    ):
        response = inspection_app.post(
            url, json={"timestamps": [1], "target": "composed", "framing": framing}
        )
        assert response.status_code == 200, response.text
        assert response.json()["current_revision"] == revision_before["current_revision"]
    assert seen[1].fit
    assert seen[2].width < seen[0].width
    assert seen[3].split
    assert inspection_app.get(f"/api/clips/{clip.id}/inspection/revision").json() == revision_before
    assert store.get_clip_edit(clip.id) is None
    assert (
        inspection_app.post(
            url, json={"timestamps": [1], "target": "source", "framing": {"mode": "fit"}}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("ratio", ["9:16", "1:1", "16:9"])
@pytest.mark.parametrize(
    "saved_framing",
    [
        {"mode": "fit"},
        {
            "mode": "slices",
            "segments": [{"start_s": 10.0, "end_s": 25.0, "framing": {"mode": "fit"}}],
        },
    ],
)
def test_frame_override_replaces_saved_framing_without_mutation(
    inspection_app, sample_clip, monkeypatch, saved_framing, ratio
):
    from autoclip.pipeline import visual_inspection as vi

    _, _, clip, _ = sample_clip
    store.patch_clip_edit(clip.id, {"framing": saved_framing, "ratio": "1:1"})
    before = store.get_clip_edit(clip.id)
    revision = inspection_app.get(f"/api/clips/{clip.id}/inspection/revision").json()
    seen = []
    image = vi.EncodedImage("image/jpeg", 1, 1, b"frame")
    monkeypatch.setattr(vi, "extract_source_frame", lambda *args, **kwargs: image)

    def render(**kwargs):
        seen.append(kwargs)
        return image

    monkeypatch.setattr(vi, "extract_composed_frame", render)
    url = f"/api/clips/{clip.id}/inspection/frames"
    for mode in ("auto", "fit", "manual", "split"):
        framing = {"mode": mode}
        if mode == "manual":
            framing.update(center_x=0.5, center_y=0.5, zoom=2.0)
        response = inspection_app.post(
            url, json={"timestamps": [1], "target": "both", "ratio": ratio, "framing": framing}
        )
        assert response.status_code == 200, response.text
        frame = response.json()["frames"][0]
        assert frame["framing_mode"] == mode
        assert frame["face_caption_collision"]["status"] == "unknown"
        assert frame["source_region"]["status"] == ("unknown" if mode == "split" else "known")
        if mode == "fit":
            assert frame["source_region"]["clipped_fraction"] == 0
        if mode in ("auto", "manual"):
            sizes = {
                "9:16": ((608, 1080), (304, 540)),
                "1:1": ((1080, 1080), (540, 540)),
                "16:9": ((1920, 1080), (960, 540)),
            }
            w, h = sizes[ratio][mode == "manual"]
            assert frame["source_region"]["clipped_fraction"] == pytest.approx(
                1 - w * h / (1920 * 1080)
            )
        assert frame["source_image"] and frame["composed_image"]
        assert seen[-1]["ratio"] == ratio
        assert store.get_clip_edit(clip.id) == before
    assert inspection_app.get(f"/api/clips/{clip.id}/inspection/revision").json() == revision
    assert (
        inspection_app.post(
            url, json={"timestamps": [1], "framing": {"mode": "slices", "segments": []}}
        ).status_code
        == 422
    )
    assert (
        inspection_app.post(
            url,
            json={
                "timestamps": [1],
                "target": "export",
                "export_id": "x",
                "framing": {"mode": "fit"},
            },
        ).status_code
        == 422
    )


def test_source_region_diagnostics_geometry_and_boundaries():
    from autoclip.pipeline.reframe.croppath import CropKeyframe, CropPath, CropSegment
    from autoclip.pipeline.visual_inspection import source_region_diagnostics

    crop = CropSegment(0, 1, 50, 100, [CropKeyframe(0, 0, 0), CropKeyframe(1, 50, 0)])
    fit = CropSegment(1, 2, 100, 100, fit=True)
    path = CropPath(100, 100, [crop, fit])
    result = source_region_diagnostics(path, 0.5)
    assert result["visible_source_rect"] == {"x": 25, "y": 0, "width": 50, "height": 100}
    assert result["clipped_fraction"] == 0.5
    assert source_region_diagnostics(path, 1)["clipped_fraction"] == 0
    assert source_region_diagnostics(CropPath(0, 100, [crop]), 0)["status"] == "unknown"
    assert source_region_diagnostics(CropPath(100, 100, []), 0)["status"] == "unknown"
    crop.keyframes = []
    assert source_region_diagnostics(path, 0)["status"] == "unknown"


@pytest.mark.parametrize("width,height", [(None, 1080), (1920, None), (0, 1080), (1920, -1)])
def test_source_region_requires_actual_source_dimensions(
    inspection_app, sample_clip, monkeypatch, width, height
):
    from dataclasses import replace

    from autoclip.pipeline import visual_inspection as vi

    _, _, clip, _ = sample_clip
    original = vi.load_clip_render_context

    def load(*args, **kwargs):
        context = list(original(*args, **kwargs))
        context[2] = replace(context[2], width=width, height=height)
        return tuple(context)

    monkeypatch.setattr(vi, "load_clip_render_context", load)
    monkeypatch.setattr(
        vi, "extract_composed_frame", lambda **kwargs: vi.EncodedImage("image/jpeg", 1, 1, b"x")
    )
    response = inspection_app.post(
        f"/api/clips/{clip.id}/inspection/frames",
        json={"timestamps": [1], "target": "composed", "framing": {"mode": "auto"}},
    )
    assert response.status_code == 200, response.text
    region = response.json()["frames"][0]["source_region"]
    assert region["status"] == "unknown"
    assert "dimensions" in region["reason"].lower()


@pytest.mark.parametrize("x,y,expected", [(-10, -20, (0, 0)), (200, 300, (50, 40))])
def test_source_region_clamps_crop_coordinates(x, y, expected):
    from autoclip.pipeline.reframe.croppath import CropKeyframe, CropPath, CropSegment
    from autoclip.pipeline.visual_inspection import source_region_diagnostics

    path = CropPath(100, 100, [CropSegment(0, 1, 50, 60, [CropKeyframe(0, x, y)])])
    rect = source_region_diagnostics(path, 0)["visible_source_rect"]
    assert (rect["x"], rect["y"]) == expected


@pytest.mark.parametrize(
    "width,height,x,y",
    [
        (0, 50, 0, 0),
        (-1, 50, 0, 0),
        (101, 50, 0, 0),
        (50, 101, 0, 0),
        (50, 0, 0, 0),
        (50, 50, float("nan"), 0),
        (50, 50, 0, float("nan")),
    ],
)
def test_source_region_rejects_invalid_geometry(width, height, x, y):
    from autoclip.pipeline.reframe.croppath import CropKeyframe, CropPath, CropSegment
    from autoclip.pipeline.visual_inspection import source_region_diagnostics

    path = CropPath(100, 100, [CropSegment(0, 1, width, height, [CropKeyframe(0, x, y)])])
    assert source_region_diagnostics(path, 0)["status"] == "unknown"


@pytest.mark.parametrize("target", ["source", "export"])
def test_non_composed_frame_has_unknown_source_region(
    inspection_app, sample_clip, monkeypatch, target
):
    from autoclip.pipeline import visual_inspection as vi

    _, _, clip, source_path = sample_clip
    store.create_export(Export(id="exp", clip_id=clip.id, path=str(source_path), ratio="9:16"))
    monkeypatch.setattr(vi, "resolve_export_media_path", lambda record: source_path)
    monkeypatch.setattr(
        vi,
        "extract_source_frame",
        lambda *args, **kwargs: vi.EncodedImage("image/jpeg", 1, 1, b"x"),
    )
    payload = {"timestamps": [1], "target": target}
    if target == "export":
        payload["export_id"] = "exp"
    response = inspection_app.post(f"/api/clips/{clip.id}/inspection/frames", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["frames"][0]["source_region"]["status"] == "unknown"
