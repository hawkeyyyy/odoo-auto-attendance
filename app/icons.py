"""Tray icon images generated with Pillow (no image files needed at runtime)."""
from __future__ import annotations

from PIL import Image, ImageDraw

COLORS = {
    "checked_in": (46, 160, 67),
    "checked_out": (130, 130, 130),
    "waiting": (222, 170, 0),
    "error": (210, 40, 50),
    "unknown": (130, 130, 130),
}


def make_icon(status: str = "checked_out", size: int = 64) -> Image.Image:
    color = COLORS.get(status, COLORS["unknown"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    d.ellipse((pad, pad, size - pad, size - pad), fill=color + (255,),
              outline=(255, 255, 255, 255), width=max(2, size // 20))
    c = size / 2
    w = max(3, size // 12)
    d.line((c, c, c, size * 0.24), fill="white", width=w)       # clock hands
    d.line((c, c, size * 0.72, c), fill="white", width=w)
    return img


def write_png(path: str) -> None:
    """1024 px PNG; PyInstaller converts it to .icns for the macOS app bundle."""
    make_icon("checked_in", 1024).save(path)


def write_ico(path: str) -> None:
    make_icon("checked_in", 256).save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
