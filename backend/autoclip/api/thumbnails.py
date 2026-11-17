"""Bounded, source-time gallery thumbnails, independent of pipeline workspaces."""

from __future__ import annotations

import array
import base64
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from .. import library, paths
from ..db import store

router = APIRouter()
_RENDER_SLOT = threading.BoundedSemaphore(1)
_TIMEOUT_S = 12
_MAX_BYTES = 1_000_000


def _no_symlinks(path: Path, root: Path) -> Path:
    """Reject symlinks and lexical escapes before resolving a local artifact."""
    try:
        relative = path.absolute().relative_to(root)
        current = root
        if root.is_symlink():
            raise ValueError("Symlink root")
        for part in relative.parts:
            if part == "..":
                raise ValueError("Path escape")
            current /= part
            if current.is_symlink():
                raise ValueError("Symlink artifact")
        return path
    except (ValueError, OSError) as exc:
        raise HTTPException(410, "Thumbnail media is unavailable.") from exc


def _source_path(source) -> Path:
    try:
        path = library.media_path(source)
        if source.library_path is None:
            _no_symlinks(path, paths.media_dir())
        if not path.is_file():
            raise OSError("Missing source")
        return path
    except (library.LibraryError, OSError) as exc:
        raise HTTPException(410, "Source media is unavailable.") from exc


def _cached(path: Path) -> bytes | None:
    if not path.exists():
        return None
    _no_symlinks(path, paths.root())
    if path.stat().st_size > _MAX_BYTES:
        return None
    data = path.read_bytes()
    return data if 0 < len(data) <= _MAX_BYTES else None


@router.get("/clips/{clip_id}/thumbnail")
def clip_thumbnail(clip_id: str) -> Response:
    clip = store.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, "Clip not found.")
    job = store.get_job(clip.job_id)
    source = store.get_source(job.source_id) if job else None
    if source is None:
        raise HTTPException(404, "The clip's source is missing.")
    if not source.has_video:
        raise HTTPException(404, "This source has no video thumbnail.")
    if not (
        math.isfinite(clip.start_s)
        and math.isfinite(clip.end_s)
        and 0 <= clip.start_s < clip.end_s <= source.duration_s
    ):
        raise HTTPException(422, "Clip bounds are outside the source.")

    source_path = _source_path(source)
    try:
        stat = source_path.stat()
        # Midpoint is explicit source time, independent of fps=1/5 strip naming.
        timestamp = clip.start_s + (clip.end_s - clip.start_s) / 2
        signature = [
            1,
            str(source_path),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            clip.start_s,
            clip.end_s,
        ]
        key = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
        clip_key = hashlib.sha256(clip_id.encode()).hexdigest()
        directory = _no_symlinks(paths.root() / "thumbnails" / clip_key, paths.root())
        target = directory / f"{key}.jpg"
        data = _cached(target)
        if data is None:
            if not _RENDER_SLOT.acquire(blocking=False):
                raise HTTPException(
                    503, "Thumbnail renderer is busy. Retry shortly.", headers={"Retry-After": "2"}
                )
            try:
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="render-", dir=directory) as temporary:
                    output = Path(temporary) / "frame.jpg"
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-nostdin",
                            "-threads",
                            "1",
                            "-protocol_whitelist",
                            "file,pipe",
                            "-ss",
                            f"{timestamp:.9f}",
                            "-i",
                            str(source_path),
                            "-map",
                            "0:v:0",
                            "-frames:v",
                            "1",
                            "-an",
                            "-sn",
                            "-dn",
                            "-vf",
                            "scale=320:180:force_original_aspect_ratio=decrease",
                            "-threads",
                            "1",
                            "-q:v",
                            "4",
                            "-y",
                            str(output),
                        ],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=_TIMEOUT_S,
                    )
                    data = output.read_bytes()
                    if not 0 < len(data) <= _MAX_BYTES:
                        raise OSError("Invalid thumbnail output")
                    # Recheck before publishing: a source replaced during decode is stale.
                    current = _source_path(source).stat()
                    if (
                        current.st_ino,
                        current.st_size,
                        current.st_mtime_ns,
                        current.st_ctime_ns,
                    ) != (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
                        raise OSError("Source changed during thumbnail render")
                    output.replace(target)
                for old in directory.glob("*.jpg"):
                    if old != target:
                        old.unlink()
            finally:
                _RENDER_SLOT.release()
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Thumbnail render timed out.") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(410, "Thumbnail is unavailable for this media.") from exc
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@router.get("/clips/{clip_id}/timeline")
def clip_timeline(clip_id: str) -> dict:
    """Generate a bounded filmstrip and audio peaks for the visible clip."""
    clip = store.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, "Clip not found.")
    job = store.get_job(clip.job_id)
    source = store.get_source(job.source_id) if job else None
    if source is None:
        raise HTTPException(404, "The clip's source is missing.")
    if not (
        math.isfinite(clip.start_s)
        and math.isfinite(clip.end_s)
        and 0 <= clip.start_s < clip.end_s <= source.duration_s
        and clip.duration_s <= 600
    ):
        raise HTTPException(
            422, "Timeline requires a clip of at most 10 minutes within the source."
        )
    source_path = _source_path(source)
    stat = source_path.stat()
    signature = [
        "timeline-v2",
        str(source_path),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        clip.start_s,
        clip.end_s,
        source.has_video,
        source.has_audio,
    ]
    key = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
    directory = _no_symlinks(
        paths.root() / "thumbnails" / "timeline" / hashlib.sha256(clip_id.encode()).hexdigest(),
        paths.root(),
    )
    target = directory / f"{key}.json"
    cached = _cached(target)
    if cached:
        return json.loads(cached)
    if not _RENDER_SLOT.acquire(blocking=False):
        raise HTTPException(
            503, "Timeline renderer is busy. Retry shortly.", headers={"Retry-After": "2"}
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="timeline-", dir=directory) as tmp:
            strip = ""
            common = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-threads",
                "1",
                "-protocol_whitelist",
                "file,pipe",
                "-ss",
                f"{clip.start_s:.6f}",
                "-t",
                f"{clip.duration_s:.6f}",
                "-i",
                str(source_path),
            ]
            if source.has_video:
                output = Path(tmp) / "strip.jpg"
                subprocess.run(
                    common
                    + [
                        "-an",
                        "-sn",
                        "-dn",
                        "-vf",
                        f"fps=12/{clip.duration_s:.6f},scale=120:68,tile=12x1",
                        "-frames:v",
                        "1",
                        "-threads",
                        "1",
                        "-y",
                        str(output),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                strip = "data:image/jpeg;base64," + base64.b64encode(output.read_bytes()).decode(
                    "ascii"
                )
            peaks = []
            if source.has_audio:
                result = subprocess.run(
                    common
                    + ["-vn", "-sn", "-dn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                samples = array.array("h")
                samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
                if sys.byteorder != "little":
                    samples.byteswap()
                size = max(1, math.ceil(len(samples) / 240))
                peaks = [
                    round(max(abs(x) for x in samples[i : i + size]) / 32768, 4)
                    for i in range(0, len(samples), size)
                ]
            data = {"strip": strip, "peaks": peaks}
            payload = json.dumps(data).encode()
            if len(payload) <= _MAX_BYTES:
                temp = Path(tmp) / "timeline.json"
                temp.write_bytes(payload)
                temp.replace(target)
                for stale in directory.glob("*.json"):
                    if stale != target:
                        stale.unlink(missing_ok=True)
            return data
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, "Could not generate timeline media.") from exc
    finally:
        _RENDER_SLOT.release()
