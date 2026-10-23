"""Placeholder brand icons, rendered with Pillow.

Deep-navy rounded square with a mint "CA" monogram — a stand-in until the
Phase 6 branding pass produces final artwork. Variants: normal (mint),
stopped (grey monogram), error (coral monogram). Files live in
desktop/assets/ and are (re)generated on demand: the tray app calls
ensure_icons() at startup, and the PNGs are also committed to the repo so a
fresh checkout has them without running anything.
"""

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

NAVY = (10, 31, 58, 255)        # #0A1F3A panel
NAVY_EDGE = (18, 52, 94, 255)   # subtle border
MINT = (61, 230, 168, 255)      # #3DE6A8
GREY = (148, 163, 184, 255)     # #94A3B8
CORAL = (248, 113, 113, 255)    # #F87171

VARIANTS = {
    "icon.png": MINT,
    "icon-stopped.png": GREY,
    "icon-error.png": CORAL,
}

_SIZE = 256


def _load_font(size):
    from PIL import ImageFont

    candidates = (
        "DejaVuSans-Bold.ttf",
        "Arial Bold.ttf",
        "arialbd.ttf",
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def render_icon(accent, size=_SIZE):
    """Return a PIL Image of the brand mark with the given monogram colour."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = size // 5
    draw.rounded_rectangle([2, 2, size - 3, size - 3], radius=radius, fill=NAVY, outline=NAVY_EDGE, width=max(2, size // 64))
    font = _load_font(int(size * 0.42))
    text = "CA"
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(((size - tw) / 2 - box[0], (size - th) / 2 - box[1]), text, font=font, fill=accent)
    return img


def ensure_icons(assets_dir=None):
    """Write any missing variant PNGs; return {filename: Path}."""
    assets_dir = Path(assets_dir) if assets_dir else ASSETS_DIR
    assets_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for filename, accent in VARIANTS.items():
        target = assets_dir / filename
        if not target.exists():
            render_icon(accent).save(target, format="PNG")
        paths[filename] = target
    return paths


if __name__ == "__main__":
    for name, path in ensure_icons().items():
        print(f"{name}: {path}")
