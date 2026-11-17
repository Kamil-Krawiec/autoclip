import pytest
from autoclip.mcp_server import validate_base_url


def test_api_url_accepts_only_plain_loopback_origins():
    assert validate_base_url("http://127.0.0.1:9879") == "http://127.0.0.1:9879"
    for url in (
        "http://example.com:9879",
        "http://127.0.0.1.evil.test:9879",
        "http://user@127.0.0.1:9879",
        "http://127.0.0.1:9879/api",
        "http://127.0.0.1:9879?redirect=example.com",
    ):
        with pytest.raises(ValueError):
            validate_base_url(url)


def test_read_clip_uses_fixed_endpoint_and_rejects_path_traversal():
    import httpx
    from autoclip.mcp_server import AutoClipClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "clip123"})

    client = AutoClipClient("http://127.0.0.1:9879", transport=httpx.MockTransport(handle))
    assert client.get_clip("clip123") == {"id": "clip123"}
    assert requests[0].url.path == "/api/clips/clip123"
    for bad_id in ("../settings", "a/b", "", "x?y"):
        with pytest.raises(ValueError):
            client.get_clip(bad_id)
    assert len(requests) == 1


def test_job_creation_requires_explicit_cost_consent():
    import httpx
    from autoclip.mcp_server import AutoClipClient

    requests = []
    client = AutoClipClient(
        "http://127.0.0.1:9879",
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(201, json={"id": "job1"})
        ),
    )
    with pytest.raises(ValueError, match="cost"):
        client.create_job("source1", {}, allow_paid=False)
    assert not requests
    assert client.create_job("source1", {}, allow_paid=True) == {"id": "job1"}
    assert requests[0].url.path == "/api/jobs"


def test_supported_operations_and_errors():
    import httpx
    from autoclip.mcp_server import AutoClipClient

    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("missing"):
            return httpx.Response(404, json={"detail": "Not found"})
        return httpx.Response(200, json={"id": "ok"})

    client = AutoClipClient("http://127.0.0.1:9879", transport=httpx.MockTransport(handle))
    client.get_source("src")
    client.get_job("job")
    client.list_clips("job")
    client.get_words("clip")
    client.trim_clip("clip", 1, 4)
    client.edit_clip("clip", {"removed_ranges": [{"start_s": 2, "end_s": 3}]})
    assert client.export_clip("clip", {})["matches_current_edit"] == "unknown"
    assert calls == [
        ("GET", "/api/sources/src"),
        ("GET", "/api/jobs/job"),
        ("GET", "/api/jobs/job/clips"),
        ("GET", "/api/clips/clip/words"),
        ("PATCH", "/api/clips/clip"),
        ("PATCH", "/api/clips/clip/edit"),
        ("POST", "/api/clips/clip/export"),
    ]
    with pytest.raises(ValueError):
        client.edit_clip("clip", {"provider": "other"})
    with pytest.raises(ValueError):
        client.retry_job("job")
    with pytest.raises(httpx.HTTPStatusError):
        client.get_source("missing")


def test_stdio_sdk_calls_real_tool_against_isolated_http():
    import asyncio
    import os
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/sources"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'[{"id":"isolated"}]')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "autoclip.mcp_server"],
            env={**os.environ, "AUTOCLIP_API_URL": f"http://127.0.0.1:{server.server_port}"},
        )
        async with stdio_client(params) as (read, write):  # noqa: SIM117
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert "sources" in {tool.name for tool in listing.tools}
                result = await session.call_tool("sources", {})
                assert not result.isError
                assert "isolated" in str(result.content)

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
