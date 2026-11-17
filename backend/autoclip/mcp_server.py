"""Local MCP adapter for the AutoClip API."""

import math
from urllib.parse import urlsplit


def validate_base_url(url: str) -> str:
    """Restrict the API origin to an explicit local HTTP listener."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid API port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("AutoClip API must be a local HTTP origin with an explicit port")
    return url.rstrip("/")


class AutoClipClient:
    """Call explicitly supported AutoClip operations over loopback HTTP."""

    def __init__(self, base_url, *, transport=None):
        import httpx

        self.client = httpx.Client(
            base_url=validate_base_url(base_url), transport=transport, timeout=30.0, trust_env=False
        )

    def get_clip(self, clip_id):
        result = self.request("GET", f"/api/clips/{self.identifier(clip_id)}")
        return self.mark_exports_unknown(result)

    def create_job(self, source_id, settings, *, allow_paid=False):
        source_id = self.identifier(source_id)
        if not allow_paid:
            raise ValueError("Job processing may incur cost; explicit consent is required")
        allowed = {
            "whisper_model",
            "language",
            "diarization",
            "min_duration_s",
            "max_duration_s",
            "max_clips",
            "caption_style",
            "ratio",
        }
        if not isinstance(settings, dict) or not set(settings) <= allowed:
            raise ValueError("Unsupported job setting")
        return self.request(
            "POST", "/api/jobs", body={"source_id": source_id, "settings": settings}
        )

    def request(self, method, path, *, body=None, params=None, timeout=30.0):
        response = self.client.request(method, path, json=body, params=params, timeout=timeout)
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except (ValueError, AttributeError):
                detail = response.text
            raise ValueError(f"AutoClip API {response.status_code}: {detail}")
        return response.json()

    @staticmethod
    def mark_exports_unknown(clip):
        for item in clip.get("exports", []):
            item["matches_current_edit"] = "unknown"
        return clip

    @staticmethod
    def identifier(value):
        import re

        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Invalid resource ID")
        return value

    def list_sources(self):
        return self.request("GET", "/api/sources")

    def ingest_youtube(self, url):
        return self.request("POST", "/api/sources/youtube", body={"url": url}, timeout=1800.0)

    def get_source(self, source_id):
        return self.request("GET", f"/api/sources/{self.identifier(source_id)}")

    def list_jobs(self):
        return self.request("GET", "/api/jobs")

    def get_job(self, job_id):
        return self.request("GET", f"/api/jobs/{self.identifier(job_id)}")

    def retry_job(self, job_id, *, allow_paid=False):
        if not allow_paid:
            raise ValueError("Retry may incur cost; explicit consent is required")
        return self.request("POST", f"/api/jobs/{self.identifier(job_id)}/retry")

    def list_clips(self, job_id):
        clips = self.request("GET", f"/api/jobs/{self.identifier(job_id)}/clips")
        return [self.mark_exports_unknown(clip) for clip in clips]

    def crop_path(self, clip_id):
        return self.request("GET", f"/api/clips/{self.identifier(clip_id)}/crop-path")

    def caption_styles(self):
        return self.request("GET", "/api/caption-styles")

    def update_clip(self, clip_id, changes):
        if not isinstance(changes, dict) or not set(changes) <= {"title", "status"}:
            raise ValueError("Unsupported clip field")
        return self.mark_exports_unknown(
            self.request("PATCH", f"/api/clips/{self.identifier(clip_id)}", body=changes)
        )

    def get_words(self, clip_id):
        return self.request("GET", f"/api/clips/{self.identifier(clip_id)}/words")

    def trim_clip(self, clip_id, start_s, end_s):
        if any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
            for x in (start_s, end_s)
        ):
            raise ValueError("Trim boundaries must be finite numbers")
        return self.mark_exports_unknown(
            self.request(
                "PATCH",
                f"/api/clips/{self.identifier(clip_id)}",
                body={"start_s": start_s, "end_s": end_s},
            )
        )

    def edit_clip(self, clip_id, edit):
        allowed = {
            "words",
            "caption_style",
            "ratio",
            "burn_captions",
            "framing",
            "caption_overrides",
            "removed_ranges",
        }
        if not isinstance(edit, dict) or not set(edit) <= allowed:
            raise ValueError("Unsupported edit field")
        return self.mark_exports_unknown(
            self.request("PATCH", f"/api/clips/{self.identifier(clip_id)}/edit", body=edit)
        )

    def inspect(self, clip_id, operation, options=None):
        endpoints = {
            "revision": "GET",
            "frames": "POST",
            "contact-sheet": "POST",
            "analyze-screen": "POST",
        }
        if operation not in endpoints:
            raise ValueError("Unsupported inspection operation")
        return self.request(
            endpoints[operation],
            f"/api/clips/{self.identifier(clip_id)}/inspection/{operation}",
            body=options if operation != "revision" else None,
            params=options if operation == "revision" else None,
            timeout=1800.0,
        )

    def export_clip(self, clip_id, options):
        if not isinstance(options, dict) or not set(options) <= {"ratio", "style", "write_srt"}:
            raise ValueError("Unsupported export field")
        result = self.request(
            "POST", f"/api/clips/{self.identifier(clip_id)}/export", body=options, timeout=1800.0
        )
        result["matches_current_edit"] = "unknown"
        return result


def main():
    import os
    from functools import partial

    import anyio
    from mcp.server.fastmcp import FastMCP

    client = AutoClipClient(os.environ.get("AUTOCLIP_API_URL", "http://127.0.0.1:9879"))
    server = FastMCP("AutoClip")

    @server.tool()
    async def sources() -> list:
        """List existing sources."""
        return await anyio.to_thread.run_sync(
            partial(
                client.list_sources,
            )
        )

    @server.tool()
    async def ingest_youtube(url: str) -> dict:
        """Download a YouTube source into the configured library."""
        return await anyio.to_thread.run_sync(partial(client.ingest_youtube, url))

    @server.tool()
    async def source(source_id: str) -> dict:
        """Get an existing source by ID."""
        return await anyio.to_thread.run_sync(partial(client.get_source, source_id))

    @server.tool()
    async def jobs() -> list:
        """List jobs and their statuses."""
        return await anyio.to_thread.run_sync(
            partial(
                client.list_jobs,
            )
        )

    @server.tool()
    async def job(job_id: str) -> dict:
        """Get job status by ID."""
        return await anyio.to_thread.run_sync(partial(client.get_job, job_id))

    @server.tool()
    async def create_job(source_id: str, settings: dict, allow_paid: bool = False) -> dict:
        """Create a job. Processing may incur costs; explicitly set allow_paid."""
        return await anyio.to_thread.run_sync(
            partial(client.create_job, source_id, settings, allow_paid=allow_paid)
        )

    @server.tool()
    async def retry_job(job_id: str, allow_paid: bool = False) -> dict:
        """Retry a failed job. Processing may incur costs; explicitly set allow_paid."""
        return await anyio.to_thread.run_sync(
            partial(client.retry_job, job_id, allow_paid=allow_paid)
        )

    @server.tool()
    async def clips(job_id: str) -> list:
        """List clips for a job."""
        return await anyio.to_thread.run_sync(partial(client.list_clips, job_id))

    @server.tool()
    async def clip(clip_id: str) -> dict:
        """Get a clip; export compatibility is unknown without a fingerprint."""
        return await anyio.to_thread.run_sync(partial(client.get_clip, clip_id))

    @server.tool()
    async def crop_path(clip_id: str) -> dict:
        """Read calculated crop path; timestamps use source seconds."""
        return await anyio.to_thread.run_sync(partial(client.crop_path, clip_id))

    @server.tool()
    async def caption_styles() -> list:
        """List available caption styles."""
        return await anyio.to_thread.run_sync(client.caption_styles)

    @server.tool()
    async def update_clip(clip_id: str, changes: dict) -> dict:
        """Update clip title or status."""
        return await anyio.to_thread.run_sync(partial(client.update_clip, clip_id, changes))

    @server.tool()
    async def transcript(clip_id: str) -> list:
        """Get clip words; timestamps are absolute source seconds."""
        return await anyio.to_thread.run_sync(partial(client.get_words, clip_id))

    @server.tool()
    async def trim(clip_id: str, start_s: float, end_s: float) -> dict:
        """Set clip boundaries in absolute source seconds."""
        return await anyio.to_thread.run_sync(partial(client.trim_clip, clip_id, start_s, end_s))

    @server.tool()
    async def edit(clip_id: str, changes: dict) -> dict:
        """Edit framing, captions or removed_ranges; slices, words and cuts use source seconds.

        Framing modes: auto, fit (blur), manual, split and slices. Use fit when unsure.
        Output time joins retained windows; clip time starts at clip start before cuts.
        """
        return await anyio.to_thread.run_sync(partial(client.edit_clip, clip_id, changes))

    @server.tool()
    async def export(clip_id: str, options: dict) -> dict:
        """Render the current edit. Export revision compatibility is unknown."""
        return await anyio.to_thread.run_sync(partial(client.export_clip, clip_id, options))

    @server.tool()
    async def inspection_revision(
        clip_id: str, ratio: str | None = None, style: str | None = None
    ) -> dict:
        """Get current revision and export freshness; legacy exports remain unknown."""
        return await anyio.to_thread.run_sync(
            partial(
                client.inspect,
                clip_id,
                "revision",
                {k: v for k, v in {"ratio": ratio, "style": style}.items() if v is not None},
            )
        )

    def image_content(result):
        import json

        from mcp.types import ImageContent, TextContent

        images = []

        def extract(value):
            if isinstance(value, dict):
                if "data_base64" in value and "mime_type" in value:
                    images.append(
                        ImageContent(
                            type="image", data=value.pop("data_base64"), mimeType=value["mime_type"]
                        )
                    )
                    value["content_index"] = len(images)
                for item in value.values():
                    extract(item)
            elif isinstance(value, list):
                for item in value:
                    extract(item)

        extract(result)
        return [TextContent(type="text", text=json.dumps(result)), *images]

    @server.tool()
    async def inspection_frames(clip_id: str, options: dict) -> list:
        """Inspect frames; timeline is source, clip, or retained output seconds."""
        return image_content(
            await anyio.to_thread.run_sync(partial(client.inspect, clip_id, "frames", options))
        )

    @server.tool()
    async def inspection_contact_sheet(clip_id: str, options: dict) -> list:
        """Inspect a contact sheet; timeline is source, clip, or retained output seconds."""
        return image_content(
            await anyio.to_thread.run_sync(
                partial(client.inspect, clip_id, "contact-sheet", options)
            )
        )

    @server.tool()
    async def inspection_analyze_screen(clip_id: str, options: dict | None = None) -> dict:
        """Get advisory CPU screen and framing analysis; fit blur is uncertain."""
        return await anyio.to_thread.run_sync(
            partial(client.inspect, clip_id, "analyze-screen", options or {})
        )

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
