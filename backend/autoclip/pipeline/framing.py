"""Resolve saved framing without modifying the automatic tracking cache."""

from __future__ import annotations

from ..db.models import Clip, ClipEdit, Job, Source
from .reframe.croppath import CropKeyframe, CropPath, CropSegment, centre_crop


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
