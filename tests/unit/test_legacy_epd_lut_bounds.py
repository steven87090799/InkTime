import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def test_actual_legacy_lut_loader_has_bounded_reads_and_rejects_wrong_table_sizes(tmp_path):
    compiler = shutil.which("g++")
    if compiler is None:
        if os.environ.get("CI"):
            pytest.fail("Hosted LUT bounds acceptance requires g++")
        pytest.skip("Native compiler unavailable")
    source = (Path(__file__).resolve().parents[2] / "esp32/ink-display-133C-photo/epd7in5.h").read_text(encoding="utf-8")
    arrays = re.findall(r"UBYTE (LUT_\w+_7IN5_V2)\[\]\s*=\s*\{.*?\};", source, re.S)
    declarations = "\n".join(re.findall(r"UBYTE LUT_\w+_7IN5_V2\[\]\s*=\s*\{.*?\};", source, re.S))
    start = source.index("template <size_t Nv")
    function = source[start:source.index("\nint EPD_7in5_V2_init()", start)]
    prefix = "#include <stddef.h>\n#include <vector>\n#include <cassert>\nusing UBYTE=unsigned char;\nstd::vector<unsigned> commands,data;\nvoid EPD_SendCommand(UBYTE v){commands.push_back(v);}\nvoid EPD_SendData(UBYTE v){data.push_back(v);}\n"
    harness = tmp_path / "lut.cpp"
    body = prefix + declarations + "\n" + function
    harness.write_text(body + "\nint main(){EPD_7IN5_V2_LUT(" + ",".join(arrays) + ");assert(commands.size()==5);assert(data.size()==210);for(unsigned i=0;i<5;i++)assert(commands[i]==0x20+i);}\n", encoding="utf-8")
    binary = tmp_path / ("lut.exe" if os.name == "nt" else "lut")
    sanitizer = [] if os.name == "nt" else ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    subprocess.run([compiler, "-std=c++11", *sanitizer, str(harness), "-o", str(binary)], check=True, capture_output=True, timeout=30)  # noqa: S603 -- extracted repository LUT and fixed harness
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)  # noqa: S603 -- only the just-built fixture binary
    harness.write_text(body + "\nUBYTE short_lut[41]={};\nint main(){EPD_7IN5_V2_LUT(short_lut," + ",".join(arrays[1:]) + ");}\n", encoding="utf-8")
    rejected = subprocess.run([compiler, "-std=c++11", str(harness), "-o", str(binary)], capture_output=True, timeout=30)  # noqa: S603 -- deliberately invalid fixed-size contract fixture
    assert rejected.returncode != 0
    assert b"7in5 V2 LUT must contain 42 bytes" in rejected.stderr
