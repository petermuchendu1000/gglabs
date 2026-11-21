"""Generate per-company install assets: icon, QR, install page, privacy, hashes, manifest.

Outputs placed under pipeline_output/apps/{company_number}/
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# ── Colour helpers ────────────────────────────────────────────────────────────

def _hex_to_rgb(h: str) -> tuple:
    h = h.lstrip('#')
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _darken(rgb: tuple, f: float = 0.30) -> tuple:
    return tuple(max(0, int(c * (1 - f))) for c in rgb)

def _lighten(rgb: tuple, f: float = 0.50) -> tuple:
    return tuple(min(255, int(c + (255 - c) * f)) for c in rgb)

def _company_seed(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)

def _text_center(draw, text: str, font, cx: float, cy: float,
                 fill=(255, 255, 255), shadow=None) -> None:
    try:
        bb = draw.textbbox((0, 0), text, font=font)
        w, h, ox, oy = bb[2]-bb[0], bb[3]-bb[1], bb[0], bb[1]
    except Exception:
        w, h, ox, oy = (*draw.textsize(text, font=font), 0, 0)
    x, y = cx - w / 2 - ox, cy - h / 2 - oy
    if shadow:
        draw.text((x + 2, y + 2), text, fill=shadow, font=font)
    draw.text((x, y), text, fill=fill, font=font)

def _clip_circle(img: Image.Image, size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([(0, 0), (size - 1, size - 1)], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), mask=mask)
    return out


# ── Initials ──────────────────────────────────────────────────────────────────

def _initials(name: str) -> str:
    parts = [p for p in name.replace('-', ' ').split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def make_icon_png(dest: Path, color: str, name: str, size: int = 512,
                  company_number: str = "") -> None:
    """Generate a unique app icon using one of 6 distinct visual styles.

    Style is selected deterministically from the company number hash so
    the same company always produces the same design across rebuilds.

    Styles:
      0 – Solid circle with inner accent ring
      1 – Diagonal two-tone split
      2 – White background + brand rounded-square
      3 – White background + brand hexagon
      4 – Outer ring + filled inner circle + 6 orbital dots
      5 – Light tinted background + oversized initial + bottom bar
    """
    rgb  = _hex_to_rgb(color)
    seed = _company_seed(company_number or name)
    style = seed % 6
    cx = cy = size // 2

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw   = ImageDraw.Draw(canvas)

    initials = _initials(name)
    font     = _load_font(size // 3)
    font_xl  = _load_font(int(size * 0.52))

    if style == 0:
        # ── Solid circle with inner accent ring ───────────────────────────────
        draw.ellipse([(0, 0), (size-1, size-1)], fill=rgb)
        lighter = _lighten(rgb, 0.28)
        rr = int(size * 0.37)
        draw.ellipse([(cx-rr, cy-rr), (cx+rr, cy+rr)], fill=lighter)
        ri = int(size * 0.30)
        draw.ellipse([(cx-ri, cy-ri), (cx+ri, cy+ri)], fill=rgb)
        _text_center(draw, initials, font, cx, cy, (255,255,255), shadow=(0,0,0,70))

    elif style == 1:
        # ── Diagonal two-tone split ────────────────────────────────────────────
        dark = _darken(rgb, 0.32)
        draw.polygon([(0,0),(size,0),(0,size)], fill=dark)
        draw.polygon([(size,0),(size,size),(0,size)], fill=rgb)
        lw = max(2, size // 80)
        draw.line([(size, 0), (0, size)], fill=(255,255,255,55), width=lw)
        _text_center(draw, initials, font, cx, cy, (255,255,255), shadow=(0,0,0,90))

    elif style == 2:
        # ── White background + brand rounded-square ────────────────────────────
        draw.ellipse([(0,0),(size-1,size-1)], fill=(255,255,255))
        m = int(size * 0.09)
        draw.rounded_rectangle([(m, m), (size-m, size-m)],
                                radius=int(size * 0.20), fill=rgb)
        _text_center(draw, initials, font, cx, cy, (255,255,255))

    elif style == 3:
        # ── White background + brand hexagon ──────────────────────────────────
        draw.ellipse([(0,0),(size-1,size-1)], fill=(255,255,255))
        r_hex = int(size * 0.41)
        pts = [
            (cx + r_hex * math.cos(math.pi/6 + i * math.pi/3),
             cy + r_hex * math.sin(math.pi/6 + i * math.pi/3))
            for i in range(6)
        ]
        draw.polygon(pts, fill=rgb)
        _text_center(draw, initials, font, cx, cy, (255,255,255))

    elif style == 4:
        # ── Outer ring + inner circle + 6 orbital dots ────────────────────────
        outer = _lighten(rgb, 0.30)
        draw.ellipse([(0,0),(size-1,size-1)], fill=outer)
        ri = int(size * 0.34)
        draw.ellipse([(cx-ri,cy-ri),(cx+ri,cy+ri)], fill=rgb)
        dot_r  = max(3, int(size * 0.040))
        orbit  = int(size * 0.41)
        for i in range(6):
            a  = i * math.pi / 3
            dx = cx + int(orbit * math.cos(a))
            dy = cy + int(orbit * math.sin(a))
            draw.ellipse([(dx-dot_r, dy-dot_r), (dx+dot_r, dy+dot_r)],
                         fill=(255, 255, 255, 200))
        _text_center(draw, initials, font, cx, cy, (255,255,255))

    else:  # style == 5
        # ── Light tint background + oversized initial + bottom accent bar ─────
        tint = _lighten(rgb, 0.86)
        draw.ellipse([(0,0),(size-1,size-1)], fill=tint)
        _text_center(draw, initials[0], font_xl, cx, cy - int(size * 0.06), rgb)
        bar_h = int(size * 0.155)
        draw.rectangle([(0, size - bar_h), (size, size)], fill=rgb)

    result = _clip_circle(canvas, size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result.save(dest, format="PNG")


def _load_font(size: int):
    for name in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def make_feature_graphic(dest: Path, color: str, name: str,
                          company_number: str = "") -> None:
    """Create a 1024×500 feature graphic (Google Play).

    Four background styles chosen by company number hash:
      0 – Solid colour + fade-to-dark overlay
      1 – Diagonal two-tone split (dark left, primary right)
      2 – Solid colour + repeating dot-grid pattern
      3 – Solid colour + large offset accent circle for depth
    """
    W, H = 1024, 500
    rgb   = _hex_to_rgb(color)
    dark  = _darken(rgb, 0.38)
    seed  = _company_seed(company_number or name)
    style = seed % 4

    img  = Image.new("RGB", (W, H), rgb)
    draw = ImageDraw.Draw(img)

    if style == 0:
        # Fade-to-dark gradient at bottom
        for y in range(H // 2, H):
            alpha = int(55 * (y - H // 2) / (H // 2))
            row = Image.new("RGBA", (W, 1), (0, 0, 0, alpha))
            img.paste(row.convert("RGB"), (0, y), row)

    elif style == 1:
        # Diagonal split: dark top-left triangle, primary bottom-right
        draw.polygon([(0, 0), (W, 0), (0, H)], fill=dark)
        draw.polygon([(W, 0), (W, H), (0, H)], fill=rgb)
        lw = max(3, W // 100)
        draw.line([(W, 0), (0, H)], fill=(255,255,255,40), width=lw)

    elif style == 2:
        # Dot-grid overlay
        dot_r   = max(2, W // 80)
        spacing = W // 14
        for gx in range(0, W + spacing, spacing):
            for gy in range(0, H + spacing, spacing):
                draw.ellipse([(gx-dot_r, gy-dot_r), (gx+dot_r, gy+dot_r)],
                             fill=_lighten(rgb, 0.20))

    else:  # style == 3
        # Large offset circle in lighter tint for depth
        lighter = _lighten(rgb, 0.22)
        cr = int(H * 1.1)
        draw.ellipse([(W - cr - int(H*0.1), H//2 - cr),
                      (W - int(H*0.1) + cr, H//2 + cr)], fill=lighter)

    # Text: company name + tagline centred
    font_lg = _load_font(68)
    font_sm = _load_font(28)

    taglines = [
        "Work Journal  •  Local  •  Private  •  Offline",
        "Simple. Secure. No Account Required.",
        "Record your work. Stay in control.",
        "Business Tools  •  100% Offline  •  No Permissions",
    ]
    tagline = taglines[seed % len(taglines)]

    try:
        bb = draw.textbbox((0, 0), name, font=font_lg)
        tw, th = bb[2]-bb[0], bb[3]-bb[1]
    except Exception:
        tw, th = draw.textsize(name, font=font_lg)

    cy = H // 2
    draw.text(((W - tw) / 2, cy - th - 14), name,
              fill=(255,255,255), font=font_lg)

    try:
        bb2 = draw.textbbox((0, 0), tagline, font=font_sm)
        tw2 = bb2[2] - bb2[0]
    except Exception:
        tw2, _ = draw.textsize(tagline, font=font_sm)
    draw.text(((W - tw2) / 2, cy + 14), tagline,
              fill=(255, 255, 255, 180), font=font_sm)

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")


def make_phone_screenshot(
    dest: Path,
    color: str,
    name: str,
    screen: str = "dashboard",
    size: tuple[int, int] = (1080, 1920),
    role_noun: str = "shift",
    role_verb_start: str = "Start Shift",
    role_verb_end: str = "End Shift",
) -> None:
    """Generate a phone/tablet screenshot mockup (24-bit PNG, no alpha).

    size: (W, H) in pixels. Defaults to 1080x1920 (phone).
           Pass (1200, 1920) for 7-inch tablet, (1600, 2560) for 10-inch tablet.
    """
    W, H = size
    rgb = tuple(int(color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
    dark_rgb = _darken(rgb, 0.35)
    light_rgb = _lighten(rgb, 0.55)

    bg = Image.new("RGB", (W, H), (248, 248, 252))
    draw = ImageDraw.Draw(bg)

    status_bar_h = 80
    draw.rectangle([(0, 0), (W, status_bar_h)], fill=dark_rgb)

    nav_bar_h = 180
    nav_y = H - nav_bar_h
    draw.rectangle([(0, nav_y), (W, H)], fill=(255, 255, 255))
    draw.line([(0, nav_y), (W, nav_y)], fill=(220, 220, 228), width=2)

    font_title = _load_font(56)
    font_body = _load_font(38)
    font_small = _load_font(30)
    font_label = _load_font(26)

    content_top = status_bar_h + 60
    pad = 60

    _NAV_TABS = ["Home", "Log", "Calc", "Info", "Contact"]

    def _nav(active_idx: int) -> None:
        tab_w = W // len(_NAV_TABS)
        for i, label in enumerate(_NAV_TABS):
            x = tab_w // 2 + i * tab_w
            is_active = i == active_idx
            dot_color = rgb if is_active else (180, 180, 190)
            lbl_color = rgb if is_active else (150, 150, 160)
            # Active tab: filled pill indicator
            if is_active:
                pill_w, pill_h = 80, 8
                draw.rounded_rectangle(
                    [(x - pill_w // 2, nav_y + 14), (x + pill_w // 2, nav_y + 14 + pill_h)],
                    radius=4, fill=rgb,
                )
            # Icon dot
            draw.ellipse([(x - 14, nav_y + 38), (x + 14, nav_y + 66)], fill=dot_color)
            # Label
            lbl_x = x - len(label) * 7
            draw.text((lbl_x, nav_y + 80 + (nav_bar_h - 120) // 2), label, fill=lbl_color, font=font_label)

    if screen == "dashboard":
        # Hero gradient banner
        hero_h = 300
        for row in range(hero_h):
            t = row / hero_h
            r2 = int(dark_rgb[0] * (1 - t) + rgb[0] * t)
            g2 = int(dark_rgb[1] * (1 - t) + rgb[1] * t)
            b2 = int(dark_rgb[2] * (1 - t) + rgb[2] * t)
            draw.line([(0, content_top + row), (W, content_top + row)], fill=(r2, g2, b2))
        # Company name + subtitle in hero
        draw.text((pad, content_top + 50), name[:28], fill=(255, 255, 255), font=font_title)
        role_cap = role_noun[0].upper() + role_noun[1:] if role_noun else "Shift"
        draw.text((pad, content_top + 130), f"{role_cap} Journal", fill=(220, 230, 255), font=font_small)
        # Stat chips in hero
        chip_w = (W - pad * 3) // 2
        chip_y = content_top + 195
        for i, (label, val) in enumerate([("Today", "3"), ("Total", "12")]):
            x = pad + i * (chip_w + pad)
            draw.rounded_rectangle([(x, chip_y), (x + chip_w, chip_y + 80)], radius=20,
                                   fill=(255, 255, 255, 60) if i == 0 else (0, 0, 0, 40))
            draw.text((x + 20, chip_y + 8), val, fill=(255, 255, 255), font=font_body)
            draw.text((x + 20 + len(val) * 18 + 8, chip_y + 18), label, fill=(200, 215, 255), font=font_label)
        content_top += hero_h + 40

        # "No active session" card
        card_h = 220
        draw.rounded_rectangle([(pad, content_top), (W - pad, content_top + card_h)], radius=28, fill=(255, 255, 255))
        draw.text((pad + 40, content_top + 28), f"No active {role_noun}", fill=(100, 100, 110), font=font_body)
        btn_y = content_top + 110
        draw.rounded_rectangle([(pad + 40, btn_y), (W - pad - 40, btn_y + 80)], radius=20, fill=rgb)
        btn_lbl = role_verb_start
        draw.text((W // 2 - len(btn_lbl) * 10, btn_y + 20), btn_lbl, fill=(255, 255, 255), font=font_body)
        content_top += card_h + 48

        draw.text((pad, content_top), "Recent Activity", fill=(40, 40, 50), font=font_body)
        content_top += 64
        for i in range(3):
            ey = content_top + i * 124
            if ey + 110 > nav_y - 20:
                break
            draw.rounded_rectangle([(pad, ey), (W - pad, ey + 108)], radius=20, fill=(255, 255, 255))
            # Left accent bar
            draw.rounded_rectangle([(pad, ey), (pad + 6, ey + 108)], radius=3, fill=rgb)
            draw.text((pad + 24, ey + 16), f"#{i + 1}  09:0{i} – 17:0{i}", fill=(30, 30, 40), font=font_small)
            draw.text((pad + 24, ey + 60), "Today", fill=(160, 160, 170), font=font_label)
            draw.text((W - pad - 130, ey + 30), f"8h 0{i}m", fill=rgb, font=font_small)
        _nav(0)

    elif screen == "dashboard_active":
        # Hero with gradient
        hero_h = 300
        for row in range(hero_h):
            t = row / hero_h
            r2 = int(dark_rgb[0] * (1 - t) + rgb[0] * t)
            g2 = int(dark_rgb[1] * (1 - t) + rgb[1] * t)
            b2 = int(dark_rgb[2] * (1 - t) + rgb[2] * t)
            draw.line([(0, content_top + row), (W, content_top + row)], fill=(r2, g2, b2))
        draw.text((pad, content_top + 50), name[:28], fill=(255, 255, 255), font=font_title)
        role_cap = role_noun[0].upper() + role_noun[1:] if role_noun else "Shift"
        draw.text((pad, content_top + 130), f"{role_cap} Journal", fill=(220, 230, 255), font=font_small)
        chip_w = (W - pad * 3) // 2
        chip_y = content_top + 195
        for i, (label, val) in enumerate([("Today", "4"), ("Total", "15")]):
            x = pad + i * (chip_w + pad)
            draw.rounded_rectangle([(x, chip_y), (x + chip_w, chip_y + 80)], radius=20,
                                   fill=(255, 255, 255, 60) if i == 0 else (0, 0, 0, 40))
            draw.text((x + 20, chip_y + 8), val, fill=(255, 255, 255), font=font_body)
            draw.text((x + 20 + len(val) * 18 + 8, chip_y + 18), label, fill=(200, 215, 255), font=font_label)
        content_top += hero_h + 40

        # Active session card (highlighted)
        card_h = 360
        draw.rounded_rectangle([(pad, content_top), (W - pad, content_top + card_h)], radius=28, fill=light_rgb)
        # Pulsing dot + "in progress" label
        dot_x, dot_y = pad + 40, content_top + 42
        draw.ellipse([(dot_x - 4, dot_y - 4), (dot_x + 20, dot_y + 20)], fill=(200, 255, 200))
        draw.ellipse([(dot_x, dot_y), (dot_x + 16, dot_y + 16)], fill=(0, 210, 100))
        role_ip = role_noun[0].upper() + role_noun[1:]
        draw.text((dot_x + 28, content_top + 32), f"{role_ip} in progress · 02:34:17", fill=(30, 30, 40), font=font_small)
        # Note field outline
        note_y = content_top + 100
        draw.rounded_rectangle([(pad + 24, note_y), (W - pad - 24, note_y + 140)], radius=14, fill=(255, 255, 255))
        draw.text((pad + 44, note_y + 14), "Note (optional)", fill=(140, 140, 150), font=font_label)
        draw.text((pad + 44, note_y + 56), "Completed safety walkthrough.", fill=(40, 40, 50), font=font_small)
        # End button (danger red)
        btn_y = content_top + 268
        draw.rounded_rectangle([(pad + 40, btn_y), (W - pad - 40, btn_y + 78)], radius=20, fill=(220, 50, 50))
        btn_lbl = role_verb_end
        draw.text((W // 2 - len(btn_lbl) * 10, btn_y + 18), btn_lbl, fill=(255, 255, 255), font=font_body)
        content_top += card_h + 48

        draw.text((pad, content_top), "Recent Activity", fill=(40, 40, 50), font=font_body)
        content_top += 64
        for i in range(2):
            ey = content_top + i * 124
            if ey + 110 > nav_y - 20:
                break
            draw.rounded_rectangle([(pad, ey), (W - pad, ey + 108)], radius=20, fill=(255, 255, 255))
            draw.rounded_rectangle([(pad, ey), (pad + 6, ey + 108)], radius=3, fill=rgb)
            draw.text((pad + 24, ey + 16), f"#{i + 1}  08:0{i} – 16:0{i}", fill=(30, 30, 40), font=font_small)
            draw.text((pad + 24, ey + 60), "Today", fill=(160, 160, 170), font=font_label)
            draw.text((W - pad - 130, ey + 30), "8h 00m", fill=rgb, font=font_small)
        _nav(0)

    elif screen == "log":
        draw.text((pad, content_top), "Activity Log", fill=(30, 30, 40), font=font_title)
        content_top += 20
        # Stat pills row
        for i, (lbl, val) in enumerate([("Entries", "27"), ("Days", "14")]):
            pill_x = pad + i * 240
            draw.rounded_rectangle([(pill_x, content_top + 10), (pill_x + 210, content_top + 60)],
                                   radius=14, fill=light_rgb)
            draw.text((pill_x + 20, content_top + 16), val, fill=rgb, font=font_body)
            draw.text((pill_x + 20 + len(val) * 18 + 8, content_top + 26), lbl, fill=(100, 100, 120), font=font_label)
        content_top += 80
        for i in range(8):
            ey = content_top + i * 124
            if ey + 108 > nav_y - 20:
                break
            draw.rounded_rectangle([(pad, ey), (W - pad, ey + 108)], radius=20, fill=(255, 255, 255))
            draw.rounded_rectangle([(pad, ey), (pad + 6, ey + 108)], radius=3, fill=rgb)
            draw.text((pad + 24, ey + 16), f"#{27 - i}  09:00 – 17:30", fill=(30, 30, 40), font=font_small)
            draw.text((pad + 24, ey + 60), "Today" if i == 0 else "Yesterday", fill=(160, 160, 170), font=font_label)
            # Duration chip
            chip_x = W - pad - 160
            draw.rounded_rectangle([(chip_x, ey + 30), (chip_x + 130, ey + 74)], radius=10, fill=light_rgb)
            draw.text((chip_x + 12, ey + 38), "8h 30m", fill=rgb, font=font_label)
        _nav(1)

    elif screen == "calculator":
        draw.text((pad, content_top), "Calculator", fill=(30, 30, 40), font=font_title)
        content_top += 90

        # Input card
        card_h = 520
        draw.rounded_rectangle([(pad, content_top), (W - pad, content_top + card_h)], radius=28, fill=(255, 255, 255))
        iy = content_top + 40
        for field_lbl, field_val in [("Hours worked", "8.5"), ("Hourly rate (£)", "15.50")]:
            draw.text((pad + 36, iy), field_lbl, fill=(120, 120, 130), font=font_label)
            iy += 40
            draw.rounded_rectangle([(pad + 30, iy), (W - pad - 30, iy + 90)], radius=14, fill=(248, 248, 252))
            draw.text((pad + 50, iy + 22), field_val, fill=(30, 30, 40), font=font_body)
            iy += 110
        # Divider
        draw.line([(pad + 30, iy), (W - pad - 30, iy)], fill=(220, 220, 230), width=2)
        iy += 24
        # Result row
        draw.text((pad + 36, iy), "Earnings", fill=(120, 120, 130), font=font_label)
        draw.text((W - pad - 160, iy), "£131.75", fill=rgb, font=font_title)
        iy += 64
        # VAT breakdown
        draw.rounded_rectangle([(pad + 30, iy), (W - pad - 30, iy + 80)], radius=14, fill=light_rgb)
        draw.text((pad + 50, iy + 12), "Net: £109.79   VAT 20%: £21.96", fill=(60, 60, 70), font=font_label)
        draw.text((pad + 50, iy + 48), "Gross: £131.75", fill=rgb, font=font_small)
        content_top += card_h + 48

        # Export row
        draw.rounded_rectangle([(pad, content_top), (W - pad, content_top + 90)], radius=24, fill=rgb)
        draw.text((W // 2 - 100, content_top + 22), "Export CSV", fill=(255, 255, 255), font=font_body)
        _nav(2)

    elif screen == "settings":
        draw.text((pad, content_top), "Contact", fill=(30, 30, 40), font=font_title)
        content_top += 100

        def _section(title: str, items: list[tuple[str, str]]) -> None:
            nonlocal content_top
            draw.text((pad, content_top), title, fill=rgb, font=font_small)
            content_top += 50
            card_inner_h = len(items) * 100 + 20
            draw.rounded_rectangle([(pad, content_top), (W - pad, content_top + card_inner_h)], radius=20, fill=(255, 255, 255))
            for j, (lbl, val) in enumerate(items):
                iy = content_top + 20 + j * 100
                draw.text((pad + 30, iy), lbl, fill=(140, 140, 150), font=font_label)
                display_val = val if len(val) <= 34 else val[:31] + "…"
                draw.text((pad + 30, iy + 34), display_val, fill=(30, 30, 40), font=font_small)
            content_top += card_inner_h + 50

        _section("Organisation", [
            ("Company", name),
            ("Company No.", "00000000"),
        ])
        _section("Support", [
            ("Email", "support@company.co.uk"),
        ])
        _section("App", [
            ("Version", "1.0"),
            ("Storage", "Local only"),
        ])
        _nav(4)

    dest.parent.mkdir(parents=True, exist_ok=True)
    bg.save(dest, format="PNG")



def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def make_install_page(dest: Path, apk_name: str, aab_name: str | None, company: dict[str, str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    apk_line = f"<p><a href=\"{apk_name}\">Download APK</a></p>" if apk_name else "<p>APK not generated.</p>"
    aab_line = f"<p>AAB: <code>{aab_name}</code></p>" if aab_name else ""
    aab_sha_line = f"<p>AAB SHA-256: <code>{company.get('aab_sha256','')}</code></p>" if company.get('aab_sha256') else ""
    html = f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Install {company.get('display_name','App')}</title>
    </head>
    <body>
      <h1>Install {company.get('display_name','App')}</h1>
      <p>Download and install the APK below:</p>
      {apk_line}
      {aab_line}
      <p>APK SHA-256: <code>{company.get('apk_sha256','')}</code></p>
      {aab_sha_line}
      <p>Support: {company.get('support_email','support@example.uk')}</p>
    </body>
    </html>
    """
    dest.write_text(html, encoding="utf-8")


def make_privacy_page(dest: Path, company: dict[str, str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    website = company.get('domain')
    website_line = f"<p>Website: https://{website}</p>" if website else ""
    html = f"""
    <!doctype html>
    <html lang="en">
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Privacy</title></head>
    <body>
    <h1>Privacy policy for {company.get('display_name','App')}</h1>
    <p>This app collects no personal data. It stores user preferences locally on the device.</p>
    <p>Contact: {company.get('support_email','support@example.uk')}</p>
    {website_line}
    </body>
    </html>
    """
    dest.write_text(html, encoding="utf-8")


def short_description(role_noun: str | None = None) -> str:
    role = (role_noun or "shift").lower()
    if role == "trip":
        return "Simple trip journal for local mileage, delivery, and activity logs."
    if role == "visit":
        return "Simple visit journal for local site notes and daily activity logs."
    return "Simple shift journal for local work logs and daily handover notes."


def full_description(display_name: str, role_noun: str | None = None, export_title: str | None = None) -> str:
    role = (role_noun or "shift").lower()
    export_title = export_title or "Shift Log"
    feature_lines = [
        f"- Start and end a {role} from the dashboard with one tap.",
        f"- Add a short note to each {role} for handover, delivery, or site context.",
        "- Live session counter tracks how many sessions you have completed today.",
        f"- Review recent {role} activity including start time, end time, and duration.",
        "- Branded interface with company name and zero-permission local storage.",
    ]

    sections = [
        f"{display_name} is a lightweight offline {role} toolkit designed for straightforward daily record keeping on Android.",
        "The app helps users organise practical work activity through a focused set of local tools. It is intentionally minimal: no account, no ads, no network connection, no location access, and no personal data sent off the device.",
        "Key features:\n" + "\n".join(feature_lines),
        f"Privacy:\n{display_name} does not collect, share, or transmit user data. Any notes or session records are kept locally on the device and can be removed by clearing app storage or uninstalling the app.",
    ]
    return "\n\n".join(sections)


def _truncate_title(name: str) -> str:
    """Google Play app title: max 30 characters."""
    if len(name) <= 30:
        return name
    truncated = name[:27].rstrip() + "..."
    print(f"  WARN: app title '{name}' ({len(name)} chars) exceeds 30-char limit — truncated to '{truncated}'")
    return truncated


def _validate_short_desc(text: str) -> str:
    """Google Play short description: max 80 characters."""
    if len(text) <= 80:
        return text
    truncated = text[:77].rstrip() + "..."
    print(f"  WARN: short description ({len(text)} chars) exceeds 80-char limit — truncated")
    return truncated


def listing_payload(company: dict[str, str]) -> dict:
    display_name = company.get("display_name") or "Work Journal"
    role_noun = company.get("role_noun") or "shift"
    export_title = company.get("export_title") or "Shift Log"
    domain = company.get("domain") or ""
    # Use the live freeprivacypolicy.com URL if already generated; fall back to domain-based path
    privacy_url = (
        company.get("privacy_policy_url")
        or (f"https://{domain}/app/privacy.html" if domain else "PENDING_PRIVACY_POLICY_URL")
    )
    website_url = f"https://{domain}" if domain else "PENDING_DOMAIN"

    app_title = _truncate_title(display_name)
    short_desc = _validate_short_desc(short_description(role_noun))

    return {
        "app_identity": {
            "app_name": app_title,
            "package_name": company.get("application_id"),
            "default_language": "English (United Kingdom) - en-GB",
            "app_or_game": "App",
            "free_or_paid": "Free",
            "contains_ads": False,
            "play_category": "Business",
            "tags": ["Productivity", "Business", "Tools"],
        },
        "store_listing": {
            "short_description": short_desc,
            "full_description": full_description(display_name, role_noun, export_title),
            "privacy_policy_url": privacy_url,
            "developer_email": company.get("support_email"),
            "developer_website": website_url,
            "developer_phone": company.get("organization_phone") or "PENDING_ORGANIZATION_PHONE",
        },
        "graphics": {
            "app_icon": "graphics/icon-512.png",
            "feature_graphic": "graphics/feature-graphic.png",
            "phone_screenshots": [f"graphics/phone-screenshot-{i}.png" for i in range(1, 5)],
            "tablet_7inch_screenshots": [f"graphics/tablet7-screenshot-{i}.png" for i in range(1, 5)],
            "tablet_10inch_screenshots": [f"graphics/tablet10-screenshot-{i}.png" for i in range(1, 5)],
            "screenshot_screens": [
                "Dashboard — idle, no active shift",
                "Dashboard — active shift with note field",
                "Activity Log — session history list",
                "Settings — company info and app details",
            ],
            "specs": {
                "icon": "512x512 px, 32-bit PNG with alpha, max 1024 KB",
                "feature_graphic": "1024x500 px, JPEG or 24-bit PNG (no alpha)",
                "phone_screenshots": "1080x1920 px, 24-bit PNG (no alpha), max 8 MB each, min 2 / max 8",
                "tablet_7inch_screenshots": "1200x1920 px, 24-bit PNG (no alpha), min 4 / max 8 — mandatory",
                "tablet_10inch_screenshots": "1600x2560 px, 24-bit PNG (no alpha), min 4 / max 8 — mandatory",
            },
        },
        "app_content": {
            "privacy_policy_required": True,
            "data_safety_required": True,
            "ads": "No ads",
            "app_access": "All functionality is available without login.",
            "target_audience": "Adults/general business users; not designed for children.",
            "content_rating_notes": [
                "No violence.",
                "No sexual content.",
                "No user-generated public content.",
                "No online interaction between users.",
                "No gambling.",
            ],
            "permissions": "Zero Android permissions declared in AndroidManifest.xml.",
        },
        "release": {
            "initial_version_code": 1,
            "initial_version_name": "1.0",
            "release_name": "1.0 initial internal test",
            "release_notes": "Initial release with offline work tools, local notes, recent activity, and privacy-first zero-permission design.",
            "recommended_first_track": "Internal testing",
            "artifact_required_for_play": "AAB",
        },
        "manual_blockers": [
            "Build and verify signed AAB once Android SDK build-tools are installed.",
            "Set up dev@{domain} email in Namecheap, update Excel support_email, rebuild app.",
            "Confirm developer phone number and OTP readiness.",
            "Upload screenshots only after reviewing against a real/emulated build.",
            "Complete Play Console Data safety section manually (required even for zero-data apps).",
            "Complete IARC content rating questionnaire in Play Console (mandatory before publish).",
            "Verify privacy policy URL is live and publicly accessible worldwide before submission.",
        ],
    }


def data_safety_payload() -> dict[str, object]:
    return {
        "collects_user_data": False,
        "shares_user_data": False,
        "data_types_collected": [],
        "data_types_shared": [],
        "data_encryption_in_transit": "Not applicable; app does not transmit user data.",
        "users_can_request_data_deletion": "Not applicable; no server-side user data is collected.",
        "local_data_note": "Session counts and notes stay on device only and can be removed by clearing app storage or uninstalling.",
        "third_party_sdks_collect_data": False,
        "ads_or_analytics_sdks": False,
    }


def write_listing_assets(company: dict[str, str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    listing = listing_payload(company)
    data_safety = data_safety_payload()

    (out_dir / "play_listing.json").write_text(json.dumps(listing, indent=2), encoding="utf-8")
    (out_dir / "data_safety.json").write_text(json.dumps(data_safety, indent=2), encoding="utf-8")

    markdown = [
        f"# {listing['app_identity']['app_name']} Play Listing",
        "",
        f"Package: `{listing['app_identity']['package_name']}`",
        f"Category: {listing['app_identity']['play_category']}",
        f"Short description: {listing['store_listing']['short_description']}",
        "",
        "## Full Description",
        listing['store_listing']['full_description'],
        "",
        "## Data Safety",
        "- Collects user data: No",
        "- Shares user data: No",
        "- Ads or analytics SDKs: No",
        "- Permissions: Zero declared Android permissions",
        "",
        "## Manual Blockers",
    ]
    markdown.extend(f"- {item}" for item in listing['manual_blockers'])
    (out_dir / "play_listing.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")


def write_manifest(dest: Path, metadata: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def backup_keystore(dest_dir: Path, keystore_path: Path, keystore_password: str, alias: str) -> None:
    signing_dir = dest_dir / "signing"
    signing_dir.mkdir(parents=True, exist_ok=True)
    dst = signing_dir / keystore_path.name
    if keystore_path.exists():
        from shutil import copy2
        copy2(keystore_path, dst)
    signing = {
        "keystore": dst.name,
        "alias": alias,
        "store_password": keystore_password,
    }
    (signing_dir / "signing.json").write_text(json.dumps(signing, indent=2), encoding="utf-8")


def generate_all(company: dict, apk_path: Path | None, aab_path: Path | None, out_dir: Path, primary_color: str) -> None:
    """Generate all pipeline output assets into a structured tree:

    {out_dir}/
      artifacts/          signed APK + AAB copies
      listing/            play_listing.json, data_safety.json, play_listing.md
      graphics/           icon-512.png, feature-graphic.png, screenshots, qr.png
      web/                install.html, privacy.html
      signing/            keystore + signing.json (written by backup_keystore)
      manifest.json       top-level summary of all artifacts
      apk.sha256
      aab.sha256
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale flat-layout files from earlier pipeline versions
    _STALE_ROOT_NAMES = {
        "icon-512.png", "install.html", "privacy.html", "qr.png",
        "play_listing.json", "play_listing.md", "data_safety.json",
        "signing.json",
    }
    for item in out_dir.iterdir():
        if item.is_file() and (item.name in _STALE_ROOT_NAMES or item.suffix in {".jks", ".apk", ".aab"}):
            item.unlink()

    artifacts_dir = out_dir / "artifacts"
    listing_dir   = out_dir / "listing"
    graphics_dir  = out_dir / "graphics"
    web_dir       = out_dir / "web"
    for d in (artifacts_dir, listing_dir, graphics_dir, web_dir):
        d.mkdir(parents=True, exist_ok=True)

    display_name = company.get("display_name", "App")
    apk_name = Path(apk_path).name if apk_path else ""
    aab_name = Path(aab_path).name if aab_path else None

    # Note: artifacts are placed in artifacts/ by collect_artifacts before this is called.

    # --- Hashes ---
    apk_sha = ""
    aab_sha = ""
    if apk_path and apk_path.exists():
        apk_sha = sha256_file(apk_path)
        (out_dir / "apk.sha256").write_text(apk_sha, encoding="utf-8")
    if aab_path and aab_path.exists():
        aab_sha = sha256_file(aab_path)
        (out_dir / "aab.sha256").write_text(aab_sha, encoding="utf-8")

    # --- Privacy policy (freeprivacypolicy.com) ---
    cn = company.get("company_number", "")
    support_email = company.get("support_email")

    # Reuse existing URL — avoid creating a duplicate policy on every run
    policy_url = None
    existing_manifest = out_dir / "manifest.json"
    if existing_manifest.exists():
        try:
            existing_data = json.loads(existing_manifest.read_text(encoding="utf-8"))
            policy_url = existing_data.get("privacy_policy_url") or None
            if policy_url:
                print(f"  [{cn}] Privacy policy URL (existing): {policy_url}")
        except Exception:
            pass

    if not policy_url and support_email:
        # Published on the company's OWN domain, not on freeprivacypolicy.com.
        #
        # That site is behind a bot challenge and answers 403 "Just a
        # moment..." to both login and register, so the old path failed on
        # every company. It was also the wrong dependency: Play Console will
        # not accept an app without a policy URL, which makes this document
        # load-bearing, and a load-bearing legal page should not sit behind a
        # third party's signup flow, one account per company, revocable at
        # their discretion.
        #
        # privacy_site refuses to publish when the app's observed behaviour
        # stops supporting the published text, so a failure here can mean
        # "the app changed and the policy would now be false". That is not
        # the same as an outage and the log says which.
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _repo = str(_Path(__file__).resolve().parents[2])
            if _repo not in _sys.path:
                _sys.path.insert(0, _repo)
            import privacy_site

            print(f"  [{cn}] Publishing privacy policy on the company's own "
                  f"domain…")
            out = privacy_site.publish_policy({
                "domain": company.get("domain"),
                "display_name": display_name,
                "support_email": support_email,
                "company_name": company.get("company_name") or display_name,
                "company_number": cn,
                "address": company.get("address") or company.get("legal_address"),
            })
            status = out.get("status")
            if status == "published":
                policy_url = out.get("url")
                print(f"  [{cn}] Privacy policy URL: {policy_url}")
            elif status == "refused":
                print(f"  [{cn}] Privacy policy NOT published: "
                      f"{out.get('reason')}")
            elif status == "not_delegated":
                print(f"  [{cn}] Privacy policy not published yet: "
                      f"{out.get('message')}")
            else:
                print(f"  [{cn}] Privacy policy publish failed: "
                      f"{out.get('error')}")
        except Exception as e:
            print(f"  [{cn}] Privacy policy publishing raised: {e}")

    metadata = {
        "display_name": display_name,
        "package": company.get("application_id"),
        "application_id": company.get("application_id"),
        "domain": company.get("domain"),
        "support_email": company.get("support_email"),
        "role_noun": company.get("role_noun"),
        "export_title": company.get("export_title"),
        "organization_phone": company.get("organization_phone"),
        "apk": f"artifacts/{apk_name}" if apk_name else None,
        "aab": f"artifacts/{aab_name}" if aab_name else None,
        "apk_sha256": apk_sha or None,
        "aab_sha256": aab_sha or None,
        "privacy_policy_url": policy_url,
    }

    # --- manifest.json (root summary) ---
    write_manifest(out_dir / "manifest.json", metadata)

    # --- Listing assets (privacy_policy_url flows in via metadata) ---
    write_listing_assets(metadata, listing_dir)

    # --- Graphics ---
    make_icon_png(graphics_dir / "icon-512.png", primary_color, display_name,
                  company_number=cn)
    make_feature_graphic(graphics_dir / "feature-graphic.png", primary_color, display_name,
                         company_number=cn)

    _screens = ["dashboard", "dashboard_active", "log", "calculator"]
    _role_noun = company.get("role_noun") or "shift"
    _role_verb_start = company.get("role_verb_start") or "Start Shift"
    _role_verb_end = company.get("role_verb_end") or "End Shift"
    _scr_kwargs = dict(role_noun=_role_noun, role_verb_start=_role_verb_start, role_verb_end=_role_verb_end)

    # Phone screenshots: 1080x1920 (min 2, max 8 — we produce 4)
    for i, scr in enumerate(_screens, 1):
        make_phone_screenshot(graphics_dir / f"phone-screenshot-{i}.png", primary_color, display_name,
                              screen=scr, size=(1080, 1920), **_scr_kwargs)

    # 7-inch tablet screenshots: 1200x1920 portrait (min 4 — mandatory)
    for i, scr in enumerate(_screens, 1):
        make_phone_screenshot(graphics_dir / f"tablet7-screenshot-{i}.png", primary_color, display_name,
                              screen=scr, size=(1200, 1920), **_scr_kwargs)

    # 10-inch tablet screenshots: 1600x2560 portrait (min 4 — mandatory)
    for i, scr in enumerate(_screens, 1):
        make_phone_screenshot(graphics_dir / f"tablet10-screenshot-{i}.png", primary_color, display_name,
                              screen=scr, size=(1600, 2560), **_scr_kwargs)

    # --- Web pages ---
    make_install_page(web_dir / "install.html", apk_name, aab_name, metadata)
    make_privacy_page(web_dir / "privacy.html", metadata)
