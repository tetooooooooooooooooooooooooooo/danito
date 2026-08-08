"""Draw the link preview image, web/static/og.png.

Run this when the branding or the tagline changes. The output is committed, so the app itself
never needs Pillow and it stays out of requirements.txt:

    pip install Pillow
    python tools/make_og_image.py

1200x630 is what Discord, Twitter and Slack all expect for a large card. Everything is sized
for the small end of that: a preview in a Discord channel is a few hundred pixels wide, so the
headline has to survive being shrunk by half.
"""

import pathlib
import sys

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:
    sys.exit("This needs Pillow: pip install Pillow")

OUT = pathlib.Path(__file__).resolve().parents[1] / "web" / "static" / "og.png"
W, H = 1200, 630
PAD = 92

BG = (12, 19, 17)
TEXT = (233, 244, 239)
DIM = (139, 167, 155)
MINT = (61, 220, 151)
MINT_BRIGHT = (94, 234, 212)

# Segoe UI on Windows, with fallbacks so this can be run anywhere.
FONTS = {
    "bold": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
    "regular": ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf"],
}


def font(kind: str, size: int):
    for path in FONTS[kind]:
        if pathlib.Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def glow(image: Image.Image, centre, radius: int, colour, strength: float):
    """The same soft mint wash the site uses behind its hero."""
    layer = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(layer)
    x, y = centre
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=colour)
    layer = layer.filter(ImageFilter.GaussianBlur(radius * 0.55))
    return Image.blend(image, layer, strength)


def gradient_text(base: Image.Image, xy, text, fnt, start, end):
    """Text filled with a left-to-right gradient, matching the wordmark on the site.

    Pillow has no gradient fill, so the text is drawn as a mask and used to punch a gradient
    through onto the background.
    """
    # Built one pixel tall and then stretched. Filling row zero of a full-height image and
    # resizing down averages in all the empty rows, which is how you get black text.
    row = Image.new("RGB", (W, 1))
    px = row.load()
    for x in range(W):
        t = x / max(W - 1, 1)
        px[x, 0] = tuple(round(start[i] + (end[i] - start[i]) * t) for i in range(3))
    gradient = row.resize((W, H))

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).text(xy, text, font=fnt, fill=255)
    base.paste(gradient, (0, 0), mask)


def main():
    image = Image.new("RGB", (W, H), BG)
    # Two washes, warmer at the top left, the way the site's background sits.
    image = glow(image, (150, -60), 520, (34, 92, 70), 0.85)
    image = glow(image, (1150, 80), 420, (24, 74, 74), 0.5)
    draw = ImageDraw.Draw(image)

    # Brand row: the mint dot and the wordmark, same as the header.
    dot_y = PAD + 16
    draw.ellipse((PAD, dot_y, PAD + 22, dot_y + 22), fill=MINT)
    draw.text((PAD + 38, PAD), "Newt", font=font("bold", 40), fill=TEXT)

    # The headline, which is the only thing legible in a small preview.
    gradient_text(image, (PAD, 214), "Keep the members", font("bold", 82),
                  (255, 255, 255), MINT_BRIGHT)
    gradient_text(image, (PAD, 308), "you already have", font("bold", 82),
                  MINT_BRIGHT, MINT)

    draw.text((PAD, 430),
              "Retention tracking, ratings, moderation and logging",
              font=font("regular", 33), fill=DIM)
    draw.text((PAD, 474), "for Discord servers.",
              font=font("regular", 33), fill=DIM)

    # A mint rule along the bottom, echoing the hairline on every card.
    draw.rectangle((0, H - 7, W, H), fill=(16, 185, 129))
    draw.rectangle((0, H - 7, int(W * 0.42), H), fill=MINT)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT, "PNG", optimize=True)
    size = OUT.stat().st_size
    print(f"wrote {OUT.relative_to(pathlib.Path.cwd())} "
          f"({image.width}x{image.height}, {size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
