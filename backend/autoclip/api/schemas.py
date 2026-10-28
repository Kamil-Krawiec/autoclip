"""Pydantic request and response models for the REST API.

Kept separate from the dataclasses in ``db.models``: those mirror SQLite rows,
these are the wire contract. Splitting them means a schema change doesn't
silently reshape the API, and the API can expose derived fields (durations,
export URLs) that don't belong in storage.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..db import models
from ..pipeline.framing import MAX_FRAMING_SLICES, SLICE_CONTIGUITY_TOLERANCE_S


class SourceOut(BaseModel):
    id: str
    type: str
    title: str
    url: str | None = None
    filename: str | None = None
    channel: str | None = None
    duration_s: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool
    has_video: bool
    library_path: str | None = None
    created_at: str

    @classmethod
    def of(cls, source: models.Source) -> SourceOut:
        # Built field by field rather than from vars(): the row carries an
        # absolute filesystem path that has no business leaving the process.
        return cls(
            id=source.id,
            type=source.type,
            title=source.title,
            url=source.url,
            filename=source.filename,
            channel=source.channel,
            duration_s=source.duration_s,
            width=source.width,
            height=source.height,
            fps=source.fps,
            has_audio=source.has_audio,
            has_video=source.has_video,
            library_path=source.library_path,
            created_at=source.created_at,
        )


class LibraryEntryOut(BaseModel):
    path: str
    name: str
    is_dir: bool
    size_bytes: int | None = None


class LibraryOut(BaseModel):
    available: bool
    directory: str
    entries: list[LibraryEntryOut]
    truncated: bool


class LibraryImportIn(BaseModel):
    path: str


class YouTubeIngestIn(BaseModel):
    url: str
    cookies_from_browser: str | None = None


class JobSettingsIn(BaseModel):
    """Per-job overrides. Anything omitted falls back to saved settings."""

    provider: str | None = None
    whisper_model: str | None = None
    language: str | None = None
    diarization: bool | None = None
    min_duration_s: float | None = Field(default=None, gt=0)
    max_duration_s: float | None = Field(default=None, gt=0)
    max_clips: int | None = Field(default=None, ge=1, le=50)
    caption_style: str | None = None
    ratio: Literal["9:16", "1:1", "16:9"] | None = None


class JobCreateIn(BaseModel):
    source_id: str
    settings: JobSettingsIn = Field(default_factory=JobSettingsIn)


class JobOut(BaseModel):
    id: str
    source_id: str
    status: str
    current_stage: str
    progress: float
    error: str | None = None
    provider: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    source: SourceOut | None = None

    @classmethod
    def of(cls, job: models.Job, source: models.Source | None = None) -> JobOut:
        return cls(
            id=job.id,
            source_id=job.source_id,
            status=job.status,
            current_stage=job.current_stage,
            progress=job.progress,
            error=job.error,
            provider=job.provider,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            source=SourceOut.of(source) if source else None,
        )


class WordOut(BaseModel):
    text: str
    start: float
    end: float
    speaker: str | None = None


class ExportOut(BaseModel):
    id: str
    clip_id: str
    ratio: str
    style: str
    size_bytes: int
    created_at: str
    download_url: str

    @classmethod
    def of(cls, export: models.Export) -> ExportOut:
        return cls(
            id=export.id,
            clip_id=export.clip_id,
            ratio=export.ratio,
            style=export.style,
            size_bytes=export.size_bytes,
            created_at=export.created_at,
            download_url=f"/api/exports/{export.id}/download",
        )


class AutoFitFramingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "fit", "split"]


class ManualFramingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["manual"]
    center_x: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    center_y: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    zoom: float = Field(ge=1, le=4, allow_inf_nan=False, strict=True)


LeafFramingIn = Annotated[AutoFitFramingIn | ManualFramingIn, Field(discriminator="mode")]


class SliceSegmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_s: float = Field(ge=0, allow_inf_nan=False, strict=True)
    end_s: float = Field(gt=0, allow_inf_nan=False, strict=True)
    framing: LeafFramingIn

    @model_validator(mode="after")
    def _check_positive_duration(self) -> SliceSegmentIn:
        if self.end_s <= self.start_s:
            raise ValueError("Slice end_s must be greater than start_s.")
        return self


class SlicesFramingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["slices"]
    segments: list[SliceSegmentIn] = Field(min_length=1, max_length=MAX_FRAMING_SLICES)

    @model_validator(mode="after")
    def _check_contiguous_sorted(self) -> SlicesFramingIn:
        for prev, curr in zip(self.segments, self.segments[1:], strict=False):
            if curr.start_s < prev.start_s:
                raise ValueError("Slice segments must be sorted by start_s.")
            if abs(curr.start_s - prev.end_s) > SLICE_CONTIGUITY_TOLERANCE_S:
                raise ValueError("Slice segments must be contiguous without gaps or overlaps.")
        return self


FramingIn = Annotated[
    AutoFitFramingIn | ManualFramingIn | SlicesFramingIn,
    Field(discriminator="mode"),
]


class ClipOut(BaseModel):
    id: str
    job_id: str
    rank: int
    start_s: float
    end_s: float
    duration_s: float
    start_word: int
    end_word: int
    title: str
    hook: str
    score: int
    reason: str
    status: str
    user_trimmed: bool
    caption_style: str = "bold_pop"
    ratio: str = "9:16"
    burn_captions: bool = True
    framing: FramingIn | None = None
    caption_overrides: dict[str, float] | None = None
    exports: list[ExportOut] = Field(default_factory=list)

    @classmethod
    def of(
        cls,
        clip: models.Clip,
        *,
        edit: models.ClipEdit | None = None,
        exports: list[models.Export] | None = None,
    ) -> ClipOut:
        return cls(
            id=clip.id,
            job_id=clip.job_id,
            rank=clip.rank,
            start_s=clip.start_s,
            end_s=clip.end_s,
            duration_s=clip.duration_s,
            start_word=clip.start_word,
            end_word=clip.end_word,
            title=clip.title,
            hook=clip.hook,
            score=clip.score,
            reason=clip.reason,
            status=clip.status,
            user_trimmed=clip.user_trimmed,
            caption_style=edit.caption_style if edit else "bold_pop",
            ratio=edit.ratio if edit else "9:16",
            burn_captions=edit.burn_captions if edit else True,
            framing=edit.framing if edit else None,
            caption_overrides=edit.caption_overrides if edit else None,
            exports=[ExportOut.of(e) for e in (exports or [])],
        )


class ClipPatchIn(BaseModel):
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, gt=0)
    title: str | None = None
    status: Literal["candidate", "kept", "discarded", "exported"] | None = None


class CaptionPatchIn(BaseModel):
    """Word-level caption edits for one clip."""

    words: list[WordOut] | None = None
    caption_style: str | None = None
    ratio: Literal["9:16", "1:1", "16:9"] | None = None


class CaptionOverridesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size_ratio: float | None = Field(
        default=None, ge=0.02, le=0.10, strict=True, allow_inf_nan=False
    )
    position_x: float | None = Field(
        default=None, ge=0.10, le=0.90, strict=True, allow_inf_nan=False
    )
    position_y: float | None = Field(
        default=None, ge=0.10, le=0.95, strict=True, allow_inf_nan=False
    )


class EditPatchIn(CaptionPatchIn):
    model_config = ConfigDict(extra="forbid")
    burn_captions: bool = True
    framing: FramingIn | None = None
    caption_overrides: CaptionOverridesIn | None = None


class ExportRequestIn(BaseModel):
    ratio: Literal["9:16", "1:1", "16:9"] | None = None
    style: str | None = None
    write_srt: bool = False


class PreviewRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_s: float = Field(default=0.0, ge=0, allow_inf_nan=False, strict=True)
    duration_s: float = Field(default=3.0, gt=0, le=5.0, allow_inf_nan=False, strict=True)


class CaptionStyleOut(BaseModel):
    key: str
    label: str
    description: str
    #: Enough for the UI to approximate the look in HTML/CSS before rendering.
    preview: dict[str, Any]


class ProviderStatusOut(BaseModel):
    name: str
    available: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)
    requires_key: bool = True
    has_key: bool = False


class SettingsOut(BaseModel):
    active_provider: str
    providers: dict[str, dict[str, Any]]
    whisper: dict[str, Any]
    clips: dict[str, Any]
    ingest: dict[str, Any]
    export: dict[str, Any]
    insecure_secret_storage: bool
    #: Which providers have a key stored. The keys themselves never leave the
    #: keyring, so the UI shows presence, not value.
    keys_present: dict[str, bool] = Field(default_factory=dict)


class SettingsIn(BaseModel):
    active_provider: str | None = None
    providers: dict[str, dict[str, Any]] | None = None
    whisper: dict[str, Any] | None = None
    clips: dict[str, Any] | None = None
    ingest: dict[str, Any] | None = None
    export: dict[str, Any] | None = None


class SecretIn(BaseModel):
    key: str
    value: str


class SystemOut(BaseModel):
    ready: bool
    python_version: str
    platform: str
    ffmpeg_version: str | None
    has_libass: bool
    nvenc_works: bool
    accel: str
    gpu_name: str | None
    compute_type: str
    diarization_available: bool
