"""Thumbnail generation, padding, and the wide JPEG derivation."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from autoclip.pipeline import ffmpeg
from autoclip.publishing import thumbnails


class FakeStatusError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class FakeImages:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.sizes: list[str] = []

    def generate(self, **kwargs):
        self.sizes.append(kwargs["size"])
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bytes):
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(result).decode())]
            )
        return SimpleNamespace(data=[SimpleNamespace(b64_json=None, url=result)])


class FakeClient:
    def __init__(self, results: list[object]) -> None:
        self.images = FakeImages(results)


@pytest.fixture
def png(tmp_path: Path):
    def make(width: int, height: int, color: str = "red") -> Path:
        path = tmp_path / f"{width}x{height}-{color}.png"
        ffmpeg.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s={width}x{height}",
                "-frames:v",
                "1",
                "-update",
                "1",
                str(path),
            ]
        )
        return path

    return make


class TestGenerate:
    def test_writes_a_validated_png_and_leaves_no_temp(self, png, tmp_path) -> None:
        source = png(1080, 1920)
        output = tmp_path / "publish" / "thumbnail.png"
        client = FakeClient([source.read_bytes()])

        thumbnails.generate_thumbnail(
            "prompt",
            output,
            model="gpt-image-2.5-flare",
            size="1080x1920",
            quality="high",
            api_key="sk-test",
            client=client,
        )

        assert output.exists()
        assert thumbnails.image_dimensions(output) == (1080, 1920)
        assert not output.with_suffix(".tmp.png").exists()
        assert client.images.sizes == ["1080x1920"]

    def test_fallback_size_is_padded_not_cropped(self, png, tmp_path) -> None:
        fallback = png(1024, 1536, "green")
        output = tmp_path / "thumbnail.png"
        client = FakeClient([FakeStatusError(400, "invalid size"), fallback.read_bytes()])

        thumbnails.generate_thumbnail(
            "prompt",
            output,
            model="gpt-image-2.5-flare",
            size="1080x1920",
            quality="high",
            api_key="sk-test",
            client=client,
        )

        assert client.images.sizes == ["1080x1920", "1024x1536"]
        assert thumbnails.image_dimensions(output) == (1080, 1920)
        # The whole 1024x1536 foreground fits inside 1080x1920; the corners are
        # the blurred fill, so the centre is the green foreground.
        info = ffmpeg.probe(output)
        assert info.width == 1080
        assert info.height == 1920

    def test_failure_keeps_the_previous_image(self, png, tmp_path) -> None:
        output = tmp_path / "thumbnail.png"
        output.write_bytes(png(1080, 1920).read_bytes())
        before = output.read_bytes()
        client = FakeClient([FakeStatusError(429, "rate limit")])

        with pytest.raises(thumbnails.ThumbnailError) as caught:
            thumbnails.generate_thumbnail(
                "prompt",
                output,
                model="gpt-image-2.5-flare",
                size="1080x1920",
                quality="high",
                api_key="sk-test",
                client=client,
            )

        assert caught.value.status_code == 429
        assert output.read_bytes() == before
        assert not output.with_suffix(".tmp.png").exists()

    def test_401_has_an_actionable_hint(self, tmp_path) -> None:
        client = FakeClient([FakeStatusError(401, "bad key")])

        with pytest.raises(thumbnails.ThumbnailError) as caught:
            thumbnails.generate_thumbnail(
                "prompt",
                tmp_path / "x.png",
                model="m",
                size="1080x1920",
                quality="high",
                api_key="sk-test",
                client=client,
            )

        assert caught.value.status_code == 401
        assert "Settings → Keys → OpenAI Images" in caught.value.hint

    def test_timeout_maps_to_504(self, tmp_path) -> None:
        class APITimeoutError(Exception):
            pass

        client = FakeClient([APITimeoutError("timed out")])

        with pytest.raises(thumbnails.ThumbnailError) as caught:
            thumbnails.generate_thumbnail(
                "prompt",
                tmp_path / "x.png",
                model="m",
                size="1080x1920",
                quality="high",
                api_key="sk-test",
                client=client,
            )

        assert caught.value.status_code == 504

    def test_missing_key_does_not_call_the_api(self, tmp_path) -> None:
        with pytest.raises(thumbnails.ThumbnailError) as caught:
            thumbnails.generate_thumbnail(
                "prompt",
                tmp_path / "x.png",
                model="m",
                size="1080x1920",
                quality="high",
                api_key="",
            )

        assert caught.value.status_code == 400
        assert "OpenAI Images" in caught.value.hint

    def test_empty_prompt_is_rejected(self, tmp_path) -> None:
        with pytest.raises(thumbnails.ThumbnailError, match="prompt is empty"):
            thumbnails.generate_thumbnail(
                "   ",
                tmp_path / "x.png",
                model="m",
                size="1080x1920",
                quality="high",
                api_key="sk-test",
            )

    def test_non_png_bytes_are_rejected(self, tmp_path) -> None:
        client = FakeClient([b"not a png"])

        with pytest.raises(thumbnails.ThumbnailError, match="not a PNG"):
            thumbnails.generate_thumbnail(
                "prompt",
                tmp_path / "x.png",
                model="m",
                size="1080x1920",
                quality="high",
                api_key="sk-test",
                client=client,
            )

    def test_wrong_dimensions_are_rejected(self, png, tmp_path) -> None:
        client = FakeClient([png(1024, 1536).read_bytes()])

        with pytest.raises(thumbnails.ThumbnailError, match="OpenAI returned 1024x1536"):
            thumbnails.generate_thumbnail(
                "prompt",
                tmp_path / "x.png",
                model="m",
                size="1080x1920",
                quality="high",
                api_key="sk-test",
                client=client,
            )

    def test_url_response_is_downloaded(self, png, tmp_path, monkeypatch) -> None:
        source = png(1080, 1920)
        downloaded = SimpleNamespace(content=source.read_bytes(), raise_for_status=lambda: None)
        monkeypatch.setattr("httpx.get", lambda *a, **k: downloaded)
        client = FakeClient(["https://example.invalid/image.png"])

        output = tmp_path / "thumbnail.png"
        thumbnails.generate_thumbnail(
            "prompt",
            output,
            model="m",
            size="1080x1920",
            quality="high",
            api_key="sk-test",
            client=client,
        )

        assert thumbnails.image_dimensions(output) == (1080, 1920)


class TestWide:
    def test_derives_1280x720_under_two_megabytes(self, png, tmp_path) -> None:
        source = png(1080, 1920)
        wide = tmp_path / "thumbnail-wide.jpg"

        thumbnails.derive_wide(source, wide)

        assert thumbnails.image_dimensions(wide) == (1280, 720)
        assert wide.stat().st_size <= thumbnails.MAX_WIDE_BYTES

    def test_missing_source_raises_thumbnail_error(self, tmp_path) -> None:
        with pytest.raises(thumbnails.ThumbnailError):
            thumbnails.derive_wide(tmp_path / "missing.png", tmp_path / "wide.jpg")


class TestSizeParsing:
    def test_parses_width_height(self) -> None:
        assert thumbnails.parse_size("1080x1920") == (1080, 1920)

    def test_rejects_junk(self) -> None:
        with pytest.raises(thumbnails.ThumbnailError, match="Invalid image size"):
            thumbnails.parse_size("large")
