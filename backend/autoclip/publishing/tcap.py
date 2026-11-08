"""Append a still to the end of a rendered export — tcap parity without tcap.

The external ``tcap`` tool appends a PNG as a short final segment so a clip
always lands on the intended thumbnail frame when scrubbing or uploading. It is
*the end* of the file, not the first frame. This module reproduces the
observable behaviour with FFmpeg: copy the source video stream, encode only the
still, concatenate, remux the original audio, and faststart.

Why not shell out to ``tcap``? A pure implementation is smaller, testable, and
adds no network/build-time dependency to the Docker image.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ..pipeline import ffmpeg

log = logging.getLogger(__name__)

#: Codecs whose streams can be copied into the concatenated output.
COPYABLE_CODECS = frozenset({"h264", "hevc"})
#: 8-bit 4:2:0/4:2:2/4:4:4 pixel formats libx264 can encode for the still.
STILL_PIX_FMTS = frozenset({"yuv420p", "yuv422p", "yuv444p"})

DURATION_TOLERANCE_S = 0.25
DEFAULT_FPS = "30"


class TcapError(RuntimeError):
    """The tcap-compatible append failed."""


def append_still(video: Path, png: Path, out: Path, *, duration_s: float = 0.15) -> Path:
    """Write ``out``: ``video`` followed by ``duration_s`` seconds of ``png``.

    The source export is only read. ``out`` is replaced atomically after
    validation. Re-running with unchanged inputs reuses an existing output, so
    a retried upload does not rebuild the file.
    """
    if not video.is_file():
        raise TcapError(f"The export does not exist: {video}")
    if not png.is_file():
        raise TcapError(f"The thumbnail does not exist: {png}")
    if not 0.05 <= duration_s <= 1.0:
        raise TcapError("The still duration must be between 0.05 and 1.0 seconds.")

    out.parent.mkdir(parents=True, exist_ok=True)
    if _is_reusable(out, video, png):
        return out

    info = _probe(video)
    if not info["has_video"]:
        raise TcapError(f"{video.name} has no video stream.")

    work = out.parent / f".{out.stem}.tcap-{uuid.uuid4().hex}"
    work.mkdir()
    temp_out = work / "short.mp4"
    try:
        if info["video_codec"] not in COPYABLE_CODECS:
            log.info("Source codec %s is not copyable; re-encoding.", info["video_codec"])
            _append_by_reencode(video, png, temp_out, info, duration_s)
        else:
            try:
                _append_by_copy(video, png, temp_out, work, info, duration_s)
            except (TcapError, ffmpeg.FFmpegError) as exc:
                log.warning("Stream-copy append failed; re-encoding instead: %s", exc)
                _append_by_reencode(video, png, temp_out, info, duration_s)

        _validate(temp_out, info, duration_s)
        os.replace(temp_out, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def _is_reusable(out: Path, video: Path, png: Path) -> bool:
    try:
        if out.stat().st_size == 0:
            return False
        return out.stat().st_mtime >= max(video.stat().st_mtime, png.stat().st_mtime)
    except OSError:
        return False


def _probe(path: Path) -> dict[str, Any]:
    command = [
        ffmpeg.ffprobe_path(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False, encoding="utf-8")
    if proc.returncode != 0:
        raise TcapError(f"ffprobe could not read {path.name}.\n{proc.stderr.strip()}")
    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TcapError(f"ffprobe returned unreadable JSON for {path.name}.") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return {
            "has_video": False,
            "has_audio": audio is not None,
            "duration_s": float((data.get("format") or {}).get("duration") or 0.0),
        }

    return {
        "has_video": True,
        "has_audio": audio is not None,
        "duration_s": float((data.get("format") or {}).get("duration") or 0.0),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "video_codec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt") or "yuv420p",
        "sar": _sample_aspect_ratio(video.get("sample_aspect_ratio")),
        "fps": _frame_rate(video),
        "time_base": video.get("time_base") or "1/90000",
    }


def _sample_aspect_ratio(raw: str | None) -> str:
    if not raw or raw in {"0:1", "N/A"}:
        return "1:1"
    return raw


def _frame_rate(video: dict[str, Any]) -> str:
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = video.get(key)
        if isinstance(rate, str) and rate not in {"0/0", "0", ""}:
            return rate
    return DEFAULT_FPS


def _fps_value(rate: str) -> float:
    try:
        numerator, _, denominator = rate.partition("/")
        return float(numerator) / float(denominator) if denominator else float(numerator)
    except (ValueError, ZeroDivisionError):
        return 30.0


def _timescale(time_base: str) -> int:
    try:
        denominator = int(time_base.split("/", 1)[1])
    except (IndexError, ValueError):
        return 90_000
    return max(1, denominator)


def _concat_line(path: Path) -> str:
    # The concat demuxer uses its own quoting; escape a single quote the way its
    # documentation does so a path with an apostrophe still parses.
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'"


def _append_by_copy(
    video: Path,
    png: Path,
    out: Path,
    work: Path,
    info: dict[str, Any],
    duration_s: float,
) -> None:
    width, height = info["width"], info["height"]
    fps = info["fps"]
    sar = info["sar"]
    pix_fmt = info["pix_fmt"] if info["pix_fmt"] in STILL_PIX_FMTS else "yuv420p"

    source_video = work / "source-video.mp4"
    ffmpeg.run(
        [
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-an",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(source_video),
        ]
    )

    still = work / "still.mp4"
    gop = max(1, round(_fps_value(fps) * 2))
    ffmpeg.run(
        [
            "-loop",
            "1",
            "-i",
            str(png),
            "-vf",
            f"scale={width}:{height},fps={fps},setsar={sar},format={pix_fmt}",
            "-t",
            f"{duration_s:.3f}",
            "-r",
            fps,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-g",
            str(gop),
            "-keyint_min",
            "1",
            "-sc_threshold",
            "0",
            "-video_track_timescale",
            str(_timescale(info["time_base"])),
            "-movflags",
            "+faststart",
            str(still),
        ]
    )

    joined = work / "joined.mp4"
    concat_list = work / "concat.txt"
    concat_list.write_text(
        f"{_concat_line(source_video)}\n{_concat_line(still)}\n", encoding="utf-8"
    )
    ffmpeg.run(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            str(joined),
        ]
    )

    if not info["has_audio"]:
        os.replace(joined, out)
        return

    audio = work / "source-audio.mka"
    ffmpeg.run(
        [
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-vn",
            "-c",
            "copy",
            str(audio),
        ]
    )
    ffmpeg.run(
        [
            "-i",
            str(joined),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )


def _append_by_reencode(
    video: Path,
    png: Path,
    out: Path,
    info: dict[str, Any],
    duration_s: float,
) -> None:
    """Full re-encode fallback: same picture, slower, always playable."""
    width, height = info["width"], info["height"]
    fps = info["fps"]
    filtergraph = (
        f"[0:v]scale={width}:{height},setsar={info['sar']},format=yuv420p[v0];"
        f"[1:v]scale={width}:{height},setsar={info['sar']},format=yuv420p[v1];"
        f"[v0][v1]concat=n=2:v=1:a=0[v]"
    )
    ffmpeg.run(
        [
            "-i",
            str(video),
            "-loop",
            "1",
            "-t",
            f"{duration_s:.3f}",
            "-i",
            str(png),
            "-filter_complex",
            filtergraph,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            fps,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )


def _validate(path: Path, source: dict[str, Any], duration_s: float) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise TcapError("The tcap output is missing or empty.")
    result = _probe(path)
    expected = source["duration_s"] + duration_s
    if abs(result["duration_s"] - expected) > DURATION_TOLERANCE_S:
        raise TcapError(
            f"The tcap output is {result['duration_s']:.2f}s, expected about {expected:.2f}s."
        )
    if (result.get("width"), result.get("height")) != (source["width"], source["height"]):
        raise TcapError("The tcap output changed the video dimensions.")
    if source["has_audio"] and not result["has_audio"]:
        raise TcapError("The tcap output lost the audio stream.")
