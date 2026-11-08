"""tcap parity: a still is appended to the end, without touching the source."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from autoclip.pipeline import ffmpeg
from autoclip.publishing import tcap

pytestmark = pytest.mark.slow


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_video(path: Path, *, codec: str = "libx264", with_audio: bool = True) -> Path:
    args = [
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1080x1920:rate=30:duration=1",
    ]
    if with_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    args += ["-c:v", codec, "-pix_fmt", "yuv420p"]
    if codec != "libx264":
        args += ["-q:v", "3"]
    if with_audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(str(path))
    ffmpeg.run(args)
    return path


def _make_png(path: Path) -> Path:
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=1080x1920",
            "-frames:v",
            "1",
            "-update",
            "1",
            str(path),
        ]
    )
    return path


@pytest.fixture
def media(tmp_path: Path) -> tuple[Path, Path]:
    return _make_video(tmp_path / "export.mp4"), _make_png(tmp_path / "thumb.png")


def test_copy_path_appends_a_still_and_preserves_audio(media, tmp_path, monkeypatch) -> None:
    video, png = media
    out = tmp_path / "publish" / "short.mp4"
    before = _sha256(video)

    def _unexpected_fallback(*args, **kwargs):
        raise AssertionError("the stream-copy path should have succeeded")

    monkeypatch.setattr(tcap, "_append_by_reencode", _unexpected_fallback)

    tcap.append_still(video, png, out)

    info = ffmpeg.probe(out)
    assert info.width == 1080
    assert info.height == 1920
    assert info.has_audio is True
    assert info.duration_s == pytest.approx(1.15, abs=0.25)
    assert _sha256(video) == before
    assert out.stat().st_size > 0


def test_rerun_reuses_the_existing_output(media, tmp_path) -> None:
    video, png = media
    out = tmp_path / "short.mp4"

    tcap.append_still(video, png, out)
    first_mtime = out.stat().st_mtime_ns

    tcap.append_still(video, png, out)

    assert out.stat().st_mtime_ns == first_mtime


def test_non_h264_source_falls_back_and_still_validates(tmp_path) -> None:
    video = _make_video(tmp_path / "export-mpeg4.mp4", codec="mpeg4")
    png = _make_png(tmp_path / "thumb.png")
    out = tmp_path / "short.mp4"
    before = _sha256(video)

    tcap.append_still(video, png, out)

    info = ffmpeg.probe(out)
    assert info.video_codec == "h264"
    assert info.width == 1080
    assert info.height == 1920
    assert info.has_audio is True
    assert info.duration_s == pytest.approx(1.15, abs=0.25)
    assert _sha256(video) == before


def test_silent_source_stays_silent(tmp_path) -> None:
    video = _make_video(tmp_path / "silent.mp4", with_audio=False)
    png = _make_png(tmp_path / "thumb.png")
    out = tmp_path / "short.mp4"

    tcap.append_still(video, png, out)

    info = ffmpeg.probe(out)
    assert info.has_audio is False
    assert info.duration_s == pytest.approx(1.15, abs=0.25)


def test_missing_input_raises_before_writing(tmp_path) -> None:
    png = _make_png(tmp_path / "thumb.png")
    out = tmp_path / "short.mp4"

    with pytest.raises(tcap.TcapError, match="does not exist"):
        tcap.append_still(tmp_path / "missing.mp4", png, out)

    assert not out.exists()


def test_invalid_duration_is_rejected(media, tmp_path) -> None:
    video, png = media

    with pytest.raises(tcap.TcapError, match="between 0.05 and 1.0"):
        tcap.append_still(video, png, tmp_path / "short.mp4", duration_s=5.0)


def test_concat_line_escapes_apostrophes() -> None:
    assert tcap._concat_line(Path("/tmp/it's here.mp4")) == "file '/tmp/it'\\''s here.mp4'"
