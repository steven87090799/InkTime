from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys

import pytest
import requests
from urllib3.filepost import encode_multipart_formdata

from inktime.app.providers.multipart import BatchMultipartBody


@pytest.mark.parametrize("filename", ["inktime-batch-anonymous.jsonl", '相片"\r\n.jsonl'])
@pytest.mark.parametrize("payload", [b"", b'{"custom_id":"photo-1"}\n', b"\x00\xff\r\n"])
def test_streamed_multipart_matches_requests_wire_format(filename, payload):
    with BytesIO(payload) as stream:
        body = BatchMultipartBody(stream, size=len(payload), filename=filename)
        boundary = body.content_type.partition("boundary=")[2]
        expected, content_type = encode_multipart_formdata(
            [("purpose", "batch"), ("file", (filename, payload, "application/jsonl"))],
            boundary=boundary,
        )
        assert body.content_type == content_type
        assert len(body) == len(expected)
        assert b"".join(body) == expected
        assert not stream.closed  # The caller, not the body, owns this handle.


def test_requests_preparation_retains_a_sized_stream_without_reading_it():
    class BoundedReads(BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.read_sizes = []

        def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            self.read_sizes.append(size)
            return super().read(size)

    payload = b"x" * (2 * 64 * 1024 + 37)
    with BoundedReads(payload) as stream:
        body = BatchMultipartBody(stream, size=len(payload), filename="batch.jsonl")
        request = requests.Request(
            "POST", "https://example.invalid/files", data=body,
            headers={"Content-Type": body.content_type},
        ).prepare()
        assert request.body is body
        assert request.headers["Content-Length"] == str(len(body))
        assert "Transfer-Encoding" not in request.headers
        assert stream.read_sizes == []
        chunks = list(request.body)
        assert max(map(len, chunks)) <= 64 * 1024
        assert chunks[1:-1] == [payload[:64 * 1024], payload[64 * 1024:2 * 64 * 1024], payload[-37:]]
        with pytest.raises(OSError, match="cannot be replayed"):
            next(iter(body))


def test_truncated_upload_fails_before_emitting_a_complete_multipart_body():
    with BytesIO(b"short") as stream:
        body = BatchMultipartBody(stream, size=100, filename="batch.jsonl")
        chunks = iter(body)
        assert next(chunks).startswith(b"--")
        assert next(chunks) == b"short"
        with pytest.raises(OSError, match="declared size"):
            next(chunks)


def test_negative_upload_size_is_rejected_before_reading():
    with BytesIO() as stream, pytest.raises(ValueError, match="negative"):
        BatchMultipartBody(stream, size=-1, filename="batch.jsonl")


@pytest.mark.skipif(sys.platform == "win32", reason="Hosted Linux/macOS RSS contract")
@pytest.mark.parametrize("size_mib", [8, 32, 150])
def test_real_requests_upload_has_a_fixed_memory_budget(tmp_path, size_mib):
    launcher = (
        "import subprocess,sys; "
        "raise SystemExit(subprocess.run([sys.executable,'-m',"
        "'tests.unit.test_batch_multipart',sys.argv[1],sys.argv[2]],timeout=90).returncode)"
    )
    completed = subprocess.run(  # noqa: S603 -- fixed module, pytest-owned path and allowlisted size
        [sys.executable, "-c", launcher, str(tmp_path), str(size_mib)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["rss_growth_bytes"] < 32 * 1024 * 1024, result
    assert result["received_bytes"] == result["content_length"]
    assert result["received_bytes"] > size_mib * 1024 * 1024
    assert result["transfer_encoding"] is None


def _upload_memory_probe(directory, size_mib):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import resource
    import threading

    import psutil

    from inktime.app.providers.openai_compatible import OpenAICompatibleProvider

    if size_mib not in {8, 32, 150}:
        raise ValueError("Unsupported upload probe size")
    state = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            remaining = int(self.headers["Content-Length"])
            state["content_length"] = remaining
            state["transfer_encoding"] = self.headers.get("Transfer-Encoding")
            state["received_bytes"] = 0
            while remaining:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                state["received_bytes"] += len(chunk)
                remaining -= len(chunk)
            payload = b'{"id":"file-loopback"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    server_thread.start()

    class LoopbackSession(requests.Session):
        def post(self, url, **kwargs):
            # Exercise the real Requests/urllib3 transport without any provider
            # network call, credentials, or billable operation.
            assert url == "https://example.invalid/v1/files"
            return super().post(f"http://127.0.0.1:{server.server_port}/files", **kwargs)

    session = LoopbackSession()
    session.trust_env = False
    provider = OpenAICompatibleProvider(
        name="offline-memory-probe", base_url="https://example.invalid/v1",
        api_key="", session=session,
    )
    path = Path(directory) / f"upload-{size_mib}.jsonl"
    with path.open("wb") as stream:
        stream.truncate(size_mib * 1024 * 1024)
    process = psutil.Process()
    baseline = process.memory_info().rss
    try:
        assert provider.upload_batch_file(path, remote_filename="inktime-batch-anonymous.jsonl") == "file-loopback"
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
    finally:
        provider.close()
        server.shutdown()
        server.server_close()
        server_thread.join(5)
        path.unlink()
    print(json.dumps({**state, "size_mib": size_mib, "baseline_rss_bytes": baseline,
                      "peak_rss_bytes": peak_bytes, "rss_growth_bytes": max(0, peak_bytes - baseline)}))


if __name__ == "__main__":
    _upload_memory_probe(sys.argv[1], int(sys.argv[2]))
