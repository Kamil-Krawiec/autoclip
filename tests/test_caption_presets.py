from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from autoclip import db
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, Job, Source, new_id
from autoclip.pipeline import captions, export, ffmpeg
from autoclip.pipeline.runner import JobWorkspace
from autoclip.pipeline.transcript import Transcript, Word
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    with TestClient(create_app()) as value:
        yield value


@pytest.fixture
def clip(client):
    source = store.create_source(Source(id=new_id(), type="upload", path="fixture.mp4"))
    job = store.create_job(Job(id=new_id(), source_id=source.id))
    value = Clip(id=new_id(), job_id=job.id, start_s=0, end_s=3)
    store.replace_clips(job.id, [value])
    return value


def test_catalog_presets_cloned_and_animation_preserved():
    catalog_keys = list(captions.PRESETS.keys())
    assert catalog_keys[:2] == ["karaoke_lower", "bold_pop_lower"]
    assert captions.DEFAULT_STYLE == "bold_pop"

    lower = captions.PRESETS["karaoke_lower"]
    fill = captions.PRESETS["karaoke_fill"]
    assert lower.size_ratio == 0.048
    assert lower.margin_v_ratio == 0.12
    assert fill.size_ratio != 0.048
    assert fill.margin_v_ratio != 0.12
    assert lower.animation == fill.animation

    pop_lower = captions.PRESETS["bold_pop_lower"]
    pop = captions.PRESETS["bold_pop"]
    assert pop_lower.size_ratio == 0.052
    assert pop_lower.margin_v_ratio == 0.14
    assert pop.size_ratio != 0.052
    assert pop.margin_v_ratio != 0.14
    assert pop_lower.animation == pop.animation

    words = [Word("test", 0.0, 1.0)]
    fill_ass = captions.build_ass(words, fill, width=1080, height=1920)
    lower_ass = captions.build_ass(words, lower, width=1080, height=1920)
    assert fill_ass.events[0].text == lower_ass.events[0].text
    assert lower_ass.styles[captions.STYLE_NAME].marginv == round(1920 * 0.12)


def test_preset_patch_resets_overrides_preserving_framing_ratio_burn_and_other_clip(client, clip):
    other = Clip(id=new_id(), job_id=clip.job_id, start_s=4, end_s=7)
    store.replace_clips(clip.job_id, [clip, other])
    url = f"/api/clips/{clip.id}/edit"
    other_url = f"/api/clips/{other.id}/edit"

    init_payload = {
        "caption_style": "bold_pop",
        "ratio": "1:1",
        "burn_captions": True,
        "framing": {"mode": "manual", "center_x": 0.4, "center_y": 0.6, "zoom": 1.5},
        "caption_overrides": {"position_x": 0.3, "position_y": 0.4, "size_ratio": 0.08},
    }
    assert client.patch(url, json=init_payload).status_code == 200

    other_payload = {
        "caption_style": "clean_lower",
        "ratio": "16:9",
        "burn_captions": False,
        "caption_overrides": {"position_x": 0.7, "position_y": 0.7},
    }
    assert client.patch(other_url, json=other_payload).status_code == 200

    patch_preset = {"caption_style": "karaoke_lower", "caption_overrides": None}
    res = client.patch(url, json=patch_preset)
    assert res.status_code == 200
    data = res.json()
    assert data["caption_style"] == "karaoke_lower"
    assert data["caption_overrides"] is None
    assert data["ratio"] == "1:1"
    assert data["burn_captions"] is True
    assert data["framing"] == {"mode": "manual", "center_x": 0.4, "center_y": 0.6, "zoom": 1.5}

    db.reset_connections()
    clip_get = client.get(f"/api/clips/{clip.id}").json()
    assert clip_get["caption_style"] == "karaoke_lower"
    assert clip_get["caption_overrides"] is None
    assert clip_get["ratio"] == "1:1"
    assert clip_get["burn_captions"] is True
    assert clip_get["framing"] == {"mode": "manual", "center_x": 0.4, "center_y": 0.6, "zoom": 1.5}

    other_get = client.get(f"/api/clips/{other.id}").json()
    assert other_get["caption_style"] == "clean_lower"
    assert other_get["ratio"] == "16:9"
    assert other_get["burn_captions"] is False
    assert other_get["caption_overrides"] == {"position_x": 0.7, "position_y": 0.7}


@pytest.mark.slow
def test_karaoke_lower_vs_karaoke_fill_real_render(client, tmp_path: Path):
    source_path = tmp_path / "black.mp4"
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
            "color=c=black:s=320x180:r=24:d=1",
            "-an",
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
            id=new_id(), type="upload", path=str(source_path), duration_s=1, width=320, height=180
        )
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    clip = Clip(id=new_id(), job_id=job.id, start_s=0, end_s=1, end_word=0)
    store.replace_clips(job.id, [clip])
    Transcript(words=[Word("Zażółć", 0.1, 0.9)]).save(JobWorkspace(job.id).transcript)
    url = f"/api/clips/{clip.id}"

    def export_bbox(payload):
        res = client.post(url + "/export", json=payload)
        assert res.status_code == 201
        record = store.get_export(res.json()["id"])
        raw = subprocess.run(
            [
                ffmpeg.ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0.5",
                "-i",
                record.path,
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
        bright = [i for i, v in enumerate(raw) if v > 40]
        assert bright
        width, height = export.ratio_dimensions(record.ratio)
        ys = [i // width for i in bright]
        return min(ys), max(ys), height

    assert (
        client.patch(
            url + "/edit", json={"caption_style": "karaoke_fill", "ratio": "9:16"}
        ).status_code
        == 200
    )
    fill_min_y, fill_max_y, height = export_bbox({})
    assert (
        client.patch(
            url + "/edit", json={"caption_style": "karaoke_lower", "caption_overrides": None}
        ).status_code
        == 200
    )
    lower_min_y, lower_max_y, _ = export_bbox({})
    assert ((lower_min_y + lower_max_y) / 2) - ((fill_min_y + fill_max_y) / 2) > 0.08 * height
