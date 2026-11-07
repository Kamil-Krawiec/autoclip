"""Clip review, editing, preview media, and export."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import library as media_library
from .. import paths
from ..config import load as load_settings
from ..db import store
from ..db.models import ClipEdit, Export, new_id
from ..pipeline import captions as captions_module
from ..pipeline import export as export_module
from ..pipeline.framing import resolve_crop_path, validate_framing_bounds
from ..pipeline.reframe.croppath import CropPath
from ..pipeline.runner import JobWorkspace
from ..pipeline.transcript import Transcript, Word
from .schemas import (
    CaptionPatchIn,
    CaptionStyleOut,
    ClipOut,
    ClipPatchIn,
    EditPatchIn,
    ExportOut,
    ExportRequestIn,
    PreviewRequestIn,
    WordOut,
)
from .thumbnails import router as thumbnail_router

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["clips"])
router.include_router(thumbnail_router)
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_ROUND_EPSILON = 1e-6
_PREVIEW_TIMEOUT_S = 120.0


class _PreviewFileResponse(FileResponse):
    async def __call__(self, scope, receive, send) -> None:
        background = self.background
        self.background = None
        try:
            await super().__call__(scope, receive, send)
        finally:
            if background is not None:
                await background()


def _clip_out(clip) -> ClipOut:
    edit = store.get_clip_edit(clip.id)
    exports = store.list_exports(clip.id)
    return ClipOut.of(clip, edit=edit, exports=exports)


async def list_clips_for_job(job_id: str) -> list[ClipOut]:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    clips = await asyncio.to_thread(store.list_clips, job_id)
    return [await asyncio.to_thread(_clip_out, clip) for clip in clips]


@router.get("/clips/{clip_id}", response_model=ClipOut)
async def get_clip(clip_id: str) -> ClipOut:
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")
    return await asyncio.to_thread(_clip_out, clip)


@router.get("/clips/{clip_id}/words", response_model=list[WordOut])
async def clip_words(clip_id: str) -> list[WordOut]:
    """Words for a clip — the caption editor's source of truth.

    Returns the user's edited words when they exist, falling back to the
    transcript's originals.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    edit = await asyncio.to_thread(store.get_clip_edit, clip_id)
    if edit is not None and edit.edited_words is not None:
        return [
            WordOut(**word)
            for word in edit.edited_words
            if word["end"] > clip.start_s and word["start"] < clip.end_s
        ]

    transcript = await asyncio.to_thread(_load_transcript, clip.job_id)
    words = transcript.slice(clip.start_word, clip.end_word)
    return [WordOut(text=w.text, start=w.start, end=w.end, speaker=w.speaker) for w in words]


@router.get("/clips/{clip_id}/crop-path")
async def clip_crop_path(clip_id: str) -> dict:
    """The computed crop path for a clip, so the preview can match the export.

    Without this the review player can only centre-crop, which on a tracked shot
    shows different framing from the file that gets rendered — sometimes with
    the speaker half out of frame. Reviewing framing against the wrong framing
    is worse than not previewing it at all.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    job = await asyncio.to_thread(store.get_job, clip.job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
    if job is None or source is None:
        raise HTTPException(status_code=404, detail="The clip's source is missing.")
    edit = await asyncio.to_thread(store.get_clip_edit, clip_id) or ClipEdit(clip_id=clip_id)
    return (
        await asyncio.to_thread(_crop_path_for, JobWorkspace(clip.job_id), clip, source, job, edit)
    ).to_dict()


@router.patch("/clips/{clip_id}", response_model=ClipOut)
async def patch_clip(clip_id: str, payload: ClipPatchIn) -> ClipOut:
    """Trim, retitle, or keep/discard a clip.

    A trim re-derives the word range from the new times, so captions stay in
    step with the boundaries without the client having to compute indices.
    """
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    start_s = payload.start_s if payload.start_s is not None else clip.start_s
    end_s = payload.end_s if payload.end_s is not None else clip.end_s
    if end_s <= start_s:
        raise HTTPException(status_code=400, detail="End must come after start.")

    trimmed = payload.start_s is not None or payload.end_s is not None
    start_word = end_word = None

    if trimmed:
        transcript = await asyncio.to_thread(_load_transcript, clip.job_id)
        first, last = transcript.indices_in_range(start_s, end_s)
        start_word, end_word = first, max(first, last - 1)

    await asyncio.to_thread(
        store.update_clip,
        clip_id,
        start_s=start_s if trimmed else None,
        end_s=end_s if trimmed else None,
        start_word=start_word,
        end_word=end_word,
        title=payload.title,
        status=payload.status,
        user_trimmed=True if trimmed else None,
    )

    updated = await asyncio.to_thread(store.get_clip, clip_id)
    return await asyncio.to_thread(_clip_out, updated)


@router.patch("/clips/{clip_id}/captions", response_model=ClipOut)
async def patch_captions(clip_id: str, payload: CaptionPatchIn) -> ClipOut:
    """Store word-level caption edits and style choices for a clip."""
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    return await _save_edit(clip, payload)


@router.patch("/clips/{clip_id}/edit", response_model=ClipOut)
async def patch_edit(clip_id: str, payload: EditPatchIn) -> ClipOut:
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")
    return await _save_edit(clip, payload)


async def _save_edit(clip, payload: CaptionPatchIn) -> ClipOut:
    if payload.caption_style is not None:
        try:
            captions_module.get_style(payload.caption_style)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    changes = payload.model_dump(exclude_unset=True)
    if "framing" in changes and changes["framing"] is not None:
        job = await asyncio.to_thread(store.get_job, clip.job_id)
        source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
        if job is None or source is None:
            raise HTTPException(status_code=404, detail="The clip's source is missing.")
        try:
            validate_framing_bounds(changes["framing"], source.duration_s)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if "caption_overrides" in changes and changes["caption_overrides"] is not None:
        raw_overrides = changes["caption_overrides"]
        normalized = {key: value for key, value in raw_overrides.items() if value is not None}
        changes["caption_overrides"] = normalized
    changes = {
        key: value
        for key, value in changes.items()
        if value is not None or key in {"framing", "caption_overrides"}
    }
    if "words" in changes:
        changes["edited_words"] = changes.pop("words")
    if (changes.get("framing") or {}).get("mode") == "auto":
        changes["framing"] = None
    await asyncio.to_thread(store.patch_clip_edit, clip.id, changes)
    return await asyncio.to_thread(_clip_out, clip)


def _resolve_existing_source_path(source) -> Path:
    source_path = media_library.media_path(source)
    if not source_path.is_file():
        raise media_library.LibraryError("Source media file is missing.")
    return source_path


async def _prepare_saved_render(
    clip_id: str,
    *,
    ratio_override: str | None = None,
    style_override: str | None = None,
    destination: Path,
    output_start_s: float | None = None,
    output_duration_s: float | None = None,
):
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")

    job = await asyncio.to_thread(store.get_job, clip.job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id) if job else None
    if job is None or source is None:
        raise HTTPException(status_code=404, detail="The clip's source is missing.")

    edit = await asyncio.to_thread(store.get_clip_edit, clip_id) or ClipEdit(clip_id=clip_id)
    ratio = ratio_override or edit.ratio
    try:
        style = captions_module.resolve_style(
            style_override if style_override is not None else edit.caption_style,
            edit.caption_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    words = [
        Word(text=w.text, start=w.start, end=w.end, speaker=w.speaker)
        for w in await clip_words(clip_id)
    ]

    workspace = JobWorkspace(clip.job_id)
    crop_path = await asyncio.to_thread(
        _crop_path_for, workspace, clip, source, job, replace(edit, ratio=ratio)
    )
    try:
        source_path = await asyncio.to_thread(_resolve_existing_source_path, source)
    except media_library.LibraryError as exc:
        raise HTTPException(status_code=410, detail="Source media is unavailable.") from exc

    request = export_module.ExportRequest(
        source=source_path,
        destination=destination,
        start_s=clip.start_s,
        end_s=clip.end_s,
        crop_path=crop_path,
        words=words,
        style=style,
        ratio=ratio,
        burn_captions=edit.burn_captions,
        output_start_s=output_start_s,
        output_duration_s=output_duration_s,
    )
    return clip, workspace, ratio, style, request


@router.post("/clips/{clip_id}/export", response_model=ExportOut, status_code=201)
async def export_clip(clip_id: str, payload: ExportRequestIn) -> ExportOut:
    """Render one clip and return its download link."""
    export_id = new_id()
    clip, workspace, ratio, style, request = await _prepare_saved_render(
        clip_id,
        ratio_override=payload.ratio,
        style_override=payload.style,
        destination=paths.exports_dir() / export_id / "placeholder.mp4",
    )
    destination = (
        paths.exports_dir()
        / clip.job_id
        / export_id
        / export_module.output_filename(clip.title or f"clip-{clip.rank}", ratio)
    )
    request = replace(request, destination=destination)

    settings = load_settings()
    settings.export.write_srt = payload.write_srt

    try:
        await asyncio.to_thread(
            export_module.export_clip,
            request,
            work_dir=workspace.captions_dir / export_id,
            settings=settings.export,
        )
    except export_module.ExportError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    record = Export(
        id=export_id,
        clip_id=clip_id,
        path=str(destination),
        ratio=ratio,
        style=style.key,
        size_bytes=destination.stat().st_size,
    )
    await asyncio.to_thread(store.create_export, record)
    await asyncio.to_thread(store.update_clip, clip_id, status="exported")

    return ExportOut.of(record)


async def _await_render_task_uninterruptibly(task: asyncio.Task[Path]) -> None:
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                break
            continue
        except BaseException:
            break


@router.post("/clips/{clip_id}/preview")
async def preview_clip(clip_id: str, payload: PreviewRequestIn | None = None) -> FileResponse:
    """Render a short exact-preview MP4 slice from the clip's saved state."""
    params = payload or PreviewRequestIn()
    clip = await asyncio.to_thread(store.get_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")
    if params.start_s >= clip.duration_s - _PREVIEW_ROUND_EPSILON:
        raise HTTPException(status_code=400, detail="Preview start is outside the clip.")

    clamped_duration = min(params.duration_s, max(0.0, clip.duration_s - params.start_s))
    if clamped_duration <= 0:
        raise HTTPException(status_code=400, detail="Preview start is outside the clip.")

    if not _PREVIEW_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="Another preview render is already in progress."
        )

    temp_dir: Path | None = None
    cancel_event = threading.Event()
    render_task: asyncio.Task[Path] | None = None
    handed_off = False
    try:
        base_tmp = paths.exports_dir() / "_previews"
        base_tmp.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=f"preview-{clip_id}-", dir=base_tmp))
        destination = temp_dir / "preview.mp4"

        _, _, _, _, request = await _prepare_saved_render(
            clip_id,
            destination=destination,
            output_start_s=params.start_s,
            output_duration_s=clamped_duration,
        )
        settings = load_settings()
        settings.export.write_srt = False

        render_task = asyncio.create_task(
            asyncio.to_thread(
                export_module.export_clip,
                request,
                work_dir=temp_dir / "work",
                settings=settings.export,
                cancelled=cancel_event.is_set,
            )
        )
        await asyncio.wait_for(asyncio.shield(render_task), timeout=_PREVIEW_TIMEOUT_S)
    except TimeoutError as exc:
        cancel_event.set()
        if render_task is not None:
            await _await_render_task_uninterruptibly(render_task)
        raise HTTPException(status_code=504, detail="Preview render timed out.") from exc
    except asyncio.CancelledError:
        cancel_event.set()
        if render_task is not None:
            await _await_render_task_uninterruptibly(render_task)
        raise
    except export_module.ExportError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    else:
        handed_off = True
        cleanup_dir = temp_dir
        return _PreviewFileResponse(
            destination,
            media_type="video/mp4",
            filename="preview.mp4",
            background=BackgroundTask(shutil.rmtree, cleanup_dir, True),
        )
    finally:
        if render_task is not None and not render_task.done():
            cancel_event.set()
            await _await_render_task_uninterruptibly(render_task)
        if not handed_off and temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        _PREVIEW_LOCK.release()


@router.get("/exports/{export_id}/download")
async def download_export(export_id: str) -> FileResponse:
    record = await asyncio.to_thread(store.get_export, export_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Export not found.")

    path = Path(record.path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="The exported file has been moved or deleted.")

    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/jobs/{job_id}/media")
async def job_media(job_id: str) -> FileResponse:
    """Serve the source video, so the review player can scrub the original.

    Range requests are handled by FileResponse, which is what makes seeking in
    the preview player usable on a long source.
    """
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    source = await asyncio.to_thread(store.get_source, job.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source media not found.")
    try:
        source_path = await asyncio.to_thread(media_library.media_path, source)
    except media_library.LibraryError as exc:
        raise HTTPException(status_code=410, detail="Source media is unavailable.") from exc
    if source.library_path is None and not source_path.exists():
        raise HTTPException(status_code=404, detail="Source media not found.")
    return FileResponse(source_path)


@router.get("/caption-styles", response_model=list[CaptionStyleOut])
async def caption_styles() -> list[CaptionStyleOut]:
    """Style presets, with enough detail for the UI to approximate them in CSS."""
    return [
        CaptionStyleOut(
            key=style.key,
            label=style.label,
            description=style.description,
            preview={
                "font": style.font,
                "primary": style.primary,
                "accent": style.accent,
                "outline": style.outline,
                "outlineWidth": style.outline_width,
                "allCaps": style.all_caps,
                "sizeRatio": style.size_ratio,
                "marginRatio": style.margin_v_ratio,
                "boxed": style.boxed,
                "animation": style.animation,
                "maxWords": style.max_words,
                "positionX": 0.5,
                "positionY": 1 - style.margin_v_ratio,
            },
        )
        for style in captions_module.PRESETS.values()
    ]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _load_transcript(job_id: str) -> Transcript:
    workspace = JobWorkspace(job_id)
    if not workspace.transcript.exists():
        raise HTTPException(status_code=404, detail="This job has no transcript yet.")
    return Transcript.load(workspace.transcript)


def _crop_path_for(workspace: JobWorkspace, clip, source, job, edit: ClipEdit) -> CropPath:
    cached = workspace.crop_path(clip.id)
    path = CropPath.load(cached) if cached.exists() else None
    return resolve_crop_path(source, clip, job, edit, path)
