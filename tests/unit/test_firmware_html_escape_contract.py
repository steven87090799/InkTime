from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


class AttributeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.attributes.append((tag, dict(attrs)))


@pytest.mark.parametrize("consumer", ["ink-display-7C-photo", "ink-display-133C-photo"])
def test_actual_cpp_escape_preserves_ssid_attribute_values_without_extra_attributes(tmp_path, consumer):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("Native C++ compiler unavailable; hosted firmware acceptance remains required")
    root = Path(__file__).resolve().parents[2]
    source = (root / "esp32" / consumer / f"{consumer}.ino").read_text(encoding="utf-8")
    start = source.index("String htmlEscape(const String &s) {")
    end = source.index("\n}", start) + 2
    function = source[start:end]
    samples = ["Bob's Wi-Fi", 'a"b&c<d>', "繁體中文網路", "x' autofocus onfocus='void(0)", "a" * 31 + "'"]
    calls = "\n".join(f"std::cout << htmlEscape({json.dumps(value, ensure_ascii=False)}) << '\\n';" for value in samples)
    harness = tmp_path / "escape.cpp"
    harness.write_text("#include <string>\n#include <iostream>\nusing String=std::string;\n#define F(x) x\n"
                       + function + "\nint main(){\n" + calls + "\n}\n", encoding="utf-8")
    binary = tmp_path / ("escape-test.exe" if os.name == "nt" else "escape-test")
    subprocess.run([compiler, "-std=c++17", str(harness), "-o", str(binary)], check=True, capture_output=True, timeout=30)  # noqa: S603 -- trusted repository function and fixed fixture inputs
    escaped = subprocess.run([str(binary)], check=True, capture_output=True, text=True, encoding="utf-8", timeout=5).stdout.splitlines()  # noqa: S603 -- only the just-built fixture binary
    assert len(escaped) == len(samples)
    for original, value in zip(samples, escaped, strict=True):
        parser = AttributeParser()
        parser.feed(f"<option value='{value}'>network</option><input value='{value}'>")
        assert parser.attributes == [("option", {"value": original}), ("input", {"value": original})]
