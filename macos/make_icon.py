"""Draws the Local Studio app icon (1024 px PNG): a dark macOS-style squircle with a white
four-point sparkle, matching the mark in the UI sidebar."""

import sys

from PIL import Image, ImageDraw, ImageFilter

S = 1024
out = sys.argv[1] if len(sys.argv) > 1 else "icon_1024.png"

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# squircle body inset per Apple's icon grid (824 px content on a 1024 canvas)
pad, body = 100, 824
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([pad, pad, pad + body, pad + body], radius=185, fill=255)

# vertical gradient #2b2b30 → #0d0d0f
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / S
    c = tuple(int(a + (b - a) * t) for a, b in zip((52, 52, 58), (13, 13, 15)))
    gd.line([(0, y), (S, y)], fill=c + (255,))

shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle([pad, pad + 14, pad + body, pad + body + 14], radius=185, fill=(0, 0, 0, 110))
img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(18)))
img.paste(grad, (0, 0), mask)

# subtle top highlight
hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(hl).rounded_rectangle([pad + 2, pad + 2, pad + body - 2, pad + body - 2], radius=183,
                                     outline=(255, 255, 255, 38), width=3)
img = Image.alpha_composite(img, hl)


def sparkle(cx, cy, r, w):
    """Four-point star: long arms of radius r, waist half-width w."""
    return [(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w),
            (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)]


star = Image.new("RGBA", (S, S), (0, 0, 0, 0))
sd = ImageDraw.Draw(star)
sd.polygon(sparkle(492, 532, 250, 62), fill=(255, 255, 255, 255))
sd.polygon(sparkle(712, 300, 92, 24), fill=(255, 255, 255, 235))
glow = star.filter(ImageFilter.GaussianBlur(26))
img = Image.alpha_composite(img, Image.eval(glow, lambda v: int(v * 0.45)))
img = Image.alpha_composite(img, star)

img.save(out)
print(out)
