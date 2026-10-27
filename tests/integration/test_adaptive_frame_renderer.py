from __future__ import annotations

from pathlib import Path
from hashlib import sha256
import os

from PIL import Image, ImageDraw
import pytest

from inktime.app.domain.analysis.scoring import SEMANTIC_SCORE_KIND
from inktime.app.domain.rendering import Rect
from inktime.app.domain.rendering.adaptive_layout import InsufficientPhotoContentError
from inktime.app.services import rendering as rendering_service
from tests.conftest import create_admin
from tests.unit.test_analysis_schema import valid_result


def _analyzed_photo(
    app,
    root: Path,
    photo_id: str,
    size: tuple[int, int],
    captured_at: str,
    *,
    side_caption: str | None = None,
    types: list[str] | None = None,
):
    Image.new("RGB", size, "#4271a4").save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("自適應相框", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(id,library_id,relative_path,width,height,status,eligible,lifecycle_status,
                               local_features_status,captured_at,created_at,updated_at)
            VALUES (?,?,?,?,?,'analyzed',1,'active','complete',?,?,?)
            """,
            (photo_id, library_id, f"{photo_id}.jpg", *size, captured_at, captured_at, captured_at),
        )
    photos.save_analysis(
        photo_id,
        None,
        "local",
        "test-ai",
        "test",
        valid_result(
            caption="這是一段足夠長的相框測試說明文字。",
            types=types or ["人物"],
            memory_score=99,
            visual_score=99,
            side_caption=side_caption
            or "這是一段相框回憶短句。",
        ),
        "{}",
        score_kind=SEMANTIC_SCORE_KIND,
        ranking_score=99,
        final_ranking_score=99,
    )


def _logical_landscape(image: Image.Image) -> Image.Image:
    return image.transpose(Image.Transpose.ROTATE_90)


@pytest.mark.parametrize("primary_size,expected_layout", [((1600, 960), "full"), ((900, 1600), "photo_pair")])
def test_auto_photo_resolves_source_count_and_draws_each_capture_date(
    app, tmp_path, primary_size, expected_layout
):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", primary_size, "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-02T10:00:00+00:00")
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan(
        "primary", layout="auto_photo", orientation="landscape", secondary_photo_id="secondary"
    )
    assert plan["layout"] == expected_layout
    assert plan["fit_mode"] == "cover"
    assert plan["capture_date_labels"]["primary"] == "2024年7月1日"
    assert plan["photo_footer_lines"]["primary"] == (
        "2024年7月1日 · " + plan["primary_caption"]["text"]
    )
    expected_ids = {"primary", "secondary"} if expected_layout == "photo_pair" else {"primary"}
    assert set(plan["capture_date_labels"]) == expected_ids
    if expected_layout == "photo_pair":
        assert plan["capture_date_labels"]["secondary"] == "2024年7月2日"
    else:
        assert plan["secondary_photo_id"] is None
    image = service.render_photo(
        "primary", layout="auto_photo", orientation="landscape", secondary_photo_id="secondary"
    )
    logical = _logical_landscape(image)
    assert logical.size == (800, 480)
    for rect in (plan["geometry"]["primary_photo"], plan["geometry"]["secondary_photo"]):
        if rect is not None:
            y = rect["y"] + rect["height"] // 2
            for x in (rect["x"], rect["x"] + rect["width"] - 1):
                assert logical.getpixel((x, y)) != (255, 255, 255), "Photo slots must fill their width"
    date_regions = plan["geometry"]["caption_rects"]
    if expected_layout == "photo_pair":
        assert plan["geometry"]["info_rects"] == []
        assert all(rect["height"] == 480 for rect in plan["geometry"]["photo_rects"])
    assert len(date_regions) == len(expected_ids)
    for rect in date_regions:
        assert rect["height"] == (480 if expected_layout == "photo_pair" else 28)
        footer = logical.crop((rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]))
        assert footer.convert("L").getextrema()[0] < 32, "Capture-date text must be visible"
        assert footer.getpixel((0, 0)) == (255, 255, 255)


def test_auto_photo_rejects_unpaired_portrait_instead_of_publishing_wrong_pair(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "wide", (1600, 900), "2024-07-02T10:00:00+00:00")
    service = app.extensions["inktime_render_service"]
    with pytest.raises(ValueError, match="需要另一張"):
        service.resolve_render_plan("primary", layout="auto_photo", orientation="landscape")
    with pytest.raises(ValueError, match="方向契約"):
        service.resolve_render_plan(
            "primary", layout="auto_photo", orientation="landscape", secondary_photo_id="wide"
        )


@pytest.mark.parametrize("primary_size", [(1600, 900), (900, 1600)])
def test_auto_photo_omits_missing_capture_date_without_substituting_display_date(app, tmp_path, primary_size):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", primary_size, "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-02T10:00:00+00:00")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET captured_at=NULL WHERE id='primary'")
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan(
        "primary", layout="auto_photo", orientation="landscape", secondary_photo_id="secondary"
    )
    assert plan["capture_date_labels"]["primary"] == ""
    assert plan["photo_footer_lines"]["primary"] == plan["primary_caption"]["text"]
    logical = _logical_landscape(service._render_plan_image(plan))
    rect = plan["geometry"]["primary_caption"]
    footer = logical.crop((rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]))
    assert footer.convert("L").getextrema()[0] < 32, "Missing dates must still keep the caption"
    if plan["secondary_photo_id"] is not None:
        assert plan["capture_date_labels"]["secondary"] == "2024年7月2日"
        rect = plan["geometry"]["secondary_caption"]
        footer = logical.crop((rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]))
        assert footer.convert("L").getextrema()[0] < 32, "The other photo must keep its capture date"


def test_auto_photo_keeps_caption_footer_when_capture_dates_are_hidden(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (1600, 900), "2024-07-01T10:00:00+00:00")
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.show_capture_date", False, changed_by=create_admin(app), source_ip="127.0.0.1")
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan("primary", layout="auto_photo", orientation="landscape")
    assert plan["capture_date_labels"] == {"primary": ""}
    assert plan["photo_footer_lines"] == {"primary": plan["primary_caption"]["text"]}
    assert plan["geometry"]["primary_caption"]["height"] == 28
    image = _logical_landscape(service._render_plan_image(plan))
    assert image.crop((0, 452, 800, 480)).convert("L").getextrema()[0] < 32


@pytest.mark.parametrize("size", [(1600, 900), (900, 1600)])
def test_auto_photo_places_captions_and_dates_without_overlap(app, tmp_path, monkeypatch, size):
    root = tmp_path / "photos"
    root.mkdir()
    for pid, captured in (("primary", "2024-07-01"), ("secondary", "2024-07-02")):
        _analyzed_photo(app, root, pid, size, captured + "T10:00:00+00:00")
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan("primary", layout="auto_photo", secondary_photo_id="secondary")
    pair = plan["layout"] == "photo_pair"
    assert plan["footer_alignment"] == (
        {"caption": "left_column", "date": "bottom_vertical"} if pair
        else {"caption": "left", "date": "right"}
    )
    calls = []
    original = ImageDraw.ImageDraw.text

    def record(draw, xy, text, *args, **kwargs):
        calls.append((xy, str(text), kwargs.get("anchor"), draw.textlength(str(text), font=kwargs["font"])))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    service._render_plan_image(plan)
    for index, key in enumerate(("primary_caption", "secondary_caption")):
        rect = plan["geometry"][key]
        if rect is None:
            continue
        if pair:
            photo = plan["geometry"]["photo_rects"][index]
            assert rect["x"] + rect["width"] < photo["x"]
            assert photo["height"] == 480 and not plan["geometry"]["info_rects"]
            glyphs = [
                call for call in calls if
                call[2] is None and rect["x"] <= call[0][0] < rect["x"] + rect["width"]
                and rect["y"] <= call[0][1] < rect["y"] + rect["height"]
            ]
            assert glyphs, "Caption glyphs must reach their own left column"
            dates = [call for call in glyphs if call[1] in {"2024", f"7/{index + 1}"}]
            assert [call[1] for call in dates] == ["2024", f"7/{index + 1}"]
            glyphs = [call for call in glyphs if call not in dates]
            assert min(call[0][1] for call in glyphs) <= rect["y"] + 12
            assert min(call[0][1] for call in dates) > max(call[0][1] for call in glyphs)
            assert all(call[3] <= rect["width"] - 6 for call in dates)
            assert dates[-1][0][1] + 14 <= rect["y"] + rect["height"]
            continue
        y = rect["y"] + rect["height"] / 2
        caption = next(call for call in calls if call[2] == "lm" and call[0] == (rect["x"] + 8, y))
        dates = [call for call in calls if call[2] == "rt" and
                 call[0][0] == rect["x"] + rect["width"] - 8 and
                 rect["y"] <= call[0][1] < rect["y"] + rect["height"]]
        assert [call[1] for call in dates] == ["2024", f"7/{index + 1}"]
        assert caption[0][0] + caption[3] + 10 <= min(call[0][0] - call[3] for call in dates)


def test_default_batch_photo_plans_reserve_distinct_portrait_sources(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    for pid in ("a", "b", "c", "d"):
        _analyzed_photo(app, root, pid, (900, 1600), "2024-07-01T10:00:00+00:00")
    plans = app.extensions["inktime_render_service"]._auto_photo_plans(["a", "b", "c", "d"], 2)
    assert len(plans) == 2
    sources = [pid for plan in plans for pid in (plan["primary_photo_id"], plan["secondary_photo_id"])]
    assert len(set(sources)) == 4
    assert all(plan["footer_alignment"] == {"caption": "left_column", "date": "bottom_vertical"} for plan in plans)


def test_auto_photo_holds_local_scanner_photos_without_ai_rows(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-02T10:00:00+00:00")
    settings = app.extensions["inktime_settings_repository"]
    actor = create_admin(app)
    settings.update_many(
        {"analysis.execution_mode": "local_only"}, changed_by=actor, source_ip="127.0.0.1"
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("DELETE FROM photo_analysis")
        connection.execute("UPDATE photos SET local_candidate_score=80, "
                           "captured_date=substr(captured_at,1,10),captured_month_day=substr(captured_at,6,5)")
    service = app.extensions["inktime_render_service"]
    with pytest.raises(InsufficientPhotoContentError):
        service.resolve_render_plan("primary", layout="auto_photo", orientation="landscape")


def test_adaptive_landscape_single_contains_matching_photo_and_keeps_footer(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (1600, 900), "2024-07-01T10:00:00+00:00")
    image = app.extensions["inktime_render_service"].render_photo(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    logical = _logical_landscape(image)
    assert logical.size == (800, 480)
    assert logical.getpixel((10, 200)) == (255, 255, 255)
    assert logical.getpixel((400, 200)) != (255, 255, 255)
    assert logical.getpixel((10, 470)) == (255, 255, 255)


def test_adaptive_landscape_pair_contains_portraits_with_left_caption_columns(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(
        app,
        root,
        "primary",
        (900, 1600),
        "2024-07-01T10:00:00+00:00",
        side_caption="A側相框回憶文字",
    )
    _analyzed_photo(
        app,
        root,
        "secondary",
        (900, 1600),
        "2024-07-01T10:30:00+00:00",
        side_caption="B側相框回憶文字",
    )
    image = app.extensions["inktime_render_service"].render_photo(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    logical = _logical_landscape(image)
    assert logical.getpixel((10, 100)) == (255, 255, 255)
    assert logical.getpixel((200, 200)) != (255, 255, 255)
    assert logical.getpixel((600, 200)) != (255, 255, 255)
    assert logical.getpixel((400, 470)) == (255, 255, 255)


def test_adaptive_portrait_single_contains_matching_photo_and_keeps_footer(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    image = app.extensions["inktime_render_service"].render_photo(
        "primary", layout="adaptive_memory", orientation="portrait"
    )
    assert image.getpixel((10, 300)) == (255, 255, 255)
    assert image.getpixel((240, 300)) != (255, 255, 255)
    assert image.getpixel((240, 780)) == (255, 255, 255)


def test_adaptive_portrait_pair_contains_landscapes_with_independent_captions(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (1600, 900), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (1600, 900), "2024-07-01T10:30:00+00:00")
    image = app.extensions["inktime_render_service"].render_photo(
        "primary", layout="adaptive_memory", orientation="portrait"
    )
    assert image.getpixel((240, 10)) == (255, 255, 255)
    assert image.getpixel((240, 175)) != (255, 255, 255)
    assert image.getpixel((240, 530)) != (255, 255, 255)
    assert image.getpixel((240, 780)) == (255, 255, 255)


def test_adaptive_pair_plans_bind_orientation_geometry_and_caption_identity(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(
        app,
        root,
        "portrait-a",
        (900, 1600),
        "2026-08-10T10:00:00+00:00",
        side_caption="A側相框回憶文字",
    )
    _analyzed_photo(
        app,
        root,
        "portrait-b",
        (900, 1600),
        "2026-08-12T10:00:00+00:00",
        side_caption="B側相框回憶文字",
    )
    _analyzed_photo(
        app,
        root,
        "landscape-a",
        (1600, 900),
        "2026-08-10T10:00:00+00:00",
        side_caption="上方相框回憶文字",
    )
    _analyzed_photo(
        app,
        root,
        "landscape-b",
        (1600, 900),
        "2026-08-16T10:00:00+00:00",
        side_caption="下方相框回憶文字",
    )
    service = app.extensions["inktime_render_service"]

    landscape = service.resolve_render_plan(
        "portrait-a",
        layout="adaptive_memory",
        secondary_photo_id="portrait-b",
        orientation="landscape",
    )
    landscape_geometry = landscape["geometry"]
    assert landscape["layout"] == "adaptive_memory"
    assert landscape["primary_caption"]["photo_id"] == "portrait-a"
    assert landscape["secondary_caption"]["photo_id"] == "portrait-b"
    assert landscape["primary_caption"] is not landscape["secondary_caption"]
    assert landscape["primary_caption_text_hash"] != landscape["secondary_caption_text_hash"]
    assert (
        landscape_geometry["primary_caption"]["x"]
        + landscape_geometry["primary_caption"]["width"]
        <= landscape_geometry["primary_photo"]["x"]
    )
    assert (
        landscape_geometry["secondary_caption"]["x"]
        + landscape_geometry["secondary_caption"]["width"]
        <= landscape_geometry["secondary_photo"]["x"]
    )
    with pytest.raises(ValueError, match="Caption 與照片識別不一致"):
        service.resolve_render_plan(
            "portrait-a",
            layout="adaptive_memory",
            secondary_photo_id="portrait-b",
            orientation="landscape",
            primary_caption=landscape["secondary_caption"],
            secondary_caption=landscape["primary_caption"],
        )

    portrait = service.resolve_render_plan(
        "landscape-a",
        layout="adaptive_memory",
        secondary_photo_id="landscape-b",
        orientation="portrait",
    )
    portrait_geometry = portrait["geometry"]
    assert portrait["layout"] == "adaptive_memory"
    assert portrait_geometry["primary_caption"]["y"] >= (
        portrait_geometry["primary_photo"]["y"] + portrait_geometry["primary_photo"]["height"]
    )
    assert portrait_geometry["secondary_caption"]["y"] >= (
        portrait_geometry["secondary_photo"]["y"]
        + portrait_geometry["secondary_photo"]["height"]
    )
    assert portrait_geometry["primary_caption"]["y"] < portrait_geometry["secondary_photo"]["y"]


def test_adaptive_caption_draw_regions_keep_a_and_b_records_unswapped(
    app, tmp_path, monkeypatch
):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(
        app,
        root,
        "a",
        (900, 1600),
        "2026-08-10T10:00:00+00:00",
        side_caption="A側相框回憶文字",
    )
    _analyzed_photo(
        app,
        root,
        "b",
        (900, 1600),
        "2026-08-12T10:00:00+00:00",
        side_caption="B側相框回憶文字",
    )
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan("a", layout="adaptive_memory", orientation="landscape")
    observed = []
    original = service._draw_adaptive_pair_caption

    def capture(canvas, record, box, *, left_side):
        observed.append((record["photo_id"], record["text"], box.as_dict(), left_side))
        return original(canvas, record, box, left_side=left_side)

    monkeypatch.setattr(service, "_draw_adaptive_pair_caption", capture)
    service._render_plan_image(plan)

    assert [(item[0], item[1]) for item in observed] == [("a", "A側相框回憶文字"), ("b", "B側相框回憶文字")]
    assert all(item[3] is True for item in observed)
    assert observed[0][2] == plan["geometry"]["primary_caption"]
    assert observed[1][2] == plan["geometry"]["secondary_caption"]


def test_landscape_side_caption_draws_upright_glyphs_without_rotating_text_strip(
    app, monkeypatch
):
    service = app.extensions["inktime_render_service"]
    canvas = Image.new("RGB", (800, 480), "white")
    box = Rect(12, 16, 80, 440)
    text_calls: list[tuple[str, tuple[float, float]]] = []
    transpose_calls = []
    original_text = ImageDraw.ImageDraw.text
    original_transpose = Image.Image.transpose

    def capture_text(draw, xy, text, *args, **kwargs):
        text_calls.append((str(text), xy))
        return original_text(draw, xy, text, *args, **kwargs)

    def capture_transpose(image, method):
        transpose_calls.append(method)
        return original_transpose(image, method)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    monkeypatch.setattr(Image.Image, "transpose", capture_transpose)

    service._draw_adaptive_pair_caption(
        canvas,
        {"photo_id": "primary", "text": "A側相框回憶文字"},
        box,
        left_side=True,
    )

    assert [text for text, _xy in text_calls] == list("A側相框回憶文字")
    assert [xy[1] for _text, xy in text_calls] == sorted(
        xy[1] for _text, xy in text_calls
    )
    assert all(box.x <= xy[0] < box.right for _text, xy in text_calls)
    assert transpose_calls == []

    physical = service._physical_frame(canvas, "landscape")
    assert physical.size == (480, 800)
    assert transpose_calls == [Image.Transpose.ROTATE_270]


@pytest.mark.parametrize("layout", ("photo_pair", "photo_pair_caption", "adaptive_memory"))
@pytest.mark.parametrize("orientation", ("portrait", "landscape"))
def test_every_pair_mode_draws_both_photo_captions_and_smaller_capture_dates(
    app, tmp_path, monkeypatch, layout, orientation
):
    root = tmp_path / "pair-dates"
    root.mkdir()
    size = (900, 1600) if orientation == "landscape" else (1600, 900)
    for photo_id, captured, caption in (
        ("a", "2024-07-01T10:00:00+00:00", "A側相框回憶文字"),
        ("b", "2021-12-31T10:00:00+00:00", "B側相框回憶文字"),
    ):
        _analyzed_photo(app, root, photo_id, size, captured, side_caption=caption)
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan(
        "a", layout=layout, secondary_photo_id="b", orientation=orientation
    )
    calls = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(draw, xy, text, *args, **kwargs):
        calls.append((str(text), kwargs["font"].size))
        return original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    image = service._render_plan_image(plan)
    if artifact_dir := os.environ.get("INKTIME_RENDER_ARTIFACT_DIR"):
        destination = Path(artifact_dir)
        destination.mkdir(parents=True, exist_ok=True)
        image.save(destination / f"{layout}-{orientation}.png")
    assert image.size == (480, 800)
    painted = "".join(text for text, _size in calls)
    for caption in ("A側相框回憶文字", "B側相框回憶文字"):
        assert caption in painted
    for year, month_day in (("2024", "7/1"), ("2021", "12/31")):
        assert year in painted
        assert month_day in painted or ("月" in painted and "日" in painted)
    assert plan["primary_caption"]["capture_date"] == "2024-07-01"
    assert plan["secondary_caption"]["capture_date"] == "2021-12-31"
    assert all(size <= 14 for text, size in calls if "年" in text or "月" in text)
    assert all(size >= 16 for text, size in calls if "A" in text or "B" in text)


@pytest.mark.parametrize("layout", ("photo_pair", "photo_pair_caption", "adaptive_memory"))
@pytest.mark.parametrize("changed_photo", ("a", "b"))
def test_frozen_pair_plan_rejects_replaced_content_even_when_photo_id_is_unchanged(app, tmp_path, layout, changed_photo):
    root = tmp_path / "frozen-content"
    root.mkdir()
    database = app.extensions["inktime_database"]
    for photo_id in ("a", "b"):
        _analyzed_photo(app, root, photo_id, (900, 1600), "2024-07-01T10:00:00+00:00")
        if photo_id == "b":
            # Adaptive pairing intentionally rejects identical content.
            Image.new("RGB", (900, 1600), "#508464").save(root / "b.jpg")
        with database.transaction() as connection:
            connection.execute("UPDATE photos SET sha256=? WHERE id=?", (sha256((root / f"{photo_id}.jpg").read_bytes()).hexdigest(), photo_id))
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan("a", layout=layout, secondary_photo_id="b", orientation="landscape")
    source = root / f"{changed_photo}.jpg"
    Image.new("RGB", (900, 1600), "#dc8054").save(source)
    with database.transaction() as connection:
        connection.execute("UPDATE photos SET sha256=? WHERE id=?", (sha256(source.read_bytes()).hexdigest(), changed_photo))
    with pytest.raises(ValueError, match="RENDER-INPUT-CHANGED"):
        service._render_plan_image(plan)


@pytest.mark.parametrize("layout", ("photo_pair", "photo_pair_caption", "adaptive_memory"))
def test_pair_dates_are_frozen_and_missing_dates_never_draw_unknown_placeholder(
    app, tmp_path, monkeypatch, layout
):
    root = tmp_path / "frozen-dates"
    root.mkdir()
    for photo_id, caption in (("a", "A側相框回憶文字"), ("b", "B側相框回憶文字")):
        _analyzed_photo(
            app, root, photo_id, (900, 1600), "2024-07-01T10:00:00+00:00", side_caption=caption
        )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET captured_at=NULL WHERE id='b'")
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan(
        "a", layout=layout, secondary_photo_id="b", orientation="landscape"
    )
    assert plan["primary_caption"]["capture_date_label"] == "2024年7月1日"
    assert plan["secondary_caption"]["capture_date_label"] == ""
    app.extensions["inktime_settings_repository"].update(
        "render.show_capture_date", False, changed_by="test", source_ip="127.0.0.1"
    )
    calls = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(draw, xy, text, *args, **kwargs):
        calls.append(str(text))
        return original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    service._render_plan_image(plan)
    assert "2024" in calls and "7/1" in calls
    assert "未知" not in "".join(calls)
    calls.clear()
    service.render_photo(
        "a", layout=layout, secondary_photo_id="b", orientation="landscape"
    )
    assert "2024" not in calls and "7/1" not in calls
    assert "A側相框回憶文字" in "".join(calls) and "B側相框回憶文字" in "".join(calls)
    assert service._date_label(None) == ""


@pytest.mark.parametrize("layout", ("photo_pair", "photo_pair_caption"))
def test_direct_pair_render_rejects_caption_bound_to_the_other_photo(app, tmp_path, layout):
    root = tmp_path / "pair-identity"
    root.mkdir()
    for photo_id in ("a", "b"):
        _analyzed_photo(app, root, photo_id, (900, 1600), "2024-07-01T10:00:00+00:00")
    service = app.extensions["inktime_render_service"]
    plan = service.resolve_render_plan(
        "a", layout=layout, secondary_photo_id="b", orientation="landscape"
    )
    with pytest.raises(ValueError, match="Caption 與照片識別不一致"):
        service.render_photo(
            "a",
            layout=layout,
            secondary_photo_id="b",
            orientation="landscape",
            primary_caption=plan["secondary_caption"],
            secondary_caption=plan["primary_caption"],
        )


def test_adaptive_square_and_missing_pair_fall_back_to_single_contain(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "square", (1000, 1000), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "portrait", (900, 1600), "2024-07-02T10:00:00+00:00")
    square = app.extensions["inktime_render_service"].render_photo(
        "square", layout="adaptive_memory", orientation="landscape"
    )
    fallback = app.extensions["inktime_render_service"].render_photo(
        "portrait", layout="adaptive_memory", orientation="landscape"
    )
    assert _logical_landscape(square).getpixel((10, 100)) == (255, 255, 255)
    assert _logical_landscape(fallback).getpixel((10, 100)) == (255, 255, 255)


def test_adaptive_selection_accepts_unrelated_two_day_and_seven_day_candidates(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(
        app,
        root,
        "primary",
        (900, 1600),
        "2026-08-10T10:00:00+00:00",
        types=["人物"],
    )
    _analyzed_photo(
        app,
        root,
        "two-day",
        (900, 1600),
        "2026-08-12T10:00:00+00:00",
        types=["風景"],
    )
    _analyzed_photo(
        app,
        root,
        "seven-day",
        (900, 1600),
        "2026-08-16T10:00:00+00:00",
        types=["建築"],
    )
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET gps_lat=25.03,gps_lon=121.56 WHERE id='primary'"
        )
        connection.execute(
            "UPDATE photos SET gps_lat=35.68,gps_lon=139.76 WHERE id IN ('two-day','seven-day')"
        )

    service = app.extensions["inktime_render_service"]
    first = service.resolve_render_plan(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    assert first["secondary_photo_id"] == "two-day"

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET eligible=0 WHERE id='two-day'")
    second = service.resolve_render_plan(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    assert second["secondary_photo_id"] == "seven-day"


def test_adaptive_selection_uses_safe_any_date_after_broken_nearby_source(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2026-08-10T10:00:00+00:00")
    _analyzed_photo(app, root, "broken", (900, 1600), "2026-08-12T10:00:00+00:00")
    _analyzed_photo(app, root, "older-safe", (900, 1600), "2024-01-01T10:00:00+00:00")
    (root / "broken.jpg").unlink()

    plan = app.extensions["inktime_render_service"].resolve_render_plan(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    assert plan["layout"] == "adaptive_memory"
    assert plan["secondary_photo_id"] == "older-safe"


def test_adaptive_duplicate_is_rejected_and_no_safe_pair_falls_back_to_single(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2026-08-10T10:00:00+00:00")
    _analyzed_photo(app, root, "duplicate", (900, 1600), "2026-08-11T10:00:00+00:00")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            "UPDATE photos SET sha256='same-content' WHERE id IN ('primary','duplicate')"
        )

    plan = app.extensions["inktime_render_service"].resolve_render_plan(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    assert plan["layout"] == "photo_info"
    assert plan["secondary_photo_id"] is None


def test_adaptive_recently_displayed_candidate_is_fallback_but_fresh_candidate_wins(
    app, tmp_path
):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2026-08-10T10:00:00+00:00")
    _analyzed_photo(app, root, "recent", (900, 1600), "2026-08-10T10:05:00+00:00")
    _analyzed_photo(app, root, "fresh", (900, 1600), "2026-08-12T10:00:00+00:00")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """UPDATE photos SET display_count=1,last_displayed_at=datetime('now')
               WHERE id='recent'"""
        )

    service = app.extensions["inktime_render_service"]
    preferred = service.resolve_render_plan(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    assert preferred["secondary_photo_id"] == "fresh"

    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET eligible=0 WHERE id='fresh'")
    fallback = service.resolve_render_plan(
        "primary", layout="adaptive_memory", orientation="landscape"
    )
    assert fallback["secondary_photo_id"] == "recent"


def test_adaptive_candidate_query_filters_freshness_before_the_bounded_limit(
    app, monkeypatch
):
    service = app.extensions["inktime_render_service"]
    observed: dict[str, object] = {}

    class EmptyResult:
        @staticmethod
        def fetchall():
            return []

    class CapturingConnection:
        def execute(self, sql, parameters):
            observed["sql"] = sql
            observed["parameters"] = parameters
            return EmptyResult()

    class CapturingSession:
        def __enter__(self):
            return CapturingConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class CapturingDatabase:
        @staticmethod
        def session():
            return CapturingSession()

    monkeypatch.setattr(service, "database", CapturingDatabase())
    rows = service._adaptive_candidate_rows(
        frame_orientation="landscape",
        where_sql="1=1",
        parameters=("primary",),
        order_sql="p.captured_at DESC,p.id DESC",
        limit=300,
        recent_mode="fresh",
    )

    compact_sql = " ".join(str(observed["sql"]).split())
    recent_filter = "NOT ( EXISTS(SELECT 1 FROM display_history recent_dh"
    assert rows == []
    assert recent_filter in compact_sql
    assert compact_sql.index(recent_filter) < compact_sql.index("ORDER BY p.captured_at")
    assert compact_sql.index("ORDER BY p.captured_at") < compact_sql.index("LIMIT ?")
    assert observed["parameters"] == ("primary", 300)


def test_all_formal_frame_layouts_render_with_the_resolved_photos(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (1600, 900), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-01T10:30:00+00:00")
    service = app.extensions["inktime_render_service"]

    for layout in ("full", "postcard", "photo_info", "calendar", "weather_sensor"):
        image = service.render_photo("primary", layout=layout, orientation="landscape", fit_mode="cover")
        assert image.size == (480, 800)
    paired = service.render_photo(
        "primary",
        layout="photo_pair",
        secondary_photo_id="secondary",
        orientation="landscape",
        fit_mode="cover",
    )
    assert paired.size == (480, 800)


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_formal_render_bounds_decoded_source_to_render_target(app, tmp_path, monkeypatch, orientation):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "large", (2400, 1600), "2024-07-01T10:00:00+00:00")
    service = app.extensions["inktime_render_service"]
    observed: list[tuple[int, int]] = []
    original_loader = service._load_oriented_photo

    def capture_loader(photo, path, *, target_size=None):
        source, orientation = original_loader(photo, path, target_size=target_size)
        observed.append(source.size)
        return source, orientation

    monkeypatch.setattr(service, "_load_oriented_photo", capture_loader)
    image = service.render_photo("large", layout="full", fit_mode="cover", orientation=orientation)

    assert image.size == (480, 800)
    assert observed
    bound = (960, 1600) if orientation == "portrait" else (1600, 960)
    assert observed[0][0] <= bound[0]
    assert observed[0][1] <= bound[1]


def test_formal_render_accepts_48mp_jpeg_and_bounds_before_exif(app, tmp_path, monkeypatch):
    service = app.extensions["inktime_render_service"]
    before_exif: list[tuple[int, int]] = []

    class VirtualLargeJpeg:
        format = "JPEG"

        def __init__(self):
            self._image = Image.new("RGB", (120, 90), "#4271a4")
            self._reported_size = (8_000, 6_000)  # 48MP virtual JPEG fixture.
            self.draft_calls: list[tuple[str, tuple[int, int]]] = []

        @property
        def size(self):
            return self._reported_size

        @property
        def width(self):
            return self._reported_size[0]

        @property
        def height(self):
            return self._reported_size[1]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self._image.close()

        def draft(self, mode, size):
            self.draft_calls.append((mode, size))
            self._image = self._image.resize((1_200, 900), Image.Resampling.BOX)
            self._reported_size = self._image.size

        def thumbnail(self, size, resample):
            self._image.thumbnail(size, resample)
            self._reported_size = self._image.size

        def __getattr__(self, name):
            return getattr(self._image, name)

    virtual = VirtualLargeJpeg()
    original_exif_transpose = rendering_service.ImageOps.exif_transpose

    # The shared source boundary validates a real regular file before opening
    # the controlled decoder; keep that filesystem security check in this test.
    Image.new("RGB", (120, 90), "#4271a4").save(tmp_path / "virtual-48mp.jpg")

    def open_virtual(_path, **_kwargs):
        return virtual

    def observe_before_exif(image, *args, **kwargs):
        before_exif.append(image.size)
        return original_exif_transpose(image, *args, **kwargs)

    monkeypatch.setattr(rendering_service.Image, "open", open_virtual)
    monkeypatch.setattr(rendering_service.ImageOps, "exif_transpose", observe_before_exif)
    source, _orientation = service._load_oriented_photo(
        {}, tmp_path / "virtual-48mp.jpg", target_size=(480, 800)
    )
    try:
        assert virtual.width * virtual.height <= 60_000_000
        assert virtual.draft_calls == [("RGB", (960, 1_600))]
        assert before_exif == [(960, 720)]
        assert source.size == (960, 720)
    finally:
        source.close()


def test_device_releases_keep_profile_manifest_and_independent_layouts(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(
        app,
        root,
        "primary",
        (900, 1600),
        "2024-07-01T10:00:00+00:00",
        side_caption="A側相框回憶文字",
    )
    _analyzed_photo(
        app,
        root,
        "secondary",
        (900, 1600),
        "2024-07-01T10:30:00+00:00",
        side_caption="B側相框回憶文字",
    )
    devices = app.extensions["inktime_device_repository"]
    portrait_id, _ = devices.create("直向", frame_orientation="portrait", layout_mode="adaptive_memory")
    landscape_id, _ = devices.create(
        "橫向",
        panel_profile="gdep073e01_6c",
        frame_orientation="landscape",
        layout_mode="adaptive_memory",
    )
    result = app.extensions["inktime_render_service"].publish(
        ["primary"], "test", device_ids=[portrait_id, landscape_id]
    )
    assert set(result["device_releases"]) == {portrait_id, landscape_id}
    with app.extensions["inktime_database"].session() as connection:
        assignments = connection.execute(
            "SELECT device_id,release_id FROM device_render_releases ORDER BY device_id"
        ).fetchall()
    assert len(assignments) == 2
    secondary_ids = set()
    expected_profiles = {portrait_id: "safe_4c", landscape_id: "gdep073e01_6c"}
    for release_id in result["device_releases"].values():
        manifest = app.extensions["inktime_release_publisher"].validate(release_id)
        device_id = next(key for key, value in result["device_releases"].items() if value == release_id)
        assert manifest["render_profile"] == expected_profiles[device_id]
        assert manifest["width"] == 480 and manifest["height"] == 800
        assert manifest["files"][0]["name"] == "photo_1.bin"
        assert manifest["files"][0]["size"] == (
            96_000 if manifest["render_profile"] == "safe_4c" else 192_000
        )
        options = manifest["render_options"]
        assert options["aggregation_scope"] == "release"
        plan = options["render_plans"][0]
        assert plan["primary_photo_id"] == "primary"
        assert plan["primary_caption"]["photo_id"] == plan["primary_photo_id"]
        assert plan["primary_caption_text_hash"] == plan["primary_caption"]["text_hash"]
        secondary_ids.add(plan["secondary_photo_id"])
        assert "secondary_sha256" in plan
        assert options["effective_dither"] == manifest["dither"]
        assert plan["profile"] == manifest["render_profile"]
        assert plan["effective_dither"] == manifest["dither"]
        if device_id == landscape_id:
            assert plan["layout"] == "adaptive_memory"
            assert plan["secondary_photo_id"] == "secondary"
            assert plan["secondary_caption"]["photo_id"] == plan["secondary_photo_id"]
            assert plan["secondary_caption_text_hash"] == plan["secondary_caption"]["text_hash"]
            assert plan["primary_caption_text_hash"] != plan["secondary_caption_text_hash"]
            assert plan["geometry"]["orientation"] == "landscape"
            assert (
                plan["geometry"]["primary_caption"]["x"]
                + plan["geometry"]["primary_caption"]["width"]
                <= plan["geometry"]["primary_photo"]["x"]
            )
        else:
            assert plan["layout"] == "photo_info"
            assert plan["secondary_photo_id"] is None
            assert plan["geometry"]["orientation"] == "portrait"
    assert secondary_ids == {None, "secondary"}


def test_each_manifest_binds_the_release_dither_and_its_own_profile_plan(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "low-risk", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "high-risk", (1600, 900), "2024-07-01T10:30:00+00:00")
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.auto_photo_smooth_enabled", True, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos
            SET brightness=40,contrast=5,underexposed_ratio=.7,e6_score=20,
                e6_contrast_score=20,e6_subject_score=20
            WHERE id='high-risk'
            """
        )

    published = app.extensions["inktime_render_service"].publish(
        ["low-risk", "high-risk"],
        "test",
        profile_keys=["safe_4c", "gdep073e01_6c"],
    )
    assert len(published["releases"]) == 2
    for item in published["releases"]:
        manifest = app.extensions["inktime_release_publisher"].validate(item["release_id"])
        options = manifest["render_options"]
        assert manifest["dither"] == "photo_smooth"
        assert options["effective_dither"] == manifest["dither"]
        assert options["quantization_plan"]["effective_dither"] == manifest["dither"]
        assert options["quantization_plan"]["profile_key"] == manifest["render_profile"]
        assert {plan["profile"] for plan in options["render_plans"]} == {manifest["render_profile"]}
        assert {plan["effective_dither"] for plan in options["render_plans"]} == {manifest["dither"]}
        assert all(plan["aggregation_scope"] == "release" for plan in options["render_plans"])
        assert any(
            risk["photo_id"] == "high-risk" and risk["risk"] == "high"
            for risk in options["quantization_plan"]["photo_risks"]
        )


def test_adaptive_secondary_risk_controls_the_preview_dither_plan(app, tmp_path, monkeypatch):
    root = tmp_path / "photos"
    root.mkdir()
    _analyzed_photo(app, root, "primary", (900, 1600), "2024-07-01T10:00:00+00:00")
    _analyzed_photo(app, root, "secondary", (900, 1600), "2024-07-01T10:30:00+00:00")
    settings = app.extensions["inktime_settings_repository"]
    settings.update("render.auto_photo_smooth_enabled", True, changed_by="test", source_ip="127.0.0.1")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            UPDATE photos
            SET brightness=40,contrast=5,underexposed_ratio=.7,e6_score=20,
                e6_contrast_score=20,e6_subject_score=20
            WHERE id='secondary'
            """
        )

    service = app.extensions["inktime_render_service"]
    fingerprint = service.preview_fingerprint("primary", layout="adaptive_memory", orientation="landscape")
    plan = fingerprint["render_plan"]

    assert plan["secondary_photo_id"] == "secondary"
    assert plan["effective_dither"] == "photo_smooth"
    assert plan["override_source"] == "auto_photo_smooth"
    assert fingerprint["render_settings"]["effective_dither"] == "photo_smooth"
    assert any(risk == {"photo_id": "secondary", "risk": "high"} for risk in plan["photo_risks"])

    def selection_must_stay_frozen(*_args, **_kwargs):
        raise AssertionError("rendering a frozen preview plan must not select again")

    monkeypatch.setattr(service, "_select_adaptive_pair_candidate", selection_must_stay_frozen)
    assert service._render_plan_image(plan).size == (480, 800)


def test_compact_pair_dates_leave_both_sentences_and_districts_visible(app, monkeypatch):
    from inktime.app.domain.rendering.layout_geometry import resolve_compact_pair_geometry
    service = app.extensions['inktime_render_service']
    geometry = resolve_compact_pair_geometry(((900, 1600), (900, 1600)))
    calls = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        calls.append((str(text), kwargs['font'].size))
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', capture)
    records = ({'photo_id': 'a', 'text': '那天微風也記得我們一起走過的路留在心裡面'},
               {'photo_id': 'b', 'text': '那些溫柔日常值得好好收藏'})
    parts = {key: {'date': '2022年8月5日', 'location': '新店區'} for key in ('a', 'b')}
    with Image.new('RGB', (800, 480), 'white') as canvas:
        service._draw_compact_pair_captions(canvas, geometry, records, parts)
    assert sum(text == '2022' for text, _ in calls) == 2
    assert sum(text == '8/5' for text, _ in calls) == 2
    assert sum(text == '新店區' for text, _ in calls) == 2
    assert all(size == 14 for text, size in calls if text in {'2022', '8/5'})
    painted = ''.join(text for text, size in calls if size >= 16)
    assert records[0]['text'] in painted and records[1]['text'] in painted


@pytest.mark.parametrize('place,visible_place', [
    ('國立故宮博物院南部院區', '國立故宮博物院南部院區'),
    ('國立臺灣史前文化博物館南科考古館', '國立臺灣史前文化博物館南科考古館'),
    ('海邊風景文化展示中心', '海邊風景文化展示中心'),
    ('山水風光' * 10, None),
])
def test_compact_pair_long_places_keep_twenty_characters_and_original_metadata(app, monkeypatch, place, visible_place):
    from inktime.app.domain.rendering.layout_geometry import resolve_compact_pair_geometry

    service = app.extensions['inktime_render_service']
    geometry = resolve_compact_pair_geometry(((900, 1600), (900, 1600)))
    text = '那天微風也記得我們一起走過的路留在心裡面'
    calls = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, xy, value, *args, **kwargs):
        calls.append((str(value), kwargs['font'].size))
        return original(draw, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', capture)
    records = tuple({'photo_id': key, 'text': text} for key in ('a', 'b'))
    parts = {key: {'date': '2026年12月31日', 'location': place} for key in ('a', 'b')}
    with Image.new('RGB', (800, 480), 'white') as canvas:
        service._draw_compact_pair_captions(canvas, geometry, records, parts)
    assert ''.join(value for value, size in calls if size >= 16) == text * 2
    place_text = ''.join(value for value, size in calls if size < 16 and value not in {'2026', '12/31'})
    if visible_place is None:
        assert '…' in place_text
    else:
        assert place_text == visible_place * 2
    assert all(part['location'] == place for part in parts.values())
    assert sum(rect.width for rect in geometry.photo_rects) == 684


def test_compact_pair_unknown_name_can_shrink_date_and_keep_its_full_name(app):
    service = app.extensions['inktime_render_service']
    place = '甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未'
    text = '那天微風也記得我們一起走過的路留在心裡'
    font_path = service.fonts.resolve(str(service.settings.get('render.font_path', '')))
    with Image.new('RGB', (44, 480), 'white') as canvas:
        lines = service._compact_pair_metadata_lines(
            ImageDraw.Draw(canvas), font_path=font_path, date_lines=['2026', '12/31'],
            place=place, sentence=text, width=38, height=464,
        )
    assert ''.join(value for value, _font, _step in lines[2:]) == place
    assert 10 <= lines[0][1].size < 14


def test_compact_pair_sixteen_character_caption_leaves_room_for_forty_character_landmark(app):
    service = app.extensions['inktime_render_service']
    place = '山水風光' * 10
    font_path = service.fonts.resolve(str(service.settings.get('render.font_path', '')))
    with Image.new('RGB', (44, 480), 'white') as canvas:
        lines = service._compact_pair_metadata_lines(
            ImageDraw.Draw(canvas), font_path=font_path, date_lines=['2026', '12/31'],
            place=place, sentence='把平凡日子收進光裡讓回憶慢慢發亮', width=38, height=464,
        )
    assert ''.join(value for value, _font, _step in lines[2:]) == place
    assert sum(step for _value, _font, step in lines) + 16 * 18 + 8 <= 464


def test_shorter_generation_limit_preserves_existing_model_sentence(app, tmp_path):
    root = tmp_path / 'retained-caption'
    root.mkdir()
    text = '那天微風也記得我們一起走過的路留在心裡面'
    _analyzed_photo(app, root, 'retained', (1600, 900), '2026-10-27T08:00:00+00:00', side_caption=text)
    app.extensions['inktime_settings_repository'].update(
        'analysis.side_caption_max_chars', 16, changed_by=create_admin(app), source_ip='127.0.0.1',
    )
    plan = app.extensions['inktime_render_service'].resolve_render_plan(
        'retained', layout='auto_photo', orientation='landscape',
    )
    assert plan['primary_caption']['text'] == text
