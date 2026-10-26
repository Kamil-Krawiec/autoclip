"""Resolve saved framing without modifying the automatic tracking cache."""

from __future__ import annotations

from ..db.models import Clip, ClipEdit, Job, Source
from .reframe.croppath import CropKeyframe, CropPath, CropSegment, centre_crop, target_crop_size


def resolve_crop_path(
    source: Source, clip: Clip, job: Job, edit: ClipEdit, cached_path: CropPath | None
) -> CropPath:
    width, height = source.width or 1920, source.height or 1080
    if edit.framing is not None and edit.framing["mode"] == "fit":
        return CropPath(
            width,
            height,
            [
                CropSegment(
                    start_s=0,
                    end_s=clip.duration_s,
                    width=width,
                    height=height,
                    keyframes=[CropKeyframe(0, 0, 0)],
                    fit=True,
                )
            ],
        )
    if edit.framing is not None and edit.framing["mode"] == "manual":
        aspect_w, aspect_h = map(int, edit.ratio.split(":"))
        base_w, base_h = target_crop_size(width, height, aspect_w, aspect_h)
        crop_w = max(2, int(base_w / edit.framing["zoom"]) // 2 * 2)
        crop_h = max(2, int(base_h / edit.framing["zoom"]) // 2 * 2)
        x = max(0, min(width - crop_w, edit.framing["center_x"] * width - crop_w / 2))
        y = max(0, min(height - crop_h, edit.framing["center_y"] * height - crop_h / 2))
        return CropPath(
            width,
            height,
            [CropSegment(0, clip.duration_s, crop_w, crop_h, [CropKeyframe(0, x, y)])],
        )
    original_ratio = job.settings.get("export", {}).get("ratio", "9:16")
    # shortcut: trimmed clips lose tracking; rebase only once original crop-time provenance exists.
    if (
        cached_path is not None
        and not clip.user_trimmed
        and edit.ratio == original_ratio
        and cached_path.source_width == width
        and cached_path.source_height == height
        and abs(cached_path.duration_s - clip.duration_s) < 0.001
    ):
        return cached_path
    aspect_w, aspect_h = map(int, edit.ratio.split(":"))
    return centre_crop(width, height, clip.duration_s, aspect_w=aspect_w, aspect_h=aspect_h)
