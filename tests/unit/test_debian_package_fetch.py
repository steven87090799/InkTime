"""Exercise transport failover without changing the authenticated package identity."""
import hashlib
import io
from urllib.error import HTTPError

import pytest

from scripts.ci import fetch_debian_packages as fetcher


@pytest.mark.parametrize("host", ["snapshot.debian.org", "snapshot-fastly.debian.org"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_snapshot_fallback_keeps_exact_hash_and_discards_bad_downloads(tmp_path, monkeypatch, host, corrupt):
    payload = b"authenticated package fixture"
    expected = hashlib.sha256(payload).hexdigest()
    filename = "example_1.0_amd64.deb"
    source = f"https://{host}/archive/debian/20261103T023120Z/pool/main/e/example/{filename}"
    monkeypatch.setattr(fetcher, "CACHE_DIRECTORY", tmp_path)
    def apt_output(args, **kwargs):
        if args[0] == "/usr/bin/apt-get":
            return f"'{source}' {filename} {len(payload)} SHA256:{expected}\n"
        return f"Package: example\nFilename: pool/main/e/example/{filename}\nSize: {len(payload)}\nSHA256: {expected}\n"
    monkeypatch.setattr(fetcher.subprocess, "check_output", apt_output)
    requested = []
    def download(url, **kwargs):
        requested.append(url)
        if len(requested) == 1:
            raise HTTPError(url, 503, "unavailable", {}, None)
        return io.BytesIO(b"corrupt" if corrupt else payload)
    monkeypatch.setattr(fetcher, "urlopen", download)
    if corrupt:
        with pytest.raises(RuntimeError, match="Verified download failed"):
            fetcher.fetch(["example=1.0"])
        assert not list(tmp_path.iterdir())
    else:
        fetcher.fetch(["example=1.0"])
        assert (tmp_path / filename).read_bytes() == payload
    assert requested == [f"https://deb.debian.org/debian/pool/main/e/example/{filename}", source]


@pytest.mark.parametrize("origin", ["http://snapshot.debian.org", "https://unreviewed.invalid"])
def test_unreviewed_origin_is_rejected_before_download(tmp_path, monkeypatch, origin):
    monkeypatch.setattr(fetcher, "CACHE_DIRECTORY", tmp_path)
    monkeypatch.setattr(fetcher.subprocess, "check_output", lambda *args, **kwargs:
                        f"'{origin}/archive/debian/pool/example.deb' example_1_amd64.deb 1 SHA256:unused\n")
    monkeypatch.setattr(fetcher, "urlopen", lambda *args, **kwargs: pytest.fail("origin must be rejected first"))
    with pytest.raises(ValueError, match="origin"):
        fetcher.fetch(["example=1"])
