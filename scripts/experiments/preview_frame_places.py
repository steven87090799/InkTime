"""Four reviewable layout mockups; synthetic scenes, no photo/model/network access."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from inktime.app.domain.rendering.layout_geometry import resolve_layout_geometry, with_photo_date_footers  # noqa: E402

FONT = ROOT / "inktime/app/domain/rendering/font_assets/LXGWWenKaiTC-Regular.ttf"


def font(size):
    return ImageFont.truetype(str(FONT), size)


def scene(size, variant):
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / height
        color = (int(155 + 55 * t), int(190 + 35 * t), int(210 + 20 * t))
        draw.line((0, y, width, y), fill=color)
    draw.ellipse((width * .68, height * .10, width * .85, height * .10 + width * .17), fill="#fff2c4")
    draw.polygon([(0, height * .58), (width * .25, height * .33), (width * .6, height * .60),
                  (width * .9, height * .38), (width, height * .5), (width, height), (0, height)], fill="#748f82")
    draw.polygon([(0, height * .72), (width * .30, height * .61), (width * .66, height * .70),
                  (width, height * .57), (width, height), (0, height)], fill="#4f756c")
    if variant % 2:
        draw.polygon([(width * .45, height * .67), (width * .55, height * .67),
                      (width * .86, height), (width * .13, height)], fill="#dbccad")
        for x in (.14, .84):
            draw.rectangle((width*x, height*.38, width*(x+.02), height*.73), fill="#5d6653")
            draw.ellipse((width*(x-.10), height*.28, width*(x+.12), height*.54), fill="#456459")
    else:
        draw.rectangle((0, height*.76, width, height), fill="#7aabb5")
        for y in range(int(height*.8), height, max(8, height//22)):
            draw.line((width*.12, y, width*.82, y), fill="#b5d0cc", width=2)
    return image


def footer(draw, box, caption, place, date):
    left, top, right, bottom = box
    draw.text((left+4, top+10), caption, font=font(19), fill="#26352f")
    draw.text((right-4, bottom-8), f"{place}・{date}", font=font(14), fill="#45554c", anchor="rs")


def render_modes(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    modes = []
    # Use the project's actual auto_photo geometry. Both landscape modes
    # put a 28 px information row BELOW every photo: caption left, date right.
    for pair in (False, True):
        geometry = with_photo_date_footers(resolve_layout_geometry("photo_pair" if pair else "full", "landscape", 800, 480))
        image = Image.new("RGB", (800, 480), "white")
        draw = ImageDraw.Draw(image)
        for index, (photo, info) in enumerate(((geometry.primary_photo, geometry.primary_caption),
                                               (geometry.secondary_photo, geometry.secondary_caption))):
            if photo is None or info is None:
                continue
            image.paste(scene((photo.width, photo.height), index), (photo.x, photo.y))
            caption = ("沿途的光留在心裡。" if index == 0 else "日常也有遠方。") if pair else "把平凡走成值得記住的風景。"
            place = "新店區" if index == 0 else "大里區"
            meta = f"{place}・2024年7月{index+1}日"
            available = info.width-28-draw.textlength(meta, font=font(14))
            while caption and draw.textlength(caption, font=font(19)) > available:
                caption = caption[:-2] + "…"
            draw.text((info.x+8, info.y+14), caption, font=font(19), fill="#26352f", anchor="lm")
            draw.text((info.right-8, info.y+14), meta, font=font(14), fill="#45554c", anchor="rm")
        stem = "02-landscape-pair" if pair else "01-landscape-single"
        title = "② 橫向相框：兩張直圖" if pair else "① 橫向相框：一張橫圖"
        modes.append((stem, title, "照片下方同一列：短句靠左，地點與日期靠右", image))

    image = Image.new("RGB", (480, 800), "white")
    image.paste(scene((452, 676), 1), (14, 14))
    draw = ImageDraw.Draw(image)
    footer(draw, (14, 700, 466, 790), "沿途的光，都留在心裡。", "新店區", "2024年7月1日")
    modes.append(("03-portrait-single", "③ 直向相框：一張直圖", "照片在上／下方文字靠左、地點與日期靠右", image))

    image = Image.new("RGB", (480, 800), "white")
    draw = ImageDraw.Draw(image)
    for index, (caption, place) in enumerate((("把平凡走成值得記住的風景。", "新店區"), ("日常也有自己的遠方。", "大里區"))):
        top = 14+index*396
        image.paste(scene((452, 294), index), (14, top))
        footer(draw, (14, top+299, 466, top+380), caption, place, f"2024年7月{index+1}日")
    modes.append(("04-portrait-pair", "④ 直向相框：兩張橫圖上下排列", "每張照片下方：文字靠左、地點與日期靠右", image))

    sheet = Image.new("RGB", (1680, 1470), "#edf0eb")
    draw = ImageDraw.Draw(sheet)
    draw.text((40, 24), "InkTime 四種排版模擬", font=font(36), fill="#26352f")
    draw.text((40, 78), "橫向依現行 auto_photo 幾何：照片下方 28px 資訊列。插圖為合成示意素材。", font=font(21), fill="#526159")
    for index, (stem, title, subtitle, frame) in enumerate(modes):
        frame.save(output / f"{stem}.png")
        x = 36 if index % 2 == 0 else 864
        y = 134 if index < 2 else 725
        draw.text((x, y), title, font=font(26), fill="#26352f")
        draw.text((x, y+38), subtitle, font=font(19), fill="#526159")
        preview = frame.copy()
        preview.thumbnail((776, 600))
        px = x+(776-preview.width)//2
        py = y+82
        draw.rounded_rectangle((px-6, py-6, px+preview.width+6, py+preview.height+6), radius=6, fill="#26352f")
        sheet.paste(preview, (px, py))
    sheet.save(output / "four-layouts.png")
    return modes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    render_modes(parser.parse_args().output_dir)
