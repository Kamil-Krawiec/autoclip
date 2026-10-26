"""Shallow browsing and reference registration for /media."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import library as media_library
from ..db import store
from ..db.models import Source, new_id
from ..pipeline import ingest
from .schemas import LibraryEntryOut, LibraryImportIn, LibraryOut, SourceOut

router = APIRouter(prefix="/api/library", tags=["library"])
MAX_ENTRIES = 1000


def _directory(directory: str) -> Path:
    if not directory:
        return media_library.LIBRARY_ROOT.resolve(strict=True)
    target = media_library.resolve_library_path(directory)
    if not target.is_dir():
        raise FileNotFoundError
    return target


def _scan(directory: str) -> LibraryOut:
    try:
        target = _directory(directory)
    except FileNotFoundError:
        if directory:
            raise
        return LibraryOut(available=False, directory="", entries=[], truncated=False)
    entries = []
    truncated = False
    with os.scandir(target) as items:
        for item in items:
            try:
                if item.is_dir(follow_symlinks=False):
                    entries.append((True, item.name, None))
                elif (
                    item.is_file(follow_symlinks=False)
                    and Path(item.name).suffix.lower() in ingest.ACCEPTED_SUFFIXES
                ):
                    entries.append((False, item.name, item.stat(follow_symlinks=False).st_size))
            except OSError:
                continue
            if len(entries) > MAX_ENTRIES:
                truncated = True
                break
    entries.sort(key=lambda row: (not row[0], row[1].casefold()))
    prefix = directory.strip("/")
    return LibraryOut(
        available=True,
        directory=prefix,
        truncated=truncated,
        entries=[
            LibraryEntryOut(
                path=f"{prefix}/{name}".strip("/"),
                name=name,
                is_dir=is_dir,
                size_bytes=size,
            )
            for is_dir, name, size in entries[:MAX_ENTRIES]
        ],
    )


@router.get("", response_model=LibraryOut)
async def browse_library(directory: str = "") -> LibraryOut:
    try:
        return await asyncio.to_thread(_scan, directory)
    except FileNotFoundError as exc:
        if not directory:
            return LibraryOut(available=False, directory="", entries=[], truncated=False)
        raise HTTPException(404, "Directory not found.") from exc
    except PermissionError as exc:
        raise HTTPException(403, "Media library could not be read.") from exc
    except OSError as exc:
        raise HTTPException(403, "Media library could not be read.") from exc
    except media_library.LibraryError as exc:
        if isinstance(exc.__cause__, PermissionError):
            raise HTTPException(403, "Media library could not be read.") from exc
        raise HTTPException(404, "Directory not found.") from exc


def _inspect_media(relative: str):
    path = media_library.resolve_library_path(relative)
    if not path.is_file():
        raise media_library.LibraryError("Choose a regular media file.")
    with path.open("rb"):
        pass
    return path, ingest._probe_and_validate(path)


@router.post("/import", response_model=SourceOut, status_code=201)
async def import_library_media(payload: LibraryImportIn) -> SourceOut:
    try:
        if Path(payload.path).suffix.lower() not in ingest.ACCEPTED_SUFFIXES:
            raise HTTPException(415, "Unsupported media file type.")
        path, info = await asyncio.to_thread(_inspect_media, payload.path)
    except ingest.IngestError as exc:
        raise HTTPException(
            422,
            detail={
                "message": "The selected library file could not be read as media.",
                "hint": "Check that the file is readable and contains audio.",
            },
        ) from exc
    except media_library.LibraryError as exc:
        if isinstance(exc.__cause__, PermissionError):
            raise HTTPException(403, "Media file is not readable.") from exc
        raise HTTPException(404, "Media file is unavailable.") from exc
    except PermissionError as exc:
        raise HTTPException(403, "Media file is not readable.") from exc
    except OSError as exc:
        raise HTTPException(404, "Media file is unavailable.") from exc
    source = Source(
        id=new_id(),
        type="upload",
        path=str(path),
        title=path.stem,
        filename=path.name,
        duration_s=info.duration_s,
        width=info.width,
        height=info.height,
        fps=info.fps,
        has_audio=info.has_audio,
        has_video=info.has_video,
        library_path=payload.path,
    )
    await asyncio.to_thread(store.create_source, source)
    return SourceOut.of(source)
