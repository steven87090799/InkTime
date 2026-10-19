"""Operation-level resource contracts, isolated from pytest's lifetime peak."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

import pytest
from PIL import Image

from inktime.app.domain.rendering.palette import _warm_vivid_c, get_display_profile
from inktime.app.services.render_workloads import RenderWorkloadService


@pytest.mark.parametrize("mode", ["RGB", "P", "RGBA"])
def test_palette_statistics_preserve_exact_counts_with_unlisted_colors(mode):
    colors = get_display_profile("gdep073e01_6c").colors
    # Include more colors than the palette, so the bounded fallback is also
    # exercised. Unlisted colors must not change the denominator or counts.
    pixels = [color.rgb for color in colors] * 130 + [(13, 17, 23), (19, 29, 31)] * 6
    with Image.new("RGB", (6, 132)) as rgb:
        rgb.putdata(pixels)
        with rgb.convert(mode) as image, image.convert("RGB") as reference:
            expected = Counter(reference.get_flattened_data() if hasattr(reference, "get_flattened_data") else reference.getdata())
            actual = RenderWorkloadService._palette_statistics(image, colors)
            assert actual == [
                {"name": color.name, "rgb": list(color.rgb), "pixels": expected[color.rgb],
                 "ratio": round(expected[color.rgb] / (6 * 132), 6)}
                for color in colors
            ]


def test_palette_statistics_native_histogram_preserves_palette_counts():
    colors = get_display_profile("gdep073e01_6c").colors
    with Image.new("RGB", (480, 800), colors[2].rgb) as image:
        stats = RenderWorkloadService._palette_statistics(image, colors)
    assert sum(row["pixels"] for row in stats) == 480 * 800
    assert stats[2]["pixels"] == 480 * 800
    assert stats[2]["ratio"] == 1
    assert all(row["pixels"] == 0 for index, row in enumerate(stats) if index != 2)


@pytest.mark.skipif(sys.platform == "win32", reason="Hosted Linux/macOS RSS contract")
@pytest.mark.parametrize(("operation", "limit_mib"), [("warm", 32), ("statistics", 16)])
def test_frame_operation_peak_is_bounded_in_a_fresh_process(operation, limit_mib):
    # A lightweight second launcher prevents a large pytest parent RSS from
    # contaminating the measured child's startup/lifetime maximum.
    launcher = (
        "import subprocess,sys; "
        "raise SystemExit(subprocess.run([sys.executable,'-m',"
        "'tests.unit.test_nas_resource_bounds',sys.argv[1]],timeout=60).returncode)"
    )
    completed = subprocess.run(  # noqa: S603 -- fixed module and allowlisted operation
        [sys.executable, "-c", launcher, operation],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["rss_growth_bytes"] < limit_mib * 1024 * 1024, result


def _memory_probe(operation):
    import resource

    import numpy as np
    import psutil

    # Warm native import/dispatch before measuring the real 480x800 operation.
    with Image.new("RGB", (1, 1), "white") as tiny, _warm_vivid_c(tiny):
        RenderWorkloadService._palette_statistics(tiny, get_display_profile("gdep073e01_6c").colors)
    random = np.random.default_rng(20261019)
    with Image.fromarray(random.integers(0, 256, (800, 480, 3), dtype=np.uint8)) as image:
        baseline = psutil.Process().memory_info().rss
        if operation == "warm":
            with _warm_vivid_c(image) as rendered:
                assert rendered.size == image.size
        elif operation == "statistics":
            assert len(RenderWorkloadService._palette_statistics(
                image, get_display_profile("gdep073e01_6c").colors
            )) == 6
        else:
            raise ValueError("Unsupported memory probe")
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
        print(json.dumps({"operation": operation, "baseline_rss_bytes": baseline,
                          "peak_rss_bytes": peak_bytes, "rss_growth_bytes": max(0, peak_bytes - baseline)}))


if __name__ == "__main__":
    _memory_probe(sys.argv[1])
