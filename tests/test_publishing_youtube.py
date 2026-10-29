"""YouTube body/retry behaviour and the background upload state machine."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from autoclip import paths
from autoclip.db import store
from autoclip.db.models import Clip, Export, Job, Publication, Source, new_id
from autoclip.publishing import service
from autoclip.publishing import youtube as youtube_module


class FakeHttpError(Exception):
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.resp = SimpleNamespace(status=status)
        self.content = content
        super().__init__(content.decode("utf-8", "ignore") or f"HTTP {status}")


class FakeRequest:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.chunks = 0

    def next_chunk(self):
        self.chunks += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return None, outcome


class FakeVideos:
    def __init__(self, owner: FakeYouTube) -> None:
        self.owner = owner

    def insert(self, **kwargs):
        self.owner.insert_calls.append(kwargs)
        return FakeRequest(self.owner.batches.pop(0))


class FakeExecute:
    def __init__(self, error: Exception | None) -> None:
        self.error = error

    def execute(self):
        if self.error is not None:
            raise self.error
        return {}


class FakeThumbnails:
    def __init__(self, owner: FakeYouTube) -> None:
        self.owner = owner

    def set(self, **kwargs):
        self.owner.thumb_calls.append(kwargs)
        return FakeExecute(self.owner.thumb_error)


class FakeYouTube:
    def __init__(self, batches: list[list[object]], thumb_error=None) -> None:
        self.batches = list(batches)
        self.insert_calls: list[dict] = []
        self.thumb_calls: list[dict] = []
        self.thumb_error = thumb_error

    def videos(self) -> FakeVideos:
        return FakeVideos(self)

    def thumbnails(self) -> FakeThumbnails:
        return FakeThumbnails(self)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "short.mp4"
    path.write_bytes(b"fake mp4 bytes")
    return path


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(youtube_module, "_sleep", sleeps.append)
    monkeypatch.setattr(youtube_module, "_media_file_upload", lambda path, **kwargs: object())
    return sleeps


class TestUploadBody:
    def test_sends_the_documented_body_and_returns_the_id(self, video, no_sleep) -> None:
        client = FakeYouTube([[None, {"id": "abc123"}]])

        video_id = youtube_module.upload_video(
            client,
            video,
            title="Tytuł",
            description="Opis",
            tags=["python", "test"],
            privacy="private",
            category_id="28",
        )

        assert video_id == "abc123"
        call = client.insert_calls[0]
        assert call["part"] == "snippet,status"
        assert call["body"]["snippet"] == {
            "title": "Tytuł",
            "description": "Opis",
            "tags": ["python", "test"],
            "categoryId": "28",
        }
        status = call["body"]["status"]
        assert status["privacyStatus"] == "private"
        assert status["selfDeclaredMadeForKids"] is False
        assert status["publicStatsViewable"] is False

    def test_missing_id_is_an_error(self, video, no_sleep) -> None:
        client = FakeYouTube([[None, {}]])

        with pytest.raises(youtube_module.PublishingError, match="video id"):
            youtube_module.upload_video(
                client,
                video,
                title="T",
                description="",
                tags=[],
                privacy="private",
                category_id="28",
            )


class TestUploadRetries:
    def test_transient_5xx_is_retried_with_backoff(self, video, no_sleep) -> None:
        client = FakeYouTube([[FakeHttpError(500), FakeHttpError(503), None, {"id": "ok"}]])

        result = youtube_module.upload_video(
            client,
            video,
            title="T",
            description="",
            tags=[],
            privacy="private",
            category_id="28",
        )

        assert result == "ok"
        assert len(no_sleep) == 2
        assert no_sleep[1] > no_sleep[0]

    def test_400_is_not_retried(self, video, no_sleep) -> None:
        client = FakeYouTube([[FakeHttpError(400, b"bad request")]])

        with pytest.raises(youtube_module.PublishingError) as caught:
            youtube_module.upload_video(
                client,
                video,
                title="T",
                description="",
                tags=[],
                privacy="private",
                category_id="28",
            )

        assert caught.value.status_code == 400
        assert len(no_sleep) == 0

    def test_made_for_kids_400_drops_the_field_and_retries_once(self, video, no_sleep) -> None:
        client = FakeYouTube(
            [
                [FakeHttpError(400, b'{"error": "madeForKids is not supported"}')],
                [None, {"id": "ok"}],
            ]
        )

        result = youtube_module.upload_video(
            client,
            video,
            title="T",
            description="",
            tags=[],
            privacy="private",
            category_id="28",
        )

        assert result == "ok"
        assert len(client.insert_calls) == 2
        assert "madeForKids" not in client.insert_calls[1]["body"]["status"]
        assert client.insert_calls[1]["body"]["status"]["selfDeclaredMadeForKids"] is False

    def test_quota_403_has_a_clear_message(self, video, no_sleep) -> None:
        client = FakeYouTube(
            [[FakeHttpError(403, b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}')]]
        )

        with pytest.raises(youtube_module.PublishingError) as caught:
            youtube_module.upload_video(
                client,
                video,
                title="T",
                description="",
                tags=[],
                privacy="private",
                category_id="28",
            )

        assert "quota" in str(caught.value).lower()
        assert caught.value.status_code == 403
        assert len(no_sleep) == 0

    def test_connection_errors_are_retried(self, video, no_sleep) -> None:
        class TransportError(Exception):
            pass

        client = FakeYouTube([[TransportError("connection reset"), None, {"id": "ok"}]])

        assert (
            youtube_module.upload_video(
                client,
                video,
                title="T",
                description="",
                tags=[],
                privacy="private",
                category_id="28",
            )
            == "ok"
        )


class TestUploadValidation:
    @pytest.mark.parametrize(
        ("title", "description", "tags"),
        [
            ("", "", []),
            ("x" * 101, "", []),
            ("<b>", "", []),
            ("ok", "ą" * 3_000, []),
            ("ok", "", [f"tag-{index:03d}" for index in range(100)]),
        ],
    )
    def test_invalid_fields_are_rejected_before_the_api(
        self, video, title, description, tags
    ) -> None:
        client = FakeYouTube([[None, {"id": "never"}]])

        with pytest.raises(youtube_module.PublishingError):
            youtube_module.upload_video(
                client,
                video,
                title=title,
                description=description,
                tags=tags,
                privacy="private",
                category_id="28",
            )

        assert client.insert_calls == []

    def test_missing_file_is_rejected(self, tmp_path) -> None:
        with pytest.raises(youtube_module.PublishingError, match="does not exist"):
            youtube_module.upload_video(
                FakeYouTube([]),
                tmp_path / "missing.mp4",
                title="T",
                description="",
                tags=[],
                privacy="private",
                category_id="28",
            )

    def test_unknown_privacy_is_rejected(self, video) -> None:
        with pytest.raises(youtube_module.PublishingError, match="privacy"):
            youtube_module.upload_video(
                FakeYouTube([]),
                video,
                title="T",
                description="",
                tags=[],
                privacy="secret",
                category_id="28",
            )


class TestThumbnail:
    def test_sets_video_id_and_jpeg(self, video, no_sleep) -> None:
        client = FakeYouTube([])

        youtube_module.set_thumbnail(client, "vid", video)

        assert client.thumb_calls[0]["videoId"] == "vid"

    def test_failure_is_wrapped_with_an_actionable_hint(self, video, no_sleep) -> None:
        client = FakeYouTube([], thumb_error=FakeHttpError(403, b"forbidden"))

        with pytest.raises(youtube_module.PublishingError) as caught:
            youtube_module.set_thumbnail(client, "vid", video)

        assert "custom thumbnail" in caught.value.hint
        assert "still at the end of the MP4" in caught.value.hint


def _make_short(video, png, out, **kwargs):
    out.write_bytes(b"short")
    return out


def _seed_publication(*, export_path=None, thumbnail=True, wide=True) -> Publication:
    source = store.create_source(
        Source(id=new_id(), type="upload", path="/tmp/src.mp4", title="Source")
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    clip = Clip(
        id=new_id(),
        job_id=job.id,
        start_s=0.0,
        end_s=10.0,
        title="Clip",
        status="exported",
    )
    store.replace_clips(job.id, [clip])
    export = store.create_export(
        Export(
            id=new_id(),
            clip_id=clip.id,
            path=str(export_path or paths.exports_dir() / "clip.mp4"),
        )
    )
    if export_path is None:
        (paths.exports_dir() / "clip.mp4").parent.mkdir(parents=True, exist_ok=True)
        (paths.exports_dir() / "clip.mp4").write_bytes(b"export")
    publication = store.create_publication(
        Publication(
            id=new_id(),
            export_id=export.id,
            clip_id=clip.id,
            title="Tytuł",
            thumbnail_path=str(paths.publish_dir("pending") / "thumbnail.png"),
        )
    )
    directory = paths.publish_dir(publication.id)
    directory.mkdir(parents=True, exist_ok=True)
    if thumbnail:
        (directory / "thumbnail.png").write_bytes(b"png")
        store.update_publication(publication.id, thumbnail_path=str(directory / "thumbnail.png"))
    if wide:
        (directory / "thumbnail-wide.jpg").write_bytes(b"jpg")
        store.update_publication(
            publication.id, thumbnail_wide_path=str(directory / "thumbnail-wide.jpg")
        )
    return publication


class TestRunUpload:
    async def test_success_persists_id_url_and_status(
        self, autoclip_home, initialised_db, monkeypatch
    ) -> None:
        publication = _seed_publication()
        monkeypatch.setattr(service.tcap, "append_still", _make_short)
        monkeypatch.setattr(service.auth, "build_client", lambda: object())
        monkeypatch.setattr(service.youtube_module, "upload_video", lambda *a, **k: "vid123")
        thumbnails: list[str] = []
        monkeypatch.setattr(
            service.youtube_module,
            "set_thumbnail",
            lambda client, video_id, jpg: thumbnails.append(video_id),
        )

        await service.run_upload(publication.id)

        loaded = store.get_publication(publication.id)
        assert loaded is not None
        assert loaded.status == "published"
        assert loaded.youtube_video_id == "vid123"
        assert loaded.youtube_url == "https://youtu.be/vid123"
        assert loaded.uploaded_at is not None
        assert loaded.final_path == str(paths.publish_dir(publication.id) / "short.mp4")
        assert thumbnails == ["vid123"]

    async def test_thumbnail_failure_does_not_fail_the_upload(
        self, autoclip_home, initialised_db, monkeypatch
    ) -> None:
        publication = _seed_publication()
        monkeypatch.setattr(service.tcap, "append_still", _make_short)
        monkeypatch.setattr(service.auth, "build_client", lambda: object())
        monkeypatch.setattr(service.youtube_module, "upload_video", lambda *a, **k: "vid123")

        def _reject(*args, **kwargs):
            raise youtube_module.PublishingError("no thumbnail for this channel")

        monkeypatch.setattr(service.youtube_module, "set_thumbnail", _reject)

        await service.run_upload(publication.id)

        loaded = store.get_publication(publication.id)
        assert loaded is not None
        assert loaded.status == "published"

    async def test_insert_failure_is_recorded_and_retry_skips_the_insert(
        self, autoclip_home, initialised_db, monkeypatch
    ) -> None:
        publication = _seed_publication()
        monkeypatch.setattr(service.tcap, "append_still", _make_short)
        monkeypatch.setattr(service.auth, "build_client", lambda: object())
        monkeypatch.setattr(
            service.youtube_module,
            "set_thumbnail",
            lambda client, video_id, jpg: None,
        )

        def _fail(*args, **kwargs):
            raise youtube_module.PublishingError("upload exploded")

        monkeypatch.setattr(service.youtube_module, "upload_video", _fail)

        await service.run_upload(publication.id)

        failed = store.get_publication(publication.id)
        assert failed is not None
        assert failed.status == "failed"
        assert "upload exploded" in (failed.error or "")

        # Pretend the insert had succeeded before a late failure.
        store.update_publication(
            publication.id,
            youtube_video_id="already-there",
            youtube_url="https://youtu.be/already-there",
        )

        def _must_not_insert(*args, **kwargs):
            raise AssertionError("a retry must not upload a second time")

        monkeypatch.setattr(service.youtube_module, "upload_video", _must_not_insert)
        await service.run_upload(publication.id)

        retried = store.get_publication(publication.id)
        assert retried is not None
        assert retried.status == "published"
        assert retried.youtube_video_id == "already-there"

    async def test_export_outside_the_exports_directory_fails(
        self, autoclip_home, initialised_db, monkeypatch
    ) -> None:
        publication = _seed_publication(export_path="/tmp/evil.mp4")
        monkeypatch.setattr(service.auth, "build_client", lambda: object())

        await service.run_upload(publication.id)

        loaded = store.get_publication(publication.id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert "outside" in (loaded.error or "")

    async def test_missing_thumbnail_fails_before_the_upload(
        self, autoclip_home, initialised_db, monkeypatch
    ) -> None:
        publication = _seed_publication(thumbnail=False, wide=False)
        monkeypatch.setattr(service.auth, "build_client", lambda: object())

        await service.run_upload(publication.id)

        loaded = store.get_publication(publication.id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert "thumbnail" in (loaded.error or "").lower()
