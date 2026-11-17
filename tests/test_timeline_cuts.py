from __future__ import annotations

import pytest
from autoclip import db
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, Job, Source, new_id
from fastapi.testclient import TestClient


@pytest.fixture
def clip_client(autoclip_home, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_NO_WORKER", "1")
    with TestClient(create_app()) as client:
        source = store.create_source(
            Source(id=new_id(), type="upload", path="fixture.mp4", duration_s=30)
        )
        job = store.create_job(Job(id=new_id(), source_id=source.id))
        clip = Clip(id=new_id(), job_id=job.id, start_s=10, end_s=25)
        store.replace_clips(job.id, [clip])
        from autoclip.pipeline.runner import JobWorkspace
        from autoclip.pipeline.transcript import Transcript, Word

        Transcript(words=[Word("hello", 10, 11), Word("world", 14, 15)]).save(
            JobWorkspace(job.id).transcript
        )
        yield client, clip


def test_removed_ranges_persist_and_can_be_restored_without_losing_other_edits(clip_client):
    client, clip = clip_client
    url = f"/api/clips/{clip.id}/edit"
    cuts = [{"start_s": 12.0, "end_s": 14.0}, {"start_s": 18.0, "end_s": 20.0}]
    saved = client.patch(url, json={"removed_ranges": cuts, "ratio": "1:1"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["removed_ranges"] == cuts
    db.reset_connections()
    assert client.get(f"/api/clips/{clip.id}").json()["removed_ranges"] == cuts
    restored = client.patch(url, json={"removed_ranges": []})
    assert restored.status_code == 200
    assert restored.json()["removed_ranges"] == []
    assert restored.json()["ratio"] == "1:1"


@pytest.mark.parametrize(
    "ranges",
    [
        [{"start_s": 12, "end_s": 11}],
        [{"start_s": 12, "end_s": 16}, {"start_s": 15, "end_s": 17}],
        [{"start_s": 0, "end_s": 31}],
        [{"start_s": 10, "end_s": 25}],
    ],
)
def test_invalid_removed_ranges_are_rejected(clip_client, ranges):
    client, clip = clip_client
    result = client.patch(f"/api/clips/{clip.id}/edit", json={"removed_ranges": ranges})
    assert result.status_code in (400, 422)


def test_kept_windows_use_source_times_and_preview_window():
    from autoclip.pipeline.export import kept_windows

    cuts = [{"start_s": 12.0, "end_s": 14.0}, {"start_s": 18.0, "end_s": 20.0}]
    assert kept_windows(10, 25, cuts) == [(0, 2), (4, 8), (10, 15)]
    assert kept_windows(10, 25, cuts, 1, 5) == [(1, 2), (4, 6)]
    assert kept_windows(10, 25, cuts, 2, 1) == []


@pytest.mark.slow
def test_removed_video_and_audio_ranges_are_rendered_together(tmp_path):
    import subprocess

    from autoclip.config import ExportSettings
    from autoclip.pipeline import captions, export
    from autoclip.pipeline.reframe.croppath import centre_crop
    from autoclip.pipeline.transcript import Word

    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=320x180:r=10:d=3,drawbox=x=0:y=0:w=320:h=180:color=blue:t=fill:enable='gte(t,2)'",
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
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    output = tmp_path / "result.mp4"
    request = export.ExportRequest(
        source=source,
        destination=output,
        start_s=0,
        end_s=3,
        crop_path=centre_crop(320, 180, 3),
        words=[Word("First", 0.1, 0.5), Word("Last", 2.1, 2.5)],
        style=captions.get_style("bold_pop"),
        removed_ranges=[{"start_s": 1, "end_s": 2}],
    )
    export.export_clip(request, work_dir=tmp_path / "work", settings=ExportSettings())
    import json

    info = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    streams = {stream["codec_type"]: stream for stream in info["streams"]}
    assert abs(float(streams["video"]["duration"]) - 2) < 0.2
    assert abs(float(streams["audio"]["duration"]) - 2) < 0.2
    assert abs(float(streams["video"]["duration"]) - float(streams["audio"]["duration"])) < 0.12

    # Captions are burned before the cut, so each word stays on its source frames.
    def white_pixels(timestamp):
        pixels = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(timestamp),
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-vf",
                "scale=270:480",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        return sum(1 for i in range(0, len(pixels), 3) if min(pixels[i : i + 3]) > 30)

    assert white_pixels(0.3) > 5
    assert white_pixels(1.3) > 5
    assert white_pixels(0.8) < white_pixels(0.3)

    from dataclasses import replace

    preview = tmp_path / "preview.mp4"
    export.export_clip(
        replace(request, destination=preview, output_start_s=1.5, output_duration_s=1.5),
        work_dir=tmp_path / "work",
        settings=ExportSettings(),
    )
    duration = float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(preview),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert abs(duration - 1) < 0.2


def test_trim_cannot_leave_only_removed_footage(clip_client):
    client, clip = clip_client
    result = client.patch(
        f"/api/clips/{clip.id}/edit", json={"removed_ranges": [{"start_s": 12, "end_s": 14}]}
    )
    assert result.status_code == 200
    result = client.patch(f"/api/clips/{clip.id}", json={"start_s": 12, "end_s": 14})
    assert result.status_code == 400


def test_timeline_visuals_are_bounded_and_generated_from_clip(clip_client, autoclip_home):
    import subprocess

    client, clip = clip_client
    job = store.get_job(clip.job_id)
    source = store.get_source(job.source_id)
    media = autoclip_home / "media" / source.id / "source.mp4"
    media.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=5:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(media),
        ],
        check=True,
        capture_output=True,
    )
    with db.connection() as conn:
        conn.execute(
            "UPDATE sources SET path=?,duration_s=3,width=160,height=90 WHERE id=?",
            (str(media), source.id),
        )
        conn.execute("UPDATE clips SET start_s=0,end_s=3 WHERE id=?", (clip.id,))
    response = client.get(f"/api/clips/{clip.id}/timeline")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["strip"].startswith("data:image/jpeg;base64,")
    assert 0 < len(data["peaks"]) <= 240
    assert all(0 <= value <= 1 for value in data["peaks"])
    assert max(data["peaks"]) > 0
    assert sum(value > 0.03 for value in data["peaks"]) > len(data["peaks"]) * 0.8
    with db.connection() as conn:
        conn.execute("UPDATE clips SET end_s=2.5 WHERE id=?", (clip.id,))
    assert client.get(f"/api/clips/{clip.id}/timeline").status_code == 200
    import hashlib

    cache = autoclip_home / "thumbnails" / "timeline" / hashlib.sha256(clip.id.encode()).hexdigest()
    assert len(list(cache.glob("*.json"))) == 1


def test_subtitle_words_follow_removed_ranges():
    from autoclip.pipeline.export import remap_words
    from autoclip.pipeline.transcript import Word

    words = [Word("before", 10.5, 11), Word("removed", 12.2, 13), Word("after", 14.5, 15)]
    result = remap_words(words, 10, [(0, 2), (4, 6)])
    assert [(w.text, w.start, w.end) for w in result] == [
        ("before", 0.5, 1),
        ("after", 2.5, 3),
    ]


def test_preview_inside_removed_range_is_rejected_cleanly(clip_client):
    client, clip = clip_client
    assert (
        client.patch(
            f"/api/clips/{clip.id}/edit", json={"removed_ranges": [{"start_s": 12, "end_s": 16}]}
        ).status_code
        == 200
    )
    result = client.post(f"/api/clips/{clip.id}/preview", json={"start_s": 2.5, "duration_s": 1})
    assert result.status_code == 400


@pytest.mark.slow
def test_cut_export_without_audio_is_supported(tmp_path):
    import subprocess

    from autoclip.config import ExportSettings
    from autoclip.pipeline import captions, export
    from autoclip.pipeline.reframe.croppath import centre_crop

    source = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x90:r=5:d=2",
            "-c:v",
            "libx264",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    result = tmp_path / "cut.mp4"
    export.export_clip(
        export.ExportRequest(
            source=source,
            destination=result,
            start_s=0,
            end_s=2,
            crop_path=centre_crop(160, 90, 2),
            words=[],
            style=captions.get_style("bold_pop"),
            removed_ranges=[{"start_s": 0.5, "end_s": 1}],
        ),
        work_dir=tmp_path / "work",
        settings=ExportSettings(),
    )
    assert result.stat().st_size > 0


def test_trim_cannot_extend_beyond_source(clip_client):
    client, clip = clip_client
    result = client.patch(f"/api/clips/{clip.id}", json={"end_s": 31})
    assert result.status_code == 400
    assert client.get(f"/api/clips/{clip.id}").json()["end_s"] == 25
