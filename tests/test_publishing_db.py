"""Schema v5, the Publication model, and the publish store queries."""

from __future__ import annotations

import sqlite3

import pytest
from autoclip import db, paths
from autoclip.db import store
from autoclip.db.models import (
    Clip,
    ClipEdit,
    Export,
    Job,
    Publication,
    Source,
    new_id,
)
from autoclip.db.schema import MIGRATIONS


def _seed_export(
    *,
    source_title: str = "Source video",
    clip_title: str = "Clip title",
    ratio: str = "9:16",
    created_at: str = "2026-10-01T10:00:00+00:00",
    export_id: str | None = None,
) -> Export:
    source = store.create_source(
        Source(id=new_id(), type="upload", path="/tmp/source.mp4", title=source_title)
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    clip = Clip(
        id=new_id(),
        job_id=job.id,
        start_s=10.0,
        end_s=52.5,
        title=clip_title,
        status="exported",
    )
    store.replace_clips(job.id, [clip])
    store.upsert_clip_edit(ClipEdit(clip_id=clip.id))
    return store.create_export(
        Export(
            id=export_id or new_id(),
            clip_id=clip.id,
            path=f"/tmp/exports/{new_id()}.mp4",
            ratio=ratio,
            created_at=created_at,
        )
    )


def _publication(export: Export, **overrides) -> Publication:
    values = {
        "id": new_id(),
        "export_id": export.id,
        "clip_id": export.clip_id,
        "title": "Draft title",
    }
    values.update(overrides)
    return Publication(**values)


class TestMigrationV5:
    def test_v4_database_upgrades_and_keeps_existing_rows(self, autoclip_home) -> None:
        db_path = paths.db_path()
        conn = db.connect(db_path)
        try:
            for version, migration in enumerate(MIGRATIONS[:4]):
                with conn:
                    migration(conn)
                    conn.execute(f"PRAGMA user_version = {version + 1}")
            with conn:
                conn.execute(
                    "INSERT INTO sources (id, type, path, title, created_at) "
                    "VALUES ('s1', 'upload', '/tmp/a.mp4', 'Old source', '2026-01-01')"
                )
                conn.execute(
                    "INSERT INTO jobs (id, source_id, status, created_at, updated_at) "
                    "VALUES ('j1', 's1', 'done', '2026-01-01', '2026-01-01')"
                )
                conn.execute(
                    "INSERT INTO clips (id, job_id, start_s, end_s, title, created_at) "
                    "VALUES ('c1', 'j1', 1, 9, 'Old clip', '2026-01-01')"
                )
                conn.execute(
                    "INSERT INTO clip_edits (clip_id, updated_at) VALUES ('c1', '2026-01-01')"
                )
                conn.execute(
                    "INSERT INTO exports (id, clip_id, path, created_at) "
                    "VALUES ('e1', 'c1', '/tmp/e1.mp4', '2026-01-01')"
                )

            assert db.migrate(conn) == 5

            assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
            assert conn.execute("SELECT title FROM sources WHERE id = 's1'").fetchone()[0] == (
                "Old source"
            )
            assert conn.execute("SELECT title FROM clips WHERE id = 'c1'").fetchone()[0] == (
                "Old clip"
            )
            assert conn.execute("SELECT path FROM exports WHERE id = 'e1'").fetchone()[0] == (
                "/tmp/e1.mp4"
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(publications)").fetchall()
            }
            assert {
                "id",
                "export_id",
                "clip_id",
                "status",
                "title",
                "description",
                "tags_json",
                "thumbnail_prompt",
                "thumbnail_path",
                "thumbnail_wide_path",
                "final_path",
                "best_day",
                "privacy_status",
                "youtube_video_id",
                "youtube_url",
                "error",
                "created_at",
                "updated_at",
                "uploaded_at",
            } <= columns
        finally:
            conn.close()

    def test_fresh_database_is_v5(self, initialised_db: int) -> None:
        assert initialised_db == 5


class TestPublicationStore:
    def test_create_and_get_round_trips_tags_and_nulls(self, initialised_db: int) -> None:
        export = _seed_export()

        created = store.create_publication(
            _publication(
                export,
                tags=["python", "test"],
                best_day="niedziela",
                privacy_status="unlisted",
            )
        )

        loaded = store.get_publication(created.id)
        assert loaded is not None
        assert loaded.export_id == export.id
        assert loaded.status == "draft"
        assert loaded.tags == ["python", "test"]
        assert loaded.best_day == "niedziela"
        assert loaded.privacy_status == "unlisted"
        assert loaded.thumbnail_path is None
        assert loaded.youtube_video_id is None

    def test_one_publication_per_export(self, initialised_db: int) -> None:
        export = _seed_export()
        store.create_publication(_publication(export))

        with pytest.raises(sqlite3.IntegrityError):
            store.create_publication(_publication(export))

    def test_get_by_export(self, initialised_db: int) -> None:
        export = _seed_export()
        created = store.create_publication(_publication(export))

        assert store.get_publication_by_export(export.id) == created
        assert store.get_publication_by_export("missing") is None

    def test_update_refreshes_timestamp_and_can_clear_nullable_fields(
        self, initialised_db: int
    ) -> None:
        export = _seed_export()
        created = store.create_publication(
            _publication(
                export,
                updated_at="2020-01-01T00:00:00+00:00",
                youtube_url="https://youtu.be/old",
                error="old error",
            )
        )

        updated = store.update_publication(
            created.id,
            status="published",
            title="New title",
            tags=["a", "b"],
            error=None,
            youtube_url=None,
            uploaded_at="2026-10-28T12:00:00+00:00",
        )

        assert updated is not None
        assert updated.status == "published"
        assert updated.title == "New title"
        assert updated.tags == ["a", "b"]
        assert updated.error is None
        assert updated.youtube_url is None
        assert updated.uploaded_at == "2026-10-28T12:00:00+00:00"
        assert updated.updated_at != "2020-01-01T00:00:00+00:00"

    def test_update_omitted_fields_are_untouched(self, initialised_db: int) -> None:
        export = _seed_export()
        created = store.create_publication(
            _publication(export, description="keep me", tags=["keep"])
        )

        store.update_publication(created.id, title="Only the title")

        loaded = store.get_publication(created.id)
        assert loaded is not None
        assert loaded.title == "Only the title"
        assert loaded.description == "keep me"
        assert loaded.tags == ["keep"]

    def test_update_missing_returns_none(self, initialised_db: int) -> None:
        assert store.update_publication("missing", title="x") is None

    def test_deleting_export_cascades_publication(self, initialised_db: int) -> None:
        export = _seed_export()
        created = store.create_publication(_publication(export))

        with db.connection() as conn:
            conn.execute("DELETE FROM exports WHERE id = ?", (export.id,))

        assert store.get_publication(created.id) is None

    def test_list_is_newest_first(self, initialised_db: int) -> None:
        first_export = _seed_export()
        second_export = _seed_export()
        first = store.create_publication(
            _publication(first_export, created_at="2026-01-01T00:00:00+00:00")
        )
        second = store.create_publication(
            _publication(second_export, created_at="2026-06-01T00:00:00+00:00")
        )

        listed = store.list_publications()

        assert [item.id for item in listed[:2]] == [second.id, first.id]


class TestPublishCandidates:
    def test_lists_latest_export_per_clip_with_publication_state(self, initialised_db: int) -> None:
        export = _seed_export(clip_title="Chosen clip")
        # A newer export of the same clip must hide the older one.
        newer = store.create_export(
            Export(
                id=new_id(),
                clip_id=export.clip_id,
                path="/tmp/newer.mp4",
                ratio="9:16",
                created_at="2026-10-02T10:00:00+00:00",
            )
        )
        publication = store.create_publication(_publication(newer, status="ready"))

        candidates = store.list_publish_candidates()

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.export_id == newer.id
        assert candidate.clip_title == "Chosen clip"
        assert candidate.source_title == "Source video"
        assert candidate.duration_s == pytest.approx(42.5)
        assert candidate.publication_id == publication.id
        assert candidate.publication_status == "ready"

    def test_filters_by_ratio(self, initialised_db: int) -> None:
        _seed_export(ratio="9:16")
        _seed_export(ratio="16:9")

        candidates = store.list_publish_candidates(ratio="16:9")

        assert [candidate.ratio for candidate in candidates] == ["16:9"]

    def test_candidate_without_publication(self, initialised_db: int) -> None:
        _seed_export()

        [candidate] = store.list_publish_candidates()

        assert candidate.publication_id is None
        assert candidate.publication_status is None

    def test_limit(self, initialised_db: int) -> None:
        for index in range(3):
            _seed_export(created_at=f"2026-10-0{index + 1}T10:00:00+00:00")

        assert len(store.list_publish_candidates(limit=2)) == 2


class TestStartupRecovery:
    def test_uploading_rows_become_failed(self, initialised_db: int) -> None:
        uploading = store.create_publication(_publication(_seed_export(), status="uploading"))
        draft = store.create_publication(_publication(_seed_export(), status="draft"))

        assert store.fail_interrupted_publications() == 1

        interrupted = store.get_publication(uploading.id)
        assert interrupted is not None
        assert interrupted.status == "failed"
        assert "interrupted by a restart" in (interrupted.error or "")
        untouched = store.get_publication(draft.id)
        assert untouched is not None
        assert untouched.status == "draft"
