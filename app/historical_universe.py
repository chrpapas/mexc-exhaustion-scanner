from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import html
import json
import logging
import os
import random
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import httpx

from app.historical_research import CacheLock, GentleLimiter
from app.mexc import is_crypto_usdt_contract, parse_spot_usdt_assets

LOGGER = logging.getLogger(__name__)

DEFAULT_MEXC_BASE_URL = "https://www.mexc.com"
DEFAULT_API_BASE_URL = "https://api.mexc.com"
DEFAULT_DELISTING_PATH = "/announcements/delistings/futures-19?page={page}"

ARTICLE_HREF_RE = re.compile(r'href=["\']([^"\']*/announcements/article/[^"\']+)["\']', re.I)
TAG_RE = re.compile(r"<[^>]+>")
EXPLICIT_USDT_RE = re.compile(r"\b([A-Z0-9]{1,32})USDT\b", re.I)
DELIST_BLOCK_RE = re.compile(
    r"(?:delist(?:ing)?(?:\s+of)?|delisting)\s+(?:the\s+)?(.{1,320}?)\s+USDT-M\b",
    re.I | re.S,
)
MONTH_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{1,2}),\s+(20\d{2})\b",
    re.I,
)
ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")

STOP_TOKENS = {
    "MEXC", "WILL", "BE", "TO", "THE", "AND", "OR", "OTHER", "OTHERS", "USDT",
    "FUTURES", "FUTURE", "PAIR", "PAIRS", "PERPETUAL", "CONTRACT", "CONTRACTS",
    "STOCK", "STOCKS", "INDEX", "ETF", "ETFS", "DELIST", "DELISTING", "FROM",
    "TRADING", "ON", "AT", "UTC", "NEW", "POSITION", "POSITIONS", "OPENINGS",
}


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    cache_dir: Path
    start: datetime
    end: datetime
    requests_per_second: float = 0.20
    request_timeout_seconds: float = 20.0
    max_runtime_minutes: float = 15.0
    max_pages: int = 80
    max_retries: int = 6
    mexc_base_url: str = DEFAULT_MEXC_BASE_URL
    api_base_url: str = DEFAULT_API_BASE_URL
    signal_csvs: tuple[Path, ...] = ()

    def validate(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("start must be before end")
        # This job shares an IP with live traffic. Keep website crawling much gentler
        # than the candle API fetcher.
        if not 0 < self.requests_per_second <= 0.5:
            raise ValueError("universe requests_per_second must be >0 and <=0.5")
        if self.max_runtime_minutes <= 0:
            raise ValueError("max_runtime_minutes must be positive")
        if not 1 <= self.max_pages <= 200:
            raise ValueError("max_pages must be between 1 and 200")


@dataclass(slots=True)
class Evidence:
    symbol: str
    source: str
    observed_at: str | None = None
    url: str | None = None
    classification: str = "crypto_or_unknown"
    title: str | None = None


@dataclass(slots=True)
class UniverseStats:
    started_at: str
    stopped_at: str | None = None
    current_active_crypto: int = 0
    csv_symbols: int = 0
    announcement_articles_seen: int = 0
    announcement_articles_parsed: int = 0
    announcement_pages_fetched: int = 0
    announcement_pages_cached: int = 0
    article_pages_fetched: int = 0
    article_pages_cached: int = 0
    excluded_stock_or_index: int = 0
    historical_extra_symbols: int = 0
    throttle_events: int = 0
    failed_requests: int = 0
    stop_reason: str | None = None


class PublicUniverseClient:
    def __init__(self, config: UniverseConfig, stats: UniverseStats) -> None:
        self.config = config
        self.stats = stats
        timeout = httpx.Timeout(config.request_timeout_seconds)
        headers = {"User-Agent": "mexc-exhaustion-historical-universe/1.3.71"}
        self._site = httpx.AsyncClient(
            base_url=config.mexc_base_url.rstrip("/"), timeout=timeout, headers=headers, follow_redirects=True
        )
        self._api = httpx.AsyncClient(
            base_url=config.api_base_url.rstrip("/"), timeout=timeout, headers=headers, follow_redirects=True
        )
        self._limiter = GentleLimiter(config.requests_per_second)

    async def close(self) -> None:
        await asyncio.gather(self._site.aclose(), self._api.aclose())

    async def get_text(self, url_or_path: str, *, api: bool = False) -> str:
        client = self._api if api else self._site
        last: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                await self._limiter.wait()
                response = await client.get(url_or_path)
                if response.status_code in {403, 418, 429}:
                    self.stats.throttle_events += 1
                    retry_after = response.headers.get("Retry-After")
                    try:
                        server_wait = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        server_wait = 0.0
                    cooldown = max(server_wait, min(60.0 * (2 ** (attempt - 1)), 600.0))
                    LOGGER.warning(
                        "MEXC protection status=%s; universe crawl cooling down %.1fs",
                        response.status_code,
                        cooldown,
                    )
                    await asyncio.sleep(cooldown)
                    if self.stats.throttle_events >= 2:
                        raise RuntimeError("MEXC universe throttling guard tripped")
                    continue
                if 500 <= response.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"server error {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                return response.text
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, RuntimeError) as exc:
                last = exc
                if "throttling guard tripped" in str(exc):
                    raise
                if attempt >= self.config.max_retries:
                    break
                await asyncio.sleep(min(3.0 * (2 ** (attempt - 1)), 60.0) + random.uniform(0.0, 0.5))
        self.stats.failed_requests += 1
        raise RuntimeError(f"GET {url_or_path} failed after retries: {last}")

    async def get_json(self, path: str) -> Any:
        text = await self.get_text(path, api=True)
        return json.loads(text)


class _DeadlineReached(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def _default_start(months: int) -> datetime:
    return _utc(datetime.now(UTC) - timedelta(days=max(1, months) * 30 + 3))


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _utc(parsed)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _atomic_gzip_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_gzip_text(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return handle.read()


def _plain_text(raw_html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", raw_html, flags=re.I | re.S)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _article_links(raw_html: str, base_url: str) -> list[str]:
    links = {urljoin(base_url.rstrip("/") + "/", html.unescape(match)) for match in ARTICLE_HREF_RE.findall(raw_html)}
    return sorted(links)


def _extract_title(raw_html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.I | re.S)
    if match:
        return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", match.group(1)))).strip()
    # MEXC pages often expose the article heading in h1 even when title is generic.
    match = re.search(r"<h1[^>]*>(.*?)</h1>", raw_html, flags=re.I | re.S)
    if match:
        return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", match.group(1)))).strip()
    return None


def _extract_date(text: str) -> datetime | None:
    iso = ISO_DATE_RE.search(text)
    if iso:
        try:
            return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=UTC)
        except ValueError:
            pass
    month = MONTH_DATE_RE.search(text)
    if month:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(f"{month.group(1)} {month.group(2)}, {month.group(3)}", fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _classification(text: str) -> str:
    lower = text.lower()
    if "stock futures" in lower or "usdt-m stock futures" in lower or "#stocks" in lower:
        return "stock"
    if "index futures" in lower or "usdt-m index futures" in lower:
        return "index"
    return "crypto_or_unknown"


def _normalize_symbol(token: str) -> str | None:
    token = token.upper().strip().replace("/", "").replace("-", "")
    if token.endswith("USDT"):
        token = token[:-4]
    if not token or token in STOP_TOKENS or token.isdigit():
        return None
    if not re.fullmatch(r"[A-Z0-9]{1,32}", token):
        return None
    return f"{token}_USDT"


def extract_futures_symbols(raw_html: str) -> tuple[set[str], str, datetime | None, str | None]:
    text = _plain_text(raw_html)
    classification = _classification(text)
    title = _extract_title(raw_html)
    observed = _extract_date((title or "") + " " + text[:3000])

    symbols: set[str] = set()
    for token in EXPLICIT_USDT_RE.findall(text):
        normalized = _normalize_symbol(token)
        if normalized:
            symbols.add(normalized)

    # Handles official wording such as "delisting WHITEWHALE and HIGH USDT-M
    # Perpetual Futures pairs" where only the final asset is adjacent to USDT-M.
    for block in DELIST_BLOCK_RE.findall(text):
        # Keep only uppercase-ish asset tokens and ignore prose/numbers.
        for token in re.findall(r"\b[A-Z0-9]{1,32}\b", block):
            normalized = _normalize_symbol(token)
            if normalized:
                symbols.add(normalized)

    if classification in {"stock", "index"}:
        return set(), classification, observed, title
    return symbols, classification, observed, title


def _symbols_from_csv(path: Path) -> set[str]:
    if not path.exists():
        return set()
    result: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return result
        symbol_field = next((name for name in reader.fieldnames if name and name.lower() == "symbol"), None)
        if symbol_field is None:
            return result
        for row in reader:
            raw = str(row.get(symbol_field) or "").strip().upper()
            if raw.endswith("_USDT") and re.fullmatch(r"[A-Z0-9]+_USDT", raw):
                result.add(raw)
    return result


async def _current_active_crypto(client: PublicUniverseClient) -> set[str]:
    contracts_payload, spot_payload = await asyncio.gather(
        client.get_json("/api/v1/contract/detail"),
        client.get_json("/api/v3/exchangeInfo"),
    )
    contracts = contracts_payload.get("data", contracts_payload) if isinstance(contracts_payload, dict) else contracts_payload
    if not isinstance(contracts, list) or not isinstance(spot_payload, dict):
        raise RuntimeError("unexpected MEXC universe responses")
    spot_assets = parse_spot_usdt_assets(spot_payload)
    symbols = {
        str(row.get("symbol") or "").upper()
        for row in contracts
        if isinstance(row, dict) and is_crypto_usdt_contract(row, spot_assets, require_spot_pair=True)
    }
    symbols.discard("")
    return symbols


def _save_outputs(config: UniverseConfig, active: set[str], evidence: dict[str, list[Evidence]], stats: UniverseStats) -> None:
    all_symbols = sorted(set(active) | set(evidence))
    extras = sorted(set(evidence) - set(active))
    output_dir = config.cache_dir / "universe-history"
    output_dir.mkdir(parents=True, exist_ok=True)

    _atomic_text(output_dir / "historical-all-symbols.txt", "\n".join(all_symbols) + ("\n" if all_symbols else ""))
    _atomic_text(output_dir / "historical-seed-symbols.txt", "\n".join(extras) + ("\n" if extras else ""))
    payload = {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "window_start": config.start.isoformat(),
        "window_end": config.end.isoformat(),
        "current_active_crypto_count": len(active),
        "historical_extra_count": len(extras),
        "symbols": {
            symbol: [asdict(item) for item in items]
            for symbol, items in sorted(evidence.items())
        },
        "notes": [
            "Only official MEXC futures-delisting pages classified as crypto/unknown are promoted from announcement evidence.",
            "Stock Futures and Index Futures announcements are excluded.",
            "The archive can still be incomplete if MEXC removed or failed to expose an old announcement; keep the evidence file with every backtest.",
        ],
    }
    _atomic_json(output_dir / "historical-universe.json", payload)
    stats.historical_extra_symbols = len(extras)
    _atomic_json(output_dir / "last-universe-run.json", asdict(stats))


async def reconstruct_universe(config: UniverseConfig, *, dry_run: bool = False) -> UniverseStats:
    config.validate()
    stats = UniverseStats(started_at=datetime.now(UTC).isoformat())
    deadline = asyncio.get_running_loop().time() + config.max_runtime_minutes * 60.0
    evidence: dict[str, list[Evidence]] = {}
    active: set[str] = set()

    # Intentionally share the exact same lock as the candle collector. This prevents
    # aggregate request rate from doubling while a long candle download is running.
    with CacheLock(config.cache_dir):
        client = PublicUniverseClient(config, stats)
        try:
            active = await _current_active_crypto(client)
            stats.current_active_crypto = len(active)

            csv_symbols: set[str] = set()
            for path in config.signal_csvs:
                for symbol in _symbols_from_csv(path):
                    csv_symbols.add(symbol)
                    evidence.setdefault(symbol, []).append(
                        Evidence(symbol=symbol, source=f"signal_csv:{path.name}")
                    )
            stats.csv_symbols = len(csv_symbols)

            if dry_run:
                stats.stop_reason = "dry_run"
                _save_outputs(config, active, evidence, stats)
                return stats

            cache_root = config.cache_dir / "universe-history" / "announcement-cache"
            seen_links: set[str] = set()

            for page in range(1, config.max_pages + 1):
                if asyncio.get_running_loop().time() >= deadline:
                    raise _DeadlineReached
                list_cache = cache_root / "lists" / f"futures-delistings-{page:03d}.html.gz"
                if list_cache.exists():
                    raw_list = _read_gzip_text(list_cache)
                    stats.announcement_pages_cached += 1
                else:
                    path = DEFAULT_DELISTING_PATH.format(page=page)
                    raw_list = await client.get_text(path)
                    _atomic_gzip_text(list_cache, raw_list)
                    stats.announcement_pages_fetched += 1

                links = _article_links(raw_list, config.mexc_base_url)
                if not links:
                    # Two empty pages are unnecessary; one empty official category page
                    # is a strong end-of-pagination signal.
                    stats.stop_reason = "announcement_pages_exhausted"
                    break

                new_links = [link for link in links if link not in seen_links]
                seen_links.update(new_links)
                stats.announcement_articles_seen = len(seen_links)

                stale_dates: list[datetime] = []
                for link in new_links:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise _DeadlineReached
                    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", link.rstrip("/").split("/")[-1])[:180]
                    article_cache = cache_root / "articles" / f"{slug}.html.gz"
                    if article_cache.exists():
                        raw_article = _read_gzip_text(article_cache)
                        stats.article_pages_cached += 1
                    else:
                        raw_article = await client.get_text(link)
                        _atomic_gzip_text(article_cache, raw_article)
                        stats.article_pages_fetched += 1

                    symbols, classification, observed, title = extract_futures_symbols(raw_article)
                    stats.announcement_articles_parsed += 1
                    if observed is not None:
                        stale_dates.append(observed)
                    if classification in {"stock", "index"}:
                        stats.excluded_stock_or_index += 1
                        continue
                    # Ignore evidence demonstrably outside the requested window. A
                    # missing date is retained, but marked; this avoids silently losing
                    # an old symbol because MEXC changed article markup.
                    if observed is not None and not (config.start - timedelta(days=7) <= observed <= config.end + timedelta(days=7)):
                        continue
                    for symbol in symbols:
                        evidence.setdefault(symbol, []).append(
                            Evidence(
                                symbol=symbol,
                                source="mexc_futures_delisting_announcement",
                                observed_at=observed.isoformat() if observed else None,
                                url=link,
                                classification=classification,
                                title=title,
                            )
                        )

                _save_outputs(config, active, evidence, stats)

                # Once an entire parsed page is older than the research start by a
                # safety margin, later pages should be older still. Stop to minimize
                # website requests. If dates are missing, continue until max_pages.
                if stale_dates and max(stale_dates) < config.start - timedelta(days=14):
                    stats.stop_reason = "reached_window_start"
                    break
            else:
                stats.stop_reason = "max_pages_reached"

        except _DeadlineReached:
            stats.stop_reason = "runtime_budget_reached"
        except Exception as exc:
            if "throttling guard tripped" in str(exc):
                stats.stop_reason = "throttling_guard"
            else:
                stats.stop_reason = "error"
                raise
        finally:
            await client.close()
            stats.stopped_at = datetime.now(UTC).isoformat()
            _save_outputs(config, active, evidence, stats)
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated MEXC historical contract-universe reconstruction")
    sub = parser.add_subparsers(dest="command", required=True)

    reconstruct = sub.add_parser("reconstruct", help="reconstruct historical crypto futures symbols")
    reconstruct.add_argument("--cache-dir", default="research-history")
    reconstruct.add_argument("--months", type=int, default=6)
    reconstruct.add_argument("--start")
    reconstruct.add_argument("--end")
    reconstruct.add_argument("--rate", type=float, default=0.20, help="website requests/sec; hard capped at 0.5")
    reconstruct.add_argument("--max-runtime-minutes", type=float, default=15.0)
    reconstruct.add_argument("--max-pages", type=int, default=80)
    reconstruct.add_argument("--signal-csv", action="append", default=[])
    reconstruct.add_argument("--dry-run", action="store_true")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    end = _parse_dt(args.end) if args.end else _utc(datetime.now(UTC))
    start = _parse_dt(args.start) if args.start else _default_start(args.months)
    config = UniverseConfig(
        cache_dir=Path(args.cache_dir).expanduser().resolve(),
        start=start,
        end=end,
        requests_per_second=args.rate,
        max_runtime_minutes=args.max_runtime_minutes,
        max_pages=args.max_pages,
        signal_csvs=tuple(Path(value).expanduser().resolve() for value in args.signal_csv),
    )
    stats = await reconstruct_universe(config, dry_run=args.dry_run)
    print(json.dumps(asdict(stats), indent=2, sort_keys=True))
    return 0


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    return asyncio.run(_async_main(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
