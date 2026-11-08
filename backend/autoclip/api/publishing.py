"""Publish page API: candidates, drafts, metadata, thumbnails, and uploads.

The router is intentionally thin. Validation of YouTube limits lives in
:mod:`autoclip.publishing.metadata`, media work in ``thumbnails``/``tcap``, and
the upload state machine in ``service``. This file owns HTTP semantics, the
409/202 rules, and the one-upload-at-a-time lock.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse

from .. import config, paths
from ..config import OPENAI_IMAGE_KEY
from ..db import store
from ..db.models import Publication, new_id, utcnow
from ..pipeline.runner import JobWorkspace
from ..pipeline.transcript import Transcript
from ..providers.base import ProviderError
from ..publishing import metadata, service, thumbnails
from ..publishing.metadata import (
    MAX_DESCRIPTION_BYTES,
    MAX_PROMPT_CHARS,
    MAX_TAGS_CHARS,
    MAX_TITLE_CHARS,
    tags_wire_length,
)
from ..publishing.youtube import PublishingError
from .schemas import (
    PublicationCreateIn,
    PublicationOut,
    PublicationPatchIn,
    PublishCandidateOut,
    PublishStatusOut,
    ThumbnailRequestIn,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/publish", tags=["publishing"])

#: Strong references keep a spawned upload from being garbage-collected.
_UPLOAD_TASKS: set[asyncio.Task] = set()
#: One upload at a time. AutoClip is a single-user local app; parallel uploads
#: would only compete for the same YouTube quota.
_UPLOAD_LOCK = asyncio.Lock()

FROZEN_STATUSES = frozenset({"uploading", "published"})


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _http_error(exc: Exception, *, default_status: int = 502) -> HTTPException:
    message = exc.args[0] if getattr(exc, "args", None) else str(exc)
    detail = {"message": str(message)}
    hint = getattr(exc, "hint", "")
    if hint:
        detail["hint"] = hint
    return HTTPException(status_code=getattr(exc, "status_code", default_status), detail=detail)


async def _load(publication_id: str) -> Publication:
    publication = await asyncio.to_thread(store.get_publication, publication_id)
    if publication is None:
        raise HTTPException(status_code=404, detail="Publication not found.")
    return publication


def _build_out(publication: Publication) -> PublicationOut:
    export = store.get_export(publication.export_id)
    clip = store.get_clip(publication.clip_id)
    if export is None or clip is None:
        raise HTTPException(status_code=409, detail="The publication's clip or export is missing.")
    job = store.get_job(clip.job_id)
    source = store.get_source(job.source_id) if job is not None else None
    if source is None:
        raise HTTPException(status_code=409, detail="The publication's source is missing.")
    return PublicationOut.of(publication, clip, source, export)


async def _out(publication: Publication) -> PublicationOut:
    return await asyncio.to_thread(_build_out, publication)


def _thumbnail_path(publication: Publication) -> Path:
    if publication.thumbnail_path:
        return Path(publication.thumbnail_path)
    return paths.publish_dir(publication.id) / "thumbnail.png"


def _wide_path(publication: Publication) -> Path:
    if publication.thumbnail_wide_path:
        return Path(publication.thumbnail_wide_path)
    return paths.publish_dir(publication.id) / "thumbnail-wide.jpg"


def _has_thumbnail(publication: Publication) -> bool:
    return _thumbnail_path(publication).is_file()


def _reject_frozen(publication: Publication) -> None:
    if publication.status in FROZEN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                "This publication is locked because it is uploading or already "
                "published. Edit the details in YouTube Studio."
            ),
        )


async def _refresh_status(publication: Publication) -> Publication:
    """Promote draft→ready (or back) after an edit, so the upload gate is honest."""
    if publication.status not in {"draft", "ready"}:
        return publication
    desired = "ready" if publication.title.strip() and _has_thumbnail(publication) else "draft"
    if desired == publication.status:
        return publication
    updated = await asyncio.to_thread(store.update_publication, publication.id, status=desired)
    return updated or publication


def _clean_title(raw: str) -> str:
    text = " ".join(raw.split())
    if not text:
        raise HTTPException(status_code=422, detail="The title cannot be empty.")
    if len(text) > MAX_TITLE_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"The title is {len(text)} characters; the limit is {MAX_TITLE_CHARS}.",
        )
    if "<" in text or ">" in text:
        raise HTTPException(status_code=422, detail="The title cannot contain '<' or '>'.")
    return text


def _clean_description(raw: str) -> str:
    if "<" in raw or ">" in raw:
        raise HTTPException(status_code=422, detail="The description cannot contain '<' or '>'.")
    size = len(raw.encode("utf-8"))
    if size > MAX_DESCRIPTION_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"The description is {size} bytes; the limit is {MAX_DESCRIPTION_BYTES}.",
        )
    return raw.strip()


def _clean_tags(raw: list[str]) -> list[str]:
    tags = metadata.normalize_tags(raw, trim=False)
    length = tags_wire_length(tags)
    if length > MAX_TAGS_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"The tags need {length} characters; the limit is {MAX_TAGS_CHARS}.",
        )
    return tags


# --------------------------------------------------------------------------
# Status and candidates (registered before /{publication_id})
# --------------------------------------------------------------------------


@router.get("/status", response_model=PublishStatusOut)
async def publish_status() -> PublishStatusOut:
    settings = config.load()
    provider, model_override = metadata.resolve_metadata_provider(settings)
    model = model_override or settings.provider(provider).model
    key_present = (
        config.get_secret(provider, settings) is not None
        if provider in config.KEYED_PROVIDERS
        else True
    )
    secrets_present = service.auth.client_secrets_path().is_file()
    token_present = service.auth.token_path().is_file()
    return PublishStatusOut(
        youtube_client_secret_present=secrets_present,
        youtube_token_present=token_present,
        youtube_ready=secrets_present and token_present,
        openai_image_key_present=config.get_secret(OPENAI_IMAGE_KEY, settings) is not None,
        metadata_provider=provider,
        metadata_model=model,
        metadata_key_present=key_present,
        image_model=settings.publish.image_model,
    )


@router.get("/candidates", response_model=list[PublishCandidateOut])
async def publish_candidates(
    limit: int = Query(50, ge=1, le=200),
    ratio: Literal["9:16", "1:1", "16:9"] = "9:16",
) -> list[PublishCandidateOut]:
    rows = await asyncio.to_thread(store.list_publish_candidates, limit, ratio)
    result: list[PublishCandidateOut] = []
    for row in rows:
        export = store.get_export(row.export_id)
        available = bool(export is not None and Path(export.path).is_file())
        result.append(
            PublishCandidateOut(
                export_id=row.export_id,
                clip_id=row.clip_id,
                job_id=row.job_id,
                source_title=row.source_title,
                clip_title=row.clip_title,
                duration_s=row.duration_s,
                ratio=row.ratio,
                size_bytes=row.size_bytes,
                export_created_at=row.export_created_at,
                download_url=f"/api/exports/{row.export_id}/download",
                available=available,
                publication_id=row.publication_id,
                publication_status=row.publication_status,
            )
        )
    return result


# --------------------------------------------------------------------------
# Publications
# --------------------------------------------------------------------------


@router.post("", response_model=PublicationOut, status_code=201)
async def create_publication(payload: PublicationCreateIn, response: Response) -> PublicationOut:
    existing = await asyncio.to_thread(store.get_publication_by_export, payload.export_id)
    if existing is not None:
        response.status_code = 200
        return await _out(existing)

    export = await asyncio.to_thread(store.get_export, payload.export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="Export not found.")
    try:
        await asyncio.to_thread(service.resolve_export_path, export)
    except PublishingError as exc:
        raise _http_error(exc) from exc

    clip = await asyncio.to_thread(store.get_clip, export.clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="The export's clip no longer exists.")
    settings = config.load()
    publication = Publication(
        id=new_id(),
        export_id=export.id,
        clip_id=clip.id,
        title=clip.title.strip() or f"Clip {clip.rank}",
        privacy_status=settings.publish.default_privacy,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    publication, inserted = await asyncio.to_thread(
        store.create_publication_idempotent, publication
    )
    response.status_code = 201 if inserted else 200
    return await _out(publication)


@router.get("/{publication_id}", response_model=PublicationOut)
async def get_publication(publication_id: str) -> PublicationOut:
    return await _out(await _load(publication_id))


@router.patch("/{publication_id}", response_model=PublicationOut)
async def patch_publication(publication_id: str, payload: PublicationPatchIn) -> PublicationOut:
    publication = await _load(publication_id)
    _reject_frozen(publication)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)

    title = _clean_title(changes["title"]) if "title" in changes else publication.title
    description = (
        _clean_description(changes["description"])
        if "description" in changes
        else publication.description
    )
    tags = _clean_tags(changes["tags"]) if "tags" in changes else publication.tags
    prompt = (
        changes["thumbnail_prompt"].strip()
        if "thumbnail_prompt" in changes
        else publication.thumbnail_prompt
    )
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"The prompt is longer than {MAX_PROMPT_CHARS} characters.",
        )
    privacy = changes.get("privacy_status", publication.privacy_status)

    updated = await asyncio.to_thread(
        store.update_publication,
        publication.id,
        title=title,
        description=description,
        tags=tags,
        thumbnail_prompt=prompt,
        privacy_status=privacy,
    )
    assert updated is not None
    updated = await _refresh_status(updated)
    return await _out(updated)


@router.post("/{publication_id}/metadata", response_model=PublicationOut)
async def generate_publication_metadata(publication_id: str) -> PublicationOut:
    publication = await _load(publication_id)
    _reject_frozen(publication)

    clip = await asyncio.to_thread(store.get_clip, publication.clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found.")
    job = await asyncio.to_thread(store.get_job, clip.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    transcript_record = await asyncio.to_thread(store.get_transcript, clip.job_id)
    transcript_path = JobWorkspace(clip.job_id).transcript
    if transcript_record is None or not transcript_path.is_file():
        raise HTTPException(
            status_code=409,
            detail={
                "message": "No transcript is available for this clip.",
                "hint": (
                    "Export or re-run the analysis so a transcript exists; you can "
                    "still type the metadata manually."
                ),
            },
        )
    transcript = await asyncio.to_thread(Transcript.load, transcript_path)
    edit = await asyncio.to_thread(store.get_clip_edit, clip.id)
    transcript_text = metadata.clip_transcript_text(edit, transcript, clip)
    if not transcript_text.strip():
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The clip's transcript is empty.",
                "hint": "Type the metadata manually or regenerate the captions first.",
            },
        )
    source = await asyncio.to_thread(store.get_source, job.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")

    settings = config.load()
    try:
        result = await metadata.generate_metadata(
            source_title=source.title,
            clip_title=clip.title,
            hook=clip.hook,
            duration_s=clip.duration_s,
            transcript_text=transcript_text,
            settings=settings,
        )
    except ProviderError as exc:
        raise _http_error(exc) from exc

    updated = await asyncio.to_thread(
        store.update_publication,
        publication.id,
        title=result.title,
        description=result.description,
        tags=result.tags,
        thumbnail_prompt=result.thumbnail_prompt,
        best_day=result.best_day,
    )
    assert updated is not None
    updated = await _refresh_status(updated)
    return await _out(updated)


@router.post("/{publication_id}/thumbnail", response_model=PublicationOut)
async def generate_publication_thumbnail(
    publication_id: str, payload: ThumbnailRequestIn | None = None
) -> PublicationOut:
    publication = await _load(publication_id)
    _reject_frozen(publication)

    prompt = (
        payload.prompt.strip()
        if payload is not None and payload.prompt is not None
        else publication.thumbnail_prompt.strip()
    )
    if not prompt:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "The thumbnail prompt is empty.",
                "hint": "Generate the metadata first, or write a prompt describing the image.",
            },
        )
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"The prompt is longer than {MAX_PROMPT_CHARS} characters.",
        )

    settings = config.load()
    api_key = config.get_secret(OPENAI_IMAGE_KEY, settings)
    if not api_key:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "No OpenAI Images API key is configured.",
                "hint": "Add it in Settings → Keys → OpenAI Images.",
            },
        )

    directory = paths.publish_dir(publication.id)
    directory.mkdir(parents=True, exist_ok=True)
    png_path = directory / "thumbnail.png"
    wide_path = directory / "thumbnail-wide.jpg"
    try:
        await asyncio.to_thread(
            thumbnails.generate_thumbnail,
            prompt,
            png_path,
            model=settings.publish.image_model,
            size=settings.publish.image_size,
            quality=settings.publish.image_quality,
            api_key=api_key,
        )
        await asyncio.to_thread(thumbnails.derive_wide, png_path, wide_path)
    except thumbnails.ThumbnailError as exc:
        raise _http_error(exc) from exc

    updated = await asyncio.to_thread(
        store.update_publication,
        publication.id,
        thumbnail_path=str(png_path),
        thumbnail_wide_path=str(wide_path),
        thumbnail_prompt=prompt,
    )
    assert updated is not None
    updated = await _refresh_status(updated)
    return await _out(updated)


@router.get("/{publication_id}/thumbnail")
async def get_publication_thumbnail(publication_id: str) -> FileResponse:
    publication = await _load(publication_id)
    path = _thumbnail_path(publication)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not generated yet.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/{publication_id}/thumbnail-wide")
async def get_publication_thumbnail_wide(publication_id: str) -> FileResponse:
    publication = await _load(publication_id)
    path = _wide_path(publication)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Wide thumbnail not generated yet.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/{publication_id}/upload", response_model=PublicationOut, status_code=202)
async def upload_publication(publication_id: str) -> PublicationOut:
    publication = await _load(publication_id)
    if publication.status in FROZEN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="This publication is already uploading or published.",
        )
    if not (
        publication.status in {"ready", "failed"}
        and publication.title.strip()
        and _has_thumbnail(publication)
    ):
        missing = "a title" if not publication.title.strip() else "a thumbnail"
        raise HTTPException(
            status_code=409,
            detail=f"Add {missing} and generate the metadata before uploading.",
        )
    if _UPLOAD_LOCK.locked():
        raise HTTPException(status_code=409, detail="Another upload is already running.")

    await _UPLOAD_LOCK.acquire()
    try:
        updated = await asyncio.to_thread(
            store.update_publication, publication.id, status="uploading", error=None
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Publication not found.")
        task = asyncio.create_task(_run_upload(publication.id))
    except BaseException:
        _UPLOAD_LOCK.release()
        raise
    _UPLOAD_TASKS.add(task)
    task.add_done_callback(_UPLOAD_TASKS.discard)
    return await _out(updated)


async def _run_upload(publication_id: str) -> None:
    try:
        await service.run_upload(publication_id)
    finally:
        _UPLOAD_LOCK.release()
