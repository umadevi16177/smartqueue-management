"""Generate the 4 indoor-navigation floor map PNGs.

One-shot: re-run if you change room positions or labels. Output goes into
app/static/floor_maps/.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "app" / "static" / "floor_maps"

W, H = 900, 560
BG = (2, 6, 23)              # near-black, matches the architecture diagram
PANEL = (15, 23, 42)
EDGE = (51, 65, 85)
ROOM = (30, 41, 59)
ROOM_BORDER = (71, 85, 105)
HIGHLIGHT = (8, 51, 68)
HIGHLIGHT_BORDER = (34, 211, 238)   # cyan
ACCENT = (52, 211, 153)             # green for path
TEXT = (226, 232, 240)
SUBTEXT = (148, 163, 184)
ARROW = (251, 146, 60)              # orange, matches floor zone in diagram
LEGEND_TEXT = (100, 116, 139)


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Menlo.ttc",
        "/System/Library/Fonts/Menlo.ttc",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


F_TITLE = _font(22)
F_LABEL = _font(15)
F_SUB = _font(12)
F_TINY = _font(10)


def _frame(draw: ImageDraw.ImageDraw, title: str, subtitle: str) -> None:
    draw.rectangle((0, 0, W - 1, H - 1), fill=BG, outline=EDGE, width=2)
    draw.rectangle((20, 20, W - 21, 70), fill=PANEL, outline=EDGE, width=1)
    draw.text((36, 30), title, fill=TEXT, font=F_TITLE)
    draw.text((36, 55), subtitle, fill=SUBTEXT, font=F_SUB)


def _legend(draw: ImageDraw.ImageDraw) -> None:
    y = H - 36
    draw.rectangle((22, y, 36, y + 14), fill=HIGHLIGHT, outline=HIGHLIGHT_BORDER, width=2)
    draw.text((44, y + 1), "Your destination", fill=LEGEND_TEXT, font=F_TINY)
    draw.line((180, y + 7, 220, y + 7), fill=ACCENT, width=3)
    draw.text((228, y + 1), "Walking path", fill=LEGEND_TEXT, font=F_TINY)
    draw.rectangle((340, y, 354, y + 14), fill=ROOM, outline=ROOM_BORDER, width=1)
    draw.text((362, y + 1), "Other rooms", fill=LEGEND_TEXT, font=F_TINY)


def _room(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    *,
    highlight: bool = False,
    sub: str = "",
) -> None:
    fill = HIGHLIGHT if highlight else ROOM
    border = HIGHLIGHT_BORDER if highlight else ROOM_BORDER
    width = 3 if highlight else 1
    draw.rectangle(box, fill=fill, outline=border, width=width)
    x = (box[0] + box[2]) // 2
    y = (box[1] + box[3]) // 2 - (8 if sub else 0)
    bbox = draw.textbbox((0, 0), label, font=F_LABEL)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x - tw // 2, y - th // 2), label, fill=TEXT, font=F_LABEL)
    if sub:
        bbox = draw.textbbox((0, 0), sub, font=F_TINY)
        sw, sh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((x - sw // 2, y + th // 2 + 2), sub, fill=SUBTEXT, font=F_TINY)


def _arrow(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[int, int]],
    color: tuple[int, int, int] = ACCENT,
) -> None:
    for a, b in zip(points, points[1:]):
        draw.line((a, b), fill=color, width=4)
    # Arrowhead at last segment.
    if len(points) >= 2:
        x1, y1 = points[-2]
        x2, y2 = points[-1]
        # 8px arrowhead
        if x1 == x2:  # vertical
            dy = 8 if y2 > y1 else -8
            draw.polygon(
                [(x2, y2), (x2 - 6, y2 - dy), (x2 + 6, y2 - dy)], fill=color
            )
        elif y1 == y2:  # horizontal
            dx = 8 if x2 > x1 else -8
            draw.polygon(
                [(x2, y2), (x2 - dx, y2 - 6), (x2 - dx, y2 + 6)], fill=color
            )


def _you_are_here(draw: ImageDraw.ImageDraw, x: int, y: int, lang_label: str = "Entry") -> None:
    draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill=ARROW, outline=TEXT, width=2)
    bbox = draw.textbbox((0, 0), lang_label, font=F_TINY)
    tw = bbox[2] - bbox[0]
    draw.text((max(4, x - tw // 2), y + 14), lang_label, fill=ARROW, font=F_TINY)


# ─── Floor maps ──────────────────────────────────────────────────────────────


def make_blood() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _frame(d, "Ground Floor — Blood Test", "Pathology Lab · Room G-12")
    _room(d, (60, 100, 280, 230), "Main Lobby")
    _room(d, (320, 100, 460, 230), "Elevators")
    _room(d, (60, 290, 280, 390), "Reception")
    _room(d, (320, 290, 460, 390), "Pharmacy")
    _room(d, (520, 130, 820, 340), "Pathology Lab", highlight=True, sub="Room G-12 · Blood Test")
    # Path runs along the corridor at y=260 (gap between row 1 and row 2 rooms).
    _arrow(d, [(45, 260), (510, 260), (560, 290)])
    _you_are_here(d, 45, 260, "Entry")
    _legend(d)
    return img


def make_ecg() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _frame(d, "Second Floor — ECG", "Cardiology · Room 204")
    _room(d, (60, 130, 200, 280), "Elevator")
    # Visual corridor (the patient walks through this).
    d.rectangle((220, 200, 620, 230), outline=EDGE, width=1)
    d.text((380, 232), "Corridor", fill=SUBTEXT, font=F_SUB)
    _room(d, (220, 100, 400, 185), "Waiting Area")
    _room(d, (420, 100, 600, 185), "Nurses Station")
    _room(d, (220, 250, 400, 360), "Consult 1")
    _room(d, (420, 250, 600, 360), "Consult 2")
    _room(d, (640, 100, 830, 360), "Cardiology", highlight=True, sub="Room 204 · ECG")
    _arrow(d, [(45, 215), (640, 215)])
    _you_are_here(d, 45, 215, "Exit lift")
    _legend(d)
    return img


def make_ultrasound() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _frame(d, "First Floor — Ultrasound", "Imaging Wing · Room 110")
    _room(d, (60, 100, 240, 230), "Stairs &\nElevator")
    _room(d, (60, 290, 240, 410), "Lobby")
    _room(d, (280, 100, 460, 230), "Reception")
    _room(d, (280, 290, 460, 410), "Specimen Room")
    _room(d, (500, 130, 830, 410), "Imaging Wing", highlight=True, sub="Room 110 · Ultrasound")
    _arrow(d, [(45, 260), (490, 260)])
    _you_are_here(d, 45, 260, "Exit lift")
    _legend(d)
    return img


def make_xray() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _frame(d, "Third Floor — X-Ray", "Radiology · Room 302 — Final test")
    _room(d, (60, 100, 220, 230), "Elevator")
    _room(d, (260, 100, 480, 230), "Reception Desk")
    _room(d, (520, 100, 700, 230), "Storage")
    _room(d, (60, 290, 220, 410), "Stairs")
    _room(d, (260, 290, 480, 410), "Changing Area")
    _room(d, (520, 290, 700, 410), "Lead Apron Locker")
    _room(d, (720, 130, 850, 380), "Radiology", highlight=True, sub="Room 302 · X-Ray")
    _arrow(d, [(45, 260), (710, 260), (720, 250)])
    _you_are_here(d, 45, 260, "Exit lift")
    _legend(d)
    return img


GENERATORS = {
    "BLOOD": ("blood.png", make_blood),
    "ECG": ("ecg.png", make_ecg),
    "ULTRASOUND": ("ultrasound.png", make_ultrasound),
    "XRAY": ("xray.png", make_xray),
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for code, (filename, generator) in GENERATORS.items():
        img = generator()
        path = OUT_DIR / filename
        img.save(path, format="PNG", optimize=True)
        print(f"  {code} → {path.relative_to(ROOT)}")
    print(f"\nGenerated {len(GENERATORS)} floor maps in {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
