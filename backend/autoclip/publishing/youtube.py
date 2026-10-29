"""YouTube Data API uploads and thumbnails.

Resumable ``videos.insert`` with the body the source pipeline shipped, plus the
weaknesses the old uploader had fixed here: field validation before the call,
transient-error retries, a clear quota message, and a best-effort
``thumbnails.set`` that cannot fail a successful upload.

The Google client is constructed by :mod:`autoclip.publishing.auth`; this module
only takes an already-authenticated client so OAuth handling stays testable in
isolation.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .metadata import MAX_DESCRIPTION_BYTES, MAX_TAGS_CHARS, MAX_TITLE_CHARS, tags_wire_length

log = logging.getLogger(__name__)

MAX_RETRIES = 5
CHUNK_SIZE = 8 * 1024 * 1024

#: Retryable server/transport failures. 400/401/403 are not retried here: they
#: need the user to fix something, and a retry only wastes quota.
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


class PublishingError(RuntimeError):
    """A YouTube upload or thumbnail call failed."""

    def __init__(self, message: str, *, hint: str = "", status_code: int = 502) -> None:
        super().__init__(message)
        self.hint = hint
        self.status_code = status_code

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n\n{self.hint}" if self.hint else base


def validate_upload_fields(title: str, description: str, tags: list[str]) -> None:
    """Enforce YouTube's hard limits before spending a quota unit."""
    clean_title = title.strip()
    if not clean_title:
        raise PublishingError("The YouTube title is empty.", status_code=400)
    if len(clean_title) > MAX_TITLE_CHARS:
        raise PublishingError(
            f"The YouTube title is {len(clean_title)} characters; the limit is {MAX_TITLE_CHARS}.",
            status_code=400,
        )
    if "<" in clean_title or ">" in clean_title:
        raise PublishingError("The YouTube title cannot contain '<' or '>'.", status_code=400)
    description_bytes = len(description.encode("utf-8"))
    if description_bytes > MAX_DESCRIPTION_BYTES:
        raise PublishingError(
            f"The description is {description_bytes} bytes; the limit is {MAX_DESCRIPTION_BYTES}.",
            status_code=400,
        )
    tags_length = tags_wire_length(tags)
    if tags_length > MAX_TAGS_CHARS:
        raise PublishingError(
            f"The tags need {tags_length} characters; the limit is {MAX_TAGS_CHARS}.",
            status_code=400,
        )


def _media_file_upload(path: str, *, mimetype: str, chunksize: int | None = None):
    from googleapiclient.http import MediaFileUpload

    if chunksize is None:
        return MediaFileUpload(path, mimetype=mimetype)
    return MediaFileUpload(path, chunksize=chunksize, resumable=True, mimetype=mimetype)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def upload_video(
    youtube: Any,
    video: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    privacy: str,
    category_id: str,
) -> str:
    """Upload one MP4 and return the YouTube video id.

    The insert is resumable and retried on 5xx/transport failures. For a 400
    that names ``madeForKids``, the field is dropped and the request rebuilt
    once: older API revisions reject it even though current docs list it.
    """
    validate_upload_fields(title, description, tags)
    if not video.is_file():
        raise PublishingError(f"The upload file does not exist: {video}", status_code=400)
    if privacy not in {"private", "unlisted", "public"}:
        raise PublishingError(f"Unknown privacy status {privacy!r}.", status_code=400)

    body: dict[str, Any] = {
        "snippet": {
            "title": title.strip(),
            "description": description,
            "tags": list(tags),
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "madeForKids": False,
            "publicStatsViewable": False,
            "containsSyntheticMedia": False,
        },
    }
    media = _media_file_upload(str(video), chunksize=CHUNK_SIZE, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response: dict[str, Any] | None = None
    retries = 0
    dropped_made_for_kids = False
    while response is None:
        try:
            _, response = request.next_chunk()
        except Exception as exc:  # noqa: BLE001 - the Google client raises many types
            if not dropped_made_for_kids and _is_made_for_kids_rejection(exc):
                log.warning("YouTube rejected madeForKids; retrying without it.")
                dropped_made_for_kids = True
                body["status"].pop("madeForKids", None)
                request = youtube.videos().insert(
                    part="snippet,status", body=body, media_body=media
                )
                continue
            if _is_retryable(exc) and retries < MAX_RETRIES:
                retries += 1
                delay = min(8.0, 0.5 * (2**retries))
                log.warning(
                    "Transient YouTube error (attempt %d/%d); retrying in %.1fs.",
                    retries,
                    MAX_RETRIES,
                    delay,
                )
                _sleep(delay)
                continue
            raise _translate_upload_error(exc) from exc

    video_id = response.get("id")
    if not video_id:
        raise PublishingError("YouTube did not return a video id after the upload.")
    return str(video_id)


def set_thumbnail(youtube: Any, video_id: str, jpg: Path) -> None:
    """Set the custom thumbnail. Callers treat a failure as non-fatal."""
    try:
        media = _media_file_upload(str(jpg), mimetype="image/jpeg")
        youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
    except Exception as exc:  # noqa: BLE001
        status = _status_of(exc)
        raise PublishingError(
            f"YouTube did not accept the custom thumbnail ({_reason(exc) or status}).",
            hint=(
                "The upload itself succeeded. Your channel may not be eligible for "
                "custom thumbnails, or the image was rejected; the tcap still is "
                "still at the end of the MP4."
            ),
            status_code=502,
        ) from exc


def _status_of(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None) if resp is not None else None
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _reason(exc: Exception) -> str:
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", "ignore")[:500]
    if isinstance(content, str):
        return content[:500]
    return str(exc)[:500]


def _is_retryable(exc: Exception) -> bool:
    status = _status_of(exc)
    if status in RETRYABLE_STATUS:
        return True
    name = type(exc).__name__.lower()
    lowered = str(exc).lower()
    return (
        "connection" in name
        or "transport" in name
        or "timeout" in name
        or "connection" in lowered
        or "timed out" in lowered
        or "broken pipe" in lowered
    )


def _is_made_for_kids_rejection(exc: Exception) -> bool:
    return _status_of(exc) == 400 and "madeforkids" in _reason(exc).lower().replace(" ", "")


def _is_quota_error(exc: Exception) -> bool:
    if _status_of(exc) != 403:
        return False
    lowered = _reason(exc).lower()
    return "quota" in lowered or "quotaexceeded" in lowered


def _translate_upload_error(exc: Exception) -> PublishingError:
    status = _status_of(exc)
    reason = _reason(exc)

    if _is_quota_error(exc):
        return PublishingError(
            "The YouTube API upload quota is exhausted.",
            hint=(
                "The default quota allows 100 videos.insert calls per day and resets "
                "at midnight US Pacific. Try again after the reset."
            ),
            status_code=403,
        )
    if status == 401:
        return PublishingError(
            "YouTube rejected the OAuth token.",
            hint="Run `autoclip publish auth` to authorize again.",
            status_code=401,
        )
    if status == 403:
        return PublishingError(
            "YouTube refused the upload (403).",
            hint=(
                "Check that the channel is allowed to upload and that the OAuth "
                "client still has the youtube.upload scope.\n"
                f"{reason}"
            ),
            status_code=403,
        )
    if status == 400:
        return PublishingError(
            "YouTube rejected the upload metadata (400).",
            hint=reason,
            status_code=400,
        )
    return PublishingError(
        f"YouTube upload failed: {reason or type(exc).__name__}",
        status_code=502,
    )
