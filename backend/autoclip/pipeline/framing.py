"""Resolve saved framing without modifying the automatic tracking cache."""

from __future__ import annotations

from typing import Any

from ..db.models import Clip, ClipEdit, Job, Source
from .reframe.croppath import CropKeyframe, CropPath, CropSegment, centre_crop, target_crop_size

MAX_FRAMING_SLICES = 64
SLICE_CONTIGUITY_TOLERANCE_S = 1e-6
_MIN_SEGMENT_SPAN_S = 1e-6


def validate_framing_bounds(framing: dict[str, Any] | None, source_duration_s: float) -> None:
    """Validate slice framing bounds against the source duration."""
    if not framing or framing.get("mode") != "slices":
        return
    if source_duration_s is None or source_duration_s <= 0:
        raise ValueError("Source duration is unavailable for validating slice bounds.")

    max_end = float(source_duration_s)
    segments = framing.get("segments") or []
    for index, seg in enumerate(segments):
        end_s = float(seg["end_s"])
        if end_s > max_end + SLICE_CONTIGUITY_TOLERANCE_S:
            raise ValueError(
                f"Slice segment {index} end_s ({end_s}) exceeds source duration ({max_end})."
            )


def interpolate_keyframe(keyframes: list[CropKeyframe], t: float) -> CropKeyframe:
    """Piecewise-linear interpolation of keyframe coordinates at clip-relative time ``t``."""
    if not keyframes:
        return CropKeyframe(t=t, x=0.0, y=0.0)
    if len(keyframes) == 1 or t <= keyframes[0].t:
        return CropKeyframe(t=t, x=keyframes[0].x, y=keyframes[0].y)
    if t >= keyframes[-1].t:
        return CropKeyframe(t=t, x=keyframes[-1].x, y=keyframes[-1].y)

    for prev, nxt in zip(keyframes, keyframes[1:], strict=False):
        if prev.t <= t <= nxt.t:
            span = nxt.t - prev.t
            if span <= 0:
                return CropKeyframe(t=t, x=prev.x, y=prev.y)
            ratio = (t - prev.t) / span
            x = prev.x + (nxt.x - prev.x) * ratio
            y = prev.y + (nxt.y - prev.y) * ratio
            return CropKeyframe(t=t, x=x, y=y)

    return CropKeyframe(t=t, x=keyframes[-1].x, y=keyframes[-1].y)


def clip_crop_segment(segment: CropSegment, start_s: float, end_s: float) -> CropSegment | None:
    """Intersect a clip-relative ``CropSegment`` with ``[start_s, end_s]`` without mutating it."""
    s0 = max(segment.start_s, start_s)
    s1 = min(segment.end_s, end_s)
    if s1 - s0 <= _MIN_SEGMENT_SPAN_S:
        return None

    if segment.fit:
        return CropSegment(
            start_s=s0,
            end_s=s1,
            width=segment.width,
            height=segment.height,
            keyframes=[CropKeyframe(t=s0, x=0.0, y=0.0)],
            strategy=segment.strategy,
            zoom=segment.zoom,
            fit=True,
        )

    if not segment.keyframes or segment.is_static:
        first = segment.keyframes[0] if segment.keyframes else CropKeyframe(0.0, 0.0, 0.0)
        keyframes = [CropKeyframe(t=s0, x=first.x, y=first.y)]
    else:
        start_kf = interpolate_keyframe(segment.keyframes, s0)
        end_kf = interpolate_keyframe(segment.keyframes, s1)
        interior = [
            CropKeyframe(t=k.t, x=k.x, y=k.y)
            for k in segment.keyframes
            if s0 + _MIN_SEGMENT_SPAN_S < k.t < s1 - _MIN_SEGMENT_SPAN_S
        ]
        keyframes = [start_kf, *interior]
        if s1 - s0 > _MIN_SEGMENT_SPAN_S:
            keyframes.append(end_kf)

    return CropSegment(
        start_s=s0,
        end_s=s1,
        width=segment.width,
        height=segment.height,
        keyframes=keyframes,
        strategy=segment.strategy,
        zoom=segment.zoom,
        fit=False,
    )


def slice_crop_path(path: CropPath, start_s: float, end_s: float) -> list[CropSegment]:
    """Intersect ``path`` segments with clip-relative ``[start_s, end_s]``."""
    out: list[CropSegment] = []
    for segment in path.segments:
        clipped = clip_crop_segment(segment, start_s, end_s)
        if clipped is not None:
            out.append(clipped)
    return out


def _fit_segment(width: int, height: int, start_s: float, end_s: float) -> CropSegment:
    return CropSegment(
        start_s=start_s,
        end_s=end_s,
        width=width,
        height=height,
        keyframes=[CropKeyframe(start_s, 0, 0)],
        fit=True,
    )


def _split_segment(width: int, height: int, start_s: float, end_s: float) -> CropSegment:
    return CropSegment(start_s, end_s, width, height, [CropKeyframe(start_s, 0, 0)], split=True)


def _manual_segment(
    width: int,
    height: int,
    ratio: str,
    framing: dict[str, Any],
    start_s: float,
    end_s: float,
) -> CropSegment:
    aspect_w, aspect_h = map(int, ratio.split(":"))
    base_w, base_h = target_crop_size(width, height, aspect_w, aspect_h)
    crop_w = max(2, int(base_w / framing["zoom"]) // 2 * 2)
    crop_h = max(2, int(base_h / framing["zoom"]) // 2 * 2)
    x = max(0, min(width - crop_w, framing["center_x"] * width - crop_w / 2))
    y = max(0, min(height - crop_h, framing["center_y"] * height - crop_h / 2))
    return CropSegment(
        start_s,
        end_s,
        crop_w,
        crop_h,
        [CropKeyframe(start_s, x, y)],
    )


def _resolve_auto_base(
    source: Source,
    clip: Clip,
    job: Job,
    ratio: str,
    cached_path: CropPath | None,
) -> CropPath:
    width, height = source.width or 1920, source.height or 1080
    original_ratio = job.settings.get("export", {}).get("ratio", "9:16")
    # shortcut: trimmed clips lose tracking; rebase only once original crop-time provenance exists.
    if (
        cached_path is not None
        and not clip.user_trimmed
        and ratio == original_ratio
        and cached_path.source_width == width
        and cached_path.source_height == height
        and abs(cached_path.duration_s - clip.duration_s) < 0.001
    ):
        return cached_path
    aspect_w, aspect_h = map(int, ratio.split(":"))
    return centre_crop(width, height, clip.duration_s, aspect_w=aspect_w, aspect_h=aspect_h)


def resolve_crop_path(
    source: Source, clip: Clip, job: Job, edit: ClipEdit, cached_path: CropPath | None
) -> CropPath:
    width, height = source.width or 1920, source.height or 1080
    if edit.framing is not None and edit.framing["mode"] == "split":
        return CropPath(width, height, [_split_segment(width, height, 0, clip.duration_s)])
    if edit.framing is not None and edit.framing["mode"] == "fit":
        return CropPath(width, height, [_fit_segment(width, height, 0, clip.duration_s)])
    if edit.framing is not None and edit.framing["mode"] == "manual":
        return CropPath(
            width,
            height,
            [_manual_segment(width, height, edit.ratio, edit.framing, 0, clip.duration_s)],
        )
    if edit.framing is not None and edit.framing["mode"] == "slices":
        auto_base = _resolve_auto_base(source, clip, job, edit.ratio, cached_path)
        return _resolve_slices(
            width, height, clip, edit.ratio, edit.framing.get("segments", []), auto_base
        )
    return _resolve_auto_base(source, clip, job, edit.ratio, cached_path)


def _resolve_slices(
    width: int,
    height: int,
    clip: Clip,
    ratio: str,
    raw_segments: list[dict[str, Any]],
    auto_base: CropPath,
) -> CropPath:
    duration_s = clip.duration_s
    if duration_s <= 0 or not raw_segments:
        return CropPath(width, height, slice_crop_path(auto_base, 0.0, max(0.0, duration_s)))

    visible: list[tuple[float, float, dict[str, Any]]] = []
    for seg in raw_segments:
        rel_start = max(0.0, min(duration_s, float(seg["start_s"]) - clip.start_s))
        rel_end = max(0.0, min(duration_s, float(seg["end_s"]) - clip.start_s))
        if rel_end - rel_start > _MIN_SEGMENT_SPAN_S:
            visible.append((rel_start, rel_end, seg["framing"]))

    if not visible:
        return CropPath(width, height, slice_crop_path(auto_base, 0.0, duration_s))

    spans: list[tuple[float, float, dict[str, Any]]] = []
    cursor = 0.0
    for start_s, end_s, leaf in visible:
        if start_s - cursor > _MIN_SEGMENT_SPAN_S:
            spans.append((cursor, start_s, {"mode": "auto"}))
            s0 = start_s
        else:
            s0 = cursor
        if end_s - s0 > _MIN_SEGMENT_SPAN_S:
            spans.append((s0, end_s, leaf))
            cursor = end_s

    if duration_s - cursor > _MIN_SEGMENT_SPAN_S:
        spans.append((cursor, duration_s, {"mode": "auto"}))
    elif spans:
        last_s0, _, last_leaf = spans[-1]
        spans[-1] = (last_s0, duration_s, last_leaf)

    resolved: list[CropSegment] = []
    for s0, s1, leaf in spans:
        mode = leaf.get("mode", "auto")
        if mode == "fit":
            resolved.append(_fit_segment(width, height, s0, s1))
        elif mode == "split":
            resolved.append(_split_segment(width, height, s0, s1))
        elif mode == "manual":
            resolved.append(_manual_segment(width, height, ratio, leaf, s0, s1))
        else:
            resolved.extend(slice_crop_path(auto_base, s0, s1))

    if resolved:
        resolved[0].start_s = 0.0
        resolved[-1].end_s = duration_s

    return CropPath(width, height, resolved)
