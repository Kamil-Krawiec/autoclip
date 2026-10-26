from __future__ import annotations

import math
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


def test_edit_caption_overrides_patch_and_reset(client, clip):
    url = f"/api/clips/{clip.id}/edit"
    saved = client.patch(url, json={"caption_overrides": {"size_ratio": 0.08}})
    assert saved.status_code == 200
    assert saved.json()["caption_overrides"] == {"size_ratio": 0.08}

    saved = client.patch(url, json={"caption_overrides": {"position_x": 0.7}})
    assert saved.json()["caption_overrides"] == {"position_x": 0.7}
    assert client.get(f"/api/clips/{clip.id}").json()["caption_overrides"] == {"position_x": 0.7}

    assert (
        client.patch(url, json={"caption_overrides": {"size_ratio": None}}).json()[
            "caption_overrides"
        ]
        is None
    )
    assert client.patch(url, json={"caption_overrides": {}}).json()["caption_overrides"] is None
    client.patch(url, json={"caption_overrides": {"position_y": 0.8}})
    assert client.patch(url, json={"caption_overrides": None}).json()["caption_overrides"] is None


@pytest.mark.parametrize(
    "override",
    [
        {"size_ratio": 0.019},
        {"size_ratio": 0.101},
        {"position_x": 0.099},
        {"position_x": 0.901},
        {"position_y": 0.099},
        {"position_y": 0.951},
        {"size_ratio": True},
        {"size_ratio": "0.05"},
        {"other": 1},
    ],
)
def test_invalid_caption_overrides_are_rejected(client, clip, override):
    response = client.patch(f"/api/clips/{clip.id}/edit", json={"caption_overrides": override})
    assert response.status_code == 422


@pytest.mark.parametrize("value", [True, "0.05", math.inf, math.nan])
def test_caption_override_numbers_are_strict_and_finite(value):
    from autoclip.api.schemas import CaptionOverridesIn

    with pytest.raises(ValueError):
        CaptionOverridesIn(size_ratio=value)


def test_caption_overrides_persist_and_legacy_route_does_not_clear(client, clip):
    other = Clip(id=new_id(), job_id=clip.job_id, start_s=4, end_s=7)
    store.replace_clips(clip.job_id, [clip, other])
    client.patch(f"/api/clips/{clip.id}/edit", json={"caption_overrides": {"position_y": 0.7}})
    client.patch(f"/api/clips/{other.id}/edit", json={"caption_overrides": {"position_x": 0.7}})
    db.reset_connections()
    assert store.get_clip_edit(clip.id).caption_overrides == {"position_y": 0.7}
    assert store.get_clip_edit(other.id).caption_overrides == {"position_x": 0.7}
    response = client.patch(f"/api/clips/{clip.id}/captions", json={"caption_style": "clean_lower"})
    assert response.json()["caption_overrides"] == {"position_y": 0.7}


def test_style_resolution_is_a_copy_and_position_applies_to_each_animation():
    for key in captions.PRESETS:
        original = captions.get_style(key)
        resolved = captions.resolve_style(key, {"size_ratio": 0.08, "position_x": 0.7})
        assert resolved is not original
        assert original.size_ratio != 0.08
        assert resolved.position_x == 0.7
        subs = captions.build_ass([Word("Cześć", 0, 1)], resolved, width=1080, height=1920)
        position_y = round(1920 * (1 - original.margin_v_ratio))
        assert subs.events and all(
            f"\\an2\\pos(756,{position_y})" in event.text for event in subs.events
        )
        assert subs.styles[captions.STYLE_NAME].fontsize == round(1920 * 0.08)


@pytest.mark.parametrize(
    "ratio,width,height",
    [("9:16", 1080, 1920), ("1:1", 1080, 1080), ("16:9", 1920, 1080)],
)
def test_default_ass_and_position_coordinates(ratio, width, height):
    del ratio
    word = [Word("napis", 0, 1)]
    style = captions.get_style("clean_lower")
    legacy = captions.build_ass(word, style, width=width, height=height)
    assert "\\pos" not in legacy.events[0].text
    positioned = captions.build_ass(
        word, captions.resolve_style("clean_lower", {"position_x": 0.7}), width=width, height=height
    )
    expected = f"\\pos({round(width * 0.7)},{round(height * (1 - style.margin_v_ratio))})"
    assert expected in positioned.events[0].text


def test_default_ass_serialization_keeps_legacy_bytes():
    subs = captions.build_ass(
        [Word("napis", 0, 1)], captions.get_style("clean_lower"), width=1080, height=1920
    )
    assert subs.to_string("ass") == (
        "[Script Info]\n"
        "; Script generated by pysubs2\n"
        "; https://pypi.python.org/pypi/pysubs2\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "Collisions: Normal\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScriptType: v4.00+\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,"
        "0,0,1,2,2,2,10,10,10,1\n"
        "Style: AutoClip,Inter,65,&H00FFFFFF,&H00FFFFFF,&H00000000,&H28000000,0,0,0,0,100,"
        "100,0,0,1,2,1,2,58,58,192,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,AutoClip,,0,0,0,,napis\n"
    )


def test_caption_style_preview_exposes_authoritative_position_defaults(client):
    styles = {style["key"]: style["preview"] for style in client.get("/api/caption-styles").json()}
    for key, style in captions.PRESETS.items():
        assert styles[key]["positionX"] == 0.5
        assert styles[key]["positionY"] == 1 - style.margin_v_ratio


@pytest.mark.slow
def test_saved_caption_layout_changes_real_api_export_and_off_has_no_captions(
    client, tmp_path: Path
):
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
    Transcript(words=[Word("Żółć", 0.1, 0.9)]).save(JobWorkspace(job.id).transcript)
    url = f"/api/clips/{clip.id}"

    def export_frame(payload):
        response = client.post(url + "/export", json=payload)
        assert response.status_code == 201
        record = store.get_export(response.json()["id"])
        assert record is not None
        frame = subprocess.run(
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
        bright = [index for index, value in enumerate(frame) if value > 40]
        if not bright:
            return None
        width, height = export.ratio_dimensions(record.ratio)
        xs = [index % width for index in bright]
        ys = [index // width for index in bright]
        return min(xs), min(ys), max(xs), max(ys)

    assert (
        client.patch(
            url + "/edit", json={"caption_style": "clean_lower", "ratio": "9:16"}
        ).status_code
        == 200
    )
    default_box = export_frame({})
    assert default_box is not None
    assert (
        client.patch(
            url + "/edit",
            json={
                "caption_overrides": {"size_ratio": 0.08, "position_x": 0.75, "position_y": 0.25}
            },
        ).status_code
        == 200
    )
    saved_box = export_frame({})
    assert saved_box is not None
    assert saved_box[2] - saved_box[0] > default_box[2] - default_box[0]
    assert (saved_box[0] + saved_box[2]) / 2 > (default_box[0] + default_box[2]) / 2 + 200
    assert (saved_box[1] + saved_box[3]) / 2 < (default_box[1] + default_box[3]) / 2 - 500
    for ratio in ("9:16", "1:1", "16:9"):
        width, height = export.ratio_dimensions(ratio)
        for style in captions.PRESETS:
            box = export_frame({"style": style, "ratio": ratio})
            assert box is not None
            center_x = (box[0] + box[2]) / 2
            center_y = (box[1] + box[3]) / 2
            assert 0.55 * width < center_x < 0.9 * width
            assert 0.1 * height < center_y < 0.3 * height
    assert client.patch(url + "/edit", json={"burn_captions": False}).status_code == 200
    assert export_frame({}) is None
