"""Renders the Local Studio logo concepts. `python logos.py sheet out.png` draws a
comparison sheet; `python logos.py <A|B|C> out.png` draws one 1024 px app icon."""

import math
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

S = 1024
PAD, BODY, RAD = 100, 824, 185          # Apple icon grid: 824 px squircle on a 1024 canvas


def squircle_mask(scale=1):
    m = Image.new("L", (S * scale, S * scale), 0)
    ImageDraw.Draw(m).rounded_rectangle([PAD * scale, PAD * scale, (PAD + BODY) * scale, (PAD + BODY) * scale],
                                        radius=RAD * scale, fill=255)
    return m


def gradient(c1, c2, angle=90):
    """Linear gradient across the canvas; angle 90 = top→bottom, 135 = top-left→bottom-right."""
    g = Image.new("RGB", (S, S))
    px = g.load()
    a = math.radians(angle)
    dx, dy = math.cos(a), math.sin(a)
    lo = min(0, dx * S) + min(0, dy * S)
    hi = max(0, dx * S) + max(0, dy * S)
    for y in range(S):
        for x in range(0, S, 2):
            t = ((x * dx + y * dy) - lo) / (hi - lo)
            c = tuple(int(p + (q - p) * t) for p, q in zip(c1, c2))
            px[x, y] = c
            if x + 1 < S:
                px[x + 1, y] = c
    return g


def base(bg):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([PAD, PAD + 16, PAD + BODY, PAD + BODY + 16], radius=RAD, fill=(0, 0, 0, 120))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(20)))
    img.paste(bg, (0, 0), squircle_mask())
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle([PAD + 2, PAD + 2, PAD + BODY - 2, PAD + BODY - 2], radius=RAD - 2,
                                          outline=(255, 255, 255, 46), width=3)
    return Image.alpha_composite(img, rim)


def sparkle(cx, cy, r, waist):
    pts = []
    for i in range(8):
        ang = -math.pi / 2 + i * math.pi / 4
        rad = r if i % 2 == 0 else waist
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    return pts


def curved_sparkle(cx, cy, r, steps=64, k=3.2):
    """Astroid-like star with concave sides — softer and more 'designed' than a polygon."""
    pts = []
    for i in range(steps * 4):
        t = 2 * math.pi * i / (steps * 4)
        x = math.copysign(abs(math.cos(t)) ** k, math.cos(t))
        y = math.copysign(abs(math.sin(t)) ** k, math.sin(t))
        pts.append((cx + r * x, cy + r * y))
    return pts


def draw_aa(fn, scale=4):
    """Supersampled RGBA layer drawn by fn(draw, scale)."""
    big = Image.new("RGBA", (S * scale, S * scale), (0, 0, 0, 0))
    fn(ImageDraw.Draw(big), scale)
    return big.resize((S, S), Image.LANCZOS)


def concept_a():
    """Midnight squircle, curved white spark with a small companion spark and soft glow."""
    img = base(gradient((40, 42, 54), (10, 10, 14), 100))

    def fn(d, s):
        d.polygon([(x * s, y * s) for x, y in curved_sparkle(500, 535, 285)], fill=(255, 255, 255, 255))
        d.polygon([(x * s, y * s) for x, y in curved_sparkle(735, 290, 92)], fill=(255, 255, 255, 230))
    layer = draw_aa(fn)
    glow = layer.filter(ImageFilter.GaussianBlur(30))
    glow = Image.merge("RGBA", (*Image.new("RGB", (S, S), (150, 160, 255)).split(), glow.split()[3].point(lambda v: int(v * .55))))
    img = Image.alpha_composite(img, glow)
    return Image.alpha_composite(img, layer)


def concept_b():
    """Silicon: a chip outline (on-device) with the spark cut into its die. Graphite + white."""
    img = base(gradient((248, 248, 250), (220, 221, 228), 100))

    def fn(d, s):
        ink = (18, 18, 22, 255)
        x0, y0, x1, y1 = 300, 300, 724, 724
        d.rounded_rectangle([x0 * s, y0 * s, x1 * s, y1 * s], radius=70 * s, fill=ink)
        for i in range(4):
            off = 360 + i * 101
            for a, b, c, e in ((off, 230, off, 300), (off, 724, off, 794), (230, off, 300, off), (724, off, 794, off)):
                d.line([(a * s, b * s), (c * s, e * s)], fill=ink, width=26 * s)
            for (a, b) in ((off, 230), (off, 794), (230, off), (794, off)):
                d.ellipse([(a - 17) * s, (b - 17) * s, (a + 17) * s, (b + 17) * s], fill=ink)
        d.polygon([(x * s, y * s) for x, y in curved_sparkle(512, 512, 150)], fill=(248, 248, 250, 255))
    return Image.alpha_composite(img, draw_aa(fn))


def concept_c():
    """Aurora: violet→indigo gradient, white curved spark framed by a thin orbit ring."""
    img = base(gradient((124, 92, 255), (33, 40, 120), 125))
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hl).ellipse([150, 60, 760, 560], fill=(255, 255, 255, 60))
    img = Image.alpha_composite(img, Image.composite(hl.filter(ImageFilter.GaussianBlur(90)), Image.new("RGBA", (S, S)), squircle_mask()))

    def fn(d, s):
        d.ellipse([232 * s, 232 * s, 792 * s, 792 * s], outline=(255, 255, 255, 120), width=10 * s)
        d.polygon([(x * s, y * s) for x, y in curved_sparkle(512, 512, 255)], fill=(255, 255, 255, 255))
        d.ellipse([742 * s, 330 * s, 790 * s, 378 * s], fill=(255, 255, 255, 255))
    return Image.alpha_composite(img, draw_aa(fn))


CONCEPTS = {"A": ("Midnight Spark", concept_a), "B": ("Silicon", concept_b), "C": ("Aurora Orbit", concept_c)}


def sheet(out):
    W, H = 2400, 1180
    bg = Image.new("RGB", (W, H), (245, 245, 247))
    d = ImageDraw.Draw(bg)
    try:
        f1 = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 54)
        f2 = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 34)
    except OSError:
        f1 = f2 = ImageFont.load_default()
    d.text((80, 50), "Local Studio — logo concepts", fill=(20, 20, 24), font=f1)
    for i, (k, (name, fn)) in enumerate(CONCEPTS.items()):
        icon = fn()
        x = 80 + i * 780
        big = icon.resize((600, 600), Image.LANCZOS)
        bg.paste(big, (x, 170), big)
        d.text((x + 20, 800), f"{k}  ·  {name}", fill=(20, 20, 24), font=f1)
        # dock-size previews on light and dark
        d.rounded_rectangle([x + 20, 900, x + 600, 1110], radius=28, fill=(30, 30, 34))
        for j, sz in enumerate((128, 64, 32)):
            sm = icon.resize((sz, sz), Image.LANCZOS)
            bg.paste(sm, (x + 50 + j * 170, 1005 - sz // 2), sm)
    bg.save(out)


if __name__ == "__main__":
    which, out = sys.argv[1], sys.argv[2]
    if which == "sheet":
        sheet(out)
    else:
        CONCEPTS[which][1]().save(out)
    print(out)
