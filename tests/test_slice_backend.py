from __future__ import annotations

from dataclasses import replace

import pytest
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, ClipEdit, Job, Source, new_id
from autoclip.pipeline.framing import resolve_crop_path
from autoclip.pipeline.reframe.croppath import (
    CropKeyframe,
    CropPath,
    CropSegment,
    Strategy,
)
from autoclip.pipeline.runner import JobWorkspace
from autoclip.pipeline.transcript import Transcript, Word
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    with TestClient(create_app()) as value:
        yield value


@pytest.fixture
def clip_and_source(client):
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
    job = store.create_job(
        Job(id=new_id(), source_id=source.id, settings={"export": {"ratio": "9:16"}})
    )
    clip = Clip(id=new_id(), job_id=job.id, start_s=5.0, end_s=15.0)
    store.replace_clips(job.id, [clip])
    Transcript(words=[Word("Cześć", 5.5, 6.5), Word("świecie", 10.0, 11.0)]).save(
        JobWorkspace(job.id).transcript
    )
    return clip, source, job


@pytest.mark.parametrize(
    "invalid_framing,expected_status",
    [
        ({"mode": "slices", "segments": []}, 422),
        (
            {
                "mode": "slices",
                "segments": [{"start_s": 5.0, "end_s": 5.0, "framing": {"mode": "auto"}}],
            },
            422,
        ),
        (
            {
                "mode": "slices",
                "segments": [
                    {"start_s": 5.0, "end_s": 8.0, "framing": {"mode": "auto"}},
                    {"start_s": 8.1, "end_s": 10.0, "framing": {"mode": "fit"}},
                ],
            },
            422,
        ),
        (
            {
                "mode": "slices",
                "segments": [
                    {"start_s": 5.0, "end_s": 8.5, "framing": {"mode": "auto"}},
                    {"start_s": 8.0, "end_s": 10.0, "framing": {"mode": "fit"}},
                ],
            },
            422,
        ),
        (
            {
                "mode": "slices",
                "segments": [
                    {
                        "start_s": 5.0,
                        "end_s": 8.0,
                        "framing": {"mode": "slices", "segments": []},
                    }
                ],
            },
            422,
        ),
        (
            {
                "mode": "slices",
                "segments": [
                    {"start_s": i * 0.2, "end_s": (i + 1) * 0.2, "framing": {"mode": "auto"}}
                    for i in range(65)
                ],
            },
            422,
        ),
        (
            {
                "mode": "slices",
                "segments": [{"start_s": 25.0, "end_s": 31.0, "framing": {"mode": "auto"}}],
            },
            400,
        ),
    ],
)
def test_argon_slice_validation_rejects_invalid_and_preserves_existing_state(
    client, clip_and_source, invalid_framing, expected_status
):
    clip, _, _ = clip_and_source
    valid_initial = {
        "mode": "slices",
        "segments": [
            {"start_s": 5.0, "end_s": 10.0, "framing": {"mode": "auto"}},
            {"start_s": 10.0, "end_s": 15.0, "framing": {"mode": "auto"}},
        ],
    }
    ok = client.patch(f"/api/clips/{clip.id}/edit", json={"framing": valid_initial})
    assert ok.status_code == 200
    assert ok.json()["framing"] == valid_initial

    bad = client.patch(f"/api/clips/{clip.id}/edit", json={"framing": invalid_framing})
    assert bad.status_code == expected_status

    after = client.get(f"/api/clips/{clip.id}").json()
    assert after["framing"] == valid_initial


def test_argon_slices_preserve_hidden_segments_across_trim_and_reexpand(client, clip_and_source):
    clip, _, _ = clip_and_source
    authored = {
        "mode": "slices",
        "segments": [
            {"start_s": 5.0, "end_s": 8.0, "framing": {"mode": "fit"}},
            {"start_s": 8.0, "end_s": 12.0, "framing": {"mode": "auto"}},
            {
                "start_s": 12.0,
                "end_s": 15.0,
                "framing": {"mode": "manual", "center_x": 0.25, "center_y": 0.75, "zoom": 2.0},
            },
        ],
    }
    assert client.patch(f"/api/clips/{clip.id}/edit", json={"framing": authored}).status_code == 200

    trimmed = client.patch(f"/api/clips/{clip.id}", json={"start_s": 8.0, "end_s": 12.0})
    assert trimmed.status_code == 200
    assert trimmed.json()["framing"] == authored

    expanded = client.patch(f"/api/clips/{clip.id}", json={"start_s": 3.0, "end_s": 18.0})
    assert expanded.status_code == 200
    assert expanded.json()["framing"] == authored

    path = client.get(f"/api/clips/{clip.id}/crop-path").json()
    segs = path["segments"]
    assert [(round(s["start_s"], 3), round(s["end_s"], 3)) for s in segs] == [
        (0.0, 2.0),
        (2.0, 5.0),
        (5.0, 9.0),
        (9.0, 12.0),
        (12.0, 15.0),
    ]
    assert [s["fit"] for s in segs] == [False, True, False, False, False]
    assert segs[3]["width"] == 304 and segs[3]["height"] == 540


def test_argon_resolve_slices_interpolates_auto_keyframes_without_mutating_cache(clip_and_source):
    clip, source, job = clip_and_source
    cached = CropPath(
        1920,
        1080,
        [
            CropSegment(
                start_s=0.0,
                end_s=6.0,
                width=608,
                height=1080,
                keyframes=[
                    CropKeyframe(t=0.0, x=100.0, y=0.0),
                    CropKeyframe(t=6.0, x=700.0, y=60.0),
                ],
                strategy=Strategy.TRACK,
            ),
            CropSegment(
                start_s=6.0,
                end_s=10.0,
                width=608,
                height=1080,
                keyframes=[CropKeyframe(t=6.0, x=400.0, y=20.0)],
                strategy=Strategy.GENERAL,
            ),
        ],
    )
    snapshot = cached.to_dict()

    edit = ClipEdit(
        clip_id=clip.id,
        ratio="9:16",
        framing={
            "mode": "slices",
            "segments": [
                {"start_s": 5.0, "end_s": 8.0, "framing": {"mode": "fit"}},
                {"start_s": 8.0, "end_s": 13.0, "framing": {"mode": "auto"}},
                {
                    "start_s": 13.0,
                    "end_s": 15.0,
                    "framing": {"mode": "manual", "center_x": 0.5, "center_y": 0.5, "zoom": 1.0},
                },
            ],
        },
    )
    resolved = resolve_crop_path(source, clip, job, edit, cached)
    assert cached.to_dict() == snapshot
    assert [(s.start_s, s.end_s) for s in resolved.segments] == [
        (0.0, 3.0),
        (3.0, 6.0),
        (6.0, 8.0),
        (8.0, 10.0),
    ]
    auto_first = resolved.segments[1]
    assert auto_first.strategy == Strategy.TRACK
    assert [(k.t, k.x, k.y) for k in auto_first.keyframes] == [
        (3.0, pytest.approx(400.0), pytest.approx(30.0)),
        (6.0, pytest.approx(700.0), pytest.approx(60.0)),
    ]

    ratio_changed = resolve_crop_path(source, clip, job, replace(edit, ratio="1:1"), cached)
    assert ratio_changed.segments[1].width == 1080 and ratio_changed.segments[1].height == 1080
    assert cached.to_dict() == snapshot
