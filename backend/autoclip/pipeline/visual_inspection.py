"""Visual inspection primitives: timeline mapping, frame extraction, contact sheets,
and deterministic edit/export revision tracking for MCP inspection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np

from .. import library as media_library
from .. import paths
from ..db import store
from ..db.models import Clip, ClipEdit, Export, Job, Source, utcnow
from . import captions as captions_module
from . import export as export_module
from . import ffmpeg
from .captions import CaptionStyle
from .framing import interpolate_keyframe, resolve_crop_path
from .reframe.croppath import CropPath, CropSegment
from .runner import JobWorkspace
from .transcript import Transcript, Word

TimelineKind = Literal["retained", "clip", "source"]
TargetKind = Literal["both", "composed", "source", "export"]
ImageFormat = Literal["jpeg", "png"]

MAX_FRAMES_PER_REQUEST = 8
MAX_CONTACT_SHEET_TILES = 8
MAX_FRAME_WIDTH = 720
DEFAULT_JPEG_QUALITY = 82
MAX_RESPONSE_BYTES = 4_000_000
_BOUNDARY_EPSILON_S = 1e-6
_SEEK_END_GUARD_S = 0.04
_FRAME_TIMEOUT_S = 15.0
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_INSPECT_RENDER_SLOT = threading.BoundedSemaphore(2)


class InspectionError(RuntimeError):
    """Frame extraction or inspection failed."""


class InspectionNotFoundError(InspectionError):
    """Requested clip, source, or export does not exist."""


class InspectionPermissionError(InspectionError):
    """Requested artifact does not belong to the clip."""


class InspectionUnavailableError(InspectionError):
    """Media or export artifact is missing or unsafe."""


class InspectionValidationError(ValueError):
    """Requested timestamp or inspection parameter is invalid."""


@dataclass(frozen=True)
class MappedTimestamp:
    requested_t: float
    timeline: TimelineKind
    source_t: float
    clip_t: float
    retained_t: float | None
    in_removed_range: bool
    window_index: int | None


@dataclass(frozen=True)
class EncodedImage:
    mime_type: str
    width: int
    height: int
    data: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def to_payload(self) -> dict[str, Any]:
        return {
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "size_bytes": len(self.data),
            "data_base64": base64.b64encode(self.data).decode("ascii"),
        }


def validate_safe_id(value: str, label: str = "id") -> str:
    if not value or not _SAFE_ID_RE.fullmatch(value):
        raise InspectionValidationError(f"Invalid {label}.")
    return value


def ensure_no_symlinks(path: Path, root: Path) -> Path:
    """Reject symlinks and lexical escapes before touching local artifacts."""
    try:
        relative = path.absolute().relative_to(root.absolute())
        current = root.absolute()
        if current.is_symlink():
            raise ValueError("Symlink root")
        for part in relative.parts:
            if part == "..":
                raise ValueError("Path escape")
            current = current / part
            if current.is_symlink():
                raise ValueError("Symlink artifact")
        return path
    except (ValueError, OSError) as exc:
        raise InspectionUnavailableError("Media path is unavailable or unsafe.") from exc


def resolve_source_media_path(source: Source) -> Path:
    try:
        path = media_library.media_path(source)
        if source.library_path is None:
            ensure_no_symlinks(path, paths.media_dir())
        else:
            ensure_no_symlinks(path, media_library.LIBRARY_ROOT)
        if not path.is_file():
            raise OSError("Missing source file")
        return path
    except (media_library.LibraryError, OSError) as exc:
        raise InspectionUnavailableError("Source media is unavailable.") from exc


def resolve_export_media_path(export: Export) -> Path:
    path = Path(export.path)
    ensure_no_symlinks(path, paths.exports_dir())
    if not path.is_file():
        raise InspectionUnavailableError("Exported file is missing.")
    return path


# --------------------------------------------------------------------------
# Timeline mapping
# --------------------------------------------------------------------------


def retained_windows_for_clip(clip: Clip, edit: ClipEdit | None) -> list[tuple[float, float]]:
    removed = edit.removed_ranges if edit else []
    return export_module.kept_windows(clip.start_s, clip.end_s, removed)


def timeline_summary(clip: Clip, edit: ClipEdit | None) -> dict[str, Any]:
    windows = retained_windows_for_clip(clip, edit)
    kept_entries: list[dict[str, float]] = []
    acc = 0.0
    for w_start, w_end in windows:
        span = w_end - w_start
        kept_entries.append(
            {
                "clip_start_s": round(w_start, 6),
                "clip_end_s": round(w_end, 6),
                "source_start_s": round(clip.start_s + w_start, 6),
                "source_end_s": round(clip.start_s + w_end, 6),
                "retained_start_s": round(acc, 6),
                "retained_end_s": round(acc + span, 6),
            }
        )
        acc += span
    return {
        "clip_start_s": clip.start_s,
        "clip_end_s": clip.end_s,
        "clip_duration_s": clip.duration_s,
        "retained_duration_s": round(acc, 6),
        "removed_ranges": edit.removed_ranges if edit else [],
        "kept_windows": kept_entries,
    }


def map_timestamp(
    t: float,
    *,
    timeline: TimelineKind = "retained",
    clip_start_s: float,
    clip_end_s: float,
    windows: list[tuple[float, float]],
    source_duration_s: float | None = None,
    allow_outside_clip: bool = False,
) -> MappedTimestamp:
    """Map a timestamp between ``retained``, ``clip``, and ``source`` coordinates.

    Window boundaries on ``retained`` jump cleanly to the start of the next
    kept window at seams; timestamps falling inside a removed range on ``clip``
    or ``source`` return ``retained_t=None`` without silently shifting time.
    """
    if not isinstance(t, int | float) or isinstance(t, bool) or not math.isfinite(t):
        raise InspectionValidationError("Timestamp must be a finite number.")
    t = float(t)
    clip_duration_s = clip_end_s - clip_start_s
    if clip_duration_s <= 0 or not windows:
        raise InspectionValidationError("Clip has no retained footage.")

    retained_duration_s = sum(w_end - w_start for w_start, w_end in windows)

    if timeline == "retained":
        if t < -_BOUNDARY_EPSILON_S or t > retained_duration_s + _BOUNDARY_EPSILON_S:
            raise InspectionValidationError(
                f"Retained timestamp {t} is outside [0, {retained_duration_s:.3f}]."
            )
        clamped_t = max(0.0, min(retained_duration_s, t))
        acc = 0.0
        for idx, (w_start, w_end) in enumerate(windows):
            span = w_end - w_start
            is_last = idx == len(windows) - 1
            if clamped_t < acc + span - _BOUNDARY_EPSILON_S or (
                is_last and clamped_t <= acc + span + _BOUNDARY_EPSILON_S
            ):
                local = max(0.0, min(span, clamped_t - acc))
                clip_t = w_start + local
                return MappedTimestamp(
                    requested_t=t,
                    timeline=timeline,
                    source_t=clip_start_s + clip_t,
                    clip_t=clip_t,
                    retained_t=clamped_t,
                    in_removed_range=False,
                    window_index=idx,
                )
            acc += span
        _, last_w_end = windows[-1]
        return MappedTimestamp(
            requested_t=t,
            timeline=timeline,
            source_t=clip_start_s + last_w_end,
            clip_t=last_w_end,
            retained_t=retained_duration_s,
            in_removed_range=False,
            window_index=len(windows) - 1,
        )

    if timeline == "clip":
        if t < -_BOUNDARY_EPSILON_S or t > clip_duration_s + _BOUNDARY_EPSILON_S:
            raise InspectionValidationError(
                f"Clip timestamp {t} is outside [0, {clip_duration_s:.3f}]."
            )
        clip_t = max(0.0, min(clip_duration_s, t))
        source_t = clip_start_s + clip_t
    elif timeline == "source":
        if allow_outside_clip:
            upper = source_duration_s if source_duration_s and source_duration_s > 0 else clip_end_s
            if t < -_BOUNDARY_EPSILON_S or t > upper + _BOUNDARY_EPSILON_S:
                raise InspectionValidationError(
                    f"Source timestamp {t} is outside [0, {upper:.3f}]."
                )
            source_t = max(0.0, min(upper, t))
            clip_t = source_t - clip_start_s
            if (
                source_t < clip_start_s - _BOUNDARY_EPSILON_S
                or source_t > clip_end_s + _BOUNDARY_EPSILON_S
            ):
                return MappedTimestamp(
                    requested_t=t,
                    timeline=timeline,
                    source_t=source_t,
                    clip_t=clip_t,
                    retained_t=None,
                    in_removed_range=False,
                    window_index=None,
                )
        else:
            if t < clip_start_s - _BOUNDARY_EPSILON_S or t > clip_end_s + _BOUNDARY_EPSILON_S:
                raise InspectionValidationError(
                    f"Source timestamp {t} is outside clip bounds "
                    f"[{clip_start_s:.3f}, {clip_end_s:.3f}]."
                )
            source_t = max(clip_start_s, min(clip_end_s, t))
            clip_t = source_t - clip_start_s
    else:
        raise InspectionValidationError(f"Unknown timeline {timeline!r}.")

    acc = 0.0
    for idx, (w_start, w_end) in enumerate(windows):
        is_trailing_end = (
            idx == len(windows) - 1
            and abs(w_end - clip_duration_s) <= _BOUNDARY_EPSILON_S
            and abs(clip_t - clip_duration_s) <= _BOUNDARY_EPSILON_S
        )
        if (
            w_start - _BOUNDARY_EPSILON_S <= clip_t < w_end - _BOUNDARY_EPSILON_S
        ) or is_trailing_end:
            local = max(0.0, min(w_end - w_start, clip_t - w_start))
            return MappedTimestamp(
                requested_t=t,
                timeline=timeline,
                source_t=source_t,
                clip_t=clip_t,
                retained_t=acc + local,
                in_removed_range=False,
                window_index=idx,
            )
        acc += w_end - w_start

    return MappedTimestamp(
        requested_t=t,
        timeline=timeline,
        source_t=source_t,
        clip_t=clip_t,
        retained_t=None,
        in_removed_range=True,
        window_index=None,
    )


# --------------------------------------------------------------------------
# Deterministic edit/export revision & freshness tracking
# --------------------------------------------------------------------------


def _revisions_dir() -> Path:
    return paths.exports_dir() / "_revisions"


def _revision_sidecar_path(export_id: str) -> Path:
    safe_id = validate_safe_id(export_id, "export_id")
    path = _revisions_dir() / f"{safe_id}.json"
    ensure_no_symlinks(path, paths.exports_dir())
    return path


def compute_render_revision(
    *,
    clip_id: str,
    start_s: float,
    end_s: float,
    ratio: str,
    style: CaptionStyle | str,
    caption_overrides: dict[str, float] | None,
    burn_captions: bool,
    crop_path: CropPath,
    framing: dict[str, Any] | None,
    removed_ranges: list[dict[str, float]],
    words: list[Word] | list[dict[str, Any]],
) -> str:
    """Compute a deterministic hash of the exact visual/caption state of a render."""
    style_key = style.key if isinstance(style, CaptionStyle) else str(style)
    norm_words = [
        {
            "text": w.text if isinstance(w, Word) else str(w["text"]),
            "start": round(float(w.start if isinstance(w, Word) else w["start"]), 6),
            "end": round(float(w.end if isinstance(w, Word) else w["end"]), 6),
            "speaker": w.speaker if isinstance(w, Word) else w.get("speaker"),
        }
        for w in words
    ]
    norm_cuts = [
        {"start_s": round(float(c["start_s"]), 6), "end_s": round(float(c["end_s"]), 6)}
        for c in removed_ranges
    ]
    norm_overrides = (
        {k: round(float(v), 6) for k, v in sorted(caption_overrides.items()) if v is not None}
        if caption_overrides
        else None
    )
    payload = {
        "schema": 1,
        "clip_id": clip_id,
        "start_s": round(float(start_s), 6),
        "end_s": round(float(end_s), 6),
        "ratio": ratio,
        "style_key": style_key,
        "caption_overrides": norm_overrides,
        "burn_captions": bool(burn_captions),
        "framing": framing,
        "crop_path": crop_path.to_dict(),
        "removed_ranges": norm_cuts,
        "words": norm_words,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_clip_words_for_revision(clip: Clip, edit: ClipEdit) -> list[Word]:
    if edit.edited_words is not None:
        return [
            Word(
                text=str(w["text"]),
                start=float(w["start"]),
                end=float(w["end"]),
                speaker=w.get("speaker"),
            )
            for w in edit.edited_words
            if float(w["end"]) > clip.start_s and float(w["start"]) < clip.end_s
        ]
    workspace = JobWorkspace(clip.job_id)
    if not workspace.transcript.exists():
        return []
    transcript = Transcript.load(workspace.transcript)
    return transcript.slice(clip.start_word, clip.end_word)


def load_clip_render_context(
    clip_id: str,
    *,
    ratio_override: str | None = None,
    style_override: str | None = None,
) -> tuple[Clip, Job, Source, ClipEdit, CropPath, CaptionStyle, str, list[Word], str]:
    validate_safe_id(clip_id, "clip_id")
    clip = store.get_clip(clip_id)
    if clip is None:
        raise InspectionNotFoundError("Clip not found.")
    job = store.get_job(clip.job_id)
    source = store.get_source(job.source_id) if job else None
    if job is None or source is None:
        raise InspectionNotFoundError("The clip's source is missing.")
    edit = store.get_clip_edit(clip_id) or ClipEdit(clip_id=clip_id)
    ratio = ratio_override or edit.ratio
    try:
        style = captions_module.resolve_style(
            style_override if style_override is not None else edit.caption_style,
            edit.caption_overrides,
        )
    except ValueError as exc:
        raise InspectionValidationError(str(exc)) from exc

    workspace = JobWorkspace(clip.job_id)
    cached = workspace.crop_path(clip.id)
    cached_path = CropPath.load(cached) if cached.exists() else None
    crop_path = resolve_crop_path(source, clip, job, replace(edit, ratio=ratio), cached_path)
    words = _load_clip_words_for_revision(clip, edit)
    revision = compute_render_revision(
        clip_id=clip.id,
        start_s=clip.start_s,
        end_s=clip.end_s,
        ratio=ratio,
        style=style,
        caption_overrides=edit.caption_overrides,
        burn_captions=edit.burn_captions,
        crop_path=crop_path,
        framing=edit.framing,
        removed_ranges=edit.removed_ranges,
        words=words,
    )
    return clip, job, source, edit, crop_path, style, ratio, words, revision


def compute_clip_revision(
    clip_id: str,
    *,
    ratio_override: str | None = None,
    style_override: str | None = None,
) -> tuple[str, dict[str, Any]]:
    clip, _, _, edit, _, style, ratio, _, revision = load_clip_render_context(
        clip_id, ratio_override=ratio_override, style_override=style_override
    )
    return revision, {
        "clip_id": clip.id,
        "ratio": ratio,
        "style": style.key,
        "timeline": timeline_summary(clip, edit),
    }


def record_export_revision(
    export_id: str,
    *,
    clip_id: str,
    revision: str,
    ratio: str | None = None,
    style: str | None = None,
) -> Path:
    """Persist the deterministic snapshot revision captured when an export started."""
    validate_safe_id(clip_id, "clip_id")
    target = _revision_sidecar_path(export_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_no_symlinks(target.parent, paths.exports_dir())
    payload = {
        "version": 1,
        "export_id": export_id,
        "clip_id": clip_id,
        "revision": revision,
        "ratio": ratio,
        "style": style,
        "recorded_at": utcnow(),
    }
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{export_id}-",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(json.dumps(payload, indent=2))
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)
    return target


def read_export_revision(export_id: str) -> dict[str, Any] | None:
    try:
        path = _revision_sidecar_path(export_id)
    except InspectionError:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("revision"), str):
            return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def assess_export_freshness(
    clip_id: str,
    current_revision: str,
    exports: list[Export],
) -> dict[str, Any]:
    """Compare latest export's recorded snapshot revision against ``current_revision``.

    Never claims deterministic freshness for legacy exports that lack a recorded
    snapshot sidecar.
    """
    if not exports:
        return {
            "status": "no_exports",
            "is_fresh": False,
            "deterministic": True,
            "reason": "No exports exist for this clip yet.",
            "current_revision": current_revision,
            "latest_export": None,
        }

    latest = exports[-1]
    sidecar = read_export_revision(latest.id)
    recorded_revision = sidecar["revision"] if sidecar else None
    latest_info = {
        "id": latest.id,
        "ratio": latest.ratio,
        "style": latest.style,
        "created_at": latest.created_at,
        "recorded_revision": recorded_revision,
    }

    if sidecar is None or sidecar.get("clip_id") not in (None, clip_id):
        return {
            "status": "unknown_legacy_export",
            "is_fresh": None,
            "deterministic": False,
            "reason": (
                "Latest export has no recorded render revision snapshot; "
                "cannot deterministically verify freshness against current edits."
            ),
            "current_revision": current_revision,
            "latest_export": latest_info,
        }

    if recorded_revision == current_revision:
        return {
            "status": "fresh",
            "is_fresh": True,
            "deterministic": True,
            "reason": "Latest export matches current edit revision.",
            "current_revision": current_revision,
            "latest_export": latest_info,
        }

    return {
        "status": "stale",
        "is_fresh": False,
        "deterministic": True,
        "reason": "Clip edit or trim changed after the latest export snapshot.",
        "current_revision": current_revision,
        "latest_export": latest_info,
    }


# --------------------------------------------------------------------------
# Frame encoding, extraction, and contact sheets
# --------------------------------------------------------------------------


def encode_image(
    bgr: np.ndarray,
    *,
    fmt: ImageFormat = "jpeg",
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> EncodedImage:
    if fmt == "jpeg":
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
        mime = "image/jpeg"
    elif fmt == "png":
        ok, buf = cv2.imencode(".png", bgr, [int(cv2.IMWRITE_PNG_COMPRESSION), 6])
        mime = "image/png"
    else:
        raise InspectionValidationError(f"Unsupported format {fmt!r}.")
    if not ok:
        raise InspectionError("Failed to encode inspection frame.")
    h, w = bgr.shape[:2]
    return EncodedImage(mime_type=mime, width=int(w), height=int(h), data=buf.tobytes())


def _scaled_dimensions(src_w: int, src_h: int, max_width: int) -> tuple[int, int]:
    if src_w <= max_width:
        w = max(2, src_w - (src_w % 2))
        h = max(2, src_h - (src_h % 2))
        return w, h
    scale = max_width / float(src_w)
    w = max(2, int(round(src_w * scale)) // 2 * 2)
    h = max(2, int(round(src_h * scale)) // 2 * 2)
    return w, h


def _decode_png_bytes(png_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise InspectionError("FFmpeg returned invalid image data.")
    return img


def _run_ffmpeg_frame(args: list[str], *, cwd: Path | None = None) -> np.ndarray:
    if not _INSPECT_RENDER_SLOT.acquire(timeout=_FRAME_TIMEOUT_S):
        raise InspectionError("Visual inspection renderer is busy. Retry shortly.")
    try:
        proc = subprocess.run(
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
                *args,
                "-frames:v",
                "1",
                "-an",
                "-sn",
                "-dn",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            cwd=cwd,
            capture_output=True,
            timeout=_FRAME_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise InspectionError("Frame extraction timed out.") from exc
    finally:
        _INSPECT_RENDER_SLOT.release()

    if proc.returncode != 0 or not proc.stdout:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise InspectionError(f"Frame extraction failed: {stderr or 'empty output'}")
    return _decode_png_bytes(proc.stdout)


def extract_source_frame(
    media_path: Path,
    source_t: float,
    *,
    duration_s: float = 0.0,
    max_width: int = MAX_FRAME_WIDTH,
    fmt: ImageFormat = "jpeg",
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> EncodedImage:
    seek_t = max(0.0, source_t)
    if duration_s > 0:
        seek_t = min(seek_t, max(0.0, duration_s - _SEEK_END_GUARD_S))
    vf = f"scale='min({int(max_width)},iw)':-2:flags=lanczos"
    bgr = _run_ffmpeg_frame(["-ss", f"{seek_t:.6f}", "-i", str(media_path), "-vf", vf])
    return encode_image(bgr, fmt=fmt, jpeg_quality=jpeg_quality)


def segment_at_clip_time(crop_path: CropPath, clip_t: float) -> tuple[CropSegment, str]:
    segments = crop_path.segments
    if not segments:
        raise InspectionError("Clip crop path has no segments.")
    chosen = segments[-1]
    for seg in segments:
        if seg.start_s - _BOUNDARY_EPSILON_S <= clip_t <= seg.end_s + _BOUNDARY_EPSILON_S:
            chosen = seg
            if clip_t < seg.end_s - _BOUNDARY_EPSILON_S:
                break
    if chosen.fit:
        mode = "fit"
    elif chosen.split:
        mode = "split"
    elif chosen.strategy.value in ("track", "wide"):
        mode = "auto"
    else:
        mode = "manual" if chosen.width != chosen.height else "auto"
    return chosen, mode


def active_framing_label(
    edit: ClipEdit, segment: CropSegment, clip_t: float, clip_start_s: float
) -> str:
    if segment.fit:
        return "fit"
    if segment.split:
        return "split"
    if edit.framing is None:
        return "auto"
    mode = edit.framing.get("mode", "auto")
    if mode in ("fit", "split", "manual"):
        return str(mode)
    if mode == "slices":
        abs_t = clip_start_s + clip_t
        segments = edit.framing.get("segments", [])
        for seg in segments:
            if (
                float(seg["start_s"]) - _BOUNDARY_EPSILON_S
                <= abs_t
                <= float(seg["end_s"]) + _BOUNDARY_EPSILON_S
            ) and (abs_t < float(seg["end_s"]) - _BOUNDARY_EPSILON_S or seg is segments[-1]):
                return str(seg["framing"].get("mode", "auto"))
        return "auto"
    return "auto"


def extract_composed_frame(
    *,
    media_path: Path,
    clip: Clip,
    source_duration_s: float,
    crop_path: CropPath,
    clip_t: float,
    words: list[Word],
    style: CaptionStyle,
    ratio: str,
    burn_captions: bool,
    max_width: int = MAX_FRAME_WIDTH,
    fmt: ImageFormat = "jpeg",
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> EncodedImage:
    """Render one composed frame matching export geometry, framing, and ASS captions."""
    out_w, out_h = export_module.ratio_dimensions(ratio)
    disp_w, disp_h = _scaled_dimensions(out_w, out_h, max_width)
    segment, _ = segment_at_clip_time(crop_path, clip_t)

    parts: list[str] = []
    if segment.split:
        parts.extend(
            export_module._split_chain(
                "[0:v]",
                0,
                out_w,
                out_h,
                segment.split_panels,
                crop_path.source_width,
                crop_path.source_height,
            )
        )
    elif segment.fit:
        parts.extend(export_module._fit_chain("[0:v]", 0, out_w, out_h))
    else:
        kf = interpolate_keyframe(segment.keyframes, clip_t)
        max_x = max(0.0, float(crop_path.source_width - segment.width))
        max_y = max(0.0, float(crop_path.source_height - segment.height))
        cx = max(0.0, min(max_x, kf.x))
        cy = max(0.0, min(max_y, kf.y))
        parts.append(
            f"[0:v]crop={segment.width}:{segment.height}:{cx:.2f}:{cy:.2f},"
            f"scale={out_w}:{out_h}:flags=lanczos,setsar=1,format=yuv420p[v0]"
        )

    source_seek_t = max(0.0, clip.start_s + clip_t)
    upper_dur = source_duration_s if source_duration_s > 0 else clip.end_s
    if upper_dur > 0:
        source_seek_t = min(source_seek_t, max(0.0, upper_dur - _SEEK_END_GUARD_S))

    if burn_captions and words:
        with tempfile.TemporaryDirectory(prefix="inspect-ass-") as tmp_dir:
            ass_path = captions_module.write_ass(
                Path(tmp_dir) / "captions.ass",
                words,
                style,
                width=out_w,
                height=out_h,
                time_offset_s=clip.start_s,
            )
            render_cwd, sub_name, fonts_name = ffmpeg.relative_filter_workspace(
                ass_path, captions_module.FONT_DIR
            )
            parts.append(
                f"[v0]setpts=PTS-STARTPTS+{clip_t:.6f}/TB,"
                f"ass=filename={sub_name}:fontsdir={fonts_name}[vass]"
            )
            parts.append(f"[vass]scale={disp_w}:{disp_h}:flags=lanczos[vout]")
            bgr = _run_ffmpeg_frame(
                [
                    "-ss",
                    f"{source_seek_t:.6f}",
                    "-i",
                    str(media_path),
                    "-filter_complex",
                    ";".join(parts),
                    "-map",
                    "[vout]",
                ],
                cwd=render_cwd,
            )
    else:
        parts.append(f"[v0]scale={disp_w}:{disp_h}:flags=lanczos[vout]")
        bgr = _run_ffmpeg_frame(
            [
                "-ss",
                f"{source_seek_t:.6f}",
                "-i",
                str(media_path),
                "-filter_complex",
                ";".join(parts),
                "-map",
                "[vout]",
            ]
        )

    return encode_image(bgr, fmt=fmt, jpeg_quality=jpeg_quality)


def build_contact_sheet(
    images_and_meta: list[tuple[EncodedImage, dict[str, Any]]],
    *,
    max_width: int = MAX_FRAME_WIDTH,
    columns: int | None = None,
    fmt: ImageFormat = "jpeg",
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> tuple[EncodedImage, list[dict[str, Any]]]:
    """Arrange decoded inspection frames into a labeled grid under ``max_width``."""
    count = len(images_and_meta)
    if count == 0 or count > MAX_CONTACT_SHEET_TILES:
        raise InspectionValidationError(
            f"Contact sheet requires 1..{MAX_CONTACT_SHEET_TILES} tiles."
        )

    cols = columns if columns is not None else min(count, 4 if count >= 4 else count)
    cols = max(1, min(cols, count))
    rows = math.ceil(count / cols)

    pad = 8
    header_h = 24
    tile_w = max(80, ((max_width - pad * (cols + 1)) // cols) // 2 * 2)

    decoded: list[tuple[np.ndarray, dict[str, Any]]] = []
    max_img_h = 0
    for enc, meta in images_and_meta:
        bgr = cv2.imdecode(np.frombuffer(enc.data, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise InspectionError("Failed to decode tile for contact sheet.")
        src_h, src_w = bgr.shape[:2]
        tile_h = max(40, int(round(src_h * (tile_w / float(src_w)))))
        resized = cv2.resize(bgr, (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        max_img_h = max(max_img_h, tile_h)
        decoded.append((resized, meta))

    cell_h = max_img_h + header_h
    canvas_w = pad + cols * (tile_w + pad)
    canvas_h = pad + rows * (cell_h + pad)
    canvas = np.full((canvas_h, canvas_w, 3), (22, 24, 29), dtype=np.uint8)

    tiles_out: list[dict[str, Any]] = []
    for idx, (tile_bgr, meta) in enumerate(decoded):
        r = idx // cols
        c = idx % cols
        x0 = pad + c * (tile_w + pad)
        y0 = pad + r * (cell_h + pad)

        cv2.rectangle(canvas, (x0, y0), (x0 + tile_w, y0 + header_h), (38, 42, 52), -1)
        ret = meta.get("retained_t")
        ret_str = f"r={ret:.2f}s" if ret is not None else "CUT"
        label = f"#{idx + 1} {ret_str} c={meta['clip_t']:.2f}s"
        if meta.get("framing_mode"):
            label += f" [{meta['framing_mode']}]"
        cv2.putText(
            canvas,
            label,
            (x0 + 5, y0 + 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            (235, 240, 250),
            1,
            cv2.LINE_AA,
        )

        th, tw = tile_bgr.shape[:2]
        img_y0 = y0 + header_h
        canvas[img_y0 : img_y0 + th, x0 : x0 + tw] = tile_bgr

        tiles_out.append(
            {
                "index": idx,
                "bbox": [int(x0), int(y0), int(tile_w), int(header_h + th)],
                **meta,
            }
        )

    sheet = encode_image(canvas, fmt=fmt, jpeg_quality=jpeg_quality)
    return sheet, tiles_out
