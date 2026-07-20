from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile

from fontTools.ttLib import TTFont, TTLibError


SUPPORTED_FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}
FONT_PREVIEW_TEXT = "把今天的風景，寫進明日的回憶。"
FONT_COMPATIBILITY_TEXT = "繁體中文臺灣回憶照片年月日0123456789，。"
DEFAULT_FONT_REFERENCE = "builtin:iansui"
DEFAULT_FONT_ASSET_ROOT = Path(__file__).resolve().parent / "font_assets"


class FontCoverageError(ValueError):
    code = "IMG-002"


@dataclass(frozen=True)
class BuiltinFont:
    key: str
    display_name: str
    style: str
    description: str
    version: str
    filename: str
    source_url: str
    license_url: str
    sha256: str

    @property
    def reference(self) -> str:
        return f"builtin:{self.key}"


BUILTIN_FONTS = (
    BuiltinFont(
        key="iansui",
        display_name="芫荽 Iansui",
        style="手寫風格",
        description="貼近臺灣教育部標準字形，筆調溫暖、清楚，適合生活感與手札感短文案。",
        version="v1.020",
        filename="Iansui-Regular.ttf",
        source_url="https://github.com/ButTaiwan/iansui",
        license_url="https://github.com/ButTaiwan/iansui/blob/v1.020/OFL.txt",
        sha256="7f1aa62e9dcbf40d0ce41a5d3f1e5ea602e66c295778ac6fefb6b84d8ed08bd5",
    ),
    BuiltinFont(
        key="lxgw-wenkai-tc",
        display_name="霞鶩文楷 TC",
        style="文青風格",
        description="帶有楷體筆意與書卷氣，版面安靜耐看，適合旅行、日常與回憶類短文案。",
        version="v1.522",
        filename="LXGWWenKaiTC-Regular.ttf",
        source_url="https://github.com/lxgw/LxgwWenKaiTC",
        license_url="https://github.com/lxgw/LxgwWenKaiTC/blob/v1.522/OFL.txt",
        sha256="b1a0795862c1415bf3f393ea50b2a4ea6275012cf5bad3f94feeb1222f555731",
    ),
)
BUILTIN_FONTS_BY_REFERENCE = {font.reference: font for font in BUILTIN_FONTS}


@dataclass(frozen=True)
class FontOption:
    reference: str
    display_name: str
    style: str
    description: str
    version: str
    filename: str
    source_url: str
    license_url: str
    built_in: bool
    active: bool
    missing_characters: tuple[str, ...] = ()
    error: str = ""

    @property
    def compatible(self) -> bool:
        return not self.error and not self.missing_characters


class FontManager:
    def __init__(self, root: Path, builtin_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.builtin_root = (builtin_root or DEFAULT_FONT_ASSET_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def scan(self) -> list[Path]:
        return sorted(
            path
            for path in self.root.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_FONT_SUFFIXES
        )

    def _uploaded_path(self, filename: str) -> Path:
        if (
            not filename
            or "\x00" in filename
            or Path(filename).name != filename
            or Path(filename).suffix.lower() not in SUPPORTED_FONT_SUFFIXES
        ):
            raise ValueError("字型檔名不合法")
        destination = (self.root / filename).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("字型路徑超出允許範圍") from exc
        return destination

    def resolve(self, reference: str, *, selectable_only: bool = False) -> Path:
        value = str(reference or "").strip()
        if value in BUILTIN_FONTS_BY_REFERENCE:
            return self.builtin_root / BUILTIN_FONTS_BY_REFERENCE[value].filename
        if value.startswith("builtin:"):
            raise ValueError("找不到指定的內建字型")
        if value.startswith("uploaded:"):
            return self._uploaded_path(value.removeprefix("uploaded:"))
        if selectable_only:
            raise ValueError("只能選擇內建或已上傳的字型")
        if not value:
            raise FontCoverageError("尚未設定繁體中文字型，已停止渲染")
        return Path(value).expanduser().resolve()

    def install(
        self,
        source: Path,
        *,
        filename: str | None = None,
        required_text: str = "",
    ) -> Path:
        destination = self._uploaded_path(filename or source.name)
        if source.suffix.lower() not in SUPPORTED_FONT_SUFFIXES:
            raise ValueError("只支援 TTF、OTF 或 TTC 字型")

        # 先解析來源，任何損壞都不得覆寫既有可用字型。
        try:
            with TTFont(source, fontNumber=0, lazy=True):
                pass
            if required_text:
                missing = self.missing_characters(source, required_text)
                if missing:
                    raise FontCoverageError(f"字型缺少繁體中文必要字元：{''.join(missing[:20])}")
        except TTLibError as exc:
            raise ValueError("字型檔案無法解析") from exc
        if source.resolve() == destination:
            return destination

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root,
                prefix=".inktime-font-",
                suffix=destination.suffix,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
            # 系統字型可能帶有不可複製的 macOS flags；只複製內容即可。
            shutil.copyfile(source, temporary_path)
            try:
                with TTFont(temporary_path, fontNumber=0, lazy=True):
                    pass
            except TTLibError as exc:
                raise ValueError("字型檔案無法解析") from exc
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return destination

    @staticmethod
    def missing_characters(font_path: Path, text: str) -> list[str]:
        with TTFont(font_path, fontNumber=0, lazy=True) as font:
            cmap = font.getBestCmap() or {}
        return sorted(
            {character for character in text if not character.isspace() and ord(character) not in cmap}
        )

    @staticmethod
    def family_and_version(font_path: Path) -> tuple[str, str]:
        with TTFont(font_path, fontNumber=0, lazy=True) as font:
            family = font["name"].getDebugName(1) or font_path.stem
            version = font["name"].getDebugName(5) or "自訂字型"
        return family, version

    def validate(self, font_path: Path, text: str) -> None:
        if not font_path.is_file():
            raise FontCoverageError("找不到設定的字型，已停止渲染")
        try:
            missing = self.missing_characters(font_path, text)
        except (OSError, TTLibError) as exc:
            raise FontCoverageError("設定的字型無法解析，已停止渲染") from exc
        if missing:
            preview = "".join(missing[:20])
            raise FontCoverageError(f"字型缺少必要字元：{preview}")

    def validate_reference(self, reference: str, text: str) -> Path:
        path = self.resolve(reference, selectable_only=True)
        self.validate(path, text)
        return path

    @staticmethod
    def reference_for_upload(path: Path) -> str:
        return f"uploaded:{path.name}"

    def options(self, current_reference: str) -> list[FontOption]:
        try:
            current_path = self.resolve(current_reference).resolve()
        except (OSError, ValueError):
            current_path = None

        options: list[FontOption] = []
        for builtin in BUILTIN_FONTS:
            path = self.builtin_root / builtin.filename
            error = ""
            missing: tuple[str, ...] = ()
            try:
                missing = tuple(self.missing_characters(path, FONT_COMPATIBILITY_TEXT))
            except (OSError, KeyError, ValueError, TTLibError) as exc:
                error = f"內建字型無法讀取：{exc}"
            options.append(
                FontOption(
                    reference=builtin.reference,
                    display_name=builtin.display_name,
                    style=builtin.style,
                    description=builtin.description,
                    version=builtin.version,
                    filename=builtin.filename,
                    source_url=builtin.source_url,
                    license_url=builtin.license_url,
                    built_in=True,
                    active=current_path == path.resolve(),
                    missing_characters=missing,
                    error=error,
                )
            )

        for path in self.scan():
            error = ""
            missing = ()
            try:
                family, version = self.family_and_version(path)
                missing = tuple(self.missing_characters(path, FONT_COMPATIBILITY_TEXT))
            except (OSError, KeyError, ValueError, TTLibError) as exc:
                family, version = path.stem, "無法辨識"
                error = f"字型無法讀取：{exc}"
            options.append(
                FontOption(
                    reference=self.reference_for_upload(path),
                    display_name=family,
                    style="自訂字型",
                    description="由管理員上傳；正式渲染仍會依每段短文案逐字檢查字元覆蓋。",
                    version=version,
                    filename=path.name,
                    source_url="",
                    license_url="",
                    built_in=False,
                    active=current_path == path.resolve(),
                    missing_characters=missing,
                    error=error,
                )
            )
        return options
