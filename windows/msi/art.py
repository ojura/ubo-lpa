"""
Draw the MSI's images: the wizard image for the Welcome and Finish pages
(493x312), the banner for the other pages (493x58), and the icon for the
Start menu and Apps & features.

Usage: python art.py OUTPUT_DIR   (needs Pillow)
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TOP, BOTTOM = (163, 36, 26), (74, 10, 6)  # maroon gradient, close to uBO's red
SCALE = 4  # draw large and scale down, for smooth edges


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    # Segoe UI on Windows, where the MSI is built; DejaVu elsewhere, for previews.
    names = (["segoeuib.ttf", "seguisb.ttf"] if bold else ["segoeui.ttf"]) + [
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    raise SystemExit("found neither Segoe UI nor DejaVu Sans")


def shield(width: float, height: float, x: float, y: float) -> list[tuple[float, float]]:
    """A heater shield: an arched top, straight sides, curves to a point."""
    def quad(p0, p1, p2, steps=24):
        return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                 (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1])
                for t in (i / steps for i in range(steps + 1))]
    left, right, top, bottom = x, x + width, y, y + height
    mid = x + width / 2
    shoulder = top + height * 0.10
    side = top + height * 0.50
    points = quad((left, shoulder), (mid, top - height * 0.06), (right, shoulder))
    points += [(right, side)]
    points += quad((right, side), (right, top + height * 0.86), (mid, bottom))
    points += quad((mid, bottom), (left, top + height * 0.86), (left, side))
    return points


def draw_shield(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float],
                fill, glyph_fill, outline=None) -> None:
    x, y, w, h = box
    draw.polygon(shield(w, h, x, y), fill=fill, outline=outline,
                 width=max(1, int(w * 0.05)) if outline else 1)
    f = font(int(h * 0.62))
    draw.text((x + w / 2, y + h * 0.47), "µ", font=f, fill=glyph_fill, anchor="mm")


def gradient(size: tuple[int, int]) -> Image.Image:
    w, h = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for row in range(h):
        t = row / max(1, h - 1)
        draw.line([(0, row), (w, row)],
                  fill=tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))
    return image


def wizard_image() -> Image.Image:
    """Welcome and Finish pages: the art panel on the left, white behind the text."""
    w, h, panel = 493 * SCALE, 312 * SCALE, 164 * SCALE
    image = Image.new("RGB", (w, h), "white")
    image.paste(gradient((panel, h)), (0, 0))
    draw = ImageDraw.Draw(image)
    s = 74 * SCALE
    draw_shield(draw, (panel / 2 - s / 2, 58 * SCALE, s, s * 1.15),
                fill="white", glyph_fill=BOTTOM)
    draw.text((panel / 2, 172 * SCALE), "uBlock Origin", font=font(15 * SCALE),
              fill="white", anchor="mm")
    draw.text((panel / 2, 192 * SCALE), "for Chrome", font=font(11 * SCALE, bold=False),
              fill=(255, 225, 220), anchor="mm")
    draw.text((panel / 2, 290 * SCALE), "ubo-lpa", font=font(9 * SCALE, bold=False),
              fill=(255, 205, 198), anchor="mm")
    return image.resize((493, 312), Image.LANCZOS)


def banner() -> Image.Image:
    """Inner pages: white behind the title text, the shield at the right."""
    w, h = 493 * SCALE, 58 * SCALE
    image = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(image)
    s = 34 * SCALE
    draw_shield(draw, (w - 22 * SCALE - s, (h - s * 1.15) / 2, s, s * 1.15),
                fill=TOP, glyph_fill="white")
    return image.resize((493, 58), Image.LANCZOS)


def icon() -> Image.Image:
    size = 256 * SCALE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw_shield(draw, (size * 0.12, size * 0.06, size * 0.76, size * 0.88),
                fill=TOP, glyph_fill="white")
    return image.resize((256, 256), Image.LANCZOS)


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    wizard_image().save(out / "dialog.bmp")
    banner().save(out / "banner.bmp")
    icon().save(out / "ubo-lpa.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                            (64, 64), (128, 128), (256, 256)])
    # PNG copies, to look at the art without building the MSI.
    wizard_image().save(out / "dialog.png")
    banner().save(out / "banner.png")
    icon().save(out / "icon.png")


if __name__ == "__main__":
    main()
