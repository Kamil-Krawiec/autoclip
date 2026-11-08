"""Metadata prompt, provider override, and platform-limit normalisation."""

from __future__ import annotations

import json

import pytest
from autoclip import config
from autoclip.db.models import Clip, ClipEdit
from autoclip.pipeline.transcript import Transcript, Word
from autoclip.providers.base import (
    DetectionConfig,
    LLMProvider,
    ProviderError,
    ProviderStatus,
    load_prompt,
)
from autoclip.publishing import metadata


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, replies: list[str]) -> None:
        super().__init__("fake-model")
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def _complete(self, system: str, user: str, config: DetectionConfig) -> str:
        self.calls.append((system, user))
        return self.replies.pop(0)

    async def health_check(self) -> ProviderStatus:
        return ProviderStatus(name=self.name, available=True)


def _reply(**overrides) -> str:
    payload = {
        "title": "Konkretny tytuł",
        "description": "Opis klipu.",
        "tags": ["python", "automatyzacja"],
        "thumbnail_prompt": 'A vertical image. Polish text "TEST" in the middle.',
        "best_day": "niedziela",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _settings() -> config.Settings:
    return config.load()


class TestTitle:
    def test_collapses_whitespace_and_strips_angle_brackets(self) -> None:
        assert metadata.normalize_title("  <b>  Dobry\n\ttytuł  </b> ") == "b Dobry tytuł /b"

    def test_truncates_at_a_word_boundary(self) -> None:
        title = "słowo " * 30

        normalized = metadata.normalize_title(title)

        assert len(normalized) <= 100
        assert not normalized.endswith(" ")

    def test_empty_falls_back_to_a_placeholder(self) -> None:
        assert metadata.normalize_title("   ") == "Nowy short"

    def test_does_not_add_a_hashtag_suffix(self) -> None:
        assert "#shorts" not in metadata.normalize_title("Zwykły tytuł")


class TestDescription:
    def test_trims_to_utf8_bytes_not_characters(self) -> None:
        # Each 'ą' is two UTF-8 bytes, so 3,000 characters is 6,000 bytes.
        normalized = metadata.normalize_description("ą" * 3_000)

        assert len(normalized.encode("utf-8")) <= 5_000
        assert normalized  # still valid text, not a mojibake prefix

    def test_angle_brackets_become_guillemets(self) -> None:
        assert metadata.normalize_description("a < b > c") == "a ‹ b › c"

    def test_empty_is_allowed(self) -> None:
        assert metadata.normalize_description("   ") == ""


class TestTags:
    def test_cleans_lowercases_and_dedupes_preserving_order(self) -> None:
        tags = metadata.normalize_tags(["#Python", "  uczenie   maszynowe ", "python", "", "#AI"])

        assert tags == ["python", "uczenie maszynowe", "ai"]

    def test_counts_quotes_and_commas_toward_the_limit(self) -> None:
        assert metadata.tags_wire_length(["a", "b c"]) == 1 + (3 + 2) + 1

    def test_trims_from_the_end_until_the_limit_fits(self) -> None:
        raw = [f"tag-{index:03d}" for index in range(100)]

        tags = metadata.normalize_tags(raw)

        assert 0 < len(tags) < len(raw)
        assert metadata.tags_wire_length(tags) <= metadata.MAX_TAGS_CHARS


class TestSchema:
    def test_coerces_null_numbers_and_comma_strings(self) -> None:
        parsed = metadata.PublishMetadata.model_validate(
            {
                "title": 42,
                "description": None,
                "tags": "#a, b",
                "thumbnail_prompt": None,
                "best_day": None,
            }
        )

        assert parsed.title == "42"
        assert parsed.description == ""
        assert parsed.tags == ["#a", " b"]
        assert parsed.thumbnail_prompt == ""
        assert parsed.best_day is None

    def test_requires_title_and_thumbnail_prompt(self) -> None:
        with pytest.raises(ValueError):
            metadata.PublishMetadata.model_validate({"description": "only description"})


class TestClipTranscript:
    def test_uses_edited_words_filtered_to_the_clip_and_sorted(self) -> None:
        clip = Clip(id="c", job_id="j", start_s=10.0, end_s=20.0, start_word=0, end_word=1)
        edit = ClipEdit(
            clip_id="c",
            edited_words=[
                {"text": "po", "start": 15.0, "end": 16.0},
                {"text": "przed", "start": 5.0, "end": 6.0},
                {"text": "korekta", "start": 11.0, "end": 12.0},
                {"text": "granica", "start": 19.5, "end": 20.5},
            ],
        )
        transcript = Transcript(words=[Word("oryginał", 10.0, 20.0)])

        text = metadata.clip_transcript_text(edit, transcript, clip)

        assert text == "korekta po granica"

    def test_falls_back_to_the_transcript_slice(self) -> None:
        transcript = Transcript(
            words=[
                Word("zero", 0.0, 1.0),
                Word("jeden", 1.0, 2.0),
                Word("dwa", 2.0, 3.0),
            ]
        )
        clip = Clip(id="c", job_id="j", start_s=1.0, end_s=3.0, start_word=1, end_word=2)

        assert metadata.clip_transcript_text(None, transcript, clip) == "jeden dwa"

    def test_empty_edited_words_is_not_a_fallback_signal(self) -> None:
        clip = Clip(id="c", job_id="j", start_s=0.0, end_s=1.0, start_word=0, end_word=0)
        transcript = Transcript(words=[Word("słowo", 0.0, 1.0)])

        assert (
            metadata.clip_transcript_text(ClipEdit(clip_id="c", edited_words=[]), transcript, clip)
            == ""
        )


class TestTranscriptCap:
    def test_short_text_is_untouched(self) -> None:
        assert metadata.cap_transcript_text("krótko") == "krótko"

    def test_long_text_keeps_head_and_tail(self) -> None:
        text = "A" * 15_000 + "B" * 15_000

        capped = metadata.cap_transcript_text(text)

        assert len(capped) <= metadata.MAX_TRANSCRIPT_CHARS
        assert capped.startswith("A")
        assert capped.endswith("B")


class TestPrompt:
    def test_prompt_has_the_safe_area_rule_and_no_forced_suffix(self) -> None:
        prompt = load_prompt(metadata.PROMPT_VERSION)

        assert "8–10%" in prompt
        assert "every edge" in prompt
        assert "#shorts" not in prompt
        assert "There is no required keyword count" in prompt


class TestGenerateMetadata:
    async def test_returns_normalized_metadata(self) -> None:
        reply = _reply(
            title="x" * 150,
            description="ą" * 3_000,
            tags=[f"tag-{index:03d}" for index in range(100)],
        )
        provider = FakeProvider([reply])

        result = await metadata.generate_metadata(
            source_title="Źródło",
            clip_title="Klip",
            hook="Hook",
            duration_s=42.0,
            transcript_text="treść",
            settings=_settings(),
            provider=provider,
        )

        assert len(result.title) <= metadata.MAX_TITLE_CHARS
        assert len(result.description.encode("utf-8")) <= metadata.MAX_DESCRIPTION_BYTES
        assert metadata.tags_wire_length(result.tags) <= metadata.MAX_TAGS_CHARS
        assert "8–10%" in provider.calls[0][0]
        assert "SOURCE_TITLE: Źródło" in provider.calls[0][1]

    async def test_retries_once_with_the_validation_error(self) -> None:
        provider = FakeProvider(["not json", _reply()])

        result = await metadata.generate_metadata(
            source_title="Źródło",
            clip_title="Klip",
            hook="",
            duration_s=10.0,
            transcript_text="treść",
            settings=_settings(),
            provider=provider,
        )

        assert result.title == "Konkretny tytuł"
        assert len(provider.calls) == 2
        assert "did not match the required schema" in provider.calls[1][1]

    async def test_two_bad_replies_raise_provider_error(self) -> None:
        provider = FakeProvider(["nope", "still nope"])

        with pytest.raises(ProviderError, match="malformed metadata twice"):
            await metadata.generate_metadata(
                source_title="Źródło",
                clip_title="Klip",
                hook="",
                duration_s=10.0,
                transcript_text="treść",
                settings=_settings(),
                provider=provider,
            )

    async def test_empty_description_is_preserved(self) -> None:
        provider = FakeProvider([_reply(description="")])

        result = await metadata.generate_metadata(
            source_title="Źródło",
            clip_title="Klip",
            hook="",
            duration_s=10.0,
            transcript_text="treść",
            settings=_settings(),
            provider=provider,
        )

        assert result.description == ""


class TestProviderResolution:
    def test_default_is_openai_with_the_publish_model(self) -> None:
        name, model = metadata.resolve_metadata_provider(_settings())

        assert name == "openai"
        assert model == "gpt-6-luna"

    def test_empty_provider_follows_the_active_provider_and_ignores_the_model(self) -> None:
        settings = _settings()
        settings.active_provider = "ollama"
        settings.publish.metadata_provider = ""
        settings.publish.metadata_model = "gpt-6-luna"

        name, model = metadata.resolve_metadata_provider(settings)

        assert name == "ollama"
        assert model is None

    def test_empty_model_uses_the_provider_default(self, monkeypatch) -> None:
        settings = _settings()
        settings.publish.metadata_model = ""
        captured: dict[str, object] = {}

        def fake_build_provider(name, configured, *, model=None):
            captured.update(name=name, settings=configured, model=model)
            return FakeProvider([_reply()])

        monkeypatch.setattr(metadata, "build_provider", fake_build_provider)

        async def run() -> None:
            await metadata.generate_metadata(
                source_title="Źródło",
                clip_title="Klip",
                hook="",
                duration_s=10.0,
                transcript_text="treść",
                settings=settings,
            )

        import asyncio

        asyncio.run(run())

        assert captured["name"] == "openai"
        assert captured["model"] is None
