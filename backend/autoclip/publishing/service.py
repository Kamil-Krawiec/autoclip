"""Background upload orchestration.

The API marks the row ``uploading`` and spawns :func:`run_upload`; this module
owns the transition to ``published`` or ``failed``. The YouTube video id is
persisted immediately after a successful insert, before the thumbnail call, so
a retry after a late failure does not upload the same file twice.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import config, paths
from ..db import store
from ..db.models import Export, utcnow
from . import auth, tcap
from . import youtube as youtube_module

log = logging.getLogger(__name__)


def resolve_export_path(export: Export) -> Path:
    """Return the export path only if it is a regular file inside ``exports/``."""
    path = Path(export.path).resolve()
    exports_root = paths.exports_dir().resolve()
    if not path.is_relative_to(exports_root):
        raise youtube_module.PublishingError(
            "The export path is outside the AutoClip exports directory.",
            status_code=409,
        )
    if not path.is_file():
        raise youtube_module.PublishingError(
            "The exported file has been moved or deleted.", status_code=410
        )
    return path


async def run_upload(publication_id: str) -> None:
    """Run one upload to completion, recording any failure on the row."""
    publication = await asyncio.to_thread(store.get_publication, publication_id)
    if publication is None:
        return

    try:
        settings = config.load()
        export = await asyncio.to_thread(store.get_export, publication.export_id)
        if export is None:
            raise youtube_module.PublishingError("The export no longer exists.", status_code=410)
        export_path = resolve_export_path(export)

        publish_dir = paths.publish_dir(publication.id)
        thumbnail = (
            Path(publication.thumbnail_path)
            if publication.thumbnail_path
            else publish_dir / "thumbnail.png"
        )
        if not thumbnail.is_file():
            raise youtube_module.PublishingError(
                "Generate a thumbnail before uploading.", status_code=409
            )
        wide = (
            Path(publication.thumbnail_wide_path)
            if publication.thumbnail_wide_path
            else publish_dir / "thumbnail-wide.jpg"
        )

        short = publish_dir / "short.mp4"
        await asyncio.to_thread(
            tcap.append_still,
            export_path,
            thumbnail,
            short,
            duration_s=settings.publish.tcap_duration_s,
        )
        await asyncio.to_thread(store.update_publication, publication.id, final_path=str(short))

        client = await asyncio.to_thread(auth.build_client)

        video_id = publication.youtube_video_id
        if not video_id:
            video_id = await asyncio.to_thread(
                youtube_module.upload_video,
                client,
                short,
                title=publication.title,
                description=publication.description,
                tags=publication.tags,
                privacy=publication.privacy_status,
                category_id=settings.publish.category_id,
            )
            # Persist before the optional thumbnail call: if it fails, a retry
            # must skip videos.insert rather than create a duplicate.
            await asyncio.to_thread(
                store.update_publication,
                publication.id,
                youtube_video_id=video_id,
                youtube_url=f"https://youtu.be/{video_id}",
                final_path=str(short),
            )

        if wide.is_file():
            try:
                await asyncio.to_thread(youtube_module.set_thumbnail, client, video_id, wide)
            except youtube_module.PublishingError as exc:
                # A channel that is not eligible for custom thumbnails must not
                # turn a successful upload into a failure.
                log.warning("Thumbnail was not set for %s: %s", video_id, exc)

        await asyncio.to_thread(
            store.update_publication,
            publication.id,
            status="published",
            uploaded_at=utcnow(),
            error=None,
            final_path=str(short),
        )
        log.info("Published %s as %s", publication.id, video_id)
    except Exception as exc:  # noqa: BLE001 - the row must record any failure
        log.exception("Upload failed for publication %s", publication_id)
        await asyncio.to_thread(
            store.update_publication,
            publication.id,
            status="failed",
            error=str(exc)[:2_000],
        )
