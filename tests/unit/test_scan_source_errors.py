import pytest

from inktime.app.workers.process_boundary import _call_child
from inktime.app.workers.scanner import ScanSourceError, ScanSourceMissingError


class Sender:
    def send(self, value):
        self.value = value

    def close(self):
        self.closed = True


def fail_source(error):
    raise error


@pytest.mark.parametrize("error,code", [
    (ScanSourceMissingError("private filesystem path"), "SCAN-001"),
    (ScanSourceError("private filesystem path"), "SCAN-002"),
])
def test_local_child_preserves_only_safe_source_code(error, code):
    sender = Sender()
    _call_child(fail_source, {"error": error}, sender)
    assert sender.value == ("error", {"type": type(error).__name__, "code": code})
    assert "private filesystem path" not in repr(sender.value)
    assert sender.closed


def test_unrecognized_error_metadata_is_not_serialized():
    error = OSError("secret URL or path")
    error.code = ["not a safe error code"]
    sender = Sender()
    _call_child(fail_source, {"error": error}, sender)
    assert sender.value == ("error", "OSError")
