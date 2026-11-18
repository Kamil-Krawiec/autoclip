"""REST endpoints for MCP visual inspection (frames, contact sheets, revision status)."""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from ..db import models, store
from ..pipeline import screen_analysis as sa
from ..pipeline import visual_inspection as vi
from .schemas import AutoFitFramingIn, LeafFramingIn, ManualFramingIn

router = APIRouter(prefix="/api", tags=["visual-inspection"])


def _validate_finite_timestamps(values: list[float]) -> list[float]:
    out: list[float] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(float(v)):
            raise ValueError("Timestamps must be finite numbers.")
        out.append(float(v))
    return out


class FramesRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamps: list[Annotated[StrictFloat | StrictInt, Field(allow_inf_nan=False)]] = Field(
        min_length=1, max_length=vi.MAX_FRAMES_PER_REQUEST
    )
    timeline: vi.TimelineKind = "retained"
    target: vi.TargetKind = "both"
    export_id: str | None = None
    max_width: int = Field(default=vi.MAX_FRAME_WIDTH, ge=160, le=vi.MAX_FRAME_WIDTH, strict=True)
    format: vi.ImageFormat = "jpeg"
    jpeg_quality: int = Field(default=vi.DEFAULT_JPEG_QUALITY, ge=40, le=95, strict=True)
    ratio: Literal["9:16", "1:1", "16:9"] | None = None
    style: str | None = None
    framing: LeafFramingIn | None = None

    @field_validator("timestamps")
    @classmethod
    def _check_timestamps(cls, values: list[float]) -> list[float]:
        return _validate_finite_timestamps(values)

    @model_validator(mode="after")
    def _check_export_target(self) -> FramesRequestIn:
        if self.framing is not None and self.target not in ("composed", "both"):
            raise ValueError("framing is only allowed for composed frames.")
        if self.target == "export" and not self.export_id:
            raise ValueError("export_id is required when target='export'.")
        if self.target != "export" and self.export_id is not None:
            raise ValueError("export_id is only allowed when target='export'.")
        return self


class ContactSheetRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamps: list[Annotated[StrictFloat | StrictInt, Field(allow_inf_nan=False)]] | None = Field(
        default=None, min_length=1, max_length=vi.MAX_CONTACT_SHEET_TILES
    )
    tile_count: int = Field(default=6, ge=1, le=vi.MAX_CONTACT_SHEET_TILES, strict=True)
    timeline: vi.TimelineKind = "retained"
    target: Literal["composed", "source", "export"] = "composed"
    export_id: str | None = None
    max_width: int = Field(default=vi.MAX_FRAME_WIDTH, ge=240, le=vi.MAX_FRAME_WIDTH, strict=True)
    columns: int | None = Field(default=None, ge=1, le=4, strict=True)
    format: vi.ImageFormat = "jpeg"
    jpeg_quality: int = Field(default=vi.DEFAULT_JPEG_QUALITY, ge=40, le=95, strict=True)
    ratio: Literal["9:16", "1:1", "16:9"] | None = None
    style: str | None = None

    @field_validator("timestamps")
    @classmethod
    def _check_timestamps(cls, values: list[float] | None) -> list[float] | None:
        if values is None:
            return None
        return _validate_finite_timestamps(values)

    @model_validator(mode="after")
    def _check_export_target(self) -> ContactSheetRequestIn:
        if self.target == "export" and not self.export_id:
            raise ValueError("export_id is required when target='export'.")
        if self.target != "export" and self.export_id is not None:
            raise ValueError("export_id is only allowed when target='export'.")
        return self


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, vi.InspectionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, vi.InspectionPermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, vi.InspectionUnavailableError):
        return HTTPException(status_code=410, detail=str(exc))
    if isinstance(exc, vi.InspectionValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, vi.InspectionError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _resolve_export_for_clip(clip_id: str, export_id: str):
    vi.validate_safe_id(export_id, "export_id")
    record = store.get_export(export_id)
    if record is None:
        raise vi.InspectionNotFoundError("Export not found.")
    if record.clip_id != clip_id:
        raise vi.InspectionPermissionError("Export does not belong to this clip.")
    export_path = vi.resolve_export_media_path(record)
    return record, export_path


@router.get("/clips/{clip_id}/inspection/revision")
async def get_inspection_revision(
    clip_id: str,
    ratio: Literal["9:16", "1:1", "16:9"] | None = None,
    style: str | None = None,
) -> dict[str, Any]:
    """Return current deterministic edit revision, timeline summary, and export freshness."""
    try:
        revision, meta = await asyncio.to_thread(
            vi.compute_clip_revision, clip_id, ratio_override=ratio, style_override=style
        )
        exports = await asyncio.to_thread(store.list_exports, clip_id)
        freshness = vi.assess_export_freshness(clip_id, revision, exports)
    except Exception as exc:
        raise _translate_error(exc) from exc

    return {
        "clip_id": clip_id,
        "current_revision": revision,
        "ratio": meta["ratio"],
        "style": meta["style"],
        "timeline": meta["timeline"],
        "export_freshness": freshness,
    }


def _extract_frames_sync(clip_id: str, payload: FramesRequestIn) -> dict[str, Any]:
    clip, job, source, edit, crop_path, style, ratio, words, revision = vi.load_clip_render_context(
        clip_id, ratio_override=payload.ratio, style_override=payload.style
    )
    if not source.has_video:
        raise vi.InspectionNotFoundError("This source has no video stream.")

    exports = store.list_exports(clip_id)
    freshness = vi.assess_export_freshness(clip_id, revision, exports)
    if payload.framing is not None:
        requested = payload.framing
        framing: models.LeafFraming | None
        if isinstance(requested, AutoFitFramingIn):
            framing = None if requested.mode == "auto" else {"mode": "fit"}
        elif isinstance(requested, ManualFramingIn):
            framing = {
                "mode": "manual",
                "center_x": requested.center_x,
                "center_y": requested.center_y,
                "zoom": requested.zoom,
            }
        else:
            panels = requested.panels
            framing = {
                "mode": "split",
                "panels": [
                    {"x": p.x, "y": p.y, "width": p.width, "height": p.height} for p in panels
                ]
                if panels is not None
                else None,
            }
        preview_edit = replace(edit, ratio=ratio, framing=framing)
        cached = vi.JobWorkspace(job.id).crop_path(clip.id)
        auto_path = vi.CropPath.load(cached) if cached.exists() else None
        crop_path = vi.resolve_crop_path(source, clip, job, preview_edit, auto_path)
        edit = preview_edit
    windows = vi.retained_windows_for_clip(clip, edit)
    summary = vi.timeline_summary(clip, edit)
    retained_duration_s = float(summary["retained_duration_s"])

    source_path = (
        vi.resolve_source_media_path(source)
        if payload.target in ("source", "composed", "both")
        else None
    )
    export_path = None
    if payload.target == "export":
        assert payload.export_id is not None
        _, export_path = _resolve_export_for_clip(clip_id, payload.export_id)

    allow_outside_clip = payload.timeline == "source" and payload.target == "source"
    total_bytes = 0
    frames_out: list[dict[str, Any]] = []

    for t in payload.timestamps:
        mapped = vi.map_timestamp(
            t,
            timeline=payload.timeline,
            clip_start_s=clip.start_s,
            clip_end_s=clip.end_s,
            windows=windows,
            source_duration_s=source.duration_s,
            allow_outside_clip=allow_outside_clip,
        )
        if payload.target == "export" and mapped.retained_t is None:
            raise vi.InspectionValidationError(
                "Requested timestamp is outside retained footage and not in export."
            )

        in_clip = clip.start_s - 1e-6 <= mapped.source_t <= clip.end_s + 1e-6
        if in_clip:
            clamped_clip_t = max(0.0, min(clip.duration_s, mapped.clip_t))
            seg, _ = vi.segment_at_clip_time(crop_path, clamped_clip_t)
            framing_mode = vi.active_framing_label(edit, seg, clamped_clip_t, clip.start_s)
            segment_strategy = seg.strategy.value
        else:
            framing_mode = None
            segment_strategy = None

        src_payload = None
        comp_payload = None
        exp_payload = None

        if payload.target in ("source", "both"):
            assert source_path is not None
            img = vi.extract_source_frame(
                source_path,
                mapped.source_t,
                duration_s=source.duration_s,
                max_width=payload.max_width,
                fmt=payload.format,
                jpeg_quality=payload.jpeg_quality,
            )
            total_bytes += img.size_bytes
            src_payload = img.to_payload()

        if payload.target in ("composed", "both"):
            assert source_path is not None
            img = vi.extract_composed_frame(
                media_path=source_path,
                clip=clip,
                source_duration_s=source.duration_s,
                crop_path=crop_path,
                clip_t=mapped.clip_t,
                words=words,
                style=style,
                ratio=ratio,
                burn_captions=edit.burn_captions,
                max_width=payload.max_width,
                fmt=payload.format,
                jpeg_quality=payload.jpeg_quality,
            )
            total_bytes += img.size_bytes
            comp_payload = img.to_payload()

        if payload.target == "export":
            assert export_path is not None and mapped.retained_t is not None
            img = vi.extract_source_frame(
                export_path,
                mapped.retained_t,
                duration_s=retained_duration_s,
                max_width=payload.max_width,
                fmt=payload.format,
                jpeg_quality=payload.jpeg_quality,
            )
            total_bytes += img.size_bytes
            exp_payload = img.to_payload()

        if total_bytes > vi.MAX_RESPONSE_BYTES:
            raise vi.InspectionValidationError(
                f"Response image payload exceeds {vi.MAX_RESPONSE_BYTES} bytes limit; "
                "use format='jpeg' or fewer/smaller frames."
            )

        frames_out.append(
            {
                "requested_t": mapped.requested_t,
                "timeline": mapped.timeline,
                "source_t": round(mapped.source_t, 6),
                "clip_t": round(mapped.clip_t, 6),
                "retained_t": round(mapped.retained_t, 6)
                if mapped.retained_t is not None
                else None,
                "in_removed_range": mapped.in_removed_range,
                "window_index": mapped.window_index,
                "framing_mode": framing_mode,
                "segment_strategy": segment_strategy,
                "source_region": vi.source_region_diagnostics(crop_path, clamped_clip_t)
                if in_clip
                and payload.target in ("composed", "both")
                and source.width is not None
                and source.height is not None
                and source.width > 0
                and source.height > 0
                else {
                    "status": "unknown",
                    "reason": "Source dimensions or composed frame geometry unavailable.",
                },
                "face_caption_collision": {
                    "status": "unknown",
                    "reason": "Face and caption geometry evidence is unavailable.",
                },
                "source_image": src_payload,
                "composed_image": comp_payload,
                "export_image": exp_payload,
            }
        )

    mime_type = "image/jpeg" if payload.format == "jpeg" else "image/png"
    return {
        "clip_id": clip.id,
        "current_revision": revision,
        "export_freshness": freshness,
        "timeline_summary": summary,
        "format": payload.format,
        "mime_type": mime_type,
        "frames": frames_out,
    }


@router.post("/clips/{clip_id}/inspection/frames")
async def inspect_frames(clip_id: str, payload: FramesRequestIn) -> dict[str, Any]:
    """Extract bounded source, composed, or exported frames with time/revision metadata."""
    try:
        return await asyncio.to_thread(_extract_frames_sync, clip_id, payload)
    except Exception as exc:
        raise _translate_error(exc) from exc


def _default_timestamps(
    *,
    timeline: vi.TimelineKind,
    clip_start_s: float,
    clip_duration_s: float,
    retained_duration_s: float,
    count: int,
) -> list[float]:
    if timeline == "retained":
        span = retained_duration_s
        origin = 0.0
    elif timeline == "clip":
        span = clip_duration_s
        origin = 0.0
    else:
        span = clip_duration_s
        origin = clip_start_s
    return [round(origin + (i + 0.5) / count * span, 4) for i in range(count)]


def _build_contact_sheet_sync(clip_id: str, payload: ContactSheetRequestIn) -> dict[str, Any]:
    clip, _, source, edit, crop_path, style, ratio, words, revision = vi.load_clip_render_context(
        clip_id, ratio_override=payload.ratio, style_override=payload.style
    )
    if not source.has_video:
        raise vi.InspectionNotFoundError("This source has no video stream.")

    exports = store.list_exports(clip_id)
    freshness = vi.assess_export_freshness(clip_id, revision, exports)
    windows = vi.retained_windows_for_clip(clip, edit)
    summary = vi.timeline_summary(clip, edit)
    retained_duration_s = float(summary["retained_duration_s"])

    timestamps = payload.timestamps or _default_timestamps(
        timeline=payload.timeline,
        clip_start_s=clip.start_s,
        clip_duration_s=clip.duration_s,
        retained_duration_s=retained_duration_s,
        count=payload.tile_count,
    )

    source_path = vi.resolve_source_media_path(source) if payload.target != "export" else None
    export_path = None
    if payload.target == "export":
        assert payload.export_id is not None
        _, export_path = _resolve_export_for_clip(clip_id, payload.export_id)

    cols = payload.columns or min(len(timestamps), 4 if len(timestamps) >= 4 else len(timestamps))
    tile_max_w = max(160, min(vi.MAX_FRAME_WIDTH, (payload.max_width // max(1, cols)) * 2))

    tiles_input: list[tuple[vi.EncodedImage, dict[str, Any]]] = []
    allow_outside_clip = payload.timeline == "source" and payload.target == "source"

    for t in timestamps:
        mapped = vi.map_timestamp(
            t,
            timeline=payload.timeline,
            clip_start_s=clip.start_s,
            clip_end_s=clip.end_s,
            windows=windows,
            source_duration_s=source.duration_s,
            allow_outside_clip=allow_outside_clip,
        )
        if payload.target == "export" and mapped.retained_t is None:
            raise vi.InspectionValidationError(
                "Requested timestamp is outside retained footage and not in export."
            )

        in_clip = clip.start_s - 1e-6 <= mapped.source_t <= clip.end_s + 1e-6
        if in_clip:
            clamped_clip_t = max(0.0, min(clip.duration_s, mapped.clip_t))
            seg, _ = vi.segment_at_clip_time(crop_path, clamped_clip_t)
            framing_mode = vi.active_framing_label(edit, seg, clamped_clip_t, clip.start_s)
            segment_strategy = seg.strategy.value
        else:
            framing_mode = None
            segment_strategy = None

        if payload.target == "source":
            assert source_path is not None
            img = vi.extract_source_frame(
                source_path,
                mapped.source_t,
                duration_s=source.duration_s,
                max_width=tile_max_w,
                fmt="png",
            )
        elif payload.target == "export":
            assert export_path is not None and mapped.retained_t is not None
            img = vi.extract_source_frame(
                export_path,
                mapped.retained_t,
                duration_s=retained_duration_s,
                max_width=tile_max_w,
                fmt="png",
            )
        else:
            assert source_path is not None
            img = vi.extract_composed_frame(
                media_path=source_path,
                clip=clip,
                source_duration_s=source.duration_s,
                crop_path=crop_path,
                clip_t=mapped.clip_t,
                words=words,
                style=style,
                ratio=ratio,
                burn_captions=edit.burn_captions,
                max_width=tile_max_w,
                fmt="png",
            )

        tiles_input.append(
            (
                img,
                {
                    "requested_t": mapped.requested_t,
                    "timeline": mapped.timeline,
                    "source_t": round(mapped.source_t, 6),
                    "clip_t": round(mapped.clip_t, 6),
                    "retained_t": (
                        round(mapped.retained_t, 6) if mapped.retained_t is not None else None
                    ),
                    "in_removed_range": mapped.in_removed_range,
                    "window_index": mapped.window_index,
                    "framing_mode": framing_mode,
                    "segment_strategy": segment_strategy,
                },
            )
        )

    sheet, tiles_out = vi.build_contact_sheet(
        tiles_input,
        max_width=payload.max_width,
        columns=payload.columns,
        fmt=payload.format,
        jpeg_quality=payload.jpeg_quality,
    )
    if sheet.size_bytes > vi.MAX_RESPONSE_BYTES:
        raise vi.InspectionValidationError(
            f"Contact sheet exceeds {vi.MAX_RESPONSE_BYTES} bytes limit; "
            "use format='jpeg' or a smaller max_width."
        )

    return {
        "clip_id": clip.id,
        "current_revision": revision,
        "export_freshness": freshness,
        "timeline_summary": summary,
        "target": payload.target,
        "image": sheet.to_payload(),
        "tiles": tiles_out,
    }


@router.post("/clips/{clip_id}/inspection/contact-sheet")
async def inspect_contact_sheet(clip_id: str, payload: ContactSheetRequestIn) -> dict[str, Any]:
    """Build a timestamp-labeled contact sheet (up to 8 tiles) for quick MCP scan."""
    try:
        return await asyncio.to_thread(_build_contact_sheet_sync, clip_id, payload)
    except Exception as exc:
        raise _translate_error(exc) from exc


class ScreenAnalysisRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_fps: float = Field(default=3.0, ge=1.0, le=6.0, allow_inf_nan=False)
    analysis_width: int = Field(default=640, ge=320, le=720, strict=True)
    ratio: Literal["9:16", "1:1", "16:9"] | None = None


def _analyze_screen_sync(clip_id: str, payload: ScreenAnalysisRequestIn) -> dict[str, Any]:
    clip, _, source, _, crop_path, _, ratio, _, revision = vi.load_clip_render_context(
        clip_id, ratio_override=payload.ratio
    )
    if not source.has_video:
        raise vi.InspectionNotFoundError("This source has no video stream.")
    source_path = vi.resolve_source_media_path(source)
    result = sa.analyze_clip_screens(
        source_path,
        start_s=clip.start_s,
        end_s=clip.end_s,
        ratio=ratio,
        crop_path=crop_path,
        sample_fps=payload.sample_fps,
        analysis_width=payload.analysis_width,
    )
    return {
        "clip_id": clip.id,
        "current_revision": revision,
        **result.to_dict(),
    }


@router.post("/clips/{clip_id}/inspection/analyze-screen")
async def analyze_screen(
    clip_id: str, payload: ScreenAnalysisRequestIn | None = None
) -> dict[str, Any]:
    """Run advisory CPU scene/ROI/scroll analysis to suggest framing for MCP."""
    req = payload or ScreenAnalysisRequestIn()
    try:
        return await asyncio.to_thread(_analyze_screen_sync, clip_id, req)
    except Exception as exc:
        raise _translate_error(exc) from exc
