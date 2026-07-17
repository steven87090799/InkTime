from __future__ import annotations

from pathlib import Path
import shutil

from fontTools.ttLib import TTFont


class FontCoverageError(ValueError):
    code = "IMG-002"


class FontManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def scan(self) -> list[Path]:
        return sorted(
            path
            for path in self.root.iterdir()
            if path.is_file() and path.suffix.lower() in {".ttf", ".otf", ".ttc"}
        )

    def install(self, source: Path) -> Path:
        if source.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
            raise ValueError("只支援 TTF、OTF 或 TTC 字型")
        destination = self.root / source.name
        # 系統字型可能帶有不可複製的 macOS flags；只複製內容即可。
        shutil.copyfile(source, destination)
        # 安裝前先確定 fontTools 可解析。
        with TTFont(destination, fontNumber=0, lazy=True):
            pass
        return destination

    @staticmethod
    def missing_characters(font_path: Path, text: str) -> list[str]:
        with TTFont(font_path, fontNumber=0, lazy=True) as font:
            cmap = font.getBestCmap() or {}
        return sorted(
            {character for character in text if not character.isspace() and ord(character) not in cmap}
        )

    def validate(self, font_path: Path, text: str) -> None:
        if not font_path.is_file():
            raise FontCoverageError("找不到設定的字型，已停止渲染")
        missing = self.missing_characters(font_path, text)
        if missing:
            preview = "".join(missing[:20])
            raise FontCoverageError(f"字型缺少必要字元：{preview}")
