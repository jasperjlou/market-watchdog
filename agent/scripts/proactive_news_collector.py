#!/usr/bin/env python3
"""Collect proactive public news and SEC filing metadata without an AI quota.

GDELT direct article links are preferred; Google News RSS is the fallback.  The
collector emits the same evidence contract used by AI workers, but never sends
messages or calls broker APIs.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable


PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("APP_DIR", str(PROJECT_DIR)))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(APP_DIR / "agent")))
STATE_DIR = AGENT_DIR / "state"
RUNS_DIR = AGENT_DIR / "runs"
STATE_PATH = STATE_DIR / "proactive_news_collector_state.json"
OUTPUT_PATH = STATE_DIR / "proactive_news_latest.json"
EVIDENCE_INDEX_PATH = STATE_DIR / "proactive_news_evidence.json"

GENERAL_USER_AGENT = "market-watchdog/1.0 public-news-monitor"
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "market-watchdog/1.0 research-monitor")
MATERIAL_FORMS = {"8-K", "10-Q", "10-K", "6-K", "20-F", "F-1", "F-1/A", "424B4", "S-1", "S-1/A"}

FEEDS = [
    {
        "theme": "memory_storage",
        "query": '("SK hynix" OR SKHY OR Micron OR Everpure OR "Pure Storage" OR HBM OR DRAM OR NAND OR "memory pricing")',
        "symbols": ["SKHY", "MU", "WDC", "STX", "P", "NVDA", "TSM", "AMAT", "SMH"],
        "topics": ["memory_storage", "semiconductors", "HBM", "DRAM", "NAND"],
        "material_terms": [
            "sk hynix", "micron", "everpure", "pure storage", "hbm", "dram", "nand", "memory pricing",
            "storage demand", "wafer capacity", "export control", "semiconductor",
        ],
    },
    {
        "theme": "space_aerospace",
        "query": '("Rocket Lab" OR "AST SpaceMobile" OR "Intuitive Machines" OR NASA OR "space contract" OR "launch failure")',
        "symbols": ["RKLB", "ASTS", "LUNR", "PL", "RDW", "XAR", "ITA", "BA", "LMT", "NOC", "RTX"],
        "topics": ["space_aerospace", "NASA", "FCC", "government_contract"],
        "material_terms": [
            "rocket lab", "ast spacemobile", "intuitive machines", "redwire", "planet labs",
            "space contract", "government award", "launch", "rocket failure", "fcc", "spectrum",
            "defense contract", "missile", "satellite funding",
        ],
    },
    {
        "theme": "gold",
        "query": '(gold OR bullion OR "real yields" OR "central bank gold" OR "gold miners")',
        "symbols": ["GLD", "IAU", "GDX", "GDXJ", "NEM", "AEM", "GC=F", "TLT", "^TNX"],
        "topics": ["gold", "real_yields", "inflation", "geopolitics"],
        "material_terms": [
            "gold", "bullion", "real yield", "central bank purchase", "gold miner",
            "inflation", "treasury yield", "geopolitical risk",
        ],
    },
    {
        "theme": "broad_market",
        "query": '("stock market" OR Nasdaq OR "market breadth" OR "Federal Reserve" OR "credit spreads" OR volatility)',
        "symbols": ["SPY", "QQQ", "IWM", "RSP", "HYG", "TLT", "^VIX", "AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA"],
        "topics": ["broad_market", "market_breadth", "rates", "credit", "volatility"],
        "material_terms": [
            "stock market", "nasdaq", "market breadth", "federal reserve", "interest rate",
            "credit spread", "volatility", "earnings guidance", "sector rotation",
        ],
    },
]

CIKS = {
    "SKHY": 2120882, "MU": 723125, "WDC": 106040, "STX": 1137789, "P": 1474432,
    "NVDA": 1045810, "TSM": 1046179, "AMAT": 6951, "RKLB": 1819994,
    "ASTS": 1780312, "LUNR": 1844452, "PL": 1836833, "RDW": 1819810,
    "BA": 12927, "LMT": 936468, "NOC": 1133421, "RTX": 101829,
    "NEM": 1164727, "MSFT": 789019, "GOOG": 1652044, "AMZN": 1018724,
    "META": 1326801, "TSLA": 1318605,
}

ALIASES = {
    "SKHY": ("sk hynix", "skhy"), "MU": ("micron", " mu "),
    "WDC": ("western digital", "wdc"), "STX": ("seagate", "stx"),
    "P": ("pure storage", "everpure"), "NVDA": ("nvidia", "nvda"),
    "TSM": ("tsmc", "taiwan semiconductor"), "AMAT": ("applied materials", "amat"),
    "RKLB": ("rocket lab", "rklb"), "ASTS": ("ast spacemobile", "asts"),
    "LUNR": ("intuitive machines", "lunr"), "PL": ("planet labs",), "RDW": ("redwire",),
    "BA": ("boeing",), "LMT": ("lockheed",), "NOC": ("northrop grumman",), "RTX": ("rtx", "raytheon"),
    "NEM": ("newmont",), "AEM": ("agnico eagle",), "MSFT": ("microsoft",),
    "GOOG": ("alphabet", "google"), "AMZN": ("amazon",), "META": ("meta platforms",), "TSLA": ("tesla",),
}

POSITIVE = (
    "beats", "raises guidance", "award", "wins contract", "approval", "surge in demand",
    "price increase", "record revenue", "capacity expansion", "successful launch", "buyback",
    "enormous demand", "demand is enormous", "unprecedented demand", "boosts investment",
    "investment milestone", "higher debut", "prices rise", "moves higher", "record high", "rally", "surges",
    "central bank buys",
    "超预期", "上调", "中标", "获批", "需求强劲", "提价", "成功发射", "回购",
)
NEGATIVE = (
    "misses", "cuts guidance", "delay", "failure", "investigation", "export restriction",
    "downgrade", "weak demand", "offering", "recall", "prices slide", "slide from record high",
    "move lower", "falls", "declines", "drops",
    "不及预期", "下调", "延期",
    "失败", "调查", "禁令", "需求疲弱", "增发",
)
TRUSTED = (
    "reuters", "associated press", "ap news", "bloomberg", "financial times", "wall street journal",
    "wsj", "cnbc", "marketwatch", "barron's", "nikkei", "yonhap", "the korea herald", "nasdaq", "cme group",
)
EVIDENCE_FIELDS = (
    "title", "url", "source_id", "source_name", "source_tier", "published_at", "collected_at",
    "symbols", "entities", "topics", "summary", "evidence_text", "why_it_matters", "limitations",
    "direction", "impact_horizon", "confidence", "trade_instruction",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def clean_text(value: Any, limit: int = 500) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def fetch_json(url: str, *, sec: bool = False, timeout: int = 15) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT if sec else GENERAL_USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def fetch_text(url: str, *, timeout: int = 15) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": GENERAL_USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for parser in (
        lambda raw: datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
        lambda raw: parsedate_to_datetime(raw),
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")),
    ):
        try:
            result = parser(text)
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            return result.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except Exception:
            continue
    return ""


def contains_term(text: str, term: str) -> bool:
    """Match names and tickers without accepting ticker substrings in words."""
    candidate = str(term or "").strip().lower()
    if not candidate:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", text.lower()) is not None


def detect_symbols(text: str, allowed: list[str]) -> list[str]:
    result = []
    for symbol in allowed:
        aliases = ALIASES.get(symbol, (symbol.lower(),))
        if any(contains_term(text, alias) for alias in aliases):
            result.append(symbol)
    return result


def is_material_item(title: str, summary: str, feed: dict[str, Any]) -> bool:
    """Keep only watched entities or theme-specific forward-looking catalysts."""
    text = f"{title} {summary}"
    if detect_symbols(text, list(feed.get("symbols") or [])):
        return True
    theme = str(feed.get("theme") or "")
    if theme == "gold":
        specific = [term for term in feed.get("material_terms") or [] if term != "gold"]
        if any(contains_term(text, term) for term in specific):
            return True
        financial_context = (
            "price", "prices", "futures", "bullion", "ounce", "market", "investor", "etf",
            "miner", "mining", "central bank", "yield", "rally", "slide", "record high", "sanction",
        )
        return contains_term(text, "gold") and any(contains_term(text, term) for term in financial_context)
    if theme == "broad_market" and contains_term(text, "federal reserve"):
        fed_catalysts = ("rate", "policy", "fomc", "minutes", "inflation", "employment", "liquidity", "balance sheet")
        if any(contains_term(text, term) for term in fed_catalysts):
            return True
        remaining = [term for term in feed.get("material_terms") or [] if term != "federal reserve"]
        return any(contains_term(text, term) for term in remaining)
    return any(contains_term(text, term) for term in feed.get("material_terms") or [])


def is_relevant_compact_item(item: dict[str, Any]) -> bool:
    topics = {str(value).strip().lower() for value in item.get("topics", []) if str(value).strip()} if isinstance(item.get("topics"), list) else set()
    candidates = [feed for feed in FEEDS if str(feed.get("theme") or "").lower() in topics] or FEEDS
    return any(is_material_item(str(item.get("title") or ""), str(item.get("summary") or ""), feed) for feed in candidates)


def infer_direction(text: str) -> str:
    lower = text.lower()
    positive = sum(term in lower for term in POSITIVE)
    negative = sum(term in lower for term in NEGATIVE)
    if positive > negative:
        return "bullish"
    if negative > positive:
        return "bearish"
    return "neutral"


def impact_horizon(text: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ("earnings", "guidance", "launch", "award", "contract", "filing", "fed", "cpi")):
        return "1_5d"
    if any(term in lower for term in ("capacity", "hbm", "dram", "nand", "export control", "backlog", "real yield")):
        return "2_6w"
    return "unspecified"


def source_tier(source_name: str, url: str) -> str:
    lower = f"{source_name} {url}".lower()
    if ".gov" in lower or "sec.gov" in lower or "federalregister.gov" in lower:
        return "S0"
    if any(name in lower for name in TRUSTED):
        return "S1"
    return "S3"


def evidence_item(
    *, title: str, url: str, source_name: str, published_at: str, feed: dict[str, Any], summary: str = "",
) -> dict[str, Any]:
    tier = source_tier(source_name, url)
    direction = infer_direction(f"{title} {summary}")
    return {
        "title": clean_text(title, 220),
        "url": url.strip(),
        "source_id": source_name or urllib.parse.urlparse(url).netloc,
        "source_name": source_name or urllib.parse.urlparse(url).netloc,
        "source_tier": tier,
        "published_at": published_at,
        "collected_at": utc_now_iso(),
        "symbols": detect_symbols(f"{title} {summary}", list(feed.get("symbols") or [])),
        "entities": [],
        "topics": list(feed.get("topics") or []),
        "summary": clean_text(summary or f"Headline metadata collected for the {feed.get('theme')} theme.", 500),
        "evidence_text": clean_text(title, 300),
        "why_it_matters": f"Potential forward input for {feed.get('theme')} price trend and risk assessment; verify the linked source before escalation.",
        "limitations": "Headline/metadata signal only; article facts and causal interpretation require verification.",
        "direction": direction,
        "impact_horizon": impact_horizon(f"{title} {summary}"),
        "confidence": "high" if tier == "S0" else ("medium" if tier == "S1" else "low"),
        "trade_instruction": False,
    }


def gdelt_url(feed: dict[str, Any], max_records: int, lookback_days: int) -> str:
    params = {
        "query": str(feed["query"]), "mode": "ArtList", "format": "json",
        "maxrecords": str(max_records), "sort": "HybridRel", "timespan": f"{lookback_days}d",
    }
    return "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)


def collect_gdelt(
    feed: dict[str, Any], max_records: int, lookback_days: int,
    json_fetch: Callable[..., dict[str, Any]] = fetch_json,
) -> list[dict[str, Any]]:
    payload = json_fetch(gdelt_url(feed, max_records, lookback_days))
    items = []
    for article in payload.get("articles", []) if isinstance(payload, dict) else []:
        if not isinstance(article, dict) or not article.get("url") or not article.get("title"):
            continue
        if not is_material_item(str(article["title"]), "", feed):
            continue
        items.append(evidence_item(
            title=str(article["title"]), url=str(article["url"]),
            source_name=str(article.get("domain") or "GDELT indexed source"),
            published_at=parse_timestamp(article.get("seendate")), feed=feed,
        ))
    return items


def google_news_url(feed: dict[str, Any], lookback_days: int) -> str:
    query = f"{feed['query']} when:{lookback_days}d"
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})


def collect_google_news(
    feed: dict[str, Any], max_records: int, lookback_days: int,
    text_fetch: Callable[..., str] = fetch_text,
) -> list[dict[str, Any]]:
    root = ET.fromstring(text_fetch(google_news_url(feed, lookback_days)))
    items = []
    for node in root.findall("./channel/item")[:max_records]:
        title = clean_text(node.findtext("title"), 220)
        url = clean_text(node.findtext("link"), 1000)
        source_node = node.find("source")
        source = clean_text(source_node.text if source_node is not None else "Google News indexed source", 120)
        published = parse_timestamp(node.findtext("pubDate"))
        if title and url and is_material_item(title, "", feed):
            items.append(evidence_item(title=title, url=url, source_name=source, published_at=published, feed=feed))
    return items


def sec_items_from_payload(symbol: str, cik: int, payload: dict[str, Any], cutoff: date) -> list[dict[str, Any]]:
    recent = ((payload.get("filings") or {}).get("recent") or {}) if isinstance(payload, dict) else {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    documents = recent.get("primaryDocument") or []
    items = []
    for index, form in enumerate(forms):
        if str(form) not in MATERIAL_FORMS or index >= len(dates) or index >= len(accessions) or index >= len(documents):
            continue
        try:
            filing_date = date.fromisoformat(str(dates[index]))
        except ValueError:
            continue
        if filing_date < cutoff:
            continue
        accession = str(accessions[index])
        document = str(documents[index])
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{document}"
        items.append({
            "title": f"{symbol} filed {form} on {filing_date.isoformat()}",
            "url": url,
            "source_id": "SEC EDGAR",
            "source_name": "SEC EDGAR",
            "source_tier": "S0",
            "published_at": f"{filing_date.isoformat()}T12:00:00Z",
            "collected_at": utc_now_iso(),
            "symbols": [symbol],
            "entities": [symbol],
            "topics": ["SEC_EDGAR", "issuer_filing"],
            "summary": f"New official {form} filing metadata. The filing must be reviewed before assigning a bullish or bearish direction.",
            "evidence_text": f"SEC accession {accession}",
            "why_it_matters": "Official issuer disclosure can change the 1-5 day and 2-6 week outlook after document review.",
            "limitations": "Direction remains neutral until the filing content is reviewed.",
            "direction": "neutral",
            "impact_horizon": "1_5d",
            "confidence": "high",
            "trade_instruction": False,
        })
    return items


def collect_sec(
    symbols: list[str], lookback_days: int,
    json_fetch: Callable[..., dict[str, Any]] = fetch_json,
) -> tuple[list[dict[str, Any]], list[str]]:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
    items: list[dict[str, Any]] = []
    errors = []
    for symbol in symbols:
        cik = CIKS.get(symbol)
        if not cik:
            continue
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        try:
            payload = json_fetch(url, sec=True)
            items.extend(sec_items_from_payload(symbol, cik, payload, cutoff))
        except Exception as exc:
            errors.append(f"SEC:{symbol}:{exc.__class__.__name__}")
    return items, errors


def dedupe_items(items: list[dict[str, Any]], seen: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    new_items = []
    updated = dict(seen)
    for item in items:
        key_source = str(item.get("url") or item.get("title") or "").strip().lower()
        if not key_source:
            continue
        key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:24]
        if key in updated:
            continue
        updated[key] = utc_now_iso()
        new_items.append(item)
    if len(updated) > 5000:
        updated = dict(list(updated.items())[-5000:])
    return new_items, updated


def compact_evidence(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]], lookback_days: int,
    *, now: datetime | None = None, max_items: int = 500,
) -> list[dict[str, Any]]:
    """Retain only normalized, deduplicated evidence needed by the 7-day model window."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current - timedelta(days=max(1, lookback_days))
    by_key: dict[str, dict[str, Any]] = {}
    timestamps: dict[str, datetime] = {}
    for raw in [*existing, *incoming]:
        if not isinstance(raw, dict):
            continue
        if not is_relevant_compact_item(raw):
            continue
        normalized = {field: raw.get(field) for field in EVIDENCE_FIELDS if field in raw}
        key_source = str(normalized.get("url") or normalized.get("title") or "").strip().lower()
        if not key_source:
            continue
        timestamp_text = parse_timestamp(normalized.get("published_at") or normalized.get("collected_at"))
        if timestamp_text:
            timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00")).astimezone(timezone.utc)
            if timestamp < cutoff:
                continue
        else:
            timestamp = current
        key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:24]
        if key not in by_key:
            by_key[key] = normalized
            timestamps[key] = timestamp
            continue
        merged = dict(by_key[key])
        for field in ("symbols", "entities", "topics"):
            old_values = merged.get(field) if isinstance(merged.get(field), list) else []
            new_values = normalized.get(field) if isinstance(normalized.get(field), list) else []
            merged[field] = list(dict.fromkeys([*old_values, *new_values]))
        tier_rank = {"S0": 0, "S1": 1, "S2": 2, "S3": 3}
        old_tier = str(merged.get("source_tier") or "S3").upper()
        new_tier = str(normalized.get("source_tier") or "S3").upper()
        if tier_rank.get(new_tier, 3) < tier_rank.get(old_tier, 3):
            merged["source_tier"] = new_tier
        old_direction = str(merged.get("direction") or "neutral")
        new_direction = str(normalized.get("direction") or "neutral")
        if old_direction == "neutral" and new_direction != "neutral":
            merged["direction"] = new_direction
        old_horizon = str(merged.get("impact_horizon") or "unspecified")
        new_horizon = str(normalized.get("impact_horizon") or "unspecified")
        if old_horizon == "unspecified" and new_horizon != "unspecified":
            merged["impact_horizon"] = new_horizon
        for field in ("summary", "evidence_text", "why_it_matters", "limitations"):
            if len(str(normalized.get(field) or "")) > len(str(merged.get(field) or "")):
                merged[field] = normalized[field]
        if timestamp >= timestamps[key] and normalized.get("collected_at"):
            merged["collected_at"] = normalized["collected_at"]
        by_key[key] = merged
        timestamps[key] = max(timestamps[key], timestamp)
    ordered = sorted(by_key, key=lambda key: (timestamps[key], key), reverse=True)[:max_items]
    return [by_key[key] for key in ordered]


def run_collection(
    *, queries_per_run: int, max_records: int, lookback_days: int, include_sec: bool,
    json_fetch: Callable[..., dict[str, Any]] = fetch_json,
    text_fetch: Callable[..., str] = fetch_text,
) -> dict[str, Any]:
    state = load_json(STATE_PATH, {"cursor": 0, "seen": {}, "provider_backoff": {}})
    cursor = int(state.get("cursor") or 0)
    count = max(1, min(queries_per_run, len(FEEDS)))
    selected = [FEEDS[(cursor + index) % len(FEEDS)] for index in range(count)]
    collected: list[dict[str, Any]] = []
    errors = []
    provider_counts = {"gdelt": 0, "google_news": 0, "sec": 0}
    provider_backoff = dict(state.get("provider_backoff") or {})
    gdelt_until = parse_timestamp(provider_backoff.get("gdelt_until"))
    gdelt_enabled = True
    if gdelt_until:
        try:
            gdelt_enabled = datetime.fromisoformat(gdelt_until.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            gdelt_enabled = True
    if gdelt_enabled:
        provider_backoff.pop("gdelt_until", None)
    for feed in selected:
        feed_items: list[dict[str, Any]] = []
        if gdelt_enabled:
            try:
                feed_items = collect_gdelt(feed, max_records, lookback_days, json_fetch)
                provider_counts["gdelt"] += len(feed_items)
            except Exception as exc:
                errors.append(f"GDELT:{feed['theme']}:{exc.__class__.__name__}")
                gdelt_enabled = False
                gdelt_until = (datetime.now(timezone.utc) + timedelta(hours=6)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                provider_backoff["gdelt_until"] = gdelt_until
        if len(feed_items) < min(3, max_records):
            try:
                google_items = collect_google_news(feed, max_records, lookback_days, text_fetch)
                provider_counts["google_news"] += len(google_items)
                feed_items.extend(google_items)
            except Exception as exc:
                errors.append(f"GOOGLE_NEWS:{feed['theme']}:{exc.__class__.__name__}")
        collected.extend(feed_items)
    if include_sec:
        selected_symbols = []
        for feed in selected:
            for symbol in feed.get("symbols", []):
                if symbol in CIKS and symbol not in selected_symbols:
                    selected_symbols.append(symbol)
        sec_items, sec_errors = collect_sec(selected_symbols[:6], lookback_days, json_fetch)
        provider_counts["sec"] += len(sec_items)
        collected.extend(sec_items)
        errors.extend(sec_errors)

    existing_index = load_json(EVIDENCE_INDEX_PATH, {"items": []})
    existing_items = existing_index.get("items", []) if isinstance(existing_index, dict) else []
    compact_items = compact_evidence(existing_items if isinstance(existing_items, list) else [], collected, lookback_days)
    write_json(EVIDENCE_INDEX_PATH, {
        "version": "1.1", "updated_at": utc_now_iso(), "retention_days": lookback_days,
        "item_count": len(compact_items), "items": compact_items,
        "actual_broker_writes": False, "external_send": False,
    })

    new_items, seen = dedupe_items(collected, state.get("seen") if isinstance(state.get("seen"), dict) else {})
    run_dir = None
    if new_items:
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_feed_{selected[0]['theme']}"
        path = RUNS_DIR / run_id
        path.mkdir(parents=True, exist_ok=True)
        evidence = {
            "version": "1.0", "worker": "manual", "mode": "news", "collected_at": utc_now_iso(),
            "task": "proactive public feed collection independent of price anomalies", "items": new_items,
            "blocked_actions_confirmed": True, "actual_broker_writes": False,
        }
        write_json(path / "raw_output.txt", evidence)
        write_json(path / "result.json", {"ok": True, "status": "feed_collected", "evidence_count": len(new_items), "errors": errors})
        run_dir = str(path)
    next_state = {
        "version": "1.1", "updated_at": utc_now_iso(), "cursor": (cursor + count) % len(FEEDS),
        "seen": seen, "provider_backoff": provider_backoff,
    }
    write_json(STATE_PATH, next_state)
    result = {
        "version": "1.0", "collected_at": utc_now_iso(), "themes": [feed["theme"] for feed in selected],
        "provider_counts": provider_counts, "raw_count": len(collected), "new_count": len(new_items),
        "errors": errors, "run_dir": run_dir, "items": new_items[:20],
        "provider_status": {"gdelt": f"backoff_until:{gdelt_until}" if not gdelt_enabled and gdelt_until else "ok"},
        "evidence_index_count": len(compact_items),
        "external_send": False, "actual_broker_writes": False,
    }
    write_json(OUTPUT_PATH, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries-per-run", type=int, default=2)
    parser.add_argument("--max-records", type=int, default=8)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--no-sec", action="store_true")
    args = parser.parse_args()
    result = run_collection(
        queries_per_run=args.queries_per_run, max_records=args.max_records,
        lookback_days=args.lookback_days, include_sec=not args.no_sec,
    )
    print(
        "PROACTIVE_NEWS_COLLECTOR "
        f"themes={','.join(result['themes'])} raw={result['raw_count']} new={result['new_count']} "
        f"errors={len(result['errors'])} external_send=false actual_broker_writes=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
