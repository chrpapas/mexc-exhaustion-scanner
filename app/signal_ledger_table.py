from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from app.signal_ledger import SignalLedger, SignalLedgerItem


@dataclass(frozen=True, slots=True)
class LedgerTableImage:
    risk_tier: str
    risk_label: str
    page: int
    total_pages: int
    filename: str
    png_bytes: bytes


_RISK_META = {
    "standard": ("STANDARD", (36, 91, 62)),
    "high_risk": ("HIGH RISK", (139, 105, 20)),
}

# Designed for Discord desktop/mobile preview. Cells deliberately favor scanability
# over verbose detail; the CSV remains the source for exact/raw values.
_COLUMNS = (
    ("STATUS", 130),
    ("TOKEN", 175),
    ("SIGNAL", 175),
    ("ENTRY", 130),
    ("+20%", 135),
    ("1D", 165),
    ("2D", 165),
    ("3D", 165),
    ("7D", 165),
    ("-100", 105),
    ("-200", 105),
    ("-300", 105),
    ("-400", 105),
)

_BG = (20, 22, 28)
_HEADER = (30, 33, 41)
_GRID = (64, 68, 80)
_TEXT = (238, 240, 245)
_MUTED = (164, 170, 184)
_GREEN = (35, 92, 60)
_GREEN_TEXT = (205, 244, 220)
_AMBER = (106, 76, 23)
_AMBER_TEXT = (255, 231, 174)
_RED = (112, 39, 45)
_RED_DEEP = (83, 29, 35)
_RED_TEXT = (255, 211, 214)
_BLUE = (41, 61, 92)
_BLUE_TEXT = (204, 223, 255)
_NEUTRAL = (38, 41, 49)


def _font(size: int, *, bold: bool = False):
    candidates = (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow fallback
        return ImageFont.load_default()


def _price(value: float | None) -> str:
    if value is None:
        return "—"
    v = abs(value)
    if v >= 1000:
        return f"{value:,.2f}"
    if v >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if v >= 0.01:
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.1f}%"


def _elapsed(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.2f}d"


def _status(item: SignalLedgerItem) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    status = item.headline_status
    if status in {"target_hit", "target_then_breach"}:
        return "TARGET", _GREEN, _GREEN_TEXT
    if status == "profitable_below_target":
        return "PROFIT", _GREEN, _GREEN_TEXT
    if status.startswith("breach_"):
        return "BREACH", _RED_DEEP, _RED_TEXT
    if status == "safe_negative":
        return "NEGATIVE", _AMBER, _AMBER_TEXT
    return "PENDING", _BLUE, _BLUE_TEXT


def _breach_before(item: SignalLedgerItem, hours: int, threshold: int = 100) -> bool:
    cutoff = item.confirmed_at + timedelta(hours=hours)
    for breach in item.breaches:
        if breach.adverse_limit_pct == threshold:
            return breach.occurred_at is not None and breach.occurred_at <= cutoff
    return False


def _cell_for_horizon(item: SignalLedgerItem, hours: int) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    horizon = next(h for h in item.horizons if h.hours == hours)
    if horizon.return_pct is None:
        return "pending", _BLUE, _BLUE_TEXT
    text = f"{_price(horizon.price)}\n{_pct(horizon.return_pct)}"
    if _breach_before(item, hours, 100):
        return text, _RED, _RED_TEXT
    if horizon.return_pct > 0:
        return text, _GREEN, _GREEN_TEXT
    return text, _AMBER, _AMBER_TEXT


def _cell_for_target(item: SignalLedgerItem) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    if item.target_20_at is not None:
        if item.first_100_breach_at is None or item.target_20_at < item.first_100_breach_at:
            return f"HIT\n{_elapsed(item.time_to_target_20_hours)}", _GREEN, _GREEN_TEXT
        return f"late\n{_elapsed(item.time_to_target_20_hours)}", _RED, _RED_TEXT
    if item.first_100_breach_at is not None:
        return "BREACH\nfirst", _RED, _RED_TEXT
    return "pending", _BLUE, _BLUE_TEXT


def _cell_for_breach(item: SignalLedgerItem, threshold: int) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    breach = next(b for b in item.breaches if b.adverse_limit_pct == threshold)
    if breach.occurred_at is None:
        return "—", _NEUTRAL, _MUTED
    intensity = {100: _RED, 200: (105, 38, 48), 300: (91, 32, 48), 400: (66, 26, 38)}[threshold]
    return _elapsed(breach.hours_after_signal), intensity, _RED_TEXT


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    base = text
    while base and draw.textbbox((0, 0), base + "…", font=font)[2] > max_width:
        base = base[:-1]
    return base + "…"


def _draw_cell(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    *,
    fill: tuple[int, int, int],
    color: tuple[int, int, int],
    font,
    align: str = "center",
) -> None:
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=fill, outline=_GRID, width=1)
    lines = str(text).split("\n")
    line_heights = []
    rendered: list[str] = []
    for line in lines:
        line = _fit_text(draw, line, font, max(10, x1 - x0 - 12))
        rendered.append(line)
        bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(max(1, bbox[3] - bbox[1]))
    spacing = 3
    total_h = sum(line_heights) + spacing * max(0, len(lines) - 1)
    y = y0 + (y1 - y0 - total_h) / 2
    for line, h in zip(rendered, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        width = bbox[2] - bbox[0]
        if align == "left":
            x = x0 + 7
        else:
            x = x0 + (x1 - x0 - width) / 2
        draw.text((x, y), line, fill=color, font=font)
        y += h + spacing


def _render_page(
    items: list[SignalLedgerItem],
    *,
    risk_tier: str,
    page: int,
    total_pages: int,
    timezone_name: str,
) -> bytes:
    label, accent = _RISK_META[risk_tier]
    tz = ZoneInfo(timezone_name)
    table_width = sum(width for _, width in _COLUMNS)
    margin = 20
    title_h = 64
    header_h = 48
    row_h = 60
    footer_h = 44
    width = table_width + margin * 2
    height = margin * 2 + title_h + header_h + row_h * len(items) + footer_h

    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)
    title_font = _font(25, bold=True)
    subtitle_font = _font(15)
    head_font = _font(14, bold=True)
    cell_font = _font(13)
    cell_bold = _font(13, bold=True)
    foot_font = _font(13)

    draw.rounded_rectangle((margin, margin, width - margin, margin + title_h - 4), radius=12, fill=_HEADER)
    draw.rectangle((margin, margin, margin + 8, margin + title_h - 4), fill=accent)
    draw.text((margin + 20, margin + 9), f"{label} • SIGNAL OUTCOME TABLE", fill=_TEXT, font=title_font)
    draw.text(
        (margin + 20, margin + 38),
        f"Page {page}/{total_pages} • newest first • green=profitable • amber=negative/safe • red=breach • blue=pending",
        fill=_MUTED,
        font=subtitle_font,
    )

    y = margin + title_h
    x = margin
    for name, col_w in _COLUMNS:
        _draw_cell(draw, (x, y, x + col_w, y + header_h), name, fill=_HEADER, color=_TEXT, font=head_font)
        x += col_w
    y += header_h

    for item in items:
        status_text, status_fill, status_text_color = _status(item)
        target_text, target_fill, target_text_color = _cell_for_target(item)
        signal_text = item.confirmed_at.astimezone(tz).strftime("%d %b\n%H:%M")
        values: list[tuple[str, tuple[int, int, int], tuple[int, int, int], object, str]] = [
            (status_text, status_fill, status_text_color, cell_bold, "center"),
            (item.symbol.replace("_USDT", ""), _NEUTRAL, _TEXT, cell_bold, "left"),
            (signal_text, _NEUTRAL, _MUTED, cell_font, "center"),
            (_price(item.signal_price), _NEUTRAL, _TEXT, cell_font, "center"),
            (target_text, target_fill, target_text_color, cell_bold, "center"),
        ]
        for hours in (24, 48, 72, 168):
            text, fill, color = _cell_for_horizon(item, hours)
            values.append((text, fill, color, cell_font, "center"))
        for threshold in (100, 200, 300, 400):
            text, fill, color = _cell_for_breach(item, threshold)
            values.append((text, fill, color, cell_font, "center"))

        x = margin
        for (_, col_w), (text, fill, color, font, align) in zip(_COLUMNS, values):
            _draw_cell(draw, (x, y, x + col_w, y + row_h), text, fill=fill, color=color, font=font, align=align)
            x += col_w
        y += row_h

    footer = (
        "Each horizon cell = price / short return. Breach columns show first elapsed time from signal. "
        "Exact timestamps/raw values are in the attached CSV."
    )
    draw.text((margin + 4, y + 13), footer, fill=_MUTED, font=foot_font)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_signal_ledger_tables(
    ledger: SignalLedger,
    *,
    timezone_name: str = "Europe/Zurich",
    rows_per_page: int = 16,
) -> tuple[LedgerTableImage, ...]:
    outputs: list[LedgerTableImage] = []
    for risk_tier in ("standard", "high_risk"):
        group = list(ledger.by_risk(risk_tier))
        if not group:
            continue
        label, _ = _RISK_META[risk_tier]
        total_pages = (len(group) + rows_per_page - 1) // rows_per_page
        for page_index in range(total_pages):
            page = page_index + 1
            page_items = group[page_index * rows_per_page:(page_index + 1) * rows_per_page]
            png = _render_page(
                page_items,
                risk_tier=risk_tier,
                page=page,
                total_pages=total_pages,
                timezone_name=timezone_name,
            )
            filename = f"signal-ledger-{risk_tier}-p{page}.png"
            outputs.append(LedgerTableImage(risk_tier, label, page, total_pages, filename, png))
    return tuple(outputs)
