"""Generate the 9:16 thumbnail with OpenAI Images, plus its 16:9 JPEG sibling.

The generated image is never cropped during normalisation. tcap scales the
still to the Short's frame, so cutting the foreground can eat the safe margin
and the text the prompt asked to keep. When the configured size is rejected we
pad a fallback render over a blurred background instead.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import openai

from ..pipeline import ffmpeg

log = logging.getLogger(__name__)

#: The safe-size fallback if a model rejects an arbitrary WxH request.
FALLBACK_SIZE = "1024x1536"
WIDE_SIZE = (1280, 720)
MAX_WIDE_BYTES = 2 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ThumbnailError(RuntimeError):
    """Thumbnail generation or validation failed."""

    def __init__(self, message: str, *, hint: str = "", status_code: int = 502) -> None:
        super().__init__(message)
        self.hint = hint
        self.status_code = status_code

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n\n{self.hint}" if self.hint else base


def parse_size(size: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2,4})x(\d{2,4})", size.strip())
    if match is None:
        raise ThumbnailError(
            f"Invalid image size {size!r}.",
            hint="Use WIDTHxHEIGHT, for example 1080x1920.",
            status_code=400,
        )
    return int(match.group(1)), int(match.group(2))


def image_dimensions(path: Path) -> tuple[int, int]:
    info = ffmpeg.probe(path)
    if not info.has_video or info.width is None or info.height is None:
        raise ThumbnailError(f"{path.name} is not a readable image.")
    return info.width, info.height


def generate_thumbnail(
    prompt: str,
    output_png: Path,
    *,
    model: str,
    size: str,
    quality: str,
    api_key: str,
    client: Any | None = None,
) -> None:
    """Generate one PNG and atomically replace ``output_png``.

    ``client`` is injectable for tests; production builds a sync OpenAI client.
    On failure the previous image is left untouched because the new bytes only
    reach the destination through :func:`os.replace` after validation.
    """
    if not prompt.strip():
        raise ThumbnailError("The thumbnail prompt is empty.", status_code=400)
    target_w, target_h = parse_size(size)
    output_png.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_png.with_suffix(".tmp.png")

    if client is None:
        if not api_key:
            raise ThumbnailError(
                "No OpenAI Images API key is configured.",
                hint="Add it in Settings → Keys → OpenAI Images.",
                status_code=400,
            )
        client = openai.OpenAI(api_key=api_key, timeout=120.0, max_retries=1)

    try:
        png_bytes, used_size = _request_image(
            client, prompt=prompt, model=model, size=size, quality=quality
        )
        tmp.write_bytes(png_bytes)
        _validate_png(tmp)

        if used_size != size:
            padded = output_png.with_suffix(".padded.png")
            try:
                pad_to_size(tmp, padded, size)
                os.replace(padded, tmp)
            finally:
                padded.unlink(missing_ok=True)

        actual = image_dimensions(tmp)
        if actual != (target_w, target_h):
            raise ThumbnailError(
                f"OpenAI returned {actual[0]}x{actual[1]}, expected {target_w}x{target_h}."
            )

        os.replace(tmp, output_png)
    except ThumbnailError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - provider SDK raises a wide family
        tmp.unlink(missing_ok=True)
        raise _translate(exc, model) from exc


def _request_image(
    client: Any,
    *,
    prompt: str,
    model: str,
    size: str,
    quality: str,
) -> tuple[bytes, str]:
    """Call the Images API, retrying once with the fallback size on a 400."""
    try:
        return _generate_once(client, prompt=prompt, model=model, size=size, quality=quality), size
    except Exception as exc:  # noqa: BLE001
        if _is_size_rejection(exc):
            log.warning(
                "Image model %s rejected size %s; retrying with %s and padding.",
                model,
                size,
                FALLBACK_SIZE,
            )
            try:
                return (
                    _generate_once(
                        client, prompt=prompt, model=model, size=FALLBACK_SIZE, quality=quality
                    ),
                    FALLBACK_SIZE,
                )
            except Exception as retry_exc:  # noqa: BLE001
                raise _translate(retry_exc, model) from retry_exc
        raise


def _generate_once(
    client: Any,
    *,
    prompt: str,
    model: str,
    size: str,
    quality: str,
) -> bytes:
    response = client.images.generate(
        model=model,
        prompt=prompt,
        size=size,
        quality=quality,
        n=1,
    )
    data = getattr(response, "data", None) or []
    if not data:
        raise ThumbnailError("OpenAI returned no image data.")
    item = data[0]
    encoded = getattr(item, "b64_json", None)
    if encoded:
        return base64.b64decode(encoded)
    url = getattr(item, "url", None)
    if url:
        import httpx

        downloaded = httpx.get(url, timeout=120.0, follow_redirects=True)
        downloaded.raise_for_status()
        return downloaded.content
    raise ThumbnailError("OpenAI returned neither b64_json nor a URL.")


def _validate_png(path: Path) -> None:
    with path.open("rb") as handle:
        signature = handle.read(len(PNG_SIGNATURE))
    if signature != PNG_SIGNATURE:
        raise ThumbnailError("The generated file is not a PNG.")


def pad_to_size(source: Path, destination: Path, size: str) -> None:
    """Fit the whole foreground over a blurred fill; never crop it."""
    width, height = parse_size(size)
    filtergraph = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:2[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    try:
        ffmpeg.run(
            [
                "-i",
                str(source),
                "-filter_complex",
                filtergraph,
                "-frames:v",
                "1",
                "-update",
                "1",
                str(destination),
            ]
        )
    except ffmpeg.FFmpegError as exc:
        raise ThumbnailError(f"Could not pad the thumbnail.\n{exc}") from exc


def derive_wide(png: Path, jpg: Path) -> None:
    """Write the 1280x720 JPEG used by ``thumbnails.set``."""
    width, height = WIDE_SIZE
    filtergraph = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:2[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    jpg.parent.mkdir(parents=True, exist_ok=True)
    try:
        for quality in (2, 4, 6, 8):
            ffmpeg.run(
                [
                    "-i",
                    str(png),
                    "-filter_complex",
                    filtergraph,
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    "-q:v",
                    str(quality),
                    str(jpg),
                ]
            )
            if jpg.stat().st_size <= MAX_WIDE_BYTES:
                break
        else:
            raise ThumbnailError("The wide JPEG is still larger than 2 MB.")
    except ffmpeg.FFmpegError as exc:
        raise ThumbnailError(f"Could not derive the wide thumbnail.\n{exc}") from exc

    if image_dimensions(jpg) != WIDE_SIZE:
        raise ThumbnailError("The wide thumbnail has the wrong dimensions.")


def _is_size_rejection(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    message = str(exc).lower()
    return status == 400 and any(
        token in message for token in ("size", "resolution", "dimension", "width", "height")
    )


def _translate(exc: Exception, model: str) -> ThumbnailError:
    status = getattr(exc, "status_code", None)
    message = str(exc)
    lowered = message.lower()

    if status == 401 or "invalid api key" in lowered or "incorrect api key" in lowered:
        return ThumbnailError(
            "OpenAI rejected the Images API key.",
            hint="Add the key in Settings → Keys → OpenAI Images.",
            status_code=401,
        )
    if status == 429 or "rate limit" in lowered:
        return ThumbnailError(
            "OpenAI Images rate limit reached.",
            hint="Wait and retry, or check your OpenAI usage.",
            status_code=429,
        )
    if status == 400:
        return ThumbnailError(
            f"The image model did not accept the request: {message}",
            hint=(
                "The configured image model may not support this size. Set "
                "image_size to 1024x1536 or choose a 2.5 model in Settings → Publishing."
            ),
            status_code=400,
        )
    if status == 404 or "model_not_found" in lowered or "does not exist" in lowered:
        return ThumbnailError(
            f"OpenAI does not recognise the image model '{model}'.",
            hint="Pick a model your account can use in Settings → Publishing.",
            status_code=404,
        )
    if _is_timeout(exc):
        return ThumbnailError(
            "The image request timed out.",
            hint="Retry; if it keeps timing out, try a faster image model.",
            status_code=504,
        )
    return ThumbnailError(f"Could not generate the thumbnail: {message}")


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


def cleanup_partial(output_png: Path) -> None:
    """Remove a temp file left by a hard crash (not by the normal error path)."""
    for name in (output_png.with_suffix(".tmp.png"), output_png.with_suffix(".padded.png")):
        with contextlib.suppress(OSError):
            name.unlink(missing_ok=True)
