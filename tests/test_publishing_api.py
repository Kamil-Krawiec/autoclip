"""REST contract for the Publish page."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from autoclip import app as app_module
from autoclip import db, paths
from autoclip.api import publishing as publishing_api
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import (
    Clip,
    Export,
    Job,
    Publication,
    Source,
    new_id,
    utcnow,
)
from autoclip.db.models import (
    Transcript as TranscriptRow,
)
from autoclip.pipeline.runner import JobWorkspace
from autoclip.pipeline.transcript import Transcript, Word
from autoclip.providers.base import ProviderError
from autoclip.publishing.metadata import PublishMetadata
from autoclip.publishing.thumbnails import ThumbnailError
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(app_module.ENV_NO_WORKER, "1")
    with TestClient(create_app()) as test_client:
        yield test_client


def _seed(
    *,
    clip_title: str = "Gotowy klip",
    source_title: str = "Źródło",
    path: Path | None = None,
    ratio: str = "9:16",
) -> tuple[Export, Clip]:
    source = store.create_source(
        Source(id=new_id(), type="upload", path="/tmp/source.mp4", title=source_title)
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    clip = Clip(
        id=new_id(),
        job_id=job.id,
        rank=1,
        start_s=10.0,
        end_s=52.5,
        title=clip_title,
        hook="Hook",
        status="exported",
        start_word=0,
        end_word=2,
    )
    store.replace_clips(job.id, [clip])
    destination = path or (paths.exports_dir() / f"{new_id()}.mp4")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"video")
    export = store.create_export(
        Export(
            id=new_id(),
            clip_id=clip.id,
            path=str(destination),
            ratio=ratio,
            created_at="2026-10-01T10:00:00+00:00",
        )
    )
    return export, clip


def _add_transcript(clip: Clip) -> None:
    workspace = JobWorkspace(clip.job_id)
    transcript = Transcript(
        words=[Word("To", 10.0, 11.0), Word("jest", 11.0, 12.0), Word("treść", 12.0, 13.0)]
    )
    transcript.save(workspace.transcript)
    store.upsert_transcript(
        TranscriptRow(
            job_id=clip.job_id,
            json_path=str(workspace.transcript),
            word_count=len(transcript.words),
        )
    )


def _mark_ready(publication_id: str, *, title: str = "Tytuł") -> None:
    directory = paths.publish_dir(publication_id)
    directory.mkdir(parents=True, exist_ok=True)
    png = directory / "thumbnail.png"
    wide = directory / "thumbnail-wide.jpg"
    png.write_bytes(b"png")
    wide.write_bytes(b"jpg")
    store.update_publication(
        publication_id,
        status="ready",
        title=title,
        thumbnail_path=str(png),
        thumbnail_wide_path=str(wide),
    )


def _draft(client: TestClient, export: Export) -> dict:
    response = client.post("/api/publish", json={"export_id": export.id})
    assert response.status_code in (200, 201)
    return response.json()


def _wait_for(client: TestClient, publication_id: str, status: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/publish/{publication_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.02)
    raise AssertionError(f"publication never reached {status}: {body}")


class TestStatus:
    def test_flags_reflect_files_and_keys(self, client, fake_keyring) -> None:
        body = client.get("/api/publish/status").json()
        assert body["youtube_ready"] is False
        assert body["openai_image_key_present"] is False
        assert body["metadata_provider"] == "openai"
        assert body["metadata_model"] == "gpt-6-luna"

        client.put("/api/settings/secrets", json={"key": "openai_image", "value": "sk-image"})
        client.put("/api/settings/secrets", json={"key": "openai", "value": "sk-llm"})
        paths.secrets_dir().mkdir(parents=True, exist_ok=True)
        (paths.secrets_dir() / "youtube_client_secret.json").write_text("{}")
        (paths.secrets_dir() / "youtube_token.json").write_text("{}")

        body = client.get("/api/publish/status").json()
        assert body["openai_image_key_present"] is True
        assert body["metadata_key_present"] is True
        assert body["youtube_client_secret_present"] is True
        assert body["youtube_token_present"] is True
        assert body["youtube_ready"] is True


class TestCandidates:
    def test_lists_the_latest_export_with_context(self, client) -> None:
        _export, clip = _seed(clip_title="Wybrany klip")
        newer = store.create_export(
            Export(
                id=new_id(),
                clip_id=clip.id,
                path=str(paths.exports_dir() / "newer.mp4"),
                ratio="9:16",
                created_at="2026-10-02T10:00:00+00:00",
            )
        )
        (paths.exports_dir() / "newer.mp4").write_bytes(b"video")
        publication = _draft(client, newer)

        candidates = client.get("/api/publish/candidates").json()

        assert len(candidates) == 1
        assert candidates[0]["export_id"] == newer.id
        assert candidates[0]["source_title"] == "Źródło"
        assert candidates[0]["clip_title"] == "Wybrany klip"
        assert candidates[0]["duration_s"] == pytest.approx(42.5)
        assert candidates[0]["available"] is True
        assert candidates[0]["publication_id"] == publication["id"]
        assert candidates[0]["publication_status"] == "draft"

    def test_missing_export_file_is_unavailable(self, client) -> None:
        export, _ = _seed()
        Path(export.path).unlink()

        candidates = client.get("/api/publish/candidates").json()

        assert candidates[0]["available"] is False


class TestCreate:
    def test_creates_a_draft_with_the_clip_title_and_default_privacy(self, client) -> None:
        export, clip = _seed()

        body = _draft(client, export)

        assert body["status"] == "draft"
        assert body["title"] == clip.title
        assert body["privacy_status"] == "private"
        assert body["export_id"] == export.id
        assert body["thumbnail_url"] is None
        assert body["ready_to_upload"] is False

    def test_second_create_is_idempotent(self, client) -> None:
        export, _ = _seed()

        first = client.post("/api/publish", json={"export_id": export.id})
        second = client.post("/api/publish", json={"export_id": export.id})

        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]

    def test_unknown_export_is_404(self, client) -> None:
        assert client.post("/api/publish", json={"export_id": "nope"}).status_code == 404

    def test_export_outside_the_exports_directory_is_rejected(self, client, tmp_path: Path) -> None:
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"video")
        export, _ = _seed(path=outside)

        response = client.post("/api/publish", json={"export_id": export.id})

        assert response.status_code == 409
        assert "outside" in response.json()["detail"]["message"]

    def test_extra_fields_are_rejected(self, client) -> None:
        export, _ = _seed()

        response = client.post(
            "/api/publish", json={"export_id": export.id, "skip_validation": True}
        )

        assert response.status_code == 422

    def test_create_race_returns_the_existing_publication(self, client, monkeypatch) -> None:
        export, _ = _seed()
        real_lookup = store.get_publication_by_export
        lookups = {"count": 0}

        def miss_the_initial_checks(export_id: str):
            # Simulate two requests that both pass the cheap "already exists"
            # read before either insert has committed.
            lookups["count"] += 1
            if lookups["count"] <= 2:
                return None
            return real_lookup(export_id)

        monkeypatch.setattr(store, "get_publication_by_export", miss_the_initial_checks)

        first = client.post("/api/publish", json={"export_id": export.id})
        second = client.post("/api/publish", json={"export_id": export.id})

        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert store.get_publication_by_export(export.id) is not None


class TestPatch:
    def test_updates_normalized_fields(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)

        response = client.patch(
            f"/api/publish/{publication['id']}",
            json={
                "title": "  Nowy   tytuł  ",
                "description": "Opis",
                "tags": ["#Python", "  uczenie  maszynowe ", "python"],
                "thumbnail_prompt": "Prompt",
                "privacy_status": "unlisted",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Nowy tytuł"
        assert body["tags"] == ["python", "uczenie maszynowe"]
        assert body["privacy_status"] == "unlisted"

    def test_overlong_title_is_422(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)

        response = client.patch(f"/api/publish/{publication['id']}", json={"title": "x" * 101})

        assert response.status_code == 422

    def test_description_limit_is_bytes(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)

        response = client.patch(
            f"/api/publish/{publication['id']}", json={"description": "ą" * 2_600}
        )

        assert response.status_code == 422

    def test_tags_over_budget_are_rejected_not_silently_trimmed(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)

        response = client.patch(
            f"/api/publish/{publication['id']}",
            json={"tags": ["x" * 251, "y" * 249]},
        )

        assert response.status_code == 422

    def test_bad_privacy_is_422(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)

        response = client.patch(
            f"/api/publish/{publication['id']}", json={"privacy_status": "secret"}
        )

        assert response.status_code == 422

    def test_published_metadata_is_frozen(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        store.update_publication(publication["id"], status="published")

        response = client.patch(f"/api/publish/{publication['id']}", json={"title": "Changed"})

        assert response.status_code == 409

    def test_saving_a_title_after_a_thumbnail_promotes_to_ready(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        directory = paths.publish_dir(publication["id"])
        directory.mkdir(parents=True, exist_ok=True)
        png = directory / "thumbnail.png"
        png.write_bytes(b"png")
        store.update_publication(publication["id"], thumbnail_path=str(png))

        response = client.patch(f"/api/publish/{publication['id']}", json={"title": "Tytuł"})

        assert response.json()["status"] == "ready"
        assert response.json()["ready_to_upload"] is True


class TestMetadata:
    def test_generates_and_saves_metadata(self, client, monkeypatch) -> None:
        export, clip = _seed()
        _add_transcript(clip)
        publication = _draft(client, export)
        _mark_ready(publication["id"], title="old")

        async def fake_generate(**kwargs):
            assert kwargs["transcript_text"] == "To jest treść"
            return PublishMetadata(
                title="Nowy tytuł",
                description="Nowy opis",
                tags=["python"],
                thumbnail_prompt="Prompt",
                best_day="niedziela",
            )

        monkeypatch.setattr(publishing_api.metadata, "generate_metadata", fake_generate)

        response = client.post(f"/api/publish/{publication['id']}/metadata")

        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Nowy tytuł"
        assert body["tags"] == ["python"]
        assert body["best_day"] == "niedziela"
        assert body["status"] == "ready"

    def test_missing_transcript_is_409(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)

        response = client.post(f"/api/publish/{publication['id']}/metadata")

        assert response.status_code == 409
        assert "transcript" in response.json()["detail"]["message"].lower()

    def test_provider_failure_is_502_and_keeps_old_fields(self, client, monkeypatch) -> None:
        export, clip = _seed()
        _add_transcript(clip)
        publication = _draft(client, export)
        client.patch(
            f"/api/publish/{publication['id']}",
            json={"title": "Stary", "description": "Stary opis"},
        )

        async def fake_generate(**kwargs):
            raise ProviderError("model exploded", hint="check the model")

        monkeypatch.setattr(publishing_api.metadata, "generate_metadata", fake_generate)

        response = client.post(f"/api/publish/{publication['id']}/metadata")

        assert response.status_code == 502
        assert response.json()["detail"]["hint"] == "check the model"
        body = client.get(f"/api/publish/{publication['id']}").json()
        assert body["title"] == "Stary"
        assert body["description"] == "Stary opis"

    def test_frozen_publication_is_409(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        store.update_publication(publication["id"], status="uploading")

        assert client.post(f"/api/publish/{publication['id']}/metadata").status_code == 409


class TestThumbnail:
    def test_generates_png_and_wide_jpeg(self, client, fake_keyring, monkeypatch) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        client.put("/api/settings/secrets", json={"key": "openai_image", "value": "sk-image"})
        prompts: list[str] = []

        def fake_generate(prompt, output, **kwargs):
            prompts.append(prompt)
            output.write_bytes(b"png")

        def fake_wide(png, jpg):
            jpg.write_bytes(b"jpg")

        monkeypatch.setattr(publishing_api.thumbnails, "generate_thumbnail", fake_generate)
        monkeypatch.setattr(publishing_api.thumbnails, "derive_wide", fake_wide)

        response = client.post(
            f"/api/publish/{publication['id']}/thumbnail", json={"prompt": "Mój prompt"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["thumbnail_prompt"] == "Mój prompt"
        assert body["thumbnail_url"].startswith(f"/api/publish/{publication['id']}/thumbnail?v=")
        assert body["thumbnail_wide_url"]
        assert prompts == ["Mój prompt"]

    def test_retry_replaces_the_image_and_prompt(self, client, fake_keyring, monkeypatch) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        client.put("/api/settings/secrets", json={"key": "openai_image", "value": "sk-image"})

        def fake_generate(prompt, output, **kwargs):
            output.write_bytes(b"png")

        monkeypatch.setattr(publishing_api.thumbnails, "generate_thumbnail", fake_generate)
        monkeypatch.setattr(
            publishing_api.thumbnails, "derive_wide", lambda png, jpg: jpg.write_bytes(b"jpg")
        )

        client.post(f"/api/publish/{publication['id']}/thumbnail", json={"prompt": "first"})
        response = client.post(
            f"/api/publish/{publication['id']}/thumbnail", json={"prompt": "second"}
        )

        assert response.json()["thumbnail_prompt"] == "second"

    def test_failure_preserves_the_previous_image_and_prompt(
        self, client, fake_keyring, monkeypatch
    ) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        client.put("/api/settings/secrets", json={"key": "openai_image", "value": "sk-image"})

        def ok(prompt, output, **kwargs):
            output.write_bytes(b"png")

        monkeypatch.setattr(publishing_api.thumbnails, "generate_thumbnail", ok)
        monkeypatch.setattr(
            publishing_api.thumbnails, "derive_wide", lambda png, jpg: jpg.write_bytes(b"jpg")
        )
        client.post(f"/api/publish/{publication['id']}/thumbnail", json={"prompt": "first"})
        directory = paths.publish_dir(publication["id"])
        before = (directory / "thumbnail.png").read_bytes()

        def fail(*args, **kwargs):
            raise ThumbnailError("no image", hint="add a key")

        monkeypatch.setattr(publishing_api.thumbnails, "generate_thumbnail", fail)

        response = client.post(
            f"/api/publish/{publication['id']}/thumbnail", json={"prompt": "second"}
        )

        assert response.status_code == 502
        assert (directory / "thumbnail.png").read_bytes() == before
        body = client.get(f"/api/publish/{publication['id']}").json()
        assert body["thumbnail_prompt"] == "first"

    def test_missing_image_key_is_409(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)

        response = client.post(
            f"/api/publish/{publication['id']}/thumbnail", json={"prompt": "prompt"}
        )

        assert response.status_code == 409
        assert "OpenAI Images" in response.json()["detail"]["hint"]

    def test_serves_png_with_no_store(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        _mark_ready(publication["id"])

        response = client.get(f"/api/publish/{publication['id']}/thumbnail")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"

    def test_missing_thumbnail_is_404(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)

        assert client.get(f"/api/publish/{publication['id']}/thumbnail").status_code == 404


class TestUpload:
    def test_upload_returns_202_then_publishes(self, client, monkeypatch) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        _mark_ready(publication["id"])

        async def fake_run(publication_id: str) -> None:
            await asyncio.sleep(0.05)
            store.update_publication(
                publication_id,
                status="published",
                youtube_video_id="vid",
                youtube_url="https://youtu.be/vid",
                uploaded_at=utcnow(),
            )

        monkeypatch.setattr(publishing_api.service, "run_upload", fake_run)

        response = client.post(f"/api/publish/{publication['id']}/upload")

        assert response.status_code == 202
        assert response.json()["status"] == "uploading"
        body = _wait_for(client, publication["id"], "published")
        assert body["youtube_url"] == "https://youtu.be/vid"
        assert body["uploaded_at"] is not None

    def test_missing_thumbnail_is_409(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        store.update_publication(publication["id"], status="ready", title="Tytuł")

        response = client.post(f"/api/publish/{publication['id']}/upload")

        assert response.status_code == 409

    def test_published_cannot_be_uploaded_again(self, client) -> None:
        export, _ = _seed()
        publication = _draft(client, export)
        store.update_publication(publication["id"], status="published")

        assert client.post(f"/api/publish/{publication['id']}/upload").status_code == 409

    def test_one_upload_at_a_time(self, client, monkeypatch) -> None:
        first_export, _ = _seed()
        first = _draft(client, first_export)
        _mark_ready(first["id"])
        second_export, _ = _seed()
        second = _draft(client, second_export)
        _mark_ready(second["id"])

        async def slow_run(publication_id: str) -> None:
            await asyncio.sleep(0.3)
            store.update_publication(publication_id, status="published")

        monkeypatch.setattr(publishing_api.service, "run_upload", slow_run)

        assert client.post(f"/api/publish/{first['id']}/upload").status_code == 202
        response = client.post(f"/api/publish/{second['id']}/upload")

        assert response.status_code == 409
        _wait_for(client, first["id"], "published")


def test_startup_recovers_interrupted_upload(autoclip_home, monkeypatch) -> None:
    monkeypatch.setenv(app_module.ENV_NO_WORKER, "1")
    db.init()
    export, _ = _seed()
    publication = store.create_publication(
        Publication(
            id=new_id(),
            export_id=export.id,
            clip_id=export.clip_id,
            status="uploading",
            title="Title",
        )
    )

    with TestClient(create_app()) as test_client:
        body = test_client.get(f"/api/publish/{publication.id}").json()

    assert body["status"] == "failed"
    assert "interrupted by a restart" in body["error"]
