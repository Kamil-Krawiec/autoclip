"""Source ingestion endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.routing import APIRoute
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from ..config import load as load_settings
from ..db import store
from ..pipeline import ingest
from .schemas import SourceOut, YouTubeIngestIn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])

#: Uploads stream to disk in chunks rather than being buffered whole — source
#: videos routinely exceed available RAM.
UPLOAD_CHUNK = 4 * 1024 * 1024
MAX_UPLOAD_BYTES = 8 * 1024**3  # Entire multipart request, including framing.


def _disk_full(exc: BaseException) -> bool:
    while exc is not None:
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
            return True
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return True
        exc = exc.__cause__
    return False


class UploadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def limited(request: Request):
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "multipart/form-data":
                raise HTTPException(415, "Upload requires multipart/form-data.")
            length = request.headers.get("content-length")
            if length is not None and length.isdecimal() and int(length) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Upload exceeds the 8 GiB request limit.")

            async def capped_stream():
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Upload exceeds the 8 GiB request limit.")
                    yield chunk

            parser = MultiPartParser(request.headers, capped_stream(), max_files=1)
            try:
                # FastAPI reuses this parsed form; its File/OpenAPI contract is unchanged.
                request._form = await parser.parse()
                return await handler(request)
            except ClientDisconnect as exc:
                raise HTTPException(400, "Upload interrupted.") from exc
            except MultiPartException as exc:
                raise HTTPException(400, exc.message) from exc
            except OSError as exc:
                raise HTTPException(
                    507 if _disk_full(exc) else 500, "Upload storage failed."
                ) from exc
            finally:
                # Starlette owns these spools; also cover older parsers that only close
                # on MultiPartException, leaving disconnect/OSError/cancellation uncovered.
                for spool in parser._files_to_close_on_error:
                    spool.close()

        return limited


@router.get("", response_model=list[SourceOut])
async def list_sources(limit: int = 50) -> list[SourceOut]:
    sources = await asyncio.to_thread(store.list_sources, limit)
    return [SourceOut.of(s) for s in sources]


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: str) -> SourceOut:
    source = await asyncio.to_thread(store.get_source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    return SourceOut.of(source)


@router.post("/youtube", response_model=SourceOut, status_code=201)
async def ingest_youtube(payload: YouTubeIngestIn) -> SourceOut:
    """Download a YouTube video and register it as a source."""
    if not ingest.is_youtube_url(payload.url):
        raise HTTPException(status_code=400, detail="That is not a YouTube URL.")

    settings = load_settings().ingest
    if payload.cookies_from_browser is not None:
        settings = settings.model_copy(
            update={"cookies_from_browser": payload.cookies_from_browser}
        )

    try:
        source = await asyncio.to_thread(ingest.ingest_youtube, payload.url, settings)
    except ingest.IngestError as exc:
        # 422 rather than 500: the request was well-formed, but the content
        # can't be fetched, and the hint tells the user what to change.
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "hint": exc.hint}
        ) from exc

    await asyncio.to_thread(store.create_source, source)
    return SourceOut.of(source)


async def upload_source(file: UploadFile = File(...)) -> SourceOut:
    """Accept a media upload and register it as a source."""
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(_store_upload, file, cancelled))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        # The worker owns the spool and staging file until it finishes; do not
        # close/unlink either while a thread is still reading, moving or probing.
        while not task.done():
            with contextlib.suppress(BaseException):
                await asyncio.shield(task)
        with contextlib.suppress(BaseException):
            task.result()
        raise
    except (OSError, sqlite3.Error, ingest.IngestError) as exc:
        if _disk_full(exc):
            raise HTTPException(507, "Not enough disk space for this upload.") from exc
        if isinstance(exc, ingest.IngestError):
            raise HTTPException(422, detail={"message": str(exc), "hint": exc.hint}) from exc
        raise HTTPException(500, "Upload storage failed.") from exc


def _store_upload(file: UploadFile, cancelled: threading.Event) -> SourceOut:
    staging_path = None
    source = None
    registered = False
    try:
        filename = Path(file.filename or "upload")
        suffix = filename.suffix.lower()
        if suffix not in ingest.ACCEPTED_SUFFIXES:
            raise HTTPException(
                415,
                detail={
                    "message": f"{suffix or 'This file'} is not a supported format.",
                    "hint": f"Accepted: {', '.join(sorted(ingest.ACCEPTED_SUFFIXES))}",
                },
            )
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as staging:
            staging_path = Path(staging.name)
            while chunk := file.file.read(UPLOAD_CHUNK):
                if cancelled.is_set():
                    raise asyncio.CancelledError()
                staging.write(chunk)
        source = ingest.ingest_file(staging_path, move=True, title=filename.stem)
        result = SourceOut.of(source)
        if cancelled.is_set():
            raise asyncio.CancelledError()
        store.create_source(source)
        registered = True
        return result
    finally:
        file.file.close()
        if staging_path is not None:
            staging_path.unlink(missing_ok=True)
        if source is not None and not registered:
            shutil.rmtree(Path(source.path).parent, ignore_errors=True)


router.add_api_route(
    "/upload",
    upload_source,
    methods=["POST"],
    response_model=SourceOut,
    status_code=201,
    route_class_override=UploadRoute,
)
