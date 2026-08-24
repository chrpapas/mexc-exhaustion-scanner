from __future__ import annotations

import io
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from app.signal_ledger import LedgerStrategyOutcome, SignalLedger, SignalLedgerItem


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

# Subscriber ledger mirrors the three selected strategies. Strategy cells contain
# their own pre-target/pre-exit breach state so a later raw breach cannot be mistaken
# for risk that was actually carried by a strategy after it had already exited.
_COLUMNS = (
    ("TOKEN", 165),
    ("SIGNAL", 160),
    ("ENTRY", 145),
    ("TP5 FREQUENT", 245),
    ("TP20 NO TIMEOUT", 255),
    ("7D SWING", 245),
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
    except TypeError:  # pragma: no cover
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


def _age_hours(item: SignalLedgerItem, effective_at) -> float:
    return max(0.0, (effective_at - item.confirmed_at).total_seconds() / 3600.0)


def _breach_text(outcome: LedgerStrategyOutcome) -> str:
    deepest = outcome.deepest_breach_before_effective_pct
    if outcome.state in {"target_hit", "closed_win", "closed_loss"}:
        return "no breach before exit" if deepest is None else f"⚠ pre-target/exit -{deepest}%"
    return "no breach so far" if deepest is None else f"⚠ so far -{deepest}%"


@dataclass(frozen=True, slots=True)
class _StrategyCell:
    main_text: str
    detail_text: str
    fill: tuple[int, int, int]
    main_color: tuple[int, int, int]
    detail_color: tuple[int, int, int]


def _strategy_cell(
    item: SignalLedgerItem,
    outcome: LedgerStrategyOutcome,
    *,
    target_pct: int | None = None,
) -> _StrategyCell:
    if not outcome.eligible:
        return _StrategyCell("N/A", "not eligible", _NEUTRAL, _MUTED, _MUTED)

    breach = _breach_text(outcome)
    has_breach = outcome.deepest_breach_before_effective_pct is not None
    detail_color = _RED_TEXT if has_breach else _MUTED

    # Primary color communicates STRATEGY STATUS only. A breach is a secondary
    # warning and must never repaint a winning target/exit red.
    if outcome.state == "target_hit":
        target = f"+{target_pct}%" if target_pct is not None else "TARGET"
        main = f"HIT {target} • {_elapsed(outcome.target_hours)}"
        return _StrategyCell(main, breach, _GREEN, _GREEN_TEXT, detail_color)

    if outcome.state in {"closed_win", "closed_loss"}:
        main = f"CLOSED 7D {_pct(outcome.return_pct)}"
        if outcome.return_pct is not None and outcome.return_pct > 0:
            return _StrategyCell(main, breach, _GREEN, _GREEN_TEXT, detail_color)
        return _StrategyCell(main, breach, _RED, _RED_TEXT, detail_color)

    if outcome.state in {"open", "tracking"}:
        age = _elapsed(_age_hours(item, outcome.effective_at)) if outcome.effective_at else "—"
        prefix = "OPEN" if outcome.state == "open" else "TRACKING"
        main = f"{prefix} {_pct(outcome.return_pct)} • {age}"
        if outcome.return_pct is not None and outcome.return_pct > 0:
            return _StrategyCell(main, breach, _BLUE, _BLUE_TEXT, detail_color)
        # An underwater OPEN trade is not a realized loss. Amber prevents the
        # subscriber from reading it as a failed TP5/TP20 strategy outcome.
        return _StrategyCell(main, breach, _AMBER, _AMBER_TEXT, detail_color)

    return _StrategyCell(outcome.state, breach, _NEUTRAL, _MUTED, detail_color)


def _draw_strategy_cell(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    cell: _StrategyCell,
    *,
    main_font,
    detail_font,
) -> None:
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=cell.fill, outline=_GRID, width=1)
    max_width = max(10, x1 - x0 - 12)
    main = _fit_text(draw, cell.main_text, main_font, max_width)
    detail = _fit_text(draw, cell.detail_text, detail_font, max_width)

    main_box = draw.textbbox((0, 0), main, font=main_font)
    detail_box = draw.textbbox((0, 0), detail, font=detail_font)
    main_h = max(1, main_box[3] - main_box[1])
    detail_h = max(1, detail_box[3] - detail_box[1])
    spacing = 6
    total_h = main_h + spacing + detail_h
    y = y0 + (y1 - y0 - total_h) / 2

    main_w = main_box[2] - main_box[0]
    detail_w = detail_box[2] - detail_box[0]
    draw.text((x0 + (x1 - x0 - main_w) / 2, y), main, fill=cell.main_color, font=main_font)
    y += main_h + spacing
    draw.text((x0 + (x1 - x0 - detail_w) / 2, y), detail, fill=cell.detail_color, font=detail_font)

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
        x = x0 + 7 if align == "left" else x0 + (x1 - x0 - width) / 2
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
    title_h = 70
    header_h = 48
    row_h = 68
    footer_h = 72
    width = table_width + margin * 2
    height = margin * 2 + title_h + header_h + row_h * len(items) + footer_h

    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)
    title_font = _font(25, bold=True)
    subtitle_font = _font(14)
    head_font = _font(13, bold=True)
    cell_font = _font(13)
    cell_bold = _font(13, bold=True)
    foot_font = _font(12)

    draw.rounded_rectangle((margin, margin, width - margin, margin + title_h - 4), radius=12, fill=_HEADER)
    draw.rectangle((margin, margin, margin + 8, margin + title_h - 4), fill=accent)
    draw.text((margin + 20, margin + 9), f"{label} • STRATEGY LEDGER", fill=_TEXT, font=title_font)
    draw.text(
        (margin + 20, margin + 40),
        f"Page {page}/{total_pages} • newest first • each strategy cell shows breach carried before its target/exit",
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
        signal_text = item.confirmed_at.astimezone(tz).strftime("%d %b\n%H:%M")
        tp5 = _strategy_cell(item, item.tp5_strategy, target_pct=5)
        tp20 = _strategy_cell(item, item.tp20_strategy, target_pct=20)
        swing = _strategy_cell(item, item.standard_7d_strategy)
        plain_values: list[tuple[str, tuple[int, int, int], tuple[int, int, int], object, str]] = [
            (item.symbol.replace("_USDT", ""), _NEUTRAL, _TEXT, cell_bold, "left"),
            (signal_text, _NEUTRAL, _MUTED, cell_font, "center"),
            (_price(item.signal_price), _NEUTRAL, _TEXT, cell_font, "center"),
        ]

        x = margin
        for (_, col_w), (text, fill, color, font, align) in zip(_COLUMNS[:3], plain_values):
            _draw_cell(draw, (x, y, x + col_w, y + row_h), text, fill=fill, color=color, font=font, align=align)
            x += col_w
        for (_, col_w), strategy_cell in zip(_COLUMNS[3:], (tp5, tp20, swing)):
            _draw_strategy_cell(
                draw,
                (x, y, x + col_w, y + row_h),
                strategy_cell,
                main_font=cell_bold,
                detail_font=_font(11, bold=True),
            )
            x += col_w
        y += row_h

    footer_1 = (
        "Green = completed win/target. Amber/blue = still open. Red primary = closed loss only. "
        "Red ⚠ text = deepest adverse breach carried before target/exit."
    )
    footer_2 = "OPEN/TRACKING uses current MTM; a red breach warning does not mean the strategy lost. Exact threshold flags are in the CSV."
    draw.text((margin + 4, y + 12), footer_1, fill=_MUTED, font=foot_font)
    draw.text((margin + 4, y + 34), footer_2, fill=_MUTED, font=foot_font)

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
