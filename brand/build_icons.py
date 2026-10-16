"""Regenerate the n16chat logo SVGs and every icon PNG from the brand source.

Not part of the app or its tests: run it by hand when the logo changes, in a
throwaway virtualenv (never add these to backend/requirements.txt):

    python3 -m venv /tmp/brand-venv
    /tmp/brand-venv/bin/pip install potracer cairosvg pillow numpy
    /tmp/brand-venv/bin/python brand/build_icons.py [path/to/source.jpeg]

The source defaults to brand/n16chat-logo-source.jpg (a reduced copy of the
operator's 2400x1792 original; pass the full-size file for a sharper trace).
The teal artwork is thresholded and traced with potrace (potracer, pure
Python); the cloud + "N16" and the word "CHAT" become separate SVGs, and the
PNGs are rendered from them with cairosvg. Commit every file it writes.
"""

import io
import sys
from pathlib import Path

import cairosvg
import numpy as np
import potrace
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
TEAL = "#106c69"
TEAL_RGB = (16, 108, 105)
WHITE = (255, 255, 255)


def load_mask(src: Path) -> np.ndarray:
    im = np.asarray(Image.open(src).convert("RGB")).astype(int)
    return im.mean(axis=2) < 150  # teal artwork vs the near-white background


def trace(mask: np.ndarray, scale: float, ox: float, oy: float) -> str:
    # potracer traces the False (dark) pixels, so pass the inverted mask.
    plist = potrace.Bitmap(~mask).trace(
        turdsize=20, alphamax=1.0, opticurve=True, opttolerance=0.2
    )

    def pt(p):
        return f"{(p.x - ox) * scale:.1f} {(p.y - oy) * scale:.1f}"

    d = []
    for curve in plist:
        d.append("M" + pt(curve.start_point))
        for s in curve.segments:
            if s.is_corner:
                d.append("L" + pt(s.c) + "L" + pt(s.end_point))
            else:
                d.append("C" + pt(s.c1) + " " + pt(s.c2) + " " + pt(s.end_point))
        d.append("Z")
    return "".join(d).replace(".0 ", " ").replace(".0C", "C").replace(".0L", "L")


def bbox(mask):
    ys, xs = np.nonzero(mask)
    return xs.min(), xs.max() + 1, ys.min(), ys.max() + 1


def svg(width, height, body, title):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {height:g}" '
        f'role="img" aria-label="{title}"><title>{title}</title>{body}</svg>\n'
    )


def render(svg_text: str, size: int) -> Image.Image:
    png = cairosvg.svg2png(bytestring=svg_text.encode(), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def filled_silhouette(img: Image.Image) -> Image.Image:
    """The cloud with its knockouts filled: for 16 px, where "N16" is mush."""
    m = 40  # margin, so the outside stays connected around the dilated art
    alpha = Image.new("L", (img.width + 2 * m, img.height + 2 * m), 0)
    alpha.paste(img.getchannel("A").point(lambda a: 255 if a > 96 else 0), (m, m))
    # Close the slit the "1" cuts through the bottom edge, then fill the holes.
    outside = alpha.filter(ImageFilter.MaxFilter(25))
    ImageDraw.floodfill(outside, (0, 0), 128)
    solid = outside.point(lambda a: 0 if a == 128 else 255).filter(ImageFilter.MinFilter(25))
    solid = solid.crop((m, m, m + img.width, m + img.height))
    out = Image.new("RGBA", img.size, TEAL_RGB + (0,))
    out.putalpha(solid)
    return out


def on_background(cloud_svg: str, size: int, fraction: float) -> Image.Image:
    bg = Image.new("RGBA", (size, size), WHITE + (255,))
    inner = round(size * fraction)
    art = render(cloud_svg, inner)
    bg.alpha_composite(art, ((size - inner) // 2, (size - inner) // 2))
    return bg.convert("RGB")


def save_png(img: Image.Image, path: Path, small: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if small:
        img.save(path, optimize=True)
    else:
        # A palette keeps the files a fraction of the size; the art is one colour.
        img.quantize(colors=64, method=Image.Quantize.FASTOCTREE).save(path, optimize=True)
    print(path.relative_to(ROOT), path.stat().st_size, "bytes")


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "brand/n16chat-logo-source.jpg"
    mask = load_mask(src)
    # The original artwork is 2400 px wide; normalise coordinates to that scale.
    scale = 2400 / mask.shape[1]
    x0, x1, y0, y1 = bbox(mask)
    cols = mask[:, x0:x1].any(axis=0)
    gap = x0 + int(np.argmax(~cols))  # first empty column: end of the cloud
    cx0, cx1, cy0, cy1 = bbox(mask[:, :gap])
    cloud_mask = mask[cy0:cy1, cx0:cx1]
    chat_mask = mask[y0:y1, gap:x1]

    pad = 8
    cw, ch = (cx1 - cx0) * scale, (cy1 - cy0) * scale
    side = max(cw, ch) + 2 * pad
    cloud_d = trace(cloud_mask, scale, -(side - cw) / 2 / scale, -(side - ch) / 2 / scale)
    cloud_svg = svg(round(side), round(side), f'<path fill="{TEAL}" d="{cloud_d}"/>', "n16chat")

    lw, lh = (x1 - x0) * scale + 2 * pad, (y1 - y0) * scale + 2 * pad
    logo_cloud = trace(cloud_mask, scale, x0 - cx0 - pad / scale, y0 - cy0 - pad / scale)
    logo_chat = trace(chat_mask, scale, x0 - gap - pad / scale, -pad / scale)
    logo_svg = svg(
        round(lw),
        round(lh),
        f'<g fill="{TEAL}"><path d="{logo_cloud}"/><path d="{logo_chat}"/></g>',
        "n16chat",
    )

    fe = ROOT / "frontend"
    for path, text in [
        (fe / "src/assets/brand/logo.svg", logo_svg),
        (fe / "src/assets/brand/cloud.svg", cloud_svg),
        (fe / "public/favicon.svg", cloud_svg),
        (ROOT / "extension/src/shared/logo.svg", logo_svg),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(path.relative_to(ROOT), path.stat().st_size, "bytes")

    big = render(cloud_svg, 512)
    tiny = filled_silhouette(big).resize((16, 16), Image.Resampling.LANCZOS)
    save_png(tiny, fe / "public/favicon-16x16.png", small=True)
    save_png(render(cloud_svg, 32), fe / "public/favicon-32x32.png", small=True)
    save_png(on_background(cloud_svg, 180, 0.84), fe / "public/apple-touch-icon.png")
    save_png(on_background(cloud_svg, 192, 0.86), fe / "public/icons/pwa-192x192.png")
    save_png(on_background(cloud_svg, 512, 0.86), fe / "public/icons/pwa-512x512.png")
    # Maskable: the art stays inside the central 80 % safe circle.
    save_png(on_background(cloud_svg, 512, 0.66), fe / "public/icons/maskable-512x512.png")

    ext = ROOT / "extension/icons"
    save_png(tiny, ext / "icon-16.png", small=True)
    for size in (32, 48, 128):
        save_png(render(cloud_svg, size), ext / f"icon-{size}.png", small=True)


if __name__ == "__main__":
    main()
