"""CRUD operations over the AutoClip database.

Every function opens its own transaction via :func:`autoclip.db.connection`,
so callers don't manage commits. Functions are synchronous; async callers should
wrap them in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
from typing import Any

from . import connection
from .models import (
    Clip,
    ClipEdit,
    ClipStatus,
    Export,
    Job,
    JobStatus,
    PrivacyStatus,
    Publication,
    PublicationStatus,
    PublishCandidate,
    Source,
    Transcript,
    utcnow,
)

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def create_source(source: Source) -> Source:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO sources (id, type, url, filename, path, title, channel,
                                 duration_s, width, height, fps, has_audio,
                                 has_video, created_at, library_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.id,
                source.type,
                source.url,
                source.filename,
                source.path,
                source.title,
                source.channel,
                source.duration_s,
                source.width,
                source.height,
                source.fps,
                int(source.has_audio),
                int(source.has_video),
                source.created_at,
                source.library_path,
            ),
        )
    return source


def get_source(source_id: str) -> Source | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return Source.from_row(row) if row else None


def list_sources(limit: int = 50) -> list[Source]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM sources ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [Source.from_row(r) for r in rows]


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def create_job(job: Job) -> Job:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, source_id, status, current_stage, progress, error,
                              provider, settings_json, created_at, updated_at,
                              started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.source_id,
                job.status,
                job.current_stage,
                job.progress,
                job.error,
                job.provider,
                json.dumps(job.settings),
                job.created_at,
                job.updated_at,
                job.started_at,
                job.finished_at,
            ),
        )
    return job


def get_job(job_id: str) -> Job | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def list_jobs(limit: int = 50, status: JobStatus | None = None) -> list[Job]:
    query = "SELECT * FROM jobs"
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [Job.from_row(r) for r in rows]


class _Unset:
    """Sentinel distinguishing "leave this alone" from "set this to NULL".

    Without it, nullable columns can only ever be written, never cleared — a
    retried job would keep displaying the error message from its failed run.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _Unset()


def update_job(
    job_id: str,
    *,
    status: JobStatus | None = None,
    current_stage: str | None = None,
    progress: float | None = None,
    error: str | None | _Unset = UNSET,
    started_at: str | None | _Unset = UNSET,
    finished_at: str | None | _Unset = UNSET,
) -> None:
    """Patch the supplied fields. Omitted fields are left untouched.

    Nullable fields take :data:`UNSET` as their default, so passing ``None``
    explicitly clears them.

    ``updated_at`` is always refreshed so the UI can detect staleness.
    """
    fields: dict[str, Any] = {"updated_at": utcnow()}
    for name, value in (
        ("status", status),
        ("current_stage", current_stage),
        ("progress", progress),
    ):
        if value is not None:
            fields[name] = value

    for name, value in (
        ("error", error),
        ("started_at", started_at),
        ("finished_at", finished_at),
    ):
        if not isinstance(value, _Unset):
            fields[name] = value

    assignments = ", ".join(f"{name} = ?" for name in fields)
    with connection() as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?",
            (*fields.values(), job_id),
        )


def next_queued_job() -> Job | None:
    """Return the oldest queued job — the FIFO the worker pulls from."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    return Job.from_row(row) if row else None


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------


def upsert_transcript(transcript: Transcript) -> Transcript:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO transcripts (job_id, json_path, language, model,
                                     has_diarization, word_count, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                json_path = excluded.json_path,
                language = excluded.language,
                model = excluded.model,
                has_diarization = excluded.has_diarization,
                word_count = excluded.word_count,
                source = excluded.source
            """,
            (
                transcript.job_id,
                transcript.json_path,
                transcript.language,
                transcript.model,
                int(transcript.has_diarization),
                transcript.word_count,
                transcript.source,
                transcript.created_at,
            ),
        )
    return transcript


def get_transcript(job_id: str) -> Transcript | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM transcripts WHERE job_id = ?", (job_id,)).fetchone()
    return Transcript.from_row(row) if row else None


# --------------------------------------------------------------------------
# Clips
# --------------------------------------------------------------------------


def replace_clips(job_id: str, clips: list[Clip]) -> list[Clip]:
    """Swap in a fresh set of clips for a job.

    Used when highlight detection reruns — a retry should not leave the previous
    run's candidates behind.
    """
    with connection() as conn:
        conn.execute("DELETE FROM clips WHERE job_id = ?", (job_id,))
        conn.executemany(
            """
            INSERT INTO clips (id, job_id, rank, start_s, end_s, start_word, end_word,
                               title, hook, score, reason, status, user_trimmed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    c.id,
                    c.job_id,
                    c.rank,
                    c.start_s,
                    c.end_s,
                    c.start_word,
                    c.end_word,
                    c.title,
                    c.hook,
                    c.score,
                    c.reason,
                    c.status,
                    int(c.user_trimmed),
                    c.created_at,
                )
                for c in clips
            ],
        )
    return clips


def get_clip(clip_id: str) -> Clip | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return Clip.from_row(row) if row else None


def list_clips(job_id: str) -> list[Clip]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM clips WHERE job_id = ? ORDER BY rank ASC", (job_id,)
        ).fetchall()
    return [Clip.from_row(r) for r in rows]


def update_clip(
    clip_id: str,
    *,
    start_s: float | None = None,
    end_s: float | None = None,
    start_word: int | None = None,
    end_word: int | None = None,
    title: str | None = None,
    status: ClipStatus | None = None,
    user_trimmed: bool | None = None,
) -> None:
    fields: dict[str, Any] = {}
    for name, value in (
        ("start_s", start_s),
        ("end_s", end_s),
        ("start_word", start_word),
        ("end_word", end_word),
        ("title", title),
        ("status", status),
    ):
        if value is not None:
            fields[name] = value
    if user_trimmed is not None:
        fields["user_trimmed"] = int(user_trimmed)

    if not fields:
        return

    assignments = ", ".join(f"{name} = ?" for name in fields)
    with connection() as conn:
        conn.execute(
            f"UPDATE clips SET {assignments} WHERE id = ?",
            (*fields.values(), clip_id),
        )


# --------------------------------------------------------------------------
# Clip edits
# --------------------------------------------------------------------------


def upsert_clip_edit(edit: ClipEdit) -> ClipEdit:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO clip_edits (clip_id, edited_words_json, caption_style, ratio,
                                    burn_captions, framing_json, caption_overrides_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(clip_id) DO UPDATE SET
                edited_words_json = excluded.edited_words_json,
                caption_style = excluded.caption_style,
                ratio = excluded.ratio,
                burn_captions = excluded.burn_captions,
                framing_json = excluded.framing_json,
                caption_overrides_json = excluded.caption_overrides_json,
                updated_at = excluded.updated_at
            """,
            (
                edit.clip_id,
                json.dumps(edit.edited_words) if edit.edited_words is not None else None,
                edit.caption_style,
                edit.ratio,
                edit.burn_captions,
                json.dumps(edit.framing) if edit.framing is not None else None,
                json.dumps(edit.caption_overrides) if edit.caption_overrides else None,
                utcnow(),
            ),
        )
    return edit


def patch_clip_edit(clip_id: str, changes: dict[str, Any]) -> ClipEdit:
    """Update only supplied sections, acquiring the write lock before any reads."""
    columns = {
        "edited_words": "edited_words_json",
        "caption_style": "caption_style",
        "ratio": "ratio",
        "burn_captions": "burn_captions",
        "framing": "framing_json",
    }
    overrides = changes.get("caption_overrides", UNSET)
    values = {
        columns[key]: (
            json.dumps(value) if key in {"edited_words", "framing"} and value is not None else value
        )
        for key, value in changes.items()
        if key != "caption_overrides"
    }
    values["updated_at"] = utcnow()
    with connection() as conn:
        conn.execute(
            "INSERT INTO clip_edits (clip_id, updated_at) VALUES (?, ?) "
            "ON CONFLICT(clip_id) DO NOTHING",
            (clip_id, values["updated_at"]),
        )
        if not isinstance(overrides, _Unset):
            values["caption_overrides_json"] = json.dumps(overrides) if overrides else None
        assignments = ", ".join(f"{name} = ?" for name in values)
        conn.execute(
            f"UPDATE clip_edits SET {assignments} WHERE clip_id = ?", (*values.values(), clip_id)
        )
        row = conn.execute("SELECT * FROM clip_edits WHERE clip_id = ?", (clip_id,)).fetchone()
    return ClipEdit.from_row(row)


def get_clip_edit(clip_id: str) -> ClipEdit | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM clip_edits WHERE clip_id = ?", (clip_id,)).fetchone()
    return ClipEdit.from_row(row) if row else None


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


def create_export(export: Export) -> Export:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO exports (id, clip_id, path, ratio, style, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export.id,
                export.clip_id,
                export.path,
                export.ratio,
                export.style,
                export.size_bytes,
                export.created_at,
            ),
        )
    return export


def get_export(export_id: str) -> Export | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    return Export.from_row(row) if row else None


def list_exports(clip_id: str) -> list[Export]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM exports WHERE clip_id = ? ORDER BY created_at DESC", (clip_id,)
        ).fetchall()
    return [Export.from_row(r) for r in rows]


# --------------------------------------------------------------------------
# Publications
# --------------------------------------------------------------------------


def create_publication(publication: Publication) -> Publication:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO publications (id, export_id, clip_id, status, title,
                                      description, tags_json, thumbnail_prompt,
                                      thumbnail_path, thumbnail_wide_path,
                                      final_path, best_day, privacy_status,
                                      youtube_video_id, youtube_url, error,
                                      created_at, updated_at, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                publication.id,
                publication.export_id,
                publication.clip_id,
                publication.status,
                publication.title,
                publication.description,
                json.dumps(publication.tags, ensure_ascii=False),
                publication.thumbnail_prompt,
                publication.thumbnail_path,
                publication.thumbnail_wide_path,
                publication.final_path,
                publication.best_day,
                publication.privacy_status,
                publication.youtube_video_id,
                publication.youtube_url,
                publication.error,
                publication.created_at,
                publication.updated_at,
                publication.uploaded_at,
            ),
        )
    return publication


def get_publication(publication_id: str) -> Publication | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM publications WHERE id = ?", (publication_id,)).fetchone()
    return Publication.from_row(row) if row else None


def get_publication_by_export(export_id: str) -> Publication | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM publications WHERE export_id = ?", (export_id,)
        ).fetchone()
    return Publication.from_row(row) if row else None


def list_publications(limit: int = 50) -> list[Publication]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM publications ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [Publication.from_row(r) for r in rows]


def update_publication(
    publication_id: str,
    *,
    status: PublicationStatus | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    thumbnail_prompt: str | None = None,
    thumbnail_path: str | None | _Unset = UNSET,
    thumbnail_wide_path: str | None | _Unset = UNSET,
    final_path: str | None | _Unset = UNSET,
    best_day: str | None | _Unset = UNSET,
    privacy_status: PrivacyStatus | None = None,
    youtube_video_id: str | None | _Unset = UNSET,
    youtube_url: str | None | _Unset = UNSET,
    error: str | None | _Unset = UNSET,
    uploaded_at: str | None | _Unset = UNSET,
) -> Publication | None:
    """Patch the supplied fields and always refresh ``updated_at``.

    Nullable artifact/upload fields take :data:`UNSET` so passing ``None``
    explicitly clears them (a retry clears the previous error).
    """
    fields: dict[str, Any] = {"updated_at": utcnow()}

    for name, value in (
        ("status", status),
        ("title", title),
        ("description", description),
        ("thumbnail_prompt", thumbnail_prompt),
        ("privacy_status", privacy_status),
    ):
        if value is not None:
            fields[name] = value
    if tags is not None:
        fields["tags_json"] = json.dumps(tags, ensure_ascii=False)

    for name, value in (
        ("thumbnail_path", thumbnail_path),
        ("thumbnail_wide_path", thumbnail_wide_path),
        ("final_path", final_path),
        ("best_day", best_day),
        ("youtube_video_id", youtube_video_id),
        ("youtube_url", youtube_url),
        ("error", error),
        ("uploaded_at", uploaded_at),
    ):
        if not isinstance(value, _Unset):
            fields[name] = value

    assignments = ", ".join(f"{name} = ?" for name in fields)
    with connection() as conn:
        conn.execute(
            f"UPDATE publications SET {assignments} WHERE id = ?",
            (*fields.values(), publication_id),
        )
        row = conn.execute("SELECT * FROM publications WHERE id = ?", (publication_id,)).fetchone()
    return Publication.from_row(row) if row else None


def list_publish_candidates(limit: int = 50, ratio: str = "9:16") -> list[PublishCandidate]:
    """Latest export per clip, with the clip's job/source context.

    Keeping only the newest export of each clip stops an edited-and-rendered
    clip from appearing several times in the list.
    """
    query = """
        SELECT e.id AS export_id, e.clip_id, e.ratio, e.size_bytes,
               e.created_at AS export_created_at,
               c.job_id, c.title AS clip_title,
               (c.end_s - c.start_s) AS duration_s,
               s.title AS source_title,
               p.id AS publication_id, p.status AS publication_status
        FROM exports e
        JOIN clips c ON c.id = e.clip_id
        JOIN jobs j ON j.id = c.job_id
        JOIN sources s ON s.id = j.source_id
        LEFT JOIN publications p ON p.export_id = e.id
        WHERE e.ratio = ?
          AND e.id = (
              SELECT e2.id FROM exports e2
              WHERE e2.clip_id = e.clip_id
              ORDER BY e2.created_at DESC, e2.id DESC
              LIMIT 1
          )
        ORDER BY e.created_at DESC
        LIMIT ?
    """
    with connection() as conn:
        rows = conn.execute(query, (ratio, limit)).fetchall()
    return [
        PublishCandidate(
            export_id=row["export_id"],
            clip_id=row["clip_id"],
            job_id=row["job_id"],
            source_title=row["source_title"],
            clip_title=row["clip_title"],
            duration_s=row["duration_s"],
            ratio=row["ratio"],
            size_bytes=row["size_bytes"],
            export_created_at=row["export_created_at"],
            publication_id=row["publication_id"],
            publication_status=row["publication_status"],
        )
        for row in rows
    ]


def fail_interrupted_publications() -> int:
    """Convert uploads left in ``uploading`` by a restart into ``failed``.

    Without this a crash during upload leaves a row that the UI polls forever.
    A retry is safe: the YouTube client reads the persisted video id before
    inserting again.
    """
    with connection() as conn:
        cursor = conn.execute(
            """
            UPDATE publications
            SET status = 'failed',
                error = 'Upload was interrupted by a restart. Retry to continue.',
                updated_at = ?
            WHERE status = 'uploading'
            """,
            (utcnow(),),
        )
        return cursor.rowcount
