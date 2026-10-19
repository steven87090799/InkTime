"""Single-pass, bounded multipart transport for an already prepared Batch file."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO
from uuid import uuid4


class BatchMultipartBody:
    """A sized iterable keeps Requests from buffering ``files=`` in memory.

    The caller owns the stream and must keep it open until the request ends.
    This body deliberately cannot be replayed: an ambiguous upload must retain
    the provider's existing no-retry fence.
    """

    CHUNK_BYTES = 64 * 1024

    def __init__(self, stream: BinaryIO, *, size: int, filename: str) -> None:
        if size < 0:
            raise ValueError("Batch upload size must not be negative")
        self._stream = stream
        self._size = size
        self._started = False
        self.filename = filename
        boundary = uuid4().hex
        # Match the multipart filename escaping used by urllib3. Do not allow
        # a remote filename to inject another header or multipart field.
        quoted_filename = filename.replace("\n", "%0A").replace("\r", "%0D").replace('"', "%22")
        self.content_type = f"multipart/form-data; boundary={boundary}"
        self._prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="purpose"\r\n\r\n'
            "batch\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{quoted_filename}"\r\n'
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode("utf-8")
        self._suffix = f"\r\n--{boundary}--\r\n".encode("ascii")

    def __len__(self) -> int:
        """Let Requests send Content-Length rather than chunked encoding."""
        return len(self._prefix) + self._size + len(self._suffix)

    def __iter__(self) -> Iterator[bytes]:
        if self._started:
            raise OSError("Batch upload body cannot be replayed")
        self._started = True
        yield self._prefix
        remaining = self._size
        while remaining:
            chunk = self._stream.read(min(self.CHUNK_BYTES, remaining))
            if not chunk:
                raise OSError("Batch upload file ended before its declared size")
            remaining -= len(chunk)
            yield chunk
        yield self._suffix
