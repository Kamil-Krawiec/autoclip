"""Generate Polish YouTube metadata from a clip transcript.

Two rules shape this module:

**The model owns the wording.** The old pipeline forced `` #shorts``, keyword
lines and a tag count; none of that is required by YouTube and it made every
description look the same. The prompt is open-ended and the model decides the
angle. Normalisation only enforces the platform's hard limits.

**The platform limits live in one place.** Title length, description *bytes*
(Polish is multi-byte) and YouTube's tags-length rule are implemented here and
reused by the API and the upload client, so the three cannot drift apart.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..config import Settings
from ..providers import build_provider
from ..providers.base import DetectionConfig, LLMProvider, load_prompt

log = logging.getLogger(__name__)

PROMPT_VERSION = "publish_v1"

#: YouTube's documented hard limits.
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_BYTES = 5_000
MAX_TAGS_CHARS = 500
MAX_PROMPT_CHARS = 4_000
MAX_TRANSCRIPT_CHARS = 20_000


class PublishMetadata(BaseModel):
    """The JSON object the metadata model returns, before platform trimming."""

    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    thumbnail_prompt: str
    best_day: str | None = None

    @field_validator("title", "description", "thumbnail_prompt", mode="before")
    @classmethod
    def _coerce_text(cls, value: Any) -> str:
        # Models occasionally return null or a number where text was asked for.
        return "" if value is None else str(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return list(value.split(","))
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if item is not None]
        return [str(value)]

    @field_validator("best_day", mode="before")
    @classmethod
    def _coerce_best_day(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


def normalize_title(raw: str) -> str:
    """Apply YouTube's title limit without adding editorial suffixes."""
    text = " ".join(raw.split()).replace("<", "").replace(">", "").strip()
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(" ,-")
    return text or "Nowy short"


def normalize_description(raw: str) -> str:
    """Trim a description to 5,000 UTF-8 bytes, not characters.

    ``<``/``>`` are rejected by the YouTube API, so they become guillemets
    rather than silently deleting the character.
    """
    text = raw.replace("<", "‹").replace(">", "›").strip()
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_DESCRIPTION_BYTES:
        text = encoded[:MAX_DESCRIPTION_BYTES].decode("utf-8", "ignore").rstrip()
    return text


def _clean_tag(raw: str) -> str:
    # Leading '#' is a hashtag marker, not part of a tag. Lowercasing keeps the
    # list consistent; spaces are collapsed because YouTube quotes such tags.
    text = " ".join(str(raw).strip().lstrip("#").split())
    return text.lower()


def tags_wire_length(tags: list[str]) -> int:
    """YouTube's tag budget: text, plus quotes around spaced tags, plus commas."""
    if not tags:
        return 0
    return sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + (len(tags) - 1)


def normalize_tags(raw: list[str], *, trim: bool = True) -> list[str]:
    """Clean, de-duplicate and (optionally) trim tags to YouTube's limit.

    ``trim=False`` is for user-edited tags: the API rejects an over-budget list
    rather than silently dropping the user's last entries.
    """
    tags: list[str] = []
    for item in raw:
        cleaned = _clean_tag(item)
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    if trim:
        while tags and tags_wire_length(tags) > MAX_TAGS_CHARS:
            tags.pop()
    return tags


def normalize_prompt(raw: str) -> str:
    text = raw.strip()
    return text[:MAX_PROMPT_CHARS].rstrip()


def cap_transcript_text(text: str, limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Keep the head and tail of a very long transcript.

    A model does not need every word of a 90-second clip, but a hard cut at the
    front would lose the payoff at the end, which is what the title usually
    comes from.
    """
    if len(text) <= limit:
        return text
    separator = "\n…\n"
    head = limit * 3 // 4
    tail = max(0, limit - head - len(separator))
    return f"{text[:head].rstrip()}{separator}{text[-tail:].lstrip()}"


def _word_time(word: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(word.get(key, default))
    except (TypeError, ValueError):
        return default


def clip_transcript_text(edit: Any, transcript: Any, clip: Any) -> str:
    """The clip's words, preferring the user's edited caption text.

    ``edit.edited_words`` is authoritative whenever it exists: the publication
    should describe what the exported video actually says, including caption
    corrections. Words are filtered to the clip's time span because the edit
    usually carries the whole clip, not the source.
    """
    edited = getattr(edit, "edited_words", None)
    if edited is not None:
        words = [
            word
            for word in edited
            if _word_time(word, "start", 0.0) < clip.end_s
            and _word_time(word, "end", 0.0) > clip.start_s
        ]
        words.sort(key=lambda word: _word_time(word, "start", 0.0))
        return " ".join(str(word.get("text", "")).strip() for word in words).strip()

    return transcript.text_between(clip.start_word, clip.end_word)


def resolve_metadata_provider(settings: Settings) -> tuple[str, str | None]:
    """Return ``(provider_name, model_override)`` for metadata generation.

    An empty ``metadata_provider`` means "follow the active provider", and in
    that mode the publish model is ignored — otherwise ``gpt-6-luna`` would be
    sent to Anthropic or Ollama and fail.
    """
    publish = settings.publish
    if publish.metadata_provider:
        return publish.metadata_provider, (publish.metadata_model or None)
    return settings.active_provider, None


def render_user_prompt(
    *,
    source_title: str,
    clip_title: str,
    hook: str,
    duration_s: float,
    transcript_text: str,
) -> str:
    return (
        f"SOURCE_TITLE: {source_title}\n"
        f"CLIP_TITLE: {clip_title}\n"
        f"HOOK: {hook}\n"
        f"DURATION_S: {duration_s:.1f}\n"
        f"TRANSCRIPT:\n{cap_transcript_text(transcript_text)}\n"
    )


async def generate_metadata(
    *,
    source_title: str,
    clip_title: str,
    hook: str,
    duration_s: float,
    transcript_text: str,
    settings: Settings,
    provider: LLMProvider | None = None,
) -> PublishMetadata:
    """Ask the configured model for metadata and normalise it to platform limits.

    ``provider`` is injectable for tests; production resolves it from settings.
    Provider/parse failures propagate as :class:`ProviderError`, which the API
    maps to a 502 with an actionable hint.
    """
    system = load_prompt(PROMPT_VERSION)
    user = render_user_prompt(
        source_title=source_title,
        clip_title=clip_title,
        hook=hook,
        duration_s=duration_s,
        transcript_text=transcript_text,
    )

    if provider is None:
        name, model = resolve_metadata_provider(settings)
        provider = build_provider(name, settings, model=model)

    raw = await provider.complete_json(
        system,
        user,
        PublishMetadata,
        config=DetectionConfig(temperature=0.7),
    )

    return PublishMetadata(
        title=normalize_title(raw.title),
        description=normalize_description(raw.description),
        tags=normalize_tags(raw.tags),
        thumbnail_prompt=normalize_prompt(raw.thumbnail_prompt),
        best_day=raw.best_day,
    )
