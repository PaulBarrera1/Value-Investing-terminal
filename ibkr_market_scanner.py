"""Read-only multi-source event-driven market scanner and research dashboard."""

from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import uuid
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from ib_async import IB, ScannerSubscription, StartupFetch
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
import requests

import standards


SCAN_CODES = (
    "TOP_PERC_GAIN",
    "TOP_PERC_LOSE",
    "MOST_ACTIVE",
    "HOT_BY_VOLUME",
)

EVENT_PATTERNS = {
    "M&A / merger arbitrage": (
        r"\bmergers?\b", r"\bacquisitions?\b", r"\btender offer\b",
        r"\bbuyout\b", r"\bdeal termination\b", r"\bantitrust\b",
        r"\bmerger agreement\b", r"\bbusiness combination\b", r"\bgo-private\b",
    ),
    "Spin-off / separation": (
        r"\bspin[- ]off\b", r"\bspinoff\b", r"\bseparation\b",
        r"\bwhen-issued\b", r"\bwhen distributed\b",
    ),
    "Restructuring / distress": (
        r"\brestructur(?:e|ing)\b", r"\bbankruptcy\b", r"\bchapter 11\b",
        r"\bdebt exchange\b", r"\bexchange offer\b", r"\bdefault\b",
        r"\bforbearance\b", r"\bdebt waiver\b",
    ),
    "Rights / recapitalization": (
        r"\bright issue\b", r"\bright offering\b", r"\brecapitaliz\w*\b",
        r"\bcapital raise\b", r"\bdebt[- ]for[- ]equity\b",
        r"\bconvertible notes?\b",
    ),
    "Activist / strategic review": (
        r"\bactivist\b", r"\bstrategic review\b", r"\bstrategic alternativ\w*\b",
        r"\bboard seat\b", r"\bproxy contest\b", r"\basset sale\b",
    ),
    "Insider transactions": (
        r"\binsider (?:buying|purchases?|sales?|selling|transactions?|trading)\b",
        r"\binsider (?:buys?|bought|sells?|sold)\b",
        r"\b(?:director|officer|executive) (?:buys?|bought|purchases?|purchased|sells?|sold)\b",
        r"\bopen[- ]market (?:purchase|sale)\b", r"\bform 4\b",
    ),
    "Tender / buyback / redemption": (
        r"\btender offer\b", r"\bbuyback\b", r"\brepurchase\b",
        r"\bredemption\b", r"\bcall(?:able)? preferred\b",
        r"\bmake-whole\b",
    ),
    "Short interest / positioning": (
        r"\bshort interest\b", r"\bshort squeeze\b", r"\bshort[- ]selling\b",
        r"\bshares sold short\b", r"\bdays to cover\b",
    ),
    "Index / mandate flow": (
        r"\bindex (?:add|addition|removal|rebalanc\w*)\b",
        r"\bindex inclusion\b", r"\bindex exclusion\b",
        r"\bbenchmark rebalanc\w*\b",
    ),
    "Fixed income / credit event": (
        r"\bbondholders?\b", r"\bnotes due\b", r"\bmaturity\b",
        r"\bcoupon\b", r"\bcredit rating\b", r"\bcredit outlook\b",
        r"\bdebt tender\b", r"\bexchangeable notes?\b",
    ),
    "Corporate action / security change": (
        r"\breverse stock split\b", r"\bshare consolidation\b",
        r"\bcusip (?:number )?change\b", r"\bpar value change\b",
        r"\bsymbol change\b", r"\b(?:ticker|trading) symbol (?:will )?change\b",
        r"\b(?:ticker|trading) symbol changed\b",
        r"\bchanges? (?:its )?(?:ticker )?symbol\b",
        r"\bnew ticker symbol\b", r"\bsecurity name change\b",
    ),
    "Delisting / listing status": (
        r"\bdelist(?:ed|ing|s)?\b", r"\bderegistr(?:ation|ed)\b",
        r"\bform 25(?:-nse)?\b", r"\bnotice of delisting\b",
        r"\blisting (?:warning|deficiency|non[- ]compliance|suspension)\b",
        r"\btrading suspension\b", r"\bsuspended from (?:listing|trading)\b",
        r"\bminimum bid price (?:requirement|deficiency|notice)\b",
    ),
}

EVENT_REGEXES = {
    category: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for category, patterns in EVENT_PATTERNS.items()
}

PUBLIC_SEC_QUERIES = {
    "M&A / merger arbitrage": '"merger agreement" OR acquisition OR buyout',
    "Spin-off / separation": '"spin-off" OR spinoff OR separation',
    "Restructuring / distress": 'restructuring OR bankruptcy OR "chapter 11" OR default',
    "Rights / recapitalization": '"rights offering" OR recapitalization OR "exchange offer"',
    "Activist / strategic review": 'activist OR "strategic review" OR "strategic alternatives"',
    "Insider transactions": '"insider purchase" OR "insider sale" OR "officer purchase"',
    "Tender / buyback / redemption": '"tender offer" OR buyback OR redemption',
    "Short interest / positioning": '"short interest" OR "short squeeze" OR "shares sold short"',
    "Index / mandate flow": '"index inclusion" OR "index removal" OR rebalancing',
    "Fixed income / credit event": '"debt exchange" OR "credit event" OR "debt tender"',
    "Corporate action / security change": (
        '"symbol change" OR "ticker symbol" OR "name change" OR CUSIP OR "reverse stock split"'
    ),
    "Delisting / listing status": (
        'delisting OR "notice of delisting" OR "listing deficiency" OR deregistration OR "Form 25"'
    ),
}

PUBLIC_SEC_FORMS = {
    "Delisting / listing status": "8-K,6-K,25,25-NSE,15",
}

SOURCE_MODES = {
    "ibkr": "IBKR Gateway",
    "sec": "SEC EDGAR (full-text search + deal-form index)",
    "global": "Global regulators and exchanges (EDGAR deal forms, UK Takeover Panel, ASX, HKEX, Nasdaq halts)",
    "news": "Online news + Tiingo/Benzinga + exchange RSS",
    "factiva": "Dow Jones Factiva",
    "public": "Public sources (SEC EDGAR + global regulators + news/RSS + Tiingo/Benzinga)",
    "all": "All sources (IBKR + SEC EDGAR + global regulators + news/RSS + Tiingo/Benzinga + Factiva)",
}

_SEC_TICKERS: dict[str, tuple[str, str]] | None = None
_SEC_TICKERS_BY_SYMBOL: dict[str, tuple[str, str]] | None = None


@dataclass(frozen=True)
class ScanJob:
    instrument: str
    location: str
    scan_code: str


@dataclass
class ScanCandidate:
    contract: object
    description: str = ""
    signals: set[str] = field(default_factory=set)
    headlines: list[dict] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)
    price: float | None = None
    quote_time: str = ""
    # Source-verified research fields (e.g. counterparty, key dates) that prefill the workbook row.
    research: dict[str, str] = field(default_factory=dict)


def snapshot_market_price(snapshot) -> float | None:
    """Return a usable last or bid/ask midpoint, excluding stale close prices."""
    prices = [getattr(snapshot, "last", None)]
    bid = getattr(snapshot, "bid", None)
    ask = getattr(snapshot, "ask", None)
    try:
        if bid is not None and ask is not None and float(ask) >= float(bid):
            prices.append((float(bid) + float(ask)) / 2)
    except (TypeError, ValueError):
        pass
    for value in prices:
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            return price
    return None


def build_scan_jobs(parameters_xml: str, max_scans: int = 80) -> tuple[list[ScanJob], int]:
    """Build supported global scanner jobs from IBKR's live scanner catalog."""
    root = ET.fromstring(parameters_xml)

    def local_name(node) -> str:
        return node.tag.rsplit("}", 1)[-1].lower()

    def child_text(node, name: str) -> str:
        return next((child.text or "" for child in node if local_name(child) == name), "")

    def split_codes(text: str) -> set[str]:
        return {code.strip() for code in text.split(",") if code.strip()}

    # ScanType <instruments> is a comma-separated list of instrument codes (e.g. "STK,STOCK.EU").
    scan_types = {}
    for scan_type in root.iter():
        if local_name(scan_type) != "scantype":
            continue
        code = child_text(scan_type, "scancode")
        if code in SCAN_CODES:
            scan_types.setdefault(code, set()).update(
                split_codes(child_text(scan_type, "instruments"))
            )

    # Each Location names the instrument codes it supports (e.g. STK.EU.LSE -> STOCK.EU).
    location_instruments = {}
    for location in root.iter():
        if local_name(location) != "location":
            continue
        code = child_text(location, "locationcode")
        if code:
            location_instruments.setdefault(code, set()).update(
                split_codes(child_text(location, "instruments"))
            )

    jobs_by_market = {}
    for location in sorted(location_instruments):
        for code in SCAN_CODES:
            for instrument in sorted(scan_types.get(code, set()) & location_instruments[location]):
                key = (instrument, location)
                jobs_by_market.setdefault(key, []).append(
                    ScanJob(instrument, location, code)
                )

    jobs = []
    for scan_index in range(max(map(len, jobs_by_market.values()), default=0)):
        for market_jobs in jobs_by_market.values():
            if scan_index < len(market_jobs):
                jobs.append(market_jobs[scan_index])

    total_jobs = len(jobs)
    if max_scans > 0:
        jobs = jobs[:max_scans]
    return jobs, total_jobs


def classify_headline(headline: str) -> list[tuple[str, list[str]]]:
    """Return event-pattern matches and the literal phrases that triggered them."""
    matches = []
    for category, patterns in EVENT_REGEXES.items():
        phrases = list(dict.fromkeys(
            match.group(0)
            for pattern in patterns
            for match in pattern.finditer(headline)
        ))
        if phrases:
            matches.append((category, phrases))
    return matches


def _http_get(url: str, user_agent: str):
    response = requests.get(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json, application/rss+xml, application/xml"},
        timeout=8,
    )
    response.raise_for_status()
    return response


def _http_get_with_headers(url: str, headers: dict[str, str]):
    response = requests.get(
        url,
        headers={"Accept": "application/json", **headers},
        timeout=8,
    )
    response.raise_for_status()
    return response


def _load_sec_tickers(user_agent: str) -> dict[str, tuple[str, str]]:
    global _SEC_TICKERS
    if _SEC_TICKERS is None:
        response = _http_get("https://www.sec.gov/files/company_tickers.json", user_agent)
        # A CIK appears once per listed class; the first entry is the primary common share,
        # later ones are warrants, units and preferreds (e.g. UMBF before UMBFO).
        _SEC_TICKERS = {}
        for entry in response.json().values():
            if entry.get("cik_str") and entry.get("ticker") and entry.get("title"):
                _SEC_TICKERS.setdefault(str(entry["cik_str"]).zfill(10), (entry["ticker"], entry["title"]))
    return _SEC_TICKERS


def _load_sec_tickers_by_symbol(user_agent: str) -> dict[str, tuple[str, str]]:
    global _SEC_TICKERS_BY_SYMBOL
    if _SEC_TICKERS_BY_SYMBOL is None:
        by_symbol = {}
        ambiguous = set()
        for cik, (symbol, company) in _load_sec_tickers(user_agent).items():
            symbol = symbol.upper()
            if symbol in by_symbol:
                ambiguous.add(symbol)
            else:
                by_symbol[symbol] = (cik, company)
        for symbol in ambiguous:
            by_symbol.pop(symbol, None)
        _SEC_TICKERS_BY_SYMBOL = by_symbol
    return _SEC_TICKERS_BY_SYMBOL


def _issuer_name_matches(headline_name: str, sec_company: str) -> bool:
    def core_tokens(value):
        return {
            token
            for token in re.findall(r"[a-z0-9]+", value.lower())
            if token not in {
                "a", "an", "and", "company", "corp", "corporation", "co",
                "inc", "incorporated", "limited", "ltd", "llc", "lp", "plc",
                "the", "holdings", "holding", "group", "class",
            }
        }

    headline_tokens = core_tokens(headline_name)
    issuer_tokens = core_tokens(sec_company)
    return bool(headline_tokens and headline_tokens == issuer_tokens)


def _match_sec_issuer_by_name(
    headline_name: str,
    sec_tickers: dict[str, tuple[str, str]],
) -> tuple[str, str] | None:
    matches = []
    for symbol, (_, company) in sec_tickers.items():
        if _issuer_name_matches(headline_name, company):
            matches.append((symbol, company))
    return matches[0] if len(matches) == 1 else None


def _headline_issuer_name(title: str, publisher: str, ticker: str = "") -> str:
    headline = title.strip()
    if publisher:
        headline = re.sub(
            rf"\s+-\s+{re.escape(publisher)}\s*$",
            "",
            headline,
            flags=re.IGNORECASE,
        ).strip()
    if ticker:
        ticker_match = re.search(
            rf"([A-Z][A-Za-z0-9&.,'’ -]{{1,80}}?)\s+\({re.escape(ticker)}\)",
            headline,
        )
        if ticker_match:
            return ticker_match.group(1).strip(" -:|")
    issuer_prefix = re.match(
        r"^\s*(.+?)\s+(?:announces?|completes?|executes?|signs?|reaches?|"
        r"agrees?\s+to|agreed\s+to|launches?|initiates?|enters?|plans?|seeks?|explores?|considers?|"
        r"faces?|files?|reports?|proposes?|approves?|sets?|updates?|raises?|"
        r"increases?|cuts?|suspends?|terminates?|closes?|acquires?|prepares?|pursues?|"
        r"confirms?|unveils?|begins?|will|shares|stock|to)\b",
        headline,
        flags=re.IGNORECASE,
    )
    if not issuer_prefix:
        return ""
    name = issuer_prefix.group(1).strip(" -:|")
    name = re.sub(r"^(?:exclusive|breaking|update)\s*[|:—-]\s*", "", name, flags=re.I)
    if name.lower() in {
        "a", "an", "the", "company", "firm", "group", "us agency",
        "u.s. agency", "regulator", "court",
    }:
        return ""
    return name


def scan_sec_edgar(days: int = 7, limit: int = 200) -> tuple[list[ScanCandidate], list[str], dict]:
    """Find recent event-related 8-K/6-K filings through SEC EDGAR full-text search."""
    warnings = []
    summary = {"sources_used": [], "news_provider_names": [], "contracts_found": 0}
    user_agent = os.environ.get(
        "SEC_USER_AGENT",
        "Value-Investing-Terminal research scanner (set SEC_USER_AGENT with contact details)",
    )
    if not re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", user_agent):
        warnings.append(
            "SEC_USER_AGENT is not configured with contact details; set it to an app name and contact email "
            "to comply with SEC fair-access guidance."
        )
    try:
        tickers = _load_sec_tickers(user_agent)
    except requests.RequestException as error:
        warnings.append(f"SEC company ticker directory request failed: {error}")
        return [], warnings, summary
    except (ValueError, KeyError, TypeError) as error:
        warnings.append(f"SEC company ticker directory response was invalid: {error}")
        return [], warnings, summary

    end_date = datetime.now(timezone.utc).date()
    start_date = (end_date - timedelta(days=days)).isoformat()
    candidates_by_cik = {}
    search_succeeded = False
    def search_category(entry):
        category, query = entry
        # EDGAR full-text search ignores a custom range unless both ends are given,
        # which returned filings from as far back as 2001.
        params = urlencode({
            "q": query,
            "dateRange": "custom",
            "startdt": start_date,
            "enddt": end_date.isoformat(),
            "forms": PUBLIC_SEC_FORMS.get(
                category,
                "8-K,6-K,10-K,10-K/A,10-Q,10-Q/A",
            ),
            "from": 0,
            "size": min(limit, 100),
        })
        try:
            response = _http_get(
                f"https://efts.sec.gov/LATEST/search-index?{params}",
                user_agent,
            )
            return category, response.json(), None
        except requests.RequestException as error:
            return category, None, f"SEC EDGAR search failed for {category}: {error}"
        except (ValueError, TypeError) as error:
            return category, None, f"SEC EDGAR returned invalid search results for {category}: {error}"

    with ThreadPoolExecutor(max_workers=4) as executor:
        search_results = executor.map(search_category, PUBLIC_SEC_QUERIES.items())
        for category, payload, error in search_results:
            if error:
                warnings.append(error)
                continue
            search_succeeded = True
            if not isinstance(payload, dict):
                warnings.append(f"SEC EDGAR returned an invalid response for {category}.")
                continue
            hits = payload.get("hits", {}).get("hits", [])
            for hit in hits:
                source = hit.get("_source", {})
                accession = str(source.get("adsh", "")).replace("-", "")
                cik_match = re.search(r"\bCIK\s*0*(\d+)", str(source.get("display_names", "")), re.I)
                raw_ciks = source.get("ciks") or []
                raw_cik = raw_ciks[0] if isinstance(raw_ciks, list) and raw_ciks else raw_ciks
                cik = str(raw_cik).zfill(10) if raw_cik else (
                    cik_match.group(1).zfill(10) if cik_match else ""
                )
                if not cik or not accession:
                    continue
                symbol, company = tickers.get(cik, ("CIK" + str(int(cik)), ""))
                display_names = source.get("display_names") or []
                if isinstance(display_names, str):
                    display_names = [display_names]
                if not company and display_names:
                    company = re.sub(r"\s*\([^)]*\)", "", str(display_names[0])).strip()
                form = str(source.get("form", "8-K"))
                filing_date = str(source.get("file_date", ""))
                description = str(source.get("file_description", ""))
                headline = f"{company or symbol}: {description or form} filing ({category})"
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{accession}-index.htm"
                candidate = candidates_by_cik.get(cik)
                if candidate is None:
                    contract = SimpleNamespace(
                        conId=int(cik),
                        symbol=symbol,
                        localSymbol=symbol,
                        secType="FILING",
                        primaryExchange="SEC EDGAR",
                        exchange="SEC EDGAR",
                        currency="",
                    )
                    candidate = ScanCandidate(contract=contract, description=company)
                    candidates_by_cik[cik] = candidate
                candidate.headlines.append({
                    "time": filing_date,
                    "provider": f"SEC EDGAR · {form}",
                    "article_id": accession,
                    "headline": headline,
                    "url": url,
                    "matches": [(category, [PUBLIC_SEC_QUERIES[category]])],
                })
                candidate.signals.add(f"SEC filing · {form}")
                if len(candidates_by_cik) >= limit:
                    break
            if len(candidates_by_cik) >= limit:
                break
    candidates = list(candidates_by_cik.values())
    summary["sources_used"] = ["SEC EDGAR"] if search_succeeded else []
    summary["news_provider_names"] = ["SEC EDGAR full-text filings"] if search_succeeded else []
    summary["contracts_found"] = len(candidates)
    if not candidates and not warnings and search_succeeded:
        warnings.append(f"SEC EDGAR found no matching 8-K/6-K event filings in the last {days} days.")
    return candidates, warnings, summary


def _event_news_query() -> str:
    return "(" + " OR ".join([
        '"merger agreement"', "acquisition", '"tender offer"', "spinoff",
        "restructuring", "bankruptcy", "activist", '"strategic review"',
        "recapitalization", "redemption", '"debt exchange"', '"business combination"',
        '"reverse stock split"', '"rights offering"', '"credit event"',
        "delisting", '"listing deficiency"', '"trading suspension"',
        '"ticker symbol change"', '"CUSIP change"',
        '"insider purchase"', '"insider sale"', '"short interest"',
        '"short squeeze"', '"days to cover"',
    ]) + ")"


def _google_news_rss_url(query: str, days: int) -> str:
    params = urlencode({
        "q": f"({query}) when:{days}d",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })
    return f"https://news.google.com/rss/search?{params}"


def _read_event_rss(
    url: str,
    label: str,
    limit: int,
    sec_tickers: dict[str, tuple[str, str]] | None = None,
) -> tuple[list[ScanCandidate], str | None]:
    try:
        response = _http_get(
            url,
            "ValueInvestingTerminal/1.0 (event-driven research scanner)",
        )
        root = ET.fromstring(response.content)
    except requests.RequestException as error:
        return [], f"{label} RSS request failed: {error}"
    except ET.ParseError as error:
        return [], f"{label} RSS response was not valid XML: {error}"

    candidates = []
    for item in root.findall(".//item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        parsed_link = urlsplit(link)
        if not title or parsed_link.scheme not in ("http", "https") or not parsed_link.netloc:
            continue
        matches = classify_headline(title)
        if not matches:
            continue
        item_id = hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]
        ticker_match = re.search(r"\(([A-Z][A-Z0-9.-]{0,5})\)", title)
        ticker = ticker_match.group(1) if ticker_match else ""
        source_node = next(
            (child for child in item if child.tag.rsplit("}", 1)[-1].lower() == "source"),
            None,
        )
        publisher = (
            source_node.text.strip()
            if source_node is not None and source_node.text
            else label
        )
        headline_issuer = _headline_issuer_name(title, publisher, ticker)
        ticker_data = sec_tickers.get(ticker) if sec_tickers and ticker else None
        if ticker_data and headline_issuer and not _issuer_name_matches(
            headline_issuer, ticker_data[1]
        ):
            ticker_data = None
        if not ticker_data and sec_tickers and headline_issuer:
            ticker_data = _match_sec_issuer_by_name(headline_issuer, sec_tickers)
        ticker_verified = ticker_data is not None
        symbol = (
            ticker if ticker and ticker_verified
            else ticker_data[0] if ticker_verified
            else f"NEWS-{item_id[:6].upper()}"
        )
        published = item.findtext("pubDate", "")
        description = (
            ticker_data[1] if ticker_verified else
            headline_issuer or "Issuer not identified in headline"
        )
        contract = SimpleNamespace(
            conId=int(item_id, 16),
            symbol=symbol,
            localSymbol=symbol,
            secType="STK" if ticker_verified else "NEWS",
            primaryExchange=publisher,
            exchange="",
            currency="",
        )
        candidates.append(ScanCandidate(
            contract=contract,
            description=description,
            signals={label},
            headlines=[{
                "time": published,
                "provider": publisher,
                "article_id": item_id,
                "headline": title,
                "url": link,
                "matches": matches,
            }],
        ))
        if len(candidates) >= limit:
            break
    return candidates, None


def _vendor_news_records(payload) -> list[dict]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = next(
            (payload[key] for key in ("news", "data", "results", "items")
             if isinstance(payload.get(key), list)),
            None,
        )
    else:
        records = None
    if records is None or any(not isinstance(record, dict) for record in records):
        raise ValueError("provider response did not contain a list of news records")
    return records


def _vendor_news_candidates(
    payload,
    label: str,
    limit: int,
    days: int,
    sec_tickers: dict[str, tuple[str, str]] | None = None,
) -> list[ScanCandidate]:
    recent_after = datetime.now(timezone.utc) - timedelta(days=days)
    candidates = []
    for record in _vendor_news_records(payload):
        title = str(record.get("title") or "").strip()
        link = str(record.get("url") or record.get("link") or "").strip()
        parsed_link = urlsplit(link)
        if not title or parsed_link.scheme not in ("http", "https") or not parsed_link.netloc:
            continue
        published = str(
            record.get("publishedDate") or record.get("published")
            or record.get("created") or record.get("date") or ""
        ).strip()
        if published:
            try:
                published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                if published_at < recent_after:
                    continue
            except ValueError:
                pass
        matches = classify_headline(title)
        if not matches:
            continue
        raw_source = record.get("source") or record.get("publisher") or label
        if isinstance(raw_source, dict):
            raw_source = raw_source.get("name") or raw_source.get("title") or label
        publisher = str(raw_source).strip() or label
        ticker_match = re.search(r"\(([A-Z][A-Z0-9.-]{0,5})\)", title)
        ticker = ticker_match.group(1) if ticker_match else ""
        raw_tickers = record.get("tickers") or record.get("symbols") or []
        if isinstance(raw_tickers, str):
            raw_tickers = [raw_tickers]
        if not ticker and isinstance(raw_tickers, list) and len(raw_tickers) == 1:
            raw_ticker = raw_tickers[0]
            ticker = (
                str(raw_ticker.get("name") or raw_ticker.get("symbol") or "")
                if isinstance(raw_ticker, dict)
                else str(raw_ticker)
            ).strip().upper()
        issuer = _headline_issuer_name(title, publisher, ticker_match.group(1) if ticker_match else "")
        ticker_data = sec_tickers.get(ticker) if sec_tickers and ticker else None
        if ticker_data and issuer and not _issuer_name_matches(issuer, ticker_data[1]):
            ticker_data = None
        if not ticker_data and sec_tickers and issuer:
            ticker_data = _match_sec_issuer_by_name(issuer, sec_tickers)
        ticker_verified = ticker_data is not None
        article_id = str(record.get("id") or record.get("article_id") or hashlib.sha1(
            link.encode("utf-8")
        ).hexdigest()[:12])
        symbol = (
            ticker if ticker and ticker_verified
            else ticker_data[0] if ticker_verified
            else f"NEWS-{hashlib.sha1(link.encode('utf-8')).hexdigest()[:6].upper()}"
        )
        contract = SimpleNamespace(
            conId=int(hashlib.sha1(link.encode("utf-8")).hexdigest()[:12], 16),
            symbol=symbol,
            localSymbol=symbol,
            secType="STK" if ticker_verified else "NEWS",
            primaryExchange=publisher,
            exchange="",
            currency="",
        )
        candidates.append(ScanCandidate(
            contract=contract,
            description=ticker_data[1] if ticker_verified else (
                issuer or "Issuer not identified in headline"
            ),
            signals={label},
            headlines=[{
                "time": published,
                "provider": publisher,
                "article_id": article_id,
                "headline": title,
                "url": link,
                "matches": matches,
            }],
        ))
        if len(candidates) >= limit:
            break
    return candidates


def _read_vendor_news(
    url: str,
    label: str,
    headers: dict[str, str],
    limit: int,
    days: int,
    sec_tickers: dict[str, tuple[str, str]] | None = None,
) -> tuple[list[ScanCandidate], str | None]:
    try:
        payload = _http_get_with_headers(url, headers).json()
        candidates = _vendor_news_candidates(payload, label, limit, days, sec_tickers)
    except requests.RequestException as error:
        return [], f"{label} request failed: {error}"
    except (ValueError, TypeError, KeyError) as error:
        return [], f"{label} returned invalid news data: {error}"
    return candidates, None


def _response_records(payload, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        nested = next((payload[key] for key in keys if key in payload), None)
        if isinstance(nested, dict):
            records = [
                {"symbol": symbol, **value}
                for symbol, value in nested.items()
                if isinstance(value, dict)
            ]
        else:
            records = nested
    else:
        records = None
    if records is None or any(not isinstance(record, dict) for record in records):
        raise ValueError("provider response did not contain a list of records")
    return records


def _format_vendor_value(value) -> str:
    if isinstance(value, float):
        return f"{value:,.4g}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value).strip()


def _flatten_vendor_metrics(value, prefix: str = "", depth: int = 0) -> dict[str, str]:
    metrics = {}
    if depth >= 3 or not isinstance(value, (dict, list)):
        return metrics
    entries = value.items() if isinstance(value, dict) else enumerate(value[:10])
    for key, item in entries:
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            text = _format_vendor_value(item)
            if text and len(text) <= 80:
                metrics[name] = text
        elif isinstance(item, (dict, list)):
            metrics.update(_flatten_vendor_metrics(item, name, depth + 1))
        if len(metrics) >= 20:
            break
    return metrics


def _candidate_symbols(candidates: list[ScanCandidate], limit: int = 50) -> tuple[list[str], bool]:
    symbols = []
    for candidate in candidates:
        contract = candidate.contract
        if getattr(contract, "secType", "") != "STK":
            continue
        symbol = str(getattr(contract, "symbol", "")).strip().upper()
        if re.fullmatch(r"[A-Z0-9.-]{1,12}", symbol) and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:limit], len(symbols) > limit


def _vendor_insider_candidate(record: dict, provider: str) -> ScanCandidate | None:
    symbol = str(record.get("company_symbol") or record.get("Ticker") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9.-]{1,12}", symbol):
        return None
    owner = record.get("owner") if isinstance(record.get("owner"), dict) else {}
    owner_name = str(owner.get("insider_name") or record.get("Name") or "Insider")
    transactions = record.get("transactions")
    transaction = transactions[0] if isinstance(transactions, list) and transactions and isinstance(transactions[0], dict) else record
    acquired_disposed = str(
        transaction.get("acquired_or_disposed")
        or record.get("AcquiredDisposedCode")
        or ""
    ).upper()
    transaction_code = str(
        transaction.get("transaction_code") or record.get("TransactionCode") or ""
    ).upper()
    if acquired_disposed == "A" or transaction_code == "P":
        action = "purchase"
    elif acquired_disposed == "D" or transaction_code == "S":
        action = "sale"
    else:
        action = "Form 4 transaction"
    company = str(record.get("company_name") or record.get("company") or "")
    date_value = str(
        record.get("filing_date") or record.get("fileDate")
        or transaction.get("date_transaction") or record.get("Date") or ""
    )
    shares = transaction.get("shares", record.get("Shares"))
    price = transaction.get("price_per_share", record.get("PricePerShare"))
    title = f"{owner_name} reports insider {action} in {symbol}"
    matches = classify_headline(title + " Form 4")
    if not matches:
        return None
    url = str(record.get("html_url") or record.get("url") or "").strip()
    article_id = str(
        record.get("accession_number") or record.get("id")
        or transaction.get("transaction_id") or hashlib.sha1(
            f"{symbol}|{date_value}|{owner_name}|{shares}".encode("utf-8")
        ).hexdigest()[:12]
    )
    metrics = {
        key: str(value)
        for key, value in {
            "Insider": owner_name,
            "Insider role": owner.get("insider_title") or record.get("officerTitle"),
            "Transaction date": date_value,
            "Acquired/disposed": acquired_disposed or action,
            "Shares": shares,
            "Price/share": price,
            "10b5-1 plan": record.get("is_10b5"),
            "Direct/indirect ownership": transaction.get("ownership") or record.get("directOrIndirectOwnership"),
        }.items()
        if value not in (None, "")
    }
    return ScanCandidate(
        contract=SimpleNamespace(
            conId=int(hashlib.sha1(article_id.encode("utf-8")).hexdigest()[:12], 16),
            symbol=symbol,
            localSymbol=symbol,
            secType="STK",
            primaryExchange="",
            exchange="",
            currency="USD",
        ),
        description=company or "Company name not provided",
        signals={provider, "SEC Form 4"},
        metrics=metrics,
        headlines=[{
            "time": date_value,
            "provider": provider,
            "article_id": article_id,
            "headline": title,
            "url": url,
            "matches": matches,
        }],
    )


def scan_vendor_research(
    candidates: list[ScanCandidate],
) -> tuple[list[ScanCandidate], list[str], list[str]]:
    """Fetch optional, credential-gated insider, short-interest, and fundamentals data."""
    warnings = []
    providers = []
    benzinga_key = os.environ.get("BENZINGA_API_KEY", "").strip()
    quiver_key = os.environ.get("QUIVER_API_KEY", "").strip()
    symbols, truncated = _candidate_symbols(candidates)
    candidates_by_symbol = {}
    for candidate in candidates:
        symbol = str(getattr(candidate.contract, "symbol", "")).upper()
        if symbol and getattr(candidate.contract, "secType", "") == "STK":
            candidates_by_symbol.setdefault(symbol, []).append(candidate)
    if truncated:
        warnings.append(
            "Structured vendor enrichment is limited to the first 50 unique stock symbols per scan."
        )

    if benzinga_key and symbols:
        headers = {"Authorization": f"token {benzinga_key}"}
        date_from = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        date_to = datetime.now(timezone.utc).date().isoformat()
        symbols_query = ",".join(symbols)

        insider_url = "https://api.benzinga.com/api/v1/sec/insider_transactions/transactions?" + urlencode({
            "pagesize": 1000,
            "date_from": date_from,
            "date_to": date_to,
            "search_keys_type": "symbol",
            "search_keys": symbols_query,
        })
        try:
            payload = _http_get_with_headers(insider_url, headers).json()
            filings = _response_records(payload, "data")
            for record in filings:
                insider = _vendor_insider_candidate(record, "Benzinga Form 4")
                if insider:
                    candidates.append(insider)
            providers.append("Benzinga Form 4")
        except requests.RequestException as error:
            warnings.append(f"Benzinga Form 4 request failed: {error}")
        except (ValueError, TypeError, KeyError) as error:
            warnings.append(f"Benzinga Form 4 returned invalid data: {error}")

        short_url = "https://api.benzinga.com/api/v1/shortinterest?" + urlencode({
            "symbols": symbols_query,
            "finraReport": "true",
            "pageSize": 1000,
        })
        try:
            payload = _http_get_with_headers(short_url, headers).json()
            data = payload.get("shortInterestData") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ValueError("response did not contain shortInterestData by symbol")
            for symbol, symbol_data in data.items():
                records = symbol_data.get("data") if isinstance(symbol_data, dict) else None
                if not isinstance(records, list):
                    continue
                latest = max(
                    (row for row in records if isinstance(row, dict)),
                    key=lambda row: str(row.get("recordDate") or ""),
                    default=None,
                )
                if not latest:
                    continue
                fields = {
                    "Short interest shares": latest.get("totalShortInterest"),
                    "Short interest % float": latest.get("shortPercentOfFloat"),
                    "Days to cover": latest.get("daysToCover"),
                    "Short interest record date": latest.get("recordDate"),
                    "FINRA settlement date": latest.get("settlementDate"),
                    "Average daily volume": latest.get("averageDailyVolume"),
                    "Short interest source": "Benzinga (FINRA-reported)",
                }
                for candidate in candidates_by_symbol.get(str(symbol).upper(), []):
                    candidate.metrics.update({
                        key: str(value) for key, value in fields.items()
                        if value not in (None, "")
                    })
            providers.append("Benzinga Short Interest")
        except requests.RequestException as error:
            warnings.append(f"Benzinga short-interest request failed: {error}")
        except (ValueError, TypeError, KeyError) as error:
            warnings.append(f"Benzinga short-interest returned invalid data: {error}")

        fundamentals_url = "https://api.benzinga.com/api/v3/fundamentals?" + urlencode({
            "symbols": symbols_query,
        })
        try:
            payload = _http_get_with_headers(fundamentals_url, headers).json()
            fundamentals = _response_records(payload, "data", "result", "results")
            for record in fundamentals:
                symbol = str(
                    record.get("symbol") or record.get("ticker")
                    or record.get("primarySymbol") or ""
                ).upper()
                if symbol not in candidates_by_symbol:
                    continue
                metrics = _flatten_vendor_metrics(record)
                for candidate in candidates_by_symbol[symbol]:
                    candidate.metrics.update({
                        f"Benzinga {key}": value for key, value in metrics.items()
                    })
            providers.append("Benzinga Fundamentals")
        except requests.RequestException as error:
            warnings.append(f"Benzinga fundamentals request failed: {error}")
        except (ValueError, TypeError, KeyError) as error:
            warnings.append(f"Benzinga fundamentals returned invalid data: {error}")
    elif benzinga_key:
        warnings.append(
            "Benzinga structured data was skipped because no verified stock symbols were found in this scan."
        )
    else:
        warnings.append(
            "Benzinga Form 4, short-interest, and fundamentals feeds were skipped because BENZINGA_API_KEY is not configured."
        )

    if quiver_key:
        try:
            payload = _http_get_with_headers(
                "https://api.quiverquant.com/beta/live/insiders",
                {"Authorization": f"Token {quiver_key}"},
            ).json()
            transactions = _response_records(payload, "data", "results")
            for record in transactions[:1000]:
                insider = _vendor_insider_candidate(record, "Quiver Insider Trading")
                if insider:
                    candidates.append(insider)
            providers.append("Quiver Insider Trading")
        except requests.RequestException as error:
            warnings.append(f"Quiver insider-trading request failed: {error}")
        except (ValueError, TypeError, KeyError) as error:
            warnings.append(f"Quiver insider-trading returned invalid data: {error}")
    else:
        warnings.append(
            "Quiver insider-trading feed was skipped because QUIVER_API_KEY is not configured."
        )

    return candidates, warnings, providers


def scan_online_news(days: int = 7, limit: int = 100) -> tuple[list[ScanCandidate], list[str], dict]:
    """Search public news and exchange RSS for recent special-situations headlines."""
    warnings = []
    sec_tickers = None
    sec_user_agent = os.environ.get("SEC_USER_AGENT", "")
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", sec_user_agent):
        try:
            sec_tickers = _load_sec_tickers_by_symbol(sec_user_agent)
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            warnings.append(
                f"SEC issuer directory unavailable for news ticker verification: {error}"
            )
    else:
        warnings.append(
            "SEC_USER_AGENT is not configured with contact details; news ticker symbols will not be treated as verified."
        )

    event_query = _event_news_query()
    otc_query = f'(OTCQX OR OTCQB OR OTCID OR site:otcmarkets.com) {event_query}'
    nyse_query = f'site:nyse.com {event_query}'
    feeds = [
        (
            "Nasdaq Trader corporate action RSS",
            "https://www.nasdaqtrader.com/Rss.aspx?feed=currentheadlines&categorylist=105",
        ),
        ("Google News", _google_news_rss_url(event_query, days)),
        (
            "Google News OTC-focused search RSS",
            _google_news_rss_url(otc_query, days),
        ),
        (
            "Google News NYSE-focused search RSS",
            _google_news_rss_url(nyse_query, days),
        ),
    ]
    vendor_feeds = []
    tiingo_key = os.environ.get("TIINGO_API_KEY", "").strip()
    if tiingo_key:
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        end_date = datetime.now(timezone.utc).date().isoformat()
        tiingo_url = "https://api.tiingo.com/tiingo/news?" + urlencode({
            "limit": min(limit, 100),
            "startDate": start_date,
            "endDate": end_date,
        })
        vendor_feeds.append((
            "Tiingo News",
            tiingo_url,
            {"Authorization": f"Token {tiingo_key}"},
        ))
    else:
        warnings.append(
            "Tiingo News API was skipped because TIINGO_API_KEY is not configured in the local environment."
        )
    benzinga_key = os.environ.get("BENZINGA_API_KEY", "").strip()
    if benzinga_key:
        benzinga_url = "https://api.benzinga.com/api/v2/news?" + urlencode({
            "pageSize": min(limit, 100),
        })
        vendor_feeds.append((
            "Benzinga News",
            benzinga_url,
            {"Authorization": f"token {benzinga_key}"},
        ))
    else:
        warnings.append(
            "Benzinga News API was skipped because BENZINGA_API_KEY is not configured in the local environment."
        )
    candidates_by_url = {}
    successful_sources = []
    providers = []
    with ThreadPoolExecutor(max_workers=4 + len(vendor_feeds)) as executor:
        results = executor.map(
            lambda feed: (
                feed[0],
                _read_event_rss(feed[1], feed[0], limit, sec_tickers),
            ),
            feeds,
        )
        for label, (feed_candidates, error) in results:
            if error:
                warnings.append(error)
                continue
            successful_sources.append(label)
            providers.append(label)
            for candidate in feed_candidates:
                url = candidate.headlines[0]["url"]
                candidates_by_url.setdefault(url, candidate)
        vendor_results = executor.map(
            lambda feed: (
                feed[0],
                _read_vendor_news(
                    feed[1], feed[0], feed[2], limit, days, sec_tickers
                ),
            ),
            vendor_feeds,
        )
        for label, (feed_candidates, error) in vendor_results:
            if error:
                warnings.append(error)
                continue
            successful_sources.append(label)
            providers.append(label)
            for candidate in feed_candidates:
                url = candidate.headlines[0]["url"]
                candidates_by_url.setdefault(url, candidate)
    candidates = list(candidates_by_url.values())
    if candidates:
        warnings.append(
            "Online news headlines are secondary-source leads; verify each event against issuer filings or other primary sources."
        )
    if not candidates and successful_sources:
        warnings.append("Online news and exchange feeds returned no recent event-pattern headlines.")
    if not successful_sources and not warnings:
        warnings.append("No online or exchange RSS source could be reached.")
    summary = {
        "sources_used": successful_sources,
        "news_provider_names": providers,
        "contracts_found": len(candidates),
    }
    return candidates, warnings, summary


def scan_factiva(days: int = 14, limit: int = 30) -> tuple[list[ScanCandidate], list[str], dict]:
    """Retrieve recent event-driven news through Dow Jones' Factiva Retrieval API."""
    summary = {
        "sources_used": [],
        "news_provider_names": [],
        "contracts_found": 0,
    }
    credentials = {
        "client_id": os.environ.get("FACTIVA_CLIENTID", "").strip(),
        "username": os.environ.get("FACTIVA_USERNAME", "").strip(),
        "password": os.environ.get("FACTIVA_PASSWORD", ""),
    }
    missing = [key for key, value in credentials.items() if not value]
    if missing:
        return [], [
            "Dow Jones Factiva was selected, but local API credentials are missing. "
            "Set FACTIVA_CLIENTID, FACTIVA_USERNAME, and FACTIVA_PASSWORD in the environment."
        ], summary
    if days < 1 or limit < 1:
        raise ValueError("Factiva lookback and result limit must be positive.")

    auth_url = "https://accounts.dowjones.com/oauth2/v1/token"
    api_url = "https://api.dowjones.com/content/gen-ai/retrieve"
    auth_payload = {
        "client_id": credentials["client_id"],
        "username": credentials["username"],
        "grant_type": "password",
        "connection": "service-account",
        "scope": "openid service_account_id",
        "password": credentials["password"],
    }
    try:
        token_response = requests.post(
            auth_url,
            data=auth_payload,
            timeout=15,
            allow_redirects=False,
        )
        if 300 <= token_response.status_code < 400:
            raise RuntimeError("Dow Jones authentication redirected unexpectedly.")
        token_response.raise_for_status()
        token_data = token_response.json()
        id_token = token_data["id_token"]
        access_token = token_data["access_token"]

        bearer_response = requests.post(
            auth_url,
            data={
                "client_id": credentials["client_id"],
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "connection": "service-account",
                "scope": "openid pib",
                "access_token": access_token,
                "assertion": id_token,
            },
            timeout=15,
            allow_redirects=False,
        )
        if 300 <= bearer_response.status_code < 400:
            raise RuntimeError("Dow Jones bearer-token request redirected unexpectedly.")
        bearer_response.raise_for_status()
        bearer_token = bearer_response.json()["access_token"]

        user_email = os.environ.get(
            "FACTIVA_METRICS_EMAIL",
            credentials["username"],
        ).strip()
        user_id = hashlib.md5(user_email.encode("utf-8")).hexdigest()
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=days)
        query_payload = {
            "data": {
                "attributes": {
                    "response_limit": min(limit, 100),
                    "query": {
                        "value": (
                            "Find recent news articles about publicly traded companies and "
                            "event-driven special situations, including mergers and acquisitions, "
                            "tender offers, spin-offs, restructurings, bankruptcies, activist "
                            "campaigns, strategic reviews, rights offerings, buybacks, redemptions, "
                            "index changes, and credit events. Return relevant original news "
                            "articles, not investment recommendations."
                        ),
                        "search_filters": [],
                        "date": {
                            "custom": {
                                "from": start_date.isoformat(),
                                "to": end_date.isoformat(),
                            },
                        },
                    },
                    "metrics_data": {
                        "user_id": user_id,
                        "work_id": uuid.uuid4().hex,
                    },
                },
                "id": "GenAIRetrieval",
                "type": "genai-content",
            },
        }
        response = requests.post(
            api_url,
            json=query_payload,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30,
            allow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            raise RuntimeError("Dow Jones Retrieval API redirected unexpectedly.")
        response.raise_for_status()
        payload = response.json()
        articles = payload["data"]
        if not isinstance(articles, list):
            raise ValueError("Dow Jones Retrieval API response data was not a list.")
    except requests.RequestException as error:
        return [], [f"Dow Jones Factiva request failed: {error}"], summary
    except (ValueError, KeyError, TypeError) as error:
        return [], [f"Dow Jones Factiva returned an invalid response: {error}"], summary
    except RuntimeError as error:
        return [], [str(error)], summary

    candidates = []
    seen_articles = set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        attributes = article.get("attributes", {})
        meta = article.get("meta", {})
        if not isinstance(attributes, dict) or not isinstance(meta, dict):
            continue
        headline_data = attributes.get("headline", {})
        headline = (
            headline_data.get("main", {}).get("text", "")
            if isinstance(headline_data, dict)
            and isinstance(headline_data.get("main"), dict)
            else ""
        )
        headline = str(headline).strip()
        if not headline:
            continue
        article_id = str(meta.get("original_doc_id") or article.get("id") or "")
        if not article_id or article_id in seen_articles:
            continue
        seen_articles.add(article_id)
        snippet_data = attributes.get("snippet", {})
        snippet_parts = snippet_data.get("content", []) if isinstance(snippet_data, dict) else []
        snippet = " ".join(
            str(part.get("text", "")).strip()
            for part in snippet_parts
            if isinstance(part, dict) and part.get("text")
        )
        matches = classify_headline(f"{headline} {snippet}")
        if not matches:
            continue
        source_data = meta.get("source", {})
        publisher = (
            str(source_data.get("name", "")).strip()
            if isinstance(source_data, dict)
            else ""
        ) or "Dow Jones Factiva"
        ticker_match = re.search(r"\(([A-Z][A-Z0-9.-]{0,5})\)", headline)
        symbol = ticker_match.group(1) if ticker_match else f"FACTIVA-{hashlib.sha1(article_id.encode('utf-8')).hexdigest()[:6].upper()}"
        company_match = re.search(
            r"^\s*(.+?)\s+\([A-Z][A-Z0-9.-]{0,5}\)",
            headline,
        )
        description = (
            company_match.group(1).strip()
            if company_match
            else "Issuer not verified from Factiva headline"
        )
        candidate = ScanCandidate(
            contract=SimpleNamespace(
                conId=int(hashlib.sha1(article_id.encode("utf-8")).hexdigest()[:8], 16),
                symbol=symbol,
                localSymbol=symbol,
                secType="STK" if ticker_match else "NEWS",
                primaryExchange=publisher,
                exchange="",
                currency="",
            ),
            description=description,
            signals={publisher},
            headlines=[{
                "time": str(attributes.get("publication_date", "")),
                "provider": f"Dow Jones Factiva · {publisher}",
                "article_id": article_id,
                "headline": headline,
                "url": "",
                "matches": matches,
            }],
        )
        candidates.append(candidate)
        if len(candidates) >= limit:
            break

    summary["sources_used"] = ["Dow Jones Factiva"]
    summary["news_provider_names"] = ["Dow Jones Factiva"]
    summary["contracts_found"] = len(candidates)
    if not candidates:
        return candidates, [
            "Dow Jones Factiva returned no recent articles matching the scanner's event patterns."
        ], summary
    return candidates, [], summary


def check_gs_marquee_connection() -> dict:
    """Verify Marquee client-credentials access with the profile endpoint only."""
    client_id = os.environ.get("GS_MARQUEE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GS_MARQUEE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise ValueError(
            "Set GS_MARQUEE_CLIENT_ID and GS_MARQUEE_CLIENT_SECRET in the local environment."
        )

    token_url = "https://idfs.gs.com/as/token.oauth2"
    profile_url = "https://api.marquee.gs.com/v1/users/self"
    try:
        token_response = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "read_user_profile",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
            allow_redirects=False,
        )
        if 300 <= token_response.status_code < 400:
            raise RuntimeError("Marquee OAuth endpoint redirected unexpectedly.")
        if token_response.status_code != 200:
            raise RuntimeError(
                f"Marquee OAuth authentication failed (HTTP {token_response.status_code})."
            )
        token_payload = token_response.json()
        access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Marquee OAuth response did not contain an access token.")

        profile_response = requests.get(
            profile_url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=15,
            allow_redirects=False,
        )
        if 300 <= profile_response.status_code < 400:
            raise RuntimeError("Marquee profile endpoint redirected unexpectedly.")
        if profile_response.status_code != 200:
            raise RuntimeError(
                f"Marquee profile check failed (HTTP {profile_response.status_code})."
            )
        profile_payload = profile_response.json()
        if not isinstance(profile_payload, dict):
            raise RuntimeError("Marquee profile endpoint returned an invalid response.")
    except requests.RequestException as error:
        raise RuntimeError(f"Marquee connection request failed: {error}") from error
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"Marquee returned an invalid JSON response: {error}") from error

    return {"ok": True, "message": "Marquee authentication and profile check succeeded."}


def event_relevance_score(candidate: ScanCandidate) -> int:
    categories = {
        category
        for item in candidate.headlines
        for category, _ in item["matches"]
    }
    headline_count = len(candidate.headlines)
    score = 25 + min(35, 15 * len(categories)) + min(20, 5 * max(0, headline_count - 1))
    score += min(20, 5 * len(candidate.signals))
    return min(100, score)


def _candidate_contract(scan_item):
    details = scan_item.contractDetails
    contract = details.contract
    description = getattr(details, "longName", "") or ""
    return contract, description


async def _scan_ibkr(
    host: str,
    port: int,
    client_id: int,
    max_scans: int,
    max_news_contracts: int,
    news_days: int,
    news_results: int,
) -> tuple[list[ScanCandidate], list[str], dict]:
    ib = IB()
    warnings = []
    summary = {
        "host": host,
        "port": port,
        "scans_planned": 0,
        "scans_total": 0,
        "scans_succeeded": 0,
        "contracts_found": 0,
        "news_contracts_queried": 0,
        "quotes_queried": 0,
        "quotes_received": 0,
        "news_provider_names": [],
        "instruments": [],
        "locations": [],
    }
    try:
        await ib.connectAsync(
            host,
            port,
            clientId=client_id,
            timeout=8,
            readonly=True,
            fetchFields=StartupFetch(0),
        )
        parameters_xml = await ib.reqScannerParametersAsync()
        jobs, total_jobs = build_scan_jobs(parameters_xml, max_scans=max_scans)
        summary["scans_planned"] = len(jobs)
        summary["scans_total"] = total_jobs
        summary["instruments"] = sorted({job.instrument for job in jobs})
        summary["locations"] = sorted({job.location for job in jobs})
        if not jobs:
            warnings.append(
                "IBKR returned no supported global scanner combinations for the selected event-oriented scan types."
            )
            return [], warnings, summary

        candidates_by_id = {}
        for index, job in enumerate(jobs, 1):
            if index == 1 or index % 10 == 0:
                print(
                    f"  Scanner requests: {index}/{len(jobs)} "
                    f"({job.instrument} {job.location} {job.scan_code})"
                )
            subscription = ScannerSubscription(
                numberOfRows=50,
                instrument=job.instrument,
                locationCode=job.location,
                scanCode=job.scan_code,
            )
            try:
                scan_data = await ib.reqScannerDataAsync(subscription)
                summary["scans_succeeded"] += 1
            except Exception as error:
                warnings.append(
                    f"Scanner {job.instrument}/{job.location}/{job.scan_code} failed: {error}"
                )
                continue
            for scan_item in scan_data:
                contract, description = _candidate_contract(scan_item)
                con_id = getattr(contract, "conId", 0)
                if not con_id:
                    continue
                candidate = candidates_by_id.setdefault(
                    con_id, ScanCandidate(contract=contract, description=description)
                )
                candidate.signals.add(
                    f"{job.instrument} {job.location} {job.scan_code}"
                )

        candidates = list(candidates_by_id.values())
        summary["contracts_found"] = len(candidates)
        try:
            providers = await ib.reqNewsProvidersAsync()
        except Exception as error:
            providers = []
            warnings.append(f"IBKR news providers could not be loaded: {error}")
        summary["news_provider_names"] = [provider.name for provider in providers]
        # IBKR expects provider codes joined with "+"; a comma-joined list silently times out.
        provider_codes = "+".join(provider.code for provider in providers if provider.code)
        if not provider_codes:
            warnings.append(
                "IBKR returned no news providers. Market scans ran, but no event headlines could be matched."
            )
            return candidates, warnings, summary

        news_candidates = sorted(
            candidates,
            key=lambda candidate: len(candidate.signals),
            reverse=True,
        )[:max_news_contracts]
        summary["news_contracts_queried"] = len(news_candidates)
        print(
            f"Checking recent headlines for {len(news_candidates)} of "
            f"{len(candidates)} discovered contracts…"
        )
        if len(candidates) > len(news_candidates):
            warnings.append(
                f"News checked for {len(news_candidates)} of {len(candidates)} scanner contracts; "
                "increase IBKR_MAX_NEWS_CONTRACTS to widen headline coverage."
            )

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=news_days)

        async def fetch_news(candidate):
            con_id = candidate.contract.conId
            try:
                articles = await ib.reqHistoricalNewsAsync(
                    con_id,
                    provider_codes,
                    start_time,
                    end_time,
                    news_results,
                )
            except Exception as error:
                warnings.append(f"News request failed for {candidate.contract.symbol}: {error}")
                return
            if articles is None:
                warnings.append(
                    f"IBKR news request timed out for {candidate.contract.symbol} "
                    f"(conId {con_id}); no headlines were confirmed."
                )
                return
            for article in articles or []:
                matches = classify_headline(article.headline)
                if matches:
                    candidate.headlines.append({
                        "time": article.time.isoformat(),
                        "provider": article.providerCode,
                        "article_id": article.articleId,
                        "headline": article.headline,
                        "matches": matches,
                    })

        semaphore = asyncio.Semaphore(4)

        async def limited_fetch(candidate):
            async with semaphore:
                await fetch_news(candidate)

        await asyncio.gather(*(limited_fetch(candidate) for candidate in news_candidates))
        quote_candidates = [candidate for candidate in news_candidates if candidate.headlines]
        summary["quotes_queried"] = len(quote_candidates)
        if quote_candidates:
            print(f"Requesting market-data snapshots for {len(quote_candidates)} event leads…")
            for start in range(0, len(quote_candidates), 25):
                batch = quote_candidates[start:start + 25]
                try:
                    snapshots = await ib.reqTickersAsync(
                        *(candidate.contract for candidate in batch)
                    )
                except Exception as error:
                    warnings.append(f"IBKR market-data snapshot batch failed: {error}")
                    continue
                quote_time = datetime.now().astimezone().isoformat()
                for candidate, snapshot in zip(batch, snapshots):
                    price = snapshot_market_price(snapshot)
                    if price is not None:
                        candidate.price = price
                        candidate.quote_time = quote_time
                        summary["quotes_received"] += 1
                if summary["quotes_received"] < len(quote_candidates):
                    warnings.append(
                        f"IBKR returned usable market-data snapshots for "
                        f"{summary['quotes_received']} of {len(quote_candidates)} event leads. "
                        "Check market-data subscriptions and quote availability."
                    )
        return candidates, warnings, summary
    finally:
        if ib.isConnected():
            ib.disconnect()


def _safe(value) -> str:
    return escape(str(value if value is not None else ""), quote=True)


SPECIAL_SITUATIONS_COLUMNS = [
    ("tier", "Tier"),
    ("ticker", "Ticker"),
    ("company", "Company"),
    ("situation_type", "Situation type"),
    ("counterparty", "Counterparty"),
    ("deal_terms", "Deal terms"),
    ("price", "Price"),
    ("consideration_value", "Consideration value"),
    ("gross_spread", "Gross spread"),
    ("annualized", "Annualized"),
    ("key_dates_catalyst", "Key dates/catalyst"),
    ("status_approvals", "Status/approvals"),
    ("bull", "Bull"),
    ("bear", "Bear"),
    ("risks", "Risks"),
    ("downside_if_broken", "Downside if broken"),
    ("market_cap", "Market cap"),
    ("sources", "Sources"),
    ("date_pulled", "Date pulled"),
]

# Deals whose risk-adjusted (probability-weighted) annualized return clears this hurdle are
# written to the tracking workbook. The dashboard only proposes; record_tracked_deal applies it.
TRACKER_MIN_ANNUALIZED = standards.HURDLE
TRACKER_FILENAME = "special_situations_tracker.xlsx"
TRACKER_COLUMNS = [
    ("id", "Lead ID"),
    ("date_added", "Date added"),
    ("date_pulled", "Date pulled"),
    ("last_updated", "Last updated"),
    ("status", "Status"),
    ("ticker", "Ticker"),
    ("company", "Company"),
    ("situation_type", "Situation type"),
    ("counterparty", "Counterparty"),
    ("deal_terms", "Deal terms"),
    ("price", "Price at pull"),
    ("consideration_value", "Consideration value"),
    ("gross_spread", "Gross spread"),
    ("close_date", "Expected close"),
    ("days", "Days to close (from pull)"),
    ("annualized", "Annualized"),
    ("key_dates_catalyst", "Key dates/catalyst"),
    ("sources", "Sources"),
    # Added after the first release; appended so existing tracker rows keep their columns.
    ("probability", "Completion probability"),
    ("break_price", "Break price"),
    ("risk_adjusted_annualized", "Risk-adj. annualized (hurdle)"),
]
TRACKER_WIDTHS = [14, 12, 12, 12, 16, 11, 30, 26, 24, 38, 12, 18, 12, 13, 12, 12, 50, 50, 12, 11, 14]

ANALYSIS_COLUMNS = [
    ("rank", "EV rank"),
    ("ticker", "Ticker"),
    ("situation_type", "Situation type"),
    ("why_mispriced", "Why mispriced (forced flow)"),
    ("value_estimate", "Value estimate (method)"),
    ("catalyst_date", "Catalyst + date"),
    ("days", "Days"),
    ("upside", "Upside if right"),
    ("downside", "Downside if wrong (valuation, opinion)"),
    ("probability", "Probability (opinion)"),
    ("ev", "EV (prob-weighted)"),
    ("annualized", "Annualized (arb)"),
    ("sizing_note", "Sizing note (liquidity/borrow)"),
]


def build_research_leads(candidates: list[ScanCandidate], pulled: str = "") -> list[dict]:
    pulled = pulled or datetime.now().astimezone().date().isoformat()
    leads = []
    for candidate in candidates:
        if not candidate.headlines:
            continue
        contract = candidate.contract
        categories = sorted({
            category
            for article in candidate.headlines
            for category, _ in article["matches"]
        })
        symbol = getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")
        con_id = getattr(contract, "conId", "")
        headline_text = " | ".join(
            article["headline"] for article in candidate.headlines
        )
        sources = "; ".join(
            f"{article['provider']}:{article['article_id']}"
            for article in candidate.headlines
        )
        if candidate.metrics:
            sources = "; ".join(filter(None, (
                sources,
                "Vendor context: " + "; ".join(
                    f"{name}={value}" for name, value in sorted(candidate.metrics.items())
                ),
            )))
        if candidate.quote_time:
            sources = "; ".join(filter(None, (
                sources, f"IBKR market-data snapshot {candidate.quote_time}"
            )))
        situation_type = "; ".join(categories)
        tier = f"Scan lead · {event_relevance_score(candidate)}/100 relevance"
        ticker = (
            "Unverified"
            if getattr(contract, "secType", "") == "NEWS"
            else str(symbol)
        )
        leads.append({
            "id": str(con_id),
            "tier": tier,
            "ticker": ticker,
            "company": candidate.description,
            "situation_type": situation_type,
            "counterparty": "",
            "deal_terms": "",
            "price": candidate.price if candidate.price is not None else "",
            "consideration_value": "",
            "gross_spread": "",
            "annualized": "",
            "key_dates_catalyst": headline_text,
            "status_approvals": "Headline match — verify event and status",
            "bull": "",
            "bear": "",
            "risks": "Confirm full source, live terms, approvals, liquidity, and break case.",
            "downside_if_broken": "",
            "market_cap": "",
            "sources": sources,
            "date_pulled": pulled,
        })
        # Fields a source states verbatim (offeror, offer-period dates) replace the blank defaults.
        leads[-1].update({
            name: value for name, value in candidate.research.items()
            if value and name in leads[-1] and name not in ("id", "ticker", "sources")
        })
        leads[-1]["cik"] = candidate.research.get("cik") or (
            str(con_id).zfill(10) if getattr(contract, "secType", "") == "FILING" else ""
        )
    return leads


def build_analysis_leads(leads: list[dict]) -> list[dict]:
    return [{
        "id": str(lead.get("id", lead.get("ticker", ""))),
        "rank": "",
        "ticker": lead["ticker"],
        "situation_type": lead["situation_type"],
        "why_mispriced": "",
        "value_estimate": "",
        "catalyst_date": lead["key_dates_catalyst"],
        "days": "",
        "upside": "",
        "downside": "",
        "probability": "",
        "ev": "",
        "annualized": "",
        "sizing_note": "",
    } for lead in leads]


def _json_for_html(data) -> str:
    return (
        json.dumps(data, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


# Detail-panel deal calculator and SEC financials. Mirrors global_event_sources.deal_math:
# value = (cash + ratio x acquirer price + CVR estimate) x FX; annualized = spread x 365 / days (simple).
DEAL_TOOLS_SCRIPT = r"""
const CALC_FIELDS=[['calc_cash','Cash per share (offer currency)'],['calc_ratio','Stock ratio (acquirer shares per share)'],
  ['calc_acquirer_price','Acquirer share price (offer currency)'],['calc_cvr','CVR / contingent value (analyst estimate)'],
  ['calc_fx','FX: 1 offer-currency unit in quote currency (blank = 1)'],['calc_close_date','Expected close date (days run from the pull date)'],
  ['calc_days','Days to close (used only without a close date)'],
  ['calc_probability','Completion probability % (your estimate)'],['calc_break_price','Break price if the deal fails (quote currency)']];
function pullDate(row){return /^\d{4}-\d{2}-\d{2}$/.test(row.date_pulled||'')?row.date_pulled:SCAN_DATE;}
function daysFromPull(row){
  const close=row.calc_close_date;
  if(!/^\d{4}-\d{2}-\d{2}$/.test(close||'')) return calcNumber(row,'calc_days');
  return Math.round((Date.parse(close+'T00:00:00Z')-Date.parse(pullDate(row)+'T00:00:00Z'))/86400000);
}
function calcNumber(row,key){const raw=row[key];const value=Number(raw);return raw!==''&&raw!=null&&Number.isFinite(value)?value:null;}
function signedPct(x){return (x>=0?'+':'')+(x*100).toFixed(1)+'%';}
function fmtMoney(v){
  if(v===null||v===undefined||!Number.isFinite(Number(v))) return 'n/a';
  const a=Math.abs(v);
  return a>=1e9?(v/1e9).toFixed(2)+'B':a>=1e6?(v/1e6).toFixed(1)+'M':Number(v).toLocaleString();
}
function dealMath(row){
  const price=Number(row.price);
  if(!(price>0)) return {error:'Needs a positive target price: the scanner quote, or type one into Price.'};
  const cash=calcNumber(row,'calc_cash')||0,ratio=calcNumber(row,'calc_ratio')||0,acquirer=calcNumber(row,'calc_acquirer_price')||0;
  const cvr=calcNumber(row,'calc_cvr')||0,fx=calcNumber(row,'calc_fx')??1,days=daysFromPull(row);
  if(fx<=0) return {error:'FX must be positive.'};
  if(row.calc_close_date&&days<=0) return {error:'Expected close date must be after the pull date ('+pullDate(row)+').'};
  if(ratio&&!acquirer) return {error:'A stock ratio needs the acquirer share price.'};
  if(!cash&&!ratio&&!cvr) return {error:'Enter cash and/or a stock ratio with the acquirer price.'};
  const p=calcNumber(row,'calc_probability'),breakPrice=calcNumber(row,'calc_break_price');
  if(p!==null&&(p<0||p>100)) return {error:'Probability is a percentage from 0 to 100.'};
  if(breakPrice!==null&&breakPrice<0) return {error:'Break price cannot be negative.'};
  const value=(cash+ratio*acquirer+cvr)*fx,spread=value/price-1;
  const parts=[cash?cash+' cash':'',ratio?ratio+' x acquirer @ '+acquirer:'',cvr?'CVR est. '+cvr:''].filter(Boolean);
  // Risk-adjusted = (p x spread + (1 - p) x break loss) x 365 / days: the hurdle is tested on this.
  let riskAdj=null,loss=null,marketP=null,needed=null;
  if(p!==null&&breakPrice!==null&&days>0){
    loss=breakPrice/price-1;
    riskAdj=(p/100*spread+(1-p/100)*loss)*365/days;
    if(spread>loss){marketP=-loss/(spread-loss);needed=(TRACKER_MIN_ANNUALIZED*days/365-loss)/(spread-loss);}
  }
  return {value,spread,days,annualized:days>0?spread*365/days:null,p,breakPrice,loss,riskAdj,marketP,needed,
    terms:parts.join(' + ')+(fx!==1?' (x FX '+fx+')':'')};
}
function renderDealTools(id){
  const host=document.getElementById('dealTools'),row=researchState.research[id];
  if(!host||!row) return;
  host.innerHTML='<h3 style="margin-top:22px">Deal calculator</h3>'+
    '<p class="sheet-note">Enter the offer terms from the source filing. Consideration value = (cash + ratio x acquirer price + CVR estimate) x FX; '+
    'annualized = gross spread x 365 / days (simple, as in the workbook), with days counted from the pull date ('+escapeCell(pullDate(row))+'). '+
    'The hurdle is risk-adjusted: (p x spread + (1 - p) x break loss) x 365 / days must reach '+(TRACKER_MIN_ANNUALIZED*100).toFixed(0)+
    '%. Use a close date that allows for regulatory slippage and a break price below the pre-deal price (deals break on bad news). '+
    'CVR value is your estimate, not a market price. Applying a deal that clears adds it to the tracking workbook.</p>'+
    '<div class="detail-grid">'+CALC_FIELDS.map(([field,label])=>'<label class="detail-field"><span>'+escapeCell(label)+
      '</span><input type="'+(field==='calc_close_date'?'date" min="'+pullDate(row):'number" step="any')+'" data-calc="'+field+
      '" value="'+escapeCell(row[field]??'')+'"></label>').join('')+'</div>'+
    '<p id="calcResult" class="sheet-note" role="status"></p>'+
    '<div class="actions"><button type="button" data-apply-calc>Apply to workbook row</button>'+
    '<button type="button" data-load-financials>Load SEC financials</button><span id="finStatus" class="small" role="status"></span></div>'+
    '<div id="finTable"></div>';
  updateCalcResult(id);
  if(row.financials) renderFinancials(row.financials);
}
function updateCalcResult(id){
  const out=document.getElementById('calcResult'),row=researchState.research[id];
  if(!out||!row) return;
  const r=dealMath(row);
  out.textContent=r.error||('Consideration '+r.value.toFixed(2)+' vs price '+Number(row.price).toFixed(2)+
    ' -> gross spread '+signedPct(r.spread)+(r.annualized!==null?', annualized '+signedPct(r.annualized)+' over '+r.days+
    'd from '+pullDate(row):', add a close date for annualized')+riskText(r));
}
function riskText(r){
  if(r.riskAdj===null) return '. Add a probability and break price to test the '+(TRACKER_MIN_ANNUALIZED*100).toFixed(0)+'% risk-adjusted hurdle.';
  const pct=x=>(x*100).toFixed(0)+'%';
  return '. Break loss '+signedPct(r.loss)+'; risk-adjusted annualized '+signedPct(r.riskAdj)+
    (r.riskAdj>=TRACKER_MIN_ANNUALIZED?' - clears the hurdle':' - below the hurdle')+
    (r.marketP!==null?'. Market-implied p '+pct(r.marketP)+' vs your '+r.p+'%'+(r.p/100-r.marketP>0.15?
      ' - you are far more confident than the market: find what it is pricing':''):'')+
    (r.needed!==null?(r.needed>1?'. Cannot clear even at 100%':'. Needs p >= '+pct(Math.max(0,r.needed))):'')+'.';
}
function renderFinancials(f){
  const table=document.getElementById('finTable');
  if(!table) return;
  const labels={shares_outstanding:'Shares outstanding',cash:'Cash & equivalents',short_term_investments:'Short-term investments',
    total_debt_current:'Debt - current',total_debt_noncurrent:'Debt - non-current',revenue:'Revenue (FY)',operating_income:'Operating income (FY)',
    net_income:'Net income (FY)',operating_cash_flow:'Operating cash flow (FY)',capex:'Capex (FY)',total_assets:'Total assets',stockholders_equity:'Shareholders equity'};
  const d=f.derived||{};
  const derived=[['Market cap (quote x SEC shares)',fmtMoney(d.market_cap)],['Net cash',fmtMoney(d.net_cash)],['Enterprise value',fmtMoney(d.enterprise_value)],
    ['Net cash per share',d.net_cash_per_share!=null?Number(d.net_cash_per_share).toFixed(2):'n/a'],['Free cash flow (FY)',fmtMoney(d.free_cash_flow)],
    ['FCF yield',d.fcf_yield!=null?signedPct(d.fcf_yield):'n/a']];
  table.innerHTML='<h3 style="margin-top:22px">SEC financials - '+escapeCell(f.entity)+'</h3>'+
    '<p class="sheet-note">Latest XBRL facts filed with the SEC (<a class="analysis-link" target="_blank" rel="noopener" href="'+escapeCell(f.source)+'">source</a>). '+
    'Market cap and EV use the scanner price'+(f.price_used?' ('+f.price_used+')':' (none available)')+'. Check period ends: balance-sheet and fiscal-year figures can differ in date.</p>'+
    '<div class="table-scroll"><table class="research-table" style="min-width:0"><thead><tr><th>Item</th><th>Value</th><th>Period end</th><th>Form</th><th>XBRL concept</th></tr></thead><tbody>'+
    derived.map(([k,v])=>'<tr><td><strong>'+escapeCell(k)+'</strong></td><td>'+escapeCell(v)+'</td><td></td><td>derived</td><td></td></tr>').join('')+
    Object.entries(labels).map(([k,label])=>{const fact=(f.facts||{})[k];return '<tr><td>'+escapeCell(label)+'</td><td>'+
      (fact?escapeCell(k==='shares_outstanding'?Number(fact.value).toLocaleString():fmtMoney(fact.value)):'not reported')+'</td><td>'+
      escapeCell(fact?fact.end:'')+'</td><td>'+escapeCell(fact?fact.form:'')+'</td><td>'+escapeCell(fact?fact.concept:'')+'</td></tr>';}).join('')+
    '</tbody></table></div>';
}
document.getElementById('selectedAnalysis').addEventListener('input',event=>{
  const input=event.target.closest('[data-calc]');
  if(!input||!selectedAnalysisId) return;
  researchState.research[selectedAnalysisId][input.dataset.calc]=input.value;
  saveResearch();updateCalcResult(selectedAnalysisId);
});
document.getElementById('selectedAnalysis').addEventListener('click',async event=>{
  const id=selectedAnalysisId,research=researchState.research[id],analysis=researchState.analysis[id];
  if(!research) return;
  if(event.target.closest('[data-apply-calc]')){
    const r=dealMath(research);
    if(r.error){document.getElementById('calcResult').textContent=r.error;return;}
    research.consideration_value=r.value.toFixed(2)+' ('+r.terms+')';
    research.gross_spread=signedPct(r.spread);
    research.annualized=r.annualized!==null?signedPct(r.annualized)+' over '+r.days+'d':'n/a (no close date)';
    if(!research.deal_terms) research.deal_terms=r.terms;
    if(analysis){
      analysis.upside=(r.spread*100).toFixed(1);
      if(r.riskAdj!==null){analysis.probability=String(r.p);analysis.downside=(r.loss*100).toFixed(1);}
      if(r.days>0) analysis.days=String(r.days);
      analysis.annualized=research.annualized;
      if(!analysis.value_estimate) analysis.value_estimate=r.value.toFixed(2)+' ('+r.terms+')';
    }
    saveResearch();renderResearch();
    const result=document.getElementById('calcResult');
    result.textContent='Applied to the workbook row and the EV analysis upside.';
    if(r.annualized===null){result.textContent+=' Add an expected close date to check it against the tracking threshold.';return;}
    if(r.riskAdj===null){result.textContent+=' Add a probability and break price to test it against the risk-adjusted hurdle; tracker not updated.';return;}
    try{
      const response=await fetch('/api/track',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        id,ticker:research.ticker,company:research.company,situation_type:research.situation_type,counterparty:research.counterparty,
        deal_terms:research.deal_terms,price:research.price,consideration_value:r.value,gross_spread:r.spread,
        close_date:research.calc_close_date||'',days:r.days,annualized:r.annualized,
        probability:r.p/100,break_price:r.breakPrice,risk_adjusted_annualized:r.riskAdj,
        key_dates_catalyst:research.key_dates_catalyst,sources:research.sources,date_pulled:pullDate(research)})});
      const tracked=await response.json();
      if(!response.ok) throw new Error(tracked.error||'Tracker update failed.');
      const message=tracked.tracked?(tracked.updated?' Tracker row updated':' Added to the tracker')+' ('+tracked.path+').':
        tracked.status.startsWith('Below threshold since')?' Below the threshold now; tracker row marked "'+tracked.status+'".':
        ' Below the '+(TRACKER_MIN_ANNUALIZED*100).toFixed(0)+'% threshold, so not tracked.';
      document.getElementById('calcResult').textContent+=message;
    }catch(error){
      const out=document.getElementById('calcResult');
      out.textContent+=' Tracker not updated: '+error.message;
    }
  }
  if(event.target.closest('[data-load-financials]')){
    const status=document.getElementById('finStatus');
    status.textContent='Loading SEC financials...';status.classList.remove('update-error');
    try{
      const response=await fetch('/api/financials',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ticker:research.ticker,cik:research.cik||'',price:research.price})});
      const result=await response.json();
      if(!response.ok) throw new Error(result.error||'SEC financials request failed.');
      research.financials=result;
      if(!research.market_cap&&result.derived&&result.derived.market_cap)
        research.market_cap=fmtMoney(result.derived.market_cap)+' (SEC shares x quote)';
      saveResearch();renderResearch();
      document.getElementById('finStatus').textContent='Loaded from SEC XBRL.';
    }catch(error){status.textContent=error.message;status.classList.add('update-error');}
  }
});
"""


def _pulled_at(summary: dict) -> datetime:
    """When the scan's data was pulled; falls back to now for reports built without a scan time."""
    try:
        return datetime.fromisoformat(summary["pulled_at"])
    except (KeyError, TypeError, ValueError):
        return datetime.now().astimezone()


def build_live_report(
    candidates: list[ScanCandidate],
    warnings: list[str],
    summary: dict,
    updated_at: str,
) -> str:
    """Add an on-demand scan refresh and an Excel-shaped research workspace."""
    page = build_report(candidates, warnings, summary, include_analysis_links=True)
    pulled_at = _pulled_at(summary)
    leads = build_research_leads(candidates, pulled_at.date().isoformat())
    analysis = build_analysis_leads(leads)
    style = """
<style>
.tabs{display:flex;gap:8px;margin:18px 0}
.tabs button,.actions button{background:#1c2128;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:9px 13px;cursor:pointer}
.tabs button.active,.actions button.primary{background:#58a6ff;color:#0d1117;font-weight:700}
.actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:15px 0}
.actions button:disabled{opacity:.55;cursor:wait}.tab-content[hidden]{display:none}
.table-scroll{overflow:auto;max-height:68vh;border:1px solid #30363d;border-radius:8px}
.research-table{border-collapse:separate;border-spacing:0;min-width:2200px;width:max-content;font-size:.74rem}
.research-table th{position:sticky;top:0;background:#1c2128;color:#8b949e;text-align:left;z-index:1}
.research-table th,.research-table td{border-bottom:1px solid #30363d;border-right:1px solid #222831;padding:5px;vertical-align:top}
.research-table td input{width:150px;min-width:110px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:7px;font:inherit}
.research-table td input.long{width:300px}
.research-table td input.numeric{width:100px;min-width:78px;text-align:right}
.detail-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin:18px 0}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px 14px}
.detail-field{display:flex;flex-direction:column;gap:5px;color:#8b949e;font-size:.75rem}
.detail-field input,.detail-field textarea,.detail-field output{width:100%;min-height:36px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:8px;font:inherit}
.detail-field textarea{min-height:74px;resize:vertical}.detail-field output{display:block;color:#e6edf3}
.detail-heading{display:flex;justify-content:space-between;align-items:start;gap:12px}
.pulled-date{background:#1c2128;border:1px solid #d29922;border-radius:6px;padding:9px 13px;margin:12px 0;font-size:.85rem}.pulled-date strong{color:#d29922;font-size:1rem}
.research-table tr.stale{opacity:.55}.research-table tr.analysis-target{outline:2px solid #58a6ff;outline-offset:-2px}.analysis-link{color:#58a6ff;text-decoration:none}.analysis-link:hover{text-decoration:underline}.sheet-note{color:#8b949e;font-size:.78rem;line-height:1.55}.update-error{color:#f85149}
</style>"""
    page = page.replace("</head>", style + "</head>", 1)
    old_meta = re.search(r'<p class="muted">.*? · .*?</p>', page)
    if old_meta:
        page = page[:old_meta.start()] + (
            f'<p class="muted">{_safe(summary.get("source_name", "Research sources"))} · Last scan: '
            + _safe(updated_at) + "</p>"
        ) + page[old_meta.end():]

    selected_source = summary.get("source_mode", "public")
    source_options = "".join(
        f'<option value="{_safe(mode)}" {"selected" if mode == selected_source else ""}>'
        f'{_safe(label)}</option>'
        for mode, label in SOURCE_MODES.items()
    )
    controls = f"""
<p class="pulled-date">Data pulled: <strong>{_safe(pulled_at.strftime("%a %b %d, %Y %H:%M %Z").strip())}</strong> · days to close and annualized returns run from this date · deals at or above {TRACKER_MIN_ANNUALIZED:.0%} risk-adjusted annualized are added to <code>{_safe(TRACKER_FILENAME)}</code></p>
<div class="actions">
  <label for="sourceMode">Scan sources</label>
  <select id="sourceMode" aria-label="Scan sources">{source_options}</select>
  <button class="primary" id="refreshScan">Refresh selected sources</button>
  <button id="checkMarquee">Test GS Marquee connection</button>
  <button id="exportWorkbook">Export research workbook</button>
  <span id="refreshStatus" class="small" role="status">Last completed scan: {_safe(updated_at)} · manual refresh only</span>
  <span id="marqueeStatus" class="small" role="status">Marquee connection not tested</span>
</div>
<div class="tabs" role="tablist">
  <button class="active" type="button" data-tab="scanner-feed" role="tab">Live scanner feed</button>
  <button type="button" data-tab="deal-analysis" role="tab">Special situations &amp; EV analysis</button>
</div>
"""
    page = page.replace('<div class="toolbar">', controls + '<section class="tab-content" id="scanner-feed">\n<div class="toolbar">', 1)

    headers = "".join(
        f"<th>{_safe(label)}</th>" for _, label in SPECIAL_SITUATIONS_COLUMNS
    )
    analysis_headers = "".join(
        f"<th>{_safe(label)}</th>" for _, label in ANALYSIS_COLUMNS
    )
    analysis_section = f"""
</section>
<section class="tab-content" id="deal-analysis" hidden>
  <p class="sheet-note">This follows the columns in your attached workbook. Click a filtered name to open its focused detail with both spreadsheet sections. Prices are populated only when the selected source provides a usable quote; source and capture time are shown in Sources. SEC filings and news headlines are research leads, not verified deal terms or valuation. Analyst fields remain editable and save in this browser on this device.</p>
  <div id="selectedAnalysis" class="detail-card" hidden></div>
  <h2>Special Situations</h2>
  <div class="table-scroll"><table class="research-table"><thead><tr>{headers}</tr></thead><tbody id="specialRows"></tbody></table></div>
  <h2 style="margin-top:24px">Analysis — EV Ranked</h2>
  <p class="sheet-note">Enter upside and downside as signed percentage points (for example 30 and -20) and probability from 0 to 100. EV = p × upside + (1 − p) × downside. All valuation opinions are analyst inputs, not scan estimates.</p>
  <div class="table-scroll"><table class="research-table"><thead><tr>{analysis_headers}</tr></thead><tbody id="analysisRows"></tbody></table></div>
</section>
"""
    page = page.replace("</main><script>", analysis_section + "</main><script>", 1)
    script = f"""
const INITIAL_LEADS={_json_for_html(leads)};
const INITIAL_ANALYSIS={_json_for_html(analysis)};
const RESEARCH_COLUMNS={_json_for_html(SPECIAL_SITUATIONS_COLUMNS)};
const ANALYSIS_COLUMNS={_json_for_html(ANALYSIS_COLUMNS)};
const STORAGE_KEY='ibkr-special-situations-research-v1';
const SCAN_DATE={_json_for_html(pulled_at.date().isoformat())};
const TRACKER_MIN_ANNUALIZED={TRACKER_MIN_ANNUALIZED!r};
let researchState={{research:{{}},analysis:{{}}}};
let selectedAnalysisId='';
try {{
  const saved=localStorage.getItem(STORAGE_KEY);
  if(saved) {{
    const parsed=JSON.parse(saved);
    if(parsed&&typeof parsed==='object'&&parsed.research&&parsed.analysis) researchState=parsed;
  }}
}} catch(error) {{
  document.getElementById('refreshStatus').textContent='Could not read saved research notes: '+error.message;
}}
const currentIds=new Set(INITIAL_LEADS.map(row=>row.id));
INITIAL_LEADS.forEach(row=>{{researchState.research[row.id]={{...row,...researchState.research[row.id],price:row.price}}}});
INITIAL_ANALYSIS.forEach(row=>{{researchState.analysis[row.id]={{...row,...researchState.analysis[row.id]}}}});
function saveResearch() {{
  try {{ localStorage.setItem(STORAGE_KEY,JSON.stringify(researchState)); }}
  catch(error) {{ document.getElementById('refreshStatus').textContent='Could not save edits: '+error.message; }}
}}
function escapeCell(value) {{
  return String(value??'').replace(/[&<>"']/g,char=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[char]);
}}
function rankAnalysis() {{
  const rows=Object.values(researchState.analysis);
  const score=row=> {{
    const u=Number(row.upside),d=Number(row.downside),p=Number(row.probability);
    return row.upside!==''&&row.downside!==''&&row.probability!==''&&
      Number.isFinite(u)&&Number.isFinite(d)&&Number.isFinite(p)&&p>=0&&p<=100
      ? p/100*u+(1-p/100)*d : null;
  }};
  rows.sort((a,b)=> {{
    const ea=score(a),eb=score(b);
    if(ea===null) return eb===null?String(a.ticker).localeCompare(String(b.ticker)):1;
    if(eb===null) return -1;
    return eb-ea;
  }});
  rows.forEach((row,index)=>row.rank=score(row)===null?'':String(index+1));
  return rows;
}}
function renderTable(tbody,rows,columns,kind,readOnlyFields,numberFields) {{
  tbody.innerHTML=rows.map(row=>'<tr class="'+(currentIds.has(row.id)?'':'stale')+'" data-id="'+escapeCell(row.id)+'">'+
    columns.map(([field])=> {{
      if(readOnlyFields.includes(field)) return '<td data-field="'+escapeCell(field)+'">'+escapeCell(row[field]||'')+'</td>';
      const number=numberFields.includes(field);
      const value=escapeCell(row[field]||'');
      const cls=number?'numeric':(['company','deal_terms','key_dates_catalyst','bull','bear','risks','sources','why_mispriced','value_estimate','catalyst_date','annualized','sizing_note','counterparty','status_approvals','downside_if_broken','consideration_value','gross_spread','market_cap'].includes(field)?'long':'');
      const min=field==='probability'?' min="0" max="100"':'';
      return '<td><input class="'+cls+'" type="'+(number?'number':'text')+'" '+(number?'step="any"':'')+min+
        ' aria-label="'+escapeCell(columns.find(column=>column[0]===field)[1])+' for '+escapeCell(row.ticker)+'" data-kind="'+kind+
        '" data-field="'+escapeCell(field)+'" value="'+value+'"></td>';
    }}).join('')+'</tr>').join('');
}}
function renderDetailFields(columns,row,kind,numberFields,readOnlyFields) {{
  const longFields=['company','deal_terms','key_dates_catalyst','bull','bear','risks','sources','why_mispriced','value_estimate','catalyst_date','annualized','sizing_note','counterparty','status_approvals','downside_if_broken','consideration_value','gross_spread','market_cap'];
  return columns.map(([field,label])=> {{
    const value=escapeCell(row[field]??'');
    let control;
    if(readOnlyFields.includes(field)) {{
      control='<output data-field="'+escapeCell(field)+'">'+value+'</output>';
    }} else if(longFields.includes(field)) {{
      control='<textarea data-kind="'+kind+'" data-field="'+escapeCell(field)+'" aria-label="'+escapeCell(label)+' for '+escapeCell(row.ticker)+'">'+value+'</textarea>';
    }} else {{
      const number=numberFields.includes(field);
      control='<input type="'+(number?'number':'text')+'" '+(number?'step="any"':'')+
        (field==='probability'?' min="0" max="100"':'')+' data-kind="'+kind+'" data-field="'+escapeCell(field)+
        '" aria-label="'+escapeCell(label)+' for '+escapeCell(row.ticker)+'" value="'+value+'">';
    }}
    return '<label class="detail-field"><span>'+escapeCell(label)+'</span>'+control+'</label>';
  }}).join('');
}}
function renderSelectedAnalysis(id) {{
  const panel=document.getElementById('selectedAnalysis');
  const research=researchState.research[id],analysis=researchState.analysis[id];
  if(!research||!analysis) {{panel.hidden=true;return;}}
  panel.hidden=false;
  panel.innerHTML='<div class="detail-heading"><div><h2>'+escapeCell(research.ticker)+' — '+escapeCell(research.company||'Research detail')+
    '</h2><p class="sheet-note">Workbook-style detail for this filtered scanner lead. Scanner-derived headlines and quote details are identified in the fields; blank transaction and valuation fields require analyst research.</p></div>'+
    '<button type="button" data-close-analysis>Close detail</button></div>'+
    '<h3>Special Situations</h3><div class="detail-grid">'+renderDetailFields(RESEARCH_COLUMNS,research,'research',['price'],['date_pulled'])+'</div>'+
    '<h3 style="margin-top:22px">Analysis — EV Ranked</h3><div class="detail-grid">'+
    renderDetailFields(ANALYSIS_COLUMNS,analysis,'analysis',['days','upside','downside','probability'],['rank','ev'])+'</div>'+
    '<div id="dealTools"></div>';
  renderDealTools(id);
}}
function renderResearch() {{
  renderTable(document.getElementById('specialRows'),Object.values(researchState.research),RESEARCH_COLUMNS,'research',['date_pulled'],['price']);
  const rows=rankAnalysis();
  renderTable(document.getElementById('analysisRows'),rows,ANALYSIS_COLUMNS,'analysis',['rank','ev'],['days','upside','downside','probability']);
  if(selectedAnalysisId) renderSelectedAnalysis(selectedAnalysisId);
  updateExpectedValues();
}}
function updateExpectedValues() {{
  document.querySelectorAll('#analysisRows tr').forEach(tr=> {{
    const row=researchState.analysis[tr.dataset.id];
    const p=Number(row.probability),u=Number(row.upside),d=Number(row.downside);
    const ev=row.upside!==''&&row.downside!==''&&row.probability!==''&&Number.isFinite(p)&&p>=0&&p<=100
      ? (p/100*u+(1-p/100)*d).toFixed(1)+'%' : '';
    const cell=tr.querySelector('[data-field="ev"]');
    if(cell) cell.textContent=ev;
  }});
  const detailEv=document.querySelector('#selectedAnalysis [data-field="ev"]');
  const detailRow=researchState.analysis[selectedAnalysisId];
  if(detailEv&&detailRow) {{
    const p=Number(detailRow.probability),u=Number(detailRow.upside),d=Number(detailRow.downside);
    detailEv.textContent=detailRow.upside!==''&&detailRow.downside!==''&&detailRow.probability!==''&&Number.isFinite(p)&&p>=0&&p<=100
      ? (p/100*u+(1-p/100)*d).toFixed(1)+'%' : '';
  }}
}}
renderResearch();
document.querySelector('main').addEventListener('input',event=> {{
  const field=event.target.closest('[data-kind][data-field]');
  if(!field) return;
  const id=field.closest('tr')?.dataset.id||selectedAnalysisId;
  const row=researchState[field.dataset.kind][id];
  if(!row) return;
  row[field.dataset.field]=field.value;
  saveResearch();
  if(field.dataset.kind==='analysis'&&['upside','downside','probability'].includes(field.dataset.field)) updateExpectedValues();
}});
document.querySelector('main').addEventListener('change',event=> {{
  if(event.target.matches('[data-kind="analysis"][data-field="upside"],[data-kind="analysis"][data-field="downside"],[data-kind="analysis"][data-field="probability"]')) renderResearch();
}});
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=> {{
  document.querySelectorAll('[data-tab]').forEach(tab=>tab.classList.toggle('active',tab===button));
  document.querySelectorAll('.tab-content').forEach(section=>section.hidden=section.id!==button.dataset.tab);
}}));
document.getElementById('scanner-feed').addEventListener('click',event=> {{
  const link=event.target.closest('[data-analysis-id]');
  if(!link) return;
  event.preventDefault();
  document.querySelector('[data-tab="deal-analysis"]').click();
  selectedAnalysisId=link.dataset.analysisId;
  renderSelectedAnalysis(selectedAnalysisId);
  document.getElementById('selectedAnalysis').scrollIntoView({{behavior:'smooth',block:'start'}});
}});
document.getElementById('selectedAnalysis').addEventListener('click',event=> {{
  if(event.target.closest('[data-close-analysis]')) {{
    document.getElementById('selectedAnalysis').hidden=true;
    selectedAnalysisId='';
  }}
}});
document.getElementById('refreshScan').addEventListener('click',async()=> {{
  const button=document.getElementById('refreshScan'),status=document.getElementById('refreshStatus');
  const sourceMode=document.getElementById('sourceMode').value;
  button.disabled=true;status.textContent='Refreshing '+document.getElementById('sourceMode').selectedOptions[0].text+'…';
  try {{
    const response=await fetch('/api/refresh',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{source_mode:sourceMode}})}});
    const result=await response.json();
    if(!response.ok) throw new Error(result.error||'Scan refresh failed.');
    location.reload();
  }} catch(error) {{
    status.textContent='Refresh failed; previous scan is still displayed. '+error.message;
    status.classList.add('update-error');button.disabled=false;
  }}
}});
document.getElementById('checkMarquee').addEventListener('click',async()=> {{
  const button=document.getElementById('checkMarquee'),status=document.getElementById('marqueeStatus');
  button.disabled=true;status.textContent='Checking GS Marquee API…';status.classList.remove('update-error');
  try {{
    const response=await fetch('/api/gs-marquee-check',{{method:'POST'}});
    const result=await response.json();
    if(!response.ok) throw new Error(result.error||'Marquee connection check failed.');
    status.textContent=result.message;
  }} catch(error) {{
    status.textContent='Marquee connection check failed: '+error.message;
    status.classList.add('update-error');
  }} finally {{button.disabled=false;}}
}});
document.getElementById('exportWorkbook').addEventListener('click',async()=> {{
  const button=document.getElementById('exportWorkbook'),status=document.getElementById('refreshStatus');
  saveResearch();button.disabled=true;status.textContent='Preparing Excel workbook…';
  try {{
    const response=await fetch('/api/export',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{research:Object.values(researchState.research),analysis:Object.values(researchState.analysis)}})}});
    if(!response.ok) {{const result=await response.json();throw new Error(result.error||'Workbook export failed.');}}
    const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=url;link.download='special_situations_'+SCAN_DATE+'.xlsx';
    document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);
    status.textContent='Workbook exported with your saved research edits.';
  }} catch(error) {{status.textContent='Export failed: '+error.message;status.classList.add('update-error');}}
  finally {{button.disabled=false;}}
}});
"""
    page = page.replace("</script></body>", script + DEAL_TOOLS_SCRIPT + "</script></body>", 1)
    page = page.replace(
        "<h1>Global event-driven market scan</h1>",
        "<h1>Global event-driven market scan</h1>" + controls,
        1,
    )
    page = page.replace(controls, "", 1) if page.count(controls) > 1 else page
    return page


def build_report(
    candidates: list[ScanCandidate],
    warnings: list[str],
    summary: dict,
    include_analysis_links: bool = False,
) -> str:
    event_candidates = [
        candidate for candidate in candidates if candidate.headlines
    ]
    event_candidates.sort(key=event_relevance_score, reverse=True)

    card_html = []
    for candidate in event_candidates:
        contract = candidate.contract
        categories = sorted({
            category
            for item in candidate.headlines
            for category, _ in item["matches"]
        })
        headlines_html = []
        for article in candidate.headlines:
            matched = ", ".join(
                category for category, _ in article["matches"]
            )
            headline = _safe(article["headline"])
            if article.get("url", "").startswith("https://"):
                headline = (
                    f'<a href="{_safe(article["url"])}" target="_blank" '
                    f'rel="noopener noreferrer">{headline}</a>'
                )
            headlines_html.append(
                '<li><strong>{}</strong><br><span>{} · {} · {}</span></li>'.format(
                    headline,
                    _safe(article["time"]),
                    _safe(article["provider"] + " · " + article["article_id"]),
                    _safe(matched),
                )
            )

        matched_criteria = list(dict.fromkeys(
            f"{category}: {', '.join(phrases)}"
            for article in candidate.headlines
            for category, phrases in article["matches"]
        ))
        matched_criteria_html = (
            '<p class="matched-criteria"><strong>Matched criteria:</strong> '
            + _safe(" · ".join(matched_criteria))
            + "</p>"
        )
        metrics_html = (
            '<p class="small"><strong>Additional data:</strong> '
            + _safe(" · ".join(
                f"{name}: {value}"
                for name, value in sorted(candidate.metrics.items())
            ))
            + "</p>"
            if candidate.metrics else ""
        )
        signals = "".join(
            f'<span class="chip">{_safe(signal)}</span>'
            for signal in sorted(candidate.signals)
        )
        instrument = getattr(contract, "secType", "UNKNOWN")
        symbol = getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")
        market = getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", "")
        is_unverified_news = instrument == "NEWS"
        visible_ticker = "Ticker unverified" if is_unverified_news else symbol
        instrument_label = "News lead" if is_unverified_news else instrument
        company_label = (
            "Headline company (unverified)" if is_unverified_news else "Company"
        )
        lead_reference = f"Lead ref {symbol}" if is_unverified_news else ""
        metadata = " · ".join(filter(None, (
            instrument_label,
            market,
            getattr(contract, "currency", ""),
            f"conId {getattr(contract, 'conId', '')}",
            lead_reference,
        )))
        heading = (
            f'<a class="analysis-link" href="#deal-analysis" data-analysis-id="{_safe(getattr(contract, "conId", ""))}" '
            f'aria-label="Open research analysis for {_safe(visible_ticker)}">{_safe(visible_ticker)}</a>'
            if include_analysis_links else _safe(symbol)
        )
        if not include_analysis_links and is_unverified_news:
            heading = _safe(visible_ticker)
        criteria_search = " ".join(
            f"{category} {' '.join(phrases)}"
            for article in candidate.headlines
            for category, phrases in article["matches"]
        )
        card_html.append(
            '<article class="idea" data-categories="{}" data-instrument="{}" data-search="{}">'
            '<div class="top"><div><h2>{} <span>{}</span></h2>'
            '<p><strong>{}:</strong> {} · <strong>Ticker:</strong> {}</p>'
            '<p class="small">{}</p></div>'
            '<div class="score">{}/100 <small>event relevance</small></div></div>'
            '<p class="disclaimer">Headline keyword match; verify the full story and primary documents.</p>'
            '<div class="chips">{}</div>{}{}<details><summary>Matched headlines ({})</summary><ul>{}</ul>'
            '<p class="small">Use the linked source where available. IBKR supplies article IDs, not durable public URLs.</p>'
            '</details><details><summary>Market scanner signals ({})</summary><div class="chips">{}</div></details>'
            '</article>'.format(
                _safe("|".join(categories).lower()),
                _safe(instrument_label.lower()),
                _safe(" ".join([
                    symbol, market, instrument_label, candidate.description, criteria_search
                ]).lower()),
                heading,
                _safe(instrument_label),
                _safe(company_label),
                _safe(candidate.description or "Issuer not identified in headline"),
                _safe("Not verified" if is_unverified_news else symbol),
                _safe(metadata),
                event_relevance_score(candidate),
                "".join(f'<span class="chip">{_safe(category)}</span>' for category in categories),
                matched_criteria_html,
                metrics_html,
                len(candidate.headlines),
                "".join(headlines_html),
                len(candidate.signals),
                signals,
            )
        )

    warnings_html = "".join(f"<li>{_safe(warning)}</li>" for warning in warnings)
    providers = ", ".join(summary["news_provider_names"]) or "none available"
    source_mode = summary.get("source_mode", "ibkr")
    ibkr_selected = source_mode in ("ibkr", "all")
    truncated = ibkr_selected and summary["scans_planned"] < summary["scans_total"]
    coverage = (
        "IBKR market scanners return ranked, capped results, and IBKR news is linked to discovered contracts. "
        "This is not an exhaustive listing of securities or events."
        if source_mode == "ibkr" else
        "SEC EDGAR search covers recent filings, while public news search is not comprehensive. "
        "Issuer and security matches can require analyst verification; no source guarantees complete market coverage."
        if source_mode in ("sec", "news", "public") else
        "Each source has coverage limits: IBKR scanners are ranked and capped, EDGAR covers filings, "
        "and online news search is not comprehensive. Treat results as leads and verify issuer, security, and event."
    )
    ibkr_stats = ""
    if ibkr_selected:
        ibkr_stats = (
            f'<span class="stat">IBKR scanner combinations: '
            f'{summary.get("scans_succeeded", 0)}/{summary.get("scans_total", 0)}</span>'
            f'<span class="stat">IBKR configured scan limit: {summary.get("scans_planned", 0)}</span>'
            f'<span class="stat">IBKR contracts checked for news: '
            f'{summary.get("news_contracts_queried", 0)}</span>'
            f'<span class="stat">Usable IBKR market-data snapshots: '
            f'{summary.get("quotes_received", 0)}/{summary.get("quotes_queried", 0)}</span>'
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_pulled_at(summary).strftime("%Y-%m-%d")} · {_safe(summary.get("source_name", "Global Event-Driven Scanner"))}</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--blue:#58a6ff;--gold:#d29922;--green:#3fb950}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px 'Segoe UI',sans-serif}}
main{{max-width:1180px;margin:auto;padding:28px 20px}}h1{{font-size:1.55rem;margin:0 0 8px}}h2{{font-size:1rem;margin:0}}p{{line-height:1.5}}
.muted,.small,.idea p,.idea li span{{color:var(--muted)}}.banner,.toolbar,.idea{{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:16px;margin:14px 0}}
.banner{{border-color:var(--gold);color:var(--muted);font-size:.84rem}}.stats{{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}}
.stat,.chip{{display:inline-block;background:#1c2128;border:1px solid var(--line);border-radius:5px;padding:5px 8px;font-size:.73rem;color:var(--muted)}}.chip{{margin:3px}}
.toolbar{{display:flex;gap:10px;flex-wrap:wrap}}input,select{{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:5px;padding:9px}}
.top{{display:flex;justify-content:space-between;gap:12px}}.top h2 span{{color:var(--blue);font-size:.78rem}}.score{{text-align:right;color:var(--green);font-weight:700;font-size:1.1rem;white-space:nowrap}}.score small{{display:block;color:var(--muted);font-size:.67rem;font-weight:400}}
.disclaimer{{font-size:.76rem}}details{{margin-top:11px}}summary{{cursor:pointer;color:var(--blue)}}li{{margin:8px 0;line-height:1.5}}.small{{font-size:.72rem}}.warnings{{color:var(--gold)}}.empty{{color:var(--muted);padding:24px;text-align:center}}
</style></head>
<body><main>
<h1>Global event-driven market scan</h1>
<p class="muted">{_safe(summary.get("source_name", "Selected research sources"))} · Data pulled {_safe(_pulled_at(summary).strftime("%a %b %d, %Y %H:%M %Z").strip())}</p>
<div class="banner"><strong>Coverage limitations:</strong> {_safe(coverage)} A headline match is a research lead, not a verified catalyst or investment recommendation.</div>
<div class="stats">
<span class="stat">Sources: {_safe(", ".join(summary.get("sources_used", [])) or "none completed")}</span>
{ibkr_stats}
<span class="stat">Unique source leads found: {summary.get("contracts_found", 0)}</span>
<span class="stat">Headline providers: {_safe(providers)}</span>
<span class="stat">Event-pattern matches: {len(event_candidates)}</span>
</div>
<details><summary>Instrument types and market locations covered</summary>
<p class="small">Instrument types: {_safe(", ".join(summary["instruments"]) or "none")}<br>
Market locations: {_safe(", ".join(summary["locations"]) or "none")}</p></details>
{"<div class='banner'>IBKR_MAX_SCANS limited this run; increase it to scan additional catalog combinations.</div>" if truncated else ""}
{"<div class='banner warnings'><strong>Coverage / request warnings:</strong><ul>" + warnings_html + "</ul></div>" if warnings else ""}
<div class="toolbar"><input id="search" type="search" placeholder="Filter symbol, exchange, or issuer">
<select id="category"><option value="">All event patterns</option>
{''.join(f'<option value="{_safe(category.lower())}">{_safe(category)}</option>' for category in EVENT_PATTERNS)}
</select><select id="instrument"><option value="">All instrument types</option>
{''.join(f'<option value="{_safe(inst.lower())}">{_safe(inst)}</option>' for inst in sorted({getattr(c.contract, "secType", "UNKNOWN") for c in event_candidates}))}
</select></div>
<div id="results">{"".join(card_html)}<div class="empty" id="noResults" {"hidden" if card_html else ""}>No recent headline or filing matched the event-pattern rules for this source selection. This does not mean no opportunities exist.</div></div>
<p id="filterStatus" class="small" role="status">{f"{len(event_candidates)} matching names" if event_candidates else "0 matching names"}</p>
<p class="small">Relevance score is a heuristic based on matched event categories, matched headlines, and independent scanner signals. It does not include valuation, downside, probabilities, transaction terms, or expected return. Check filings, offering documents, deal terms, security seniority, liquidity, and complete source coverage before acting.</p>
</main><script>
const search=document.getElementById('search'), category=document.getElementById('category'), instrument=document.getElementById('instrument');
function filterCards(){{
  const q=search.value.toLowerCase();
  const cards=[...document.querySelectorAll('article.idea')];
  const matches=cards.filter(card=>{{
    const matchText=card.dataset.search.includes(q);
    const matchCategory=!category.value||card.dataset.categories.includes(category.value);
    const matchType=!instrument.value||card.dataset.instrument===instrument.value;
    return matchText&&matchCategory&&matchType;
  }});
  cards.forEach(card=>{{card.hidden=!matches.includes(card)}});
  document.getElementById('noResults').hidden=matches.length>0;
  document.getElementById('noResults').textContent=cards.length
    ? 'No names match the selected filters.'
    : 'No recent headline or filing matched the event-pattern rules for this source selection. This does not mean no opportunities exist.';
  document.getElementById('filterStatus').textContent=matches.length+' matching '+(matches.length===1?'name':'names');
}}
search.addEventListener('input',filterCards);
[category,instrument].forEach(control=>control.addEventListener('change',filterCards));
</script></body></html>"""


def _contract_fields(contract) -> list:
    symbol = getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")
    if getattr(contract, "secType", "") == "NEWS":
        symbol = "Unverified"
    return [
        symbol,
        getattr(contract, "secType", "UNKNOWN"),
        getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", ""),
        getattr(contract, "currency", ""),
        getattr(contract, "conId", ""),
    ]


def _write_sheet(sheet, headers: list[str], rows: list[list], widths: list[int]) -> None:
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            # Keep headlines that start with "=" from being stored as formulas.
            if isinstance(cell.value, str):
                cell.data_type = "s"
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    if rows:
        sheet.auto_filter.ref = sheet.dimensions


def _clean_rows(rows, columns, fallback_rows) -> list[dict]:
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return fallback_rows
    valid_fields = {key for key, _ in columns}
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cleaned.append({
            key: str(row.get(key, ""))[:4000] if key in valid_fields else ""
            for key, _ in columns
        })
    return cleaned or fallback_rows


def _optional_number(value):
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def build_excel_report(
    candidates: list[ScanCandidate],
    warnings: list[str],
    summary: dict,
    path,
    research_rows: list[dict] | None = None,
    analysis_rows: list[dict] | None = None,
) -> None:
    """Write Excel-matched research tabs, event matches, scanner contracts, and run summary."""
    workbook = Workbook()

    event_candidates = sorted(
        (candidate for candidate in candidates if candidate.headlines),
        key=event_relevance_score,
        reverse=True,
    )
    match_rows = []
    for candidate in event_candidates:
        symbol, instrument, market, currency, con_id = _contract_fields(candidate.contract)
        for article in candidate.headlines:
            match_rows.append([
                event_relevance_score(candidate),
                symbol,
                candidate.description,
                instrument,
                market,
                currency,
                con_id,
                "; ".join(category for category, _ in article["matches"]),
                article["headline"],
                "; ".join(phrase for _, phrases in article["matches"] for phrase in phrases),
                article["time"],
                article["provider"],
                article["article_id"],
                len(candidate.signals),
                "; ".join(
                    f"{name}={value}" for name, value in sorted(candidate.metrics.items())
                ),
            ])
    _write_sheet(
        workbook.active,
        ["Event score", "Symbol", "Name", "Type", "Exchange", "Currency", "conId",
         "Event categories", "Headline", "Matched phrases", "Headline time",
         "Provider", "Article ID", "Scanner signals", "Vendor metrics"],
        match_rows,
        [11, 12, 34, 8, 12, 9, 12, 30, 80, 28, 26, 10, 22, 14, 50],
    )
    workbook.active.title = "Event Matches"

    contract_rows = [
        [
            *_contract_fields(candidate.contract),
            candidate.description,
            len(candidate.signals),
            "; ".join(sorted(candidate.signals)),
            len(candidate.headlines),
            event_relevance_score(candidate) if candidate.headlines else None,
        ]
        for candidate in sorted(candidates, key=lambda c: len(c.signals), reverse=True)
    ]
    _write_sheet(
        workbook.create_sheet("Scanner Contracts"),
        ["Symbol", "Type", "Exchange", "Currency", "conId", "Name",
         "Signal count", "Scanner signals", "Event headlines", "Event score"],
        contract_rows,
        [12, 8, 12, 9, 12, 34, 12, 70, 15, 11],
    )

    summary_rows = [
        ["Data pulled", _pulled_at(summary).strftime("%Y-%m-%d %H:%M %Z")],
        ["Workbook written", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")],
        ["Selected source mode", summary.get("source_name", summary.get("source_mode", "IBKR Gateway"))],
        ["Sources used", ", ".join(summary.get("sources_used", [])) or "none completed"],
        ["Gateway", f"{summary['host']}:{summary['port']}"],
        ["Scanner combinations run", f"{summary['scans_succeeded']}/{summary['scans_total']}"],
        ["Configured scan limit", summary["scans_planned"]],
        ["Unique contracts found", summary["contracts_found"]],
        ["Contracts checked for news", summary["news_contracts_queried"]],
        ["Usable market-data snapshots", f"{summary.get('quotes_received', 0)}/{summary.get('quotes_queried', 0)}"],
        ["Headline providers", ", ".join(summary["news_provider_names"]) or "none available"],
        ["Contracts with event matches", len(event_candidates)],
        ["Instrument types", ", ".join(summary["instruments"]) or "none"],
        ["Market locations", ", ".join(summary["locations"]) or "none"],
        [],
        ["Warnings", ""],
        *[["", warning] for warning in warnings],
        [],
        ["Note", "Headline keyword matches are research leads, not verified catalysts. "
                 "Verify full stories and primary documents before acting."],
    ]
    _write_sheet(workbook.create_sheet("Run Summary"), ["Item", "Value"], summary_rows, [30, 120])

    default_research = build_research_leads(candidates, _pulled_at(summary).date().isoformat())
    special_rows = _clean_rows(research_rows, SPECIAL_SITUATIONS_COLUMNS, default_research)
    special_values = [
        [
            _optional_number(row.get(key)) if key == "price"
            else row.get(key, "")
            for key, _ in SPECIAL_SITUATIONS_COLUMNS
        ]
        for row in special_rows
    ]
    special_sheet = workbook.create_sheet("Special Situations")
    _write_sheet(
        special_sheet,
        [label for _, label in SPECIAL_SITUATIONS_COLUMNS],
        special_values,
        [22, 14, 30, 28, 22, 38, 13, 20, 15, 20, 55, 45, 50, 50, 55, 32, 16, 45, 13],
    )

    default_analysis = build_analysis_leads(special_rows)
    research_analysis = _clean_rows(analysis_rows, ANALYSIS_COLUMNS, default_analysis)
    ranked_analysis = sorted(
        research_analysis,
        key=lambda row: (
            _optional_number(row.get("upside")) is None
            or _optional_number(row.get("downside")) is None
            or _optional_number(row.get("probability")) is None,
            -(
                (_optional_number(row.get("probability")) or 0) / 100
                * (_optional_number(row.get("upside")) or 0)
                + (1 - (_optional_number(row.get("probability")) or 0) / 100)
                * (_optional_number(row.get("downside")) or 0)
            ),
            row.get("ticker", ""),
        ),
    )
    analysis_values = []
    for rank, row in enumerate(ranked_analysis, 1):
        analysis_values.append([
            rank if all(_optional_number(row.get(key)) is not None for key in ("upside", "downside", "probability")) else "",
            row.get("ticker", ""),
            row.get("situation_type", ""),
            row.get("why_mispriced", ""),
            row.get("value_estimate", ""),
            row.get("catalyst_date", ""),
            _optional_number(row.get("days")),
            _optional_number(row.get("upside")),
            _optional_number(row.get("downside")),
            _optional_number(row.get("probability")),
            None,
            row.get("annualized", ""),
            row.get("sizing_note", ""),
        ])
    analysis_sheet = workbook.create_sheet("Analysis - EV Ranked")
    _write_sheet(
        analysis_sheet,
        [label for _, label in ANALYSIS_COLUMNS],
        analysis_values,
        [10, 14, 28, 48, 36, 44, 10, 17, 26, 19, 24, 22, 42],
    )
    for row_number in range(2, len(analysis_values) + 2):
        cell = analysis_sheet.cell(row=row_number, column=11)
        cell.value = (
            f'=IF(OR(H{row_number}="",I{row_number}="",J{row_number}=""),"",'
            f'H{row_number}*J{row_number}/100+I{row_number}*(1-J{row_number}/100))'
        )
        cell.data_type = "f"
        cell.number_format = '0.0"%"'
    for row_number in range(2, len(special_values) + 2):
        price_cell = special_sheet.cell(row=row_number, column=7)
        if price_cell.value is not None:
            price_cell.number_format = "0.00"

    screened = summary.get("screened_out") or []
    if screened:
        _write_sheet(
            workbook.create_sheet("Screened Out"),
            ["Ticker", "Company", "Reason screened out", "First headline", "Source", "Link"],
            [[row["ticker"], row["company"], row["reason"], row["headline"], row["source"], row.get("url", "")]
             for row in screened],
            [14, 32, 60, 70, 26, 50],
        )

    workbook.save(path)


def tracker_path() -> Path:
    return _report_directory() / TRACKER_FILENAME


def record_tracked_deal(deal: dict, path=None, today: str = "") -> dict:
    """Add or update a deal in the tracking workbook when its risk-adjusted annualized return
    (probability-weighted, see standards.py) clears the hurdle.

    One row per lead ID. A deal already tracked that falls below the hurdle, or no longer meets
    the standards (meets_standards False), stays in the workbook with status "Below threshold";
    a new deal below the hurdle is not added. The threshold is applied here, not by the dashboard.
    """
    path = Path(path or tracker_path())
    today = today or datetime.now().astimezone().date().isoformat()
    lead_id = str(deal.get("id", "")).strip()
    if not lead_id:
        raise ValueError("Tracked deal needs a lead ID.")
    risk_adjusted = _optional_number(deal.get("risk_adjusted_annualized"))
    clears = (risk_adjusted is not None and risk_adjusted >= TRACKER_MIN_ANNUALIZED
              and deal.get("meets_standards", True) is not False)

    if path.exists():
        workbook = load_workbook(path)
        sheet = workbook.active
    else:
        if not clears:
            return {"tracked": False, "status": "Below threshold", "path": str(path)}
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Tracker"
        _write_sheet(sheet, [label for _, label in TRACKER_COLUMNS], [], TRACKER_WIDTHS)

    existing_row = next(
        (row[0].row for row in sheet.iter_rows(min_row=2, max_col=1) if str(row[0].value) == lead_id),
        None,
    )
    if existing_row is None and not clears:
        return {"tracked": False, "status": "Below threshold", "path": str(path)}

    for column, (_, label) in enumerate(TRACKER_COLUMNS, 1):   # trackers made before the new columns
        if sheet.cell(row=1, column=column).value != label:
            sheet.cell(row=1, column=column, value=label).font = copy(sheet.cell(row=1, column=1).font)
            sheet.column_dimensions[sheet.cell(row=1, column=column).column_letter].width = TRACKER_WIDTHS[column - 1]
    numbers = {"price", "consideration_value", "gross_spread", "days", "annualized", "probability",
               "break_price", "risk_adjusted_annualized"}
    values = {key: (_optional_number(deal.get(key)) if key in numbers else str(deal.get(key, ""))[:4000])
              for key, _ in TRACKER_COLUMNS}
    values.update({
        "id": lead_id,
        "last_updated": today,
        "status": "Active" if clears else f"Below threshold since {today}",
    })
    row_number = existing_row or sheet.max_row + 1
    if existing_row:
        values["date_added"] = sheet.cell(row=row_number, column=2).value or today
    else:
        values["date_added"] = today
    for column, (key, _) in enumerate(TRACKER_COLUMNS, 1):
        cell = sheet.cell(row=row_number, column=column, value=values[key])
        if isinstance(values[key], str):
            cell.data_type = "s"   # keep text like "=..." from becoming a formula
        if key in ("gross_spread", "annualized", "risk_adjusted_annualized") and values[key] is not None:
            cell.number_format = "0.0%"
        elif key == "probability" and values[key] is not None:
            cell.number_format = "0%"
        elif key in ("price", "consideration_value", "break_price") and values[key] is not None:
            cell.number_format = "0.00"
    sheet.auto_filter.ref = sheet.dimensions
    try:
        workbook.save(path)
    except PermissionError as error:
        raise RuntimeError(f"Close {path.name} in Excel so the tracker can be updated.") from error
    return {"tracked": clears, "status": values["status"], "path": str(path), "updated": bool(existing_row)}


def _scan_from_ibkr():
    """Run a single read-only IBKR scan using the current environment settings."""
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    client_id = int(os.environ.get("IBKR_CLIENT_ID", "37"))
    max_scans = int(os.environ.get("IBKR_MAX_SCANS", "0"))
    max_news_contracts = int(os.environ.get("IBKR_MAX_NEWS_CONTRACTS", "150"))
    news_days = int(os.environ.get("IBKR_NEWS_DAYS", "7"))
    news_results = int(os.environ.get("IBKR_NEWS_RESULTS_PER_CONTRACT", "10"))
    if max_scans < 0 or max_news_contracts < 0 or news_days < 1 or news_results < 1:
        raise ValueError("IBKR scan limits must be non-negative and news limits must be positive.")

    print(f"Connecting read-only to IBKR Gateway at {host}:{port}…")
    try:
        candidates, warnings, summary = asyncio.run(
            _scan_ibkr(
                host,
                port,
                client_id,
                max_scans,
                max_news_contracts,
                news_days,
                news_results,
            )
        )
    except Exception as error:
        raise RuntimeError(
            f"IBKR scan failed: {error}. Confirm Gateway is running, API socket clients are enabled, "
            f"and IBKR_HOST/IBKR_PORT are correct (Gateway defaults: paper 4002, live 4001)."
        ) from error
    return candidates, warnings, summary


def merge_duplicate_candidates(candidates: list[ScanCandidate]) -> list[ScanCandidate]:
    """Combine leads for the same instrument found by several sources (e.g. an SEC filer in both
    full-text search and the daily form index), keeping every headline and signal."""
    merged: dict[tuple, ScanCandidate] = {}
    for candidate in candidates:
        key = (getattr(candidate.contract, "secType", ""), getattr(candidate.contract, "conId", None))
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        known = {(item.get("provider"), item.get("article_id")) for item in existing.headlines}
        existing.headlines.extend(
            item for item in candidate.headlines if (item.get("provider"), item.get("article_id")) not in known
        )
        existing.signals |= candidate.signals
        existing.metrics = {**candidate.metrics, **existing.metrics}
        existing.research = {**candidate.research, **existing.research}
        if existing.price is None and candidate.price is not None:
            existing.price, existing.quote_time = candidate.price, candidate.quote_time
    return list(merged.values())


_SEC_PROFILES: standards.SecProfiles | None = None


def _sec_profiles() -> standards.SecProfiles:
    global _SEC_PROFILES
    if _SEC_PROFILES is None:
        _SEC_PROFILES = standards.SecProfiles(os.environ.get(
            "SEC_USER_AGENT", "Value-Investing-Terminal research scanner (set SEC_USER_AGENT with contact details)"))
    return _SEC_PROFILES


def _candidate_market(candidate: ScanCandidate) -> tuple[bool, str]:
    """MSCI developed-market test: SEC domicile for filers, the ISIN or listing suffix otherwise."""
    contract = candidate.contract
    symbol = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", ""))
    if getattr(contract, "secType", "") == "FILING":
        cik = candidate.research.get("cik") or str(getattr(contract, "conId", ""))
        return standards.sec_market_status(_sec_profiles().get(cik))
    if getattr(contract, "exchange", "") == "UK Takeover Panel":
        domicile = standards.listing_country(symbol) if standards.ISIN_RE.match(symbol.upper()) else ""
        return standards.market_status(domicile, "United Kingdom")
    return standards.market_status("", standards.listing_country(symbol))


def apply_standards(candidates: list[ScanCandidate]) -> tuple[list[ScanCandidate], list[dict]]:
    """Keep event leads that are primary-source hard catalysts in MSCI developed markets.

    Returns the kept candidates and one row per screened-out lead with its reason, so nothing is
    dropped silently. Scanner contracts with no event headline are left alone.
    """
    kept, screened = [], []
    for candidate in candidates:
        if not candidate.headlines:
            kept.append(candidate)
            continue
        kind, reason = standards.screen_candidate(candidate)
        if kind:
            in_universe, basis = _candidate_market(candidate)
            if in_universe:
                candidate.research.setdefault("situation_type", kind)
                candidate.research.update({
                    "tier": f"Hard catalyst · {kind}",
                    "risks": (f"{basis}. Price it in the deal calculator (terms, probability, break price): it counts "
                              f"against the {standards.HURDLE:.0%} risk-adjusted hurdle only then."),
                })
                kept.append(candidate)
                continue
            reason = f"Outside universe: {basis}"
        first = candidate.headlines[0]
        contract = candidate.contract
        screened.append({
            "ticker": str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")),
            "company": candidate.description,
            "reason": reason,
            "headline": first.get("headline", ""),
            "source": first.get("provider", ""),
            "url": first.get("url", ""),
        })
    return kept, screened


def _standards_warning(kept: int, screened: list[dict]) -> str:
    reasons = Counter(row["reason"].split(":")[0] for row in screened).most_common(4)
    return (f"Standards kept {kept} of {kept + len(screened)} event leads (primary-source hard catalysts in MSCI "
            f"developed markets). Screened out {len(screened)}, mainly: "
            + "; ".join(f"{reason} ({count})" for reason, count in reasons)
            + ". Each one and its reason is on the Screened Out tab of the exported workbook.")


def _scan_selected_sources(source_mode: str) -> tuple[list[ScanCandidate], list[str], dict]:
    if source_mode not in SOURCE_MODES:
        raise ValueError(f"Unknown scan source selection: {source_mode}")

    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    candidates = []
    warnings = []
    summary = {
        "host": host,
        "port": port,
        "source_mode": source_mode,
        "source_name": SOURCE_MODES[source_mode],
        "sources_used": [],
        "scans_planned": 0,
        "scans_total": 0,
        "scans_succeeded": 0,
        "contracts_found": 0,
        "news_contracts_queried": 0,
        "quotes_queried": 0,
        "quotes_received": 0,
        "news_provider_names": [],
        "instruments": [],
        "locations": [],
    }

    if source_mode in ("ibkr", "all"):
        try:
            ibkr_candidates, ibkr_warnings, ibkr_summary = _scan_from_ibkr()
            candidates.extend(ibkr_candidates)
            warnings.extend(ibkr_warnings)
            summary.update(ibkr_summary)
            summary["source_mode"] = source_mode
            summary["source_name"] = SOURCE_MODES[source_mode]
            summary["sources_used"].append("IBKR Gateway")
        except Exception as error:
            if source_mode == "ibkr":
                raise
            warnings.append(f"IBKR source unavailable; continuing with public sources: {error}")

    if source_mode in ("sec", "public", "all"):
        sec_candidates, sec_warnings, sec_summary = scan_sec_edgar()
        candidates.extend(sec_candidates)
        warnings.extend(sec_warnings)
        summary["sources_used"].extend(sec_summary["sources_used"])
        summary["news_provider_names"].extend(sec_summary["news_provider_names"])

    if source_mode in ("sec", "global", "public", "all"):
        import global_event_sources   # imports this module, so it is loaded on first use

        sources = global_event_sources.GLOBAL_SOURCES
        if source_mode == "sec":
            sources = (global_event_sources.scan_edgar_deal_forms,)
        for scan_source in sources:
            try:
                found, source_warnings, source_summary = scan_source()
            except Exception as error:   # one failing regulator feed must not sink the scan
                warnings.append(f"{scan_source.__name__} failed: {error}")
                continue
            candidates.extend(found)
            warnings.extend(source_warnings)
            summary["sources_used"].extend(source_summary["sources_used"])
            summary["news_provider_names"].extend(source_summary["news_provider_names"])

    if source_mode in ("news", "public", "all"):
        news_candidates, news_warnings, news_summary = scan_online_news()
        candidates.extend(news_candidates)
        warnings.extend(news_warnings)
        summary["sources_used"].extend(news_summary["sources_used"])
        summary["news_provider_names"].extend(news_summary["news_provider_names"])

    if source_mode in ("factiva", "all"):
        factiva_candidates, factiva_warnings, factiva_summary = scan_factiva()
        candidates.extend(factiva_candidates)
        warnings.extend(factiva_warnings)
        summary["sources_used"].extend(factiva_summary["sources_used"])
        summary["news_provider_names"].extend(factiva_summary["news_provider_names"])

    if source_mode in ("news", "public", "all"):
        candidates, vendor_warnings, vendor_providers = scan_vendor_research(candidates)
        warnings.extend(vendor_warnings)
        summary["sources_used"].extend(vendor_providers)
        summary["news_provider_names"].extend(vendor_providers)

    candidates = merge_duplicate_candidates(candidates)
    candidates, screened = apply_standards(candidates)
    summary["screened_out"] = screened
    if screened:
        warnings.insert(0, _standards_warning(sum(1 for c in candidates if c.headlines), screened))
    summary["pulled_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    summary["sources_used"] = list(dict.fromkeys(summary["sources_used"]))
    summary["news_provider_names"] = list(dict.fromkeys(summary["news_provider_names"]))
    summary["contracts_found"] = len(candidates)
    if not summary["sources_used"] and not warnings:
        warnings.append("None of the selected research sources returned results.")
    return candidates, warnings, summary


def _report_directory() -> Path:
    output_dir = Path(
        os.environ.get("IBKR_REPORT_DIR", Path(__file__).resolve().parent / "reports")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _write_scan_reports(candidates, warnings, summary, updated_at: str) -> Path:
    output_dir = _report_directory()
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    workbook_path = output_dir / f"event_ideas_global_{suffix}.xlsx"
    build_excel_report(candidates, warnings, summary, workbook_path)
    (output_dir / f"event_ideas_global_{suffix}.html").write_text(
        build_report(candidates, warnings, summary), encoding="utf-8"
    )
    return workbook_path


def run_market_scan() -> Path:
    """Connect to local IBKR read-only and write Excel and HTML scan reports."""
    print(f"Connecting read-only to IBKR Gateway at {os.environ.get('IBKR_HOST', '127.0.0.1')}:{os.environ.get('IBKR_PORT', '4002')}…")
    candidates, warnings, summary = _scan_from_ibkr()
    workbook_path = _write_scan_reports(
        candidates, warnings, summary, datetime.now().astimezone().isoformat()
    )
    print(
        f"Scanned {summary['scans_succeeded']}/{summary['scans_total']} combinations; "
        f"found {summary['contracts_found']} contracts and "
        f"{sum(bool(candidate.headlines) for candidate in candidates)} event matches."
    )
    if warnings:
        for warning in warnings:
            print(f"  ⚠ {warning}")
    return workbook_path


def create_live_dashboard_server(state: dict, refresh_scan, host="127.0.0.1", port=0):
    """Create the local report server; refresh_scan replaces the latest snapshot."""
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_json(self, status_code: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            body = build_live_report(
                state["candidates"],
                state["warnings"],
                state["summary"],
                state["updated_at"],
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/api/gs-marquee-check":
                try:
                    self._send_json(200, check_gs_marquee_connection())
                except ValueError as error:
                    self._send_json(503, {"error": str(error)})
                except RuntimeError as error:
                    self._send_json(502, {"error": str(error)})
                return

            if self.path == "/api/refresh":
                if not state["refresh_lock"].acquire(blocking=False):
                    self._send_json(409, {"error": "A scan is already running."})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 10_000:
                        self._send_json(413, {"error": "Refresh request is too large."})
                        return
                    payload = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(payload, dict):
                        raise ValueError("Refresh request must be a JSON object.")
                    source_mode = payload.get("source_mode", state.get("source_mode", "public"))
                    if source_mode not in SOURCE_MODES:
                        raise ValueError(f"Unknown scan source selection: {source_mode}")
                    state["source_mode"] = source_mode
                    self._send_json(200, {"updated_at": refresh_scan(source_mode)})
                except (ValueError, json.JSONDecodeError) as error:
                    self._send_json(400, {"error": str(error)})
                except Exception as error:
                    self._send_json(500, {"error": str(error)})
                finally:
                    state["refresh_lock"].release()
                return

            if self.path == "/api/track":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 100_000:
                        self._send_json(413, {"error": "Tracked deal must be a JSON object under 100 KB."})
                        return
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("Tracked deal must be a JSON object.")
                    with state.setdefault("tracker_lock", threading.Lock()):
                        self._send_json(200, record_tracked_deal(payload, state.get("tracker_path")))
                except (ValueError, json.JSONDecodeError) as error:
                    self._send_json(400, {"error": str(error)})
                except RuntimeError as error:
                    self._send_json(409, {"error": str(error)})
                return

            if self.path == "/api/financials":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 10_000:
                        self._send_json(413, {"error": "Financials request must be a small JSON object."})
                        return
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("Financials request must be a JSON object.")
                    import global_event_sources

                    price = _optional_number(payload.get("price"))
                    self._send_json(200, global_event_sources.sec_financial_snapshot(
                        ticker=str(payload.get("ticker", "")).strip(),
                        cik=str(payload.get("cik", "")).strip(),
                        price=price if price and price > 0 else None,
                    ))
                except (ValueError, json.JSONDecodeError) as error:
                    self._send_json(400, {"error": str(error)})
                except requests.RequestException as error:
                    self._send_json(502, {"error": f"SEC financials request failed: {error}"})
                return

            if self.path == "/api/export":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 2_000_000:
                        self._send_json(413, {"error": "Workbook research data must be under 2 MB."})
                        return
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("Workbook research data must be a JSON object.")
                    workbook = BytesIO()
                    build_excel_report(
                        state["candidates"],
                        state["warnings"],
                        state["summary"],
                        workbook,
                        payload.get("research"),
                        payload.get("analysis"),
                    )
                    body = workbook.getvalue()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                    self.send_header(
                        "Content-Disposition",
                        'attachment; filename="special_situations_latest.xlsx"',
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except (ValueError, json.JSONDecodeError) as error:
                    self._send_json(400, {"error": str(error)})
                except Exception as error:
                    self._send_json(500, {"error": f"Workbook export failed: {error}"})
                return
            self.send_error(404)

        def log_message(self, format_string: str, *args):
            print(f"[dashboard] {self.address_string()} - {format_string % args}")

    try:
        server = ThreadingHTTPServer((host, port), DashboardHandler)
    except OSError as error:
        raise RuntimeError(
            f"Could not start the local dashboard at http://{host}:{port}: {error}. "
            "Set IBKR_DASHBOARD_PORT to another available local port."
        ) from error
    server.daemon_threads = True
    return server


def run_live_dashboard(open_browser: bool = True) -> None:
    """Serve the dashboard even when IBKR is unavailable for the initial scan."""
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    default_source_mode = os.environ.get("IBKR_SCAN_SOURCE", "all")
    if default_source_mode not in SOURCE_MODES:
        raise ValueError(
            f"IBKR_SCAN_SOURCE must be one of: {', '.join(SOURCE_MODES)}"
        )
    state = {
        "candidates": [],
        "warnings": [],
        "summary": {
            "host": host,
            "port": port,
            "scans_planned": 0,
            "scans_total": 0,
            "scans_succeeded": 0,
            "contracts_found": 0,
            "news_contracts_queried": 0,
            "quotes_queried": 0,
            "quotes_received": 0,
            "source_mode": default_source_mode,
            "source_name": SOURCE_MODES[default_source_mode],
            "sources_used": [],
            "news_provider_names": [],
            "instruments": [],
            "locations": [],
        },
        "updated_at": f"Initial {SOURCE_MODES[default_source_mode]} scan is starting…",
        "source_mode": default_source_mode,
        "refresh_lock": threading.Lock(),
    }

    def refresh_scan(source_mode=default_source_mode):
        candidates, warnings, summary = _scan_selected_sources(source_mode)
        updated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        _write_scan_reports(candidates, warnings, summary, updated_at)
        state["candidates"] = candidates
        state["warnings"] = warnings
        state["summary"] = summary
        state["updated_at"] = updated_at
        state["source_mode"] = source_mode
        return updated_at

    def run_initial_scan():
        if not state["refresh_lock"].acquire(blocking=False):
            return
        try:
            refresh_scan()
        except Exception as error:
            warning = f"Initial {SOURCE_MODES[state['source_mode']]} scan failed: {error}"
            state["warnings"] = [warning]
            state["updated_at"] = (
                "Initial scan failed · "
                + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            )
            print(f"  Warning: {warning}")
        finally:
            state["refresh_lock"].release()

    dashboard_host = "127.0.0.1"
    dashboard_port = int(os.environ.get("IBKR_DASHBOARD_PORT", "8765"))
    server = create_live_dashboard_server(state, refresh_scan, dashboard_host, dashboard_port)
    url = f"http://{dashboard_host}:{server.server_port}/"
    print(f"Live research dashboard: {url}")
    print("Refresh is manual; close this terminal to stop the local dashboard.")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    initial_scan = threading.Thread(
        target=run_initial_scan,
        name="ibkr-initial-scan",
        daemon=True,
    )
    initial_scan.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    # Scheduled runs redirect output to a log file, where Windows' default encoding can't print "⚠".
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"Report written to {run_market_scan()}")
