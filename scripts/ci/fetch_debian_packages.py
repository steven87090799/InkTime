"""Prefetch exact APT selections, checking SHA-256 from authenticated indexes."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen


def fetch(packages: list[str]) -> None:
    plan = subprocess.check_output(  # noqa: S603 -- fixed executable and Dockerfile-owned package arguments.
        ["/usr/bin/apt-get", "--print-uris", "--yes", "--download-only", "install", "--no-install-recommends", *packages],
        text=True,
    )
    cache = Path("/var/cache/apt/archives")
    cache.mkdir(parents=True, exist_ok=True)
    for line in plan.splitlines():
        if not line.startswith("'"):
            continue
        url, filename, size, *_ = shlex.split(line)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "snapshot.debian.org":
            raise ValueError("Unreviewed package origin")
        if Path(filename).name != filename:
            raise ValueError("Invalid cache filename")
        pool = "pool/" + unquote(parsed.path.split("/pool/", 1)[1])
        package = filename.split("_", 1)[0]
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", package):
            raise ValueError("Invalid package name")
        metadata = subprocess.check_output(["/usr/bin/apt-cache", "show", package], text=True)  # noqa: S603 -- validated package name, no shell.
        hashes = set()
        for block in metadata.split("\n\n"):
            fields = dict(row.split(": ", 1) for row in block.splitlines() if ": " in row and not row.startswith(" "))
            if fields.get("Filename") == pool and fields.get("Size") == size:
                hashes.add(fields.get("SHA256", ""))
        if len(hashes) != 1 or len(next(iter(hashes))) != 64:
            raise ValueError("Missing unambiguous SHA-256 in authenticated APT index")
        expected = next(iter(hashes))
        archive = "debian-security" if "/archive/debian-security/" in parsed.path else "debian"
        urls = [f"https://deb.debian.org/{archive}/{pool}", url]
        errors = []
        for candidate in urls:
            temporary = None
            try:
                digest = hashlib.sha256()
                count = 0
                with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
                    temporary = Path(output.name)
                    with urlopen(candidate, timeout=60) as response:  # noqa: S310 -- HTTPS URLs from the reviewed origins above.
                        while chunk := response.read(1024 * 1024):
                            count += len(chunk)
                            if count > int(size):
                                raise ValueError("Package exceeds indexed size")
                            digest.update(chunk)
                            output.write(chunk)
                if count != int(size) or digest.hexdigest() != expected:
                    raise ValueError("Package size/SHA-256 mismatch")
                os.replace(temporary, cache / filename)
                break
            except (OSError, ValueError) as error:
                errors.append(type(error).__name__)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Verified download failed for {package}: {errors}")


if __name__ == "__main__":
    fetch(sys.argv[1:])
