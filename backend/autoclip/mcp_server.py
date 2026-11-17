"""Local MCP adapter for the AutoClip API."""

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
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]+", clip_id):
            raise ValueError("Invalid clip ID")
        response = self.client.get(f"/api/clips/{clip_id}")
        response.raise_for_status()
        return response.json()

    def create_job(self, source_id, settings, *, allow_paid=False):
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
            raise ValueError("Invalid source ID")
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
        response = self.client.post(
            "/api/jobs", json={"source_id": source_id, "settings": settings}
        )
        response.raise_for_status()
        return response.json()

    def request(self, method, path, *, body=None, timeout=30.0):
        response = self.client.request(method, path, json=body, timeout=timeout)
        response.raise_for_status()
        return response.json()

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
        if isinstance(clips, list):
            for clip in clips:
                for item in clip.get("exports", []):
                    item["matches_current_edit"] = "unknown"
        return clips

    def get_words(self, clip_id):
        return self.request("GET", f"/api/clips/{self.identifier(clip_id)}/words")

    def trim_clip(self, clip_id, start_s, end_s):
        return self.request(
            "PATCH",
            f"/api/clips/{self.identifier(clip_id)}",
            body={
                "start_s": start_s,
                "end_s": end_s,
            },
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
        return self.request("PATCH", f"/api/clips/{self.identifier(clip_id)}/edit", body=edit)

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

    from mcp.server.fastmcp import FastMCP

    client = AutoClipClient(os.environ.get("AUTOCLIP_API_URL", "http://127.0.0.1:9879"))
    server = FastMCP("AutoClip")

    @server.tool()
    def sources() -> list:
        """List existing sources."""
        return client.list_sources()

    @server.tool()
    def ingest_youtube(url: str) -> dict:
        """Download a YouTube source into the configured library."""
        return client.ingest_youtube(url)

    @server.tool()
    def source(source_id: str) -> dict:
        """Get an existing source by ID."""
        return client.get_source(source_id)

    @server.tool()
    def jobs() -> list:
        """List jobs and their statuses."""
        return client.list_jobs()

    @server.tool()
    def job(job_id: str) -> dict:
        """Get job status by ID."""
        return client.get_job(job_id)

    @server.tool()
    def create_job(source_id: str, settings: dict, allow_paid: bool = False) -> dict:
        """Create a job. Processing may incur costs; explicitly set allow_paid."""
        return client.create_job(source_id, settings, allow_paid=allow_paid)

    @server.tool()
    def retry_job(job_id: str, allow_paid: bool = False) -> dict:
        """Retry a failed job. Processing may incur costs; explicitly set allow_paid."""
        return client.retry_job(job_id, allow_paid=allow_paid)

    @server.tool()
    def clips(job_id: str) -> list:
        """List clips for a job."""
        return client.list_clips(job_id)

    @server.tool()
    def clip(clip_id: str) -> dict:
        """Get a clip; export compatibility is unknown without a fingerprint."""
        result = client.get_clip(clip_id)
        for item in result.get("exports", []):
            item["matches_current_edit"] = "unknown"
        return result

    @server.tool()
    def transcript(clip_id: str) -> list:
        """Get clip words; timestamps are absolute source seconds."""
        return client.get_words(clip_id)

    @server.tool()
    def trim(clip_id: str, start_s: float, end_s: float) -> dict:
        """Set clip boundaries in absolute source seconds."""
        return client.trim_clip(clip_id, start_s, end_s)

    @server.tool()
    def edit(clip_id: str, changes: dict) -> dict:
        """Edit framing, captions or removed_ranges; slices, words and cuts use source seconds.

        Framing modes: auto, fit (blur), manual, split and slices. Use fit when unsure.
        Output time joins retained windows; clip time starts at clip start before cuts.
        """
        return client.edit_clip(clip_id, changes)

    @server.tool()
    def export(clip_id: str, options: dict) -> dict:
        """Render the current edit. Export revision compatibility is unknown."""
        return client.export_clip(clip_id, options)

    @server.tool()
    def inspection_availability() -> dict:
        """Report availability of frame and screen inspection."""
        return {"available": False, "reason": "Visual inspection is not integrated"}

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
