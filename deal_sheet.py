"""Build a finished special-situations deal sheet from SEC filings and live prices.

Finds pending US-listed mergers and tender offers in EDGAR, reads the deal terms from the
filings (cash per share, exchange ratio, CVRs, expected close or tender expiry), prices the
target and acquirer, and writes a workbook in the layout of the analyst sheet:
"Special Situations <date>" (tiered) and "Analysis - EV Ranked". Deals at or above the
tracker hurdle (25% annualized by default) are added to special_situations_tracker.xlsx.

Everything here is extracted by pattern from filing text, so each row links its source filing.
Probabilities are a labeled default for the analyst to edit; EV recalculates in Excel.

Usage: py deal_sheet.py [--days 270] [--open]
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from global_event_sources import _sec_user_agent, deal_math
from ibkr_market_scanner import _load_sec_tickers, _report_directory, record_tracked_deal, TRACKER_MIN_ANNUALIZED

EFTS = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

# Full-text searches that surface pending deals, run one form at a time: EDGAR returns fewer hits
# for a multi-form filter than the forms do separately (136 vs 506 for "per share in cash").
DEAL_QUERIES = (
    ('"per share in cash"', ("8-K", "SC TO-T", "SC 14D9", "DEFM14A", "PREM14A", "425")),
    ('"converted into the right to receive"', ("8-K", "DEFM14A", "PREM14A", "425")),
    ('"exchange ratio" "merger"', ("8-K", "425", "DEFM14A", "PREM14A")),
)
# Forms whose filer (or first-listed subject) is the target company.
TARGET_FORMS = {"DEFM14A", "PREM14A", "SC 14D9", "SC 14D9/A", "SC TO-T", "SC TO-T/A"}
# Filed after the announcement, these mean the deal closed or the shares were delisted.
CLOSED_FORMS = {"25-NSE", "25", "15-12B", "15-12G", "15-15D"}
DEFAULT_PROBABILITY = 0.85
DEFAULT_MERGER_DAYS = 180
DEFAULT_TENDER_DAYS = 30
# A real deal this wide is rare (financing or regulatory doubt); a misread term is more likely. Those
# rows go to their own tier so they are checked against the source before anyone trusts them.
VERIFY_SPREAD = 0.40

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
CASH_RE = re.compile(
    r"\$\s?(\d{1,4}(?:\.\d{1,4})?)\s+(?:per\s+share\s+)?in\s+cash"
    r"|\$\s?(\d{1,4}(?:\.\d{1,4})?)\s+per\s+(?:share|Share)[^.;]{0,60}?in\s+cash",
    re.I,
)
RATIO_RE = re.compile(
    r"(\d{1,3}\.\d{2,6})\s+(?:of\s+a\s+)?(?:validly\s+issued[^.;]{0,60}?)?"
    r"(?:shares?|ordinary\s+shares?|common\s+shares?|American\s+Depositary\s+Shares?)\s+of\s+(?:the\s+)?"
    r"([A-Z][A-Za-z0-9&.'\- ]{1,50}?)(?:'s|’s)?\s+(?:Class\s+[A-Z]\s+)?(?:common|ordinary|stock)",
)
EXCHANGE_RATIO_RE = re.compile(r"exchange\s+ratio\s+(?:of\s+|equal\s+to\s+)?(\d{1,3}\.\d{2,6})", re.I)
CVR_RE = re.compile(r"contingent\s+value\s+rights?", re.I)
CVR_AMOUNT_RE = re.compile(r"contingent\s+value\s+right[^.]{0,250}?up\s+to\s+(?:an\s+aggregate\s+of\s+)?\$\s?(\d{1,3}(?:\.\d{1,4})?)", re.I)
TENDER_EXPIRY_RE = re.compile(
    # [^;] rather than [^.]: the expiry sentence has "11:59 p.m." in it. "offer" must lead the clause,
    # or a merger proxy's "HSR waiting period expired on June 1" reads as a tender expiry.
    rf"\boffer\b[^;]{{0,80}}?\b(?:expire|expiration)[^;]{{0,160}}?\b(?:on\s+)?((?:{MONTHS})\s+\d{{1,2}},\s+20\d\d)", re.I)
CLOSE_RE = re.compile(
    r"expected\s+to\s+(?:close|be\s+completed|be\s+consummated|be\s+closed)\s+(?:in|by|during|at|before|around)?\s*"
    r"(?:the\s+)?(?:end\s+of\s+(?:the\s+)?)?"
    r"(first|second|third|fourth|1st|2nd|3rd|4th|early|mid|middle|late|q[1-4])?[\s-]*"
    r"(quarter|half)?\s*(?:of\s+)?(?:calendar\s+(?:year\s+)?|fiscal\s+(?:year\s+)?)?(?:year\s+)?(20\d\d)",
    re.I,
)
PERIOD_END = {
    ("first", "quarter"): (3, 31), ("1st", "quarter"): (3, 31), ("q1", ""): (3, 31),
    ("second", "quarter"): (6, 30), ("2nd", "quarter"): (6, 30), ("q2", ""): (6, 30),
    ("third", "quarter"): (9, 30), ("3rd", "quarter"): (9, 30), ("q3", ""): (9, 30),
    ("fourth", "quarter"): (12, 31), ("4th", "quarter"): (12, 31), ("q4", ""): (12, 31),
    ("first", "half"): (6, 30), ("1st", "half"): (6, 30),
    ("second", "half"): (12, 31), ("2nd", "half"): (12, 31),
    ("early", ""): (3, 31), ("mid", ""): (6, 30), ("middle", ""): (6, 30), ("late", ""): (12, 31),
    ("", ""): (12, 31),
}


@dataclass
class Filing:
    form: str
    filed: str
    url: str
    ciks: list[str]


@dataclass
class Deal:
    cik: str
    ticker: str
    company: str
    filings: list[Filing] = field(default_factory=list)
    cash: float = 0.0
    ratio: float = 0.0
    acquirer: str = ""
    acquirer_ticker: str = ""
    cvr: bool = False
    cvr_max: float | None = None
    tender: bool = False
    close_date: date | None = None
    close_basis: str = ""
    terms_source: str = ""
    price: float | None = None
    acquirer_price: float | None = None
    undisturbed: float | None = None
    status: str = ""
    closed: bool = False
    terminated: bool = False
    market_cap: float | None = None
    is_acquirer: bool = False


# ---------------------------------------------------------------- HTTP

class Sec:
    """SEC requests with the declared user agent, held under the 10 requests/second fair-access limit."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = _sec_user_agent()
        self.lock = threading.Lock()
        self.last = 0.0

    def get(self, url: str, **kwargs) -> requests.Response:
        for attempt in range(3):
            with self.lock:
                wait = self.last + 0.13 - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.last = time.monotonic()
            response = self.session.get(url, timeout=25, **kwargs)
            if response.status_code in (429, 503):
                time.sleep(2 + attempt * 3)
                continue
            response.raise_for_status()
            return response
        response.raise_for_status()
        return response


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text.replace("\xa0", " "))


def yahoo_prices(symbol: str, announced: date | None = None) -> tuple[float | None, float | None]:
    """(last price, close on the last trading day before the announcement) from Yahoo's chart feed."""
    if not symbol:
        return None, None
    params = {"range": "5d", "interval": "1d"}
    if announced:
        start = datetime.combine(announced - timedelta(days=10), datetime.min.time())
        params = {"period1": int(start.timestamp()), "period2": int(time.time()), "interval": "1d"}
    try:
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.replace('.', '-')}",
            params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=12,
        )
        result = response.json()["chart"]["result"][0]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None, None
    last = result.get("meta", {}).get("regularMarketPrice")
    undisturbed = None
    if announced:
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
        for stamp, close in zip(result.get("timestamp") or [], closes):
            if close and date.fromtimestamp(stamp) < announced:
                undisturbed = close
    return (float(last) if last else None), (float(undisturbed) if undisturbed else None)


# ---------------------------------------------------------------- discovery

def _efts_page(sec: Sec, params: dict) -> dict | None:
    """One search page; EDGAR full-text search sometimes answers with a transient backend error body."""
    for attempt in range(5):
        try:
            payload = sec.get(EFTS, params=params).json()
        except (requests.RequestException, ValueError):
            payload = {}
        if "hits" in payload:
            return payload["hits"]
        time.sleep(2 ** (attempt + 1))
    return None


def search_deal_filings(sec: Sec, start: date, end: date, max_per_query: int = 1000,
                        warnings: list | None = None) -> list[Filing]:
    filings = {}
    searches = [(query, form) for query, forms in DEAL_QUERIES for form in forms]
    for query, form in searches:
        for offset in range(0, max_per_query, 100):
            params = {"q": query, "dateRange": "custom", "startdt": start.isoformat(),
                      "enddt": end.isoformat(), "forms": form, "from": offset}
            hits = _efts_page(sec, params)
            if hits is None:
                if warnings is not None:
                    warnings.append(f"EDGAR search {query} {form} failed at result {offset}; coverage is partial.")
                break
            for hit in hits["hits"]:
                source = hit["_source"]
                accession, _, filename = hit["_id"].partition(":")
                ciks = [c.lstrip("0") for c in source.get("ciks", [])]
                if not ciks:
                    continue
                url = f"{ARCHIVES}/{ciks[0]}/{accession.replace('-', '')}/{filename}"
                filings[url] = Filing(source.get("form", ""), source.get("file_date", ""), url, ciks)
            if offset + 100 >= hits["total"]["value"]:
                break
    return list(filings.values())


def group_by_target(filings: list[Filing], tickers: dict) -> dict[str, Deal]:
    """Every listed company named on a deal filing is a possible target; pricing weeds out acquirers."""
    deals = {}
    for filing in filings:
        listed = [c for c in filing.ciks if c.zfill(10) in tickers]
        if filing.form in TARGET_FORMS and listed:
            listed = listed[:1]
        for cik in listed:
            ticker, company = tickers[cik.zfill(10)]
            deal = deals.setdefault(cik, Deal(cik, ticker, company))
            deal.filings.append(filing)
    return deals


# ---------------------------------------------------------------- terms

def _mode(values):
    return Counter(values).most_common(1)[0][0] if values else None


def _period_end(word: str, unit: str, year: str) -> date:
    word, unit = (word or "").lower(), (unit or "").lower()
    month, day = PERIOD_END.get((word, unit)) or PERIOD_END.get((word, "")) or (12, 31)
    return date(int(year), month, day)


def extract_terms(text: str) -> dict:
    cash = [float(a or b) for a, b in CASH_RE.findall(text) if 0 < float(a or b) < 5000]
    ratios = [(float(r), name.strip()) for r, name in RATIO_RE.findall(text) if 0.001 < float(r) < 50]
    ratio_values = [r for r, _ in ratios] + [float(r) for r in EXCHANGE_RATIO_RE.findall(text) if 0.001 < float(r) < 50]
    ratio = _mode(ratio_values)
    acquirer = _mode([name for r, name in ratios if r == ratio]) or ""
    cvr_amounts = [float(a) for a in CVR_AMOUNT_RE.findall(text)]
    terms = {
        "cash": _mode(cash) or 0.0,
        "ratio": ratio or 0.0,
        "acquirer": acquirer,
        "cvr": bool(CVR_RE.search(text)),
        "cvr_max": max(cvr_amounts) if cvr_amounts else None,
        "close_date": None,
        "close_basis": "",
        "tender": False,
    }
    expiry = TENDER_EXPIRY_RE.search(text)
    if expiry and re.search(r"offer\s+to\s+purchase", text, re.I):
        try:
            terms.update(close_date=datetime.strptime(expiry.group(1), "%B %d, %Y").date(),
                         close_basis="tender expiry", tender=True)
        except ValueError:
            pass
    if not terms["close_date"]:
        match = CLOSE_RE.search(text)
        if match:
            word, unit, year = match.groups()
            terms.update(close_date=_period_end(word, unit, year),
                         close_basis=f"guided: {' '.join(p for p in (word, unit, year) if p)}")
    return terms


NAME_SUFFIXES = re.compile(
    r"[,/.]|\b(inc|incorporated|corp|corporation|co|company|holdings?|group|ltd|limited|plc|n\.?v|s\.?a|lp|llc|the|de|md|nv|bancorp|bancshares)\b",
    re.I)


def short_name(company: str) -> str:
    """'TRICO BANCSHARES /' -> 'trico'; 'C. H. ROBINSON WORLDWIDE' -> 'robinson'."""
    words = [w for w in NAME_SUFFIXES.sub(" ", company).split() if len(w) > 1]
    return " ".join(words[:2]).lower() if len(words) > 1 and len(words[0]) <= 3 else (words[0].lower() if words else "")


def deal_role(text: str, company: str) -> str:
    """'target', 'acquirer' or '' from how the filing talks about the company."""
    name = short_name(company)
    if len(name) < 3:
        return ""
    n = re.escape(name)
    # Drop the period in "Inc." so "Viatris Inc. today announced it will acquire" reads as one clause.
    lowered = re.sub(r"\b(inc|corp|co|ltd|plc|llc|n\.v|s\.a)\.", r"\1", text.lower())
    target = len(re.findall(
        rf"(?:acquire|acquisition\s+of|acquiring|purchase\s+of|to\s+buy|merger\s+of)\s+(?:all\s+(?:of\s+)?the\s+outstanding\s+(?:shares|common\s+stock)\s+of\s+)?{n}"
        rf"|{n}(?:'s|’s)?\s+(?:stockholders|shareholders)\s+(?:will|would)\s+receive"
        rf"|(?:share|shares)\s+of\s+{n}(?:'s|’s)?\s+(?:common|ordinary|class)"
        rf"|{n}[^.]{{0,60}}?\b(?:to\s+be\s+acquired|will\s+be\s+acquired|agreed\s+to\s+be\s+acquired)", lowered))
    acquirer = len(re.findall(
        rf"{n}[^.]{{0,60}}?\b(?:to\s+acquire|will\s+acquire|has\s+agreed\s+to\s+acquire|agreed\s+to\s+acquire|announced\s+(?:its|the)\s+acquisition)"
        rf"|acquired\s+by\s+{n}|(?:shares|share)\s+of\s+{n}(?:'s|’s)?\s+common\s+stock\s+(?:for|in\s+exchange)", lowered))
    if target > acquirer:
        return "target"
    if acquirer > target:
        return "acquirer"
    return ""


def read_terms(sec: Sec, deal: Deal) -> None:
    """Read terms from the newest press release or deal document that yields any.

    A document that reads as this company buying someone else marks it as the acquirer and stops,
    so the buyer does not show up as a second, fictional target.
    """
    def rank(f: Filing):
        name = f.url.lower()
        press = 1 if re.search(r"ex-?99|ex99|press", name) else 0
        return (f.filed, press)
    for filing in sorted(deal.filings, key=rank, reverse=True)[:4]:
        try:
            text = html_to_text(sec.get(filing.url).text[:1_500_000])[:400_000]
        except requests.RequestException:
            continue
        terms = extract_terms(text)
        if not (terms["cash"] or terms["ratio"]):
            continue
        role = deal_role(text, deal.company)
        if role == "acquirer" or (terms["acquirer"] and short_name(terms["acquirer"]) == short_name(deal.company)):
            deal.is_acquirer = True
            return
        if role != "target" and filing.form not in TARGET_FORMS:
            continue   # an 8-K or 425 that never says this company is being bought
        for key, value in terms.items():
            setattr(deal, key, value)
        deal.terms_source = filing.url
        deal.tender = terms["tender"] or (
            filing.form.startswith(("SC TO-T", "SC 14D9")) and bool(re.search(r"offer\s+to\s+purchase", text, re.I)))
        return


def read_status(sec: Sec, deal: Deal, tickers_by_cik: dict) -> None:
    """Closed / terminated flags and a status line from the target's filing history."""
    announced = min(f.filed for f in deal.filings)
    try:
        recent = sec.get(f"https://data.sec.gov/submissions/CIK{deal.cik.zfill(10)}.json").json()["filings"]["recent"]
    except (requests.RequestException, ValueError, KeyError):
        return
    events = []
    for form, filed, items in zip(recent["form"], recent["filingDate"], recent.get("items", [""] * len(recent["form"]))):
        if filed < announced:
            continue
        items = items or ""
        if form in CLOSED_FORMS or (form == "8-K" and "2.01" in items):
            deal.closed = True
        if form == "8-K" and "1.02" in items:
            deal.terminated = True
            events.append(f"8-K 1.02 (termination?) {filed}")
        if form == "8-K" and "5.07" in items:
            events.append(f"Vote results 8-K {filed}")
        if form in ("DEFM14A", "PREM14A", "SC 14D9", "S-4", "S-4/A", "SC TO-T"):
            events.append(f"{form} {filed}")
    seen, lines = set(), []
    for event in events:                       # newest first, one line per form
        key = event.split(" ")[0]
        if key not in seen:
            seen.add(key)
            lines.append(event)
    deal.status = "; ".join(lines[:4]) or f"Announced {announced}"


def resolve_acquirer(deal: Deal, tickers_by_cik: dict, by_name: dict) -> None:
    """Acquirer ticker for a stock deal: the company named in the exchange-ratio sentence,
    preferring a listed co-filer on the deal's own filings with that name."""
    if not deal.ratio:
        return
    wanted = short_name(deal.acquirer) if deal.acquirer else ""
    others = []
    for filing in deal.filings:
        for cik in filing.ciks:
            record = tickers_by_cik.get(cik.zfill(10))
            if cik != deal.cik and record and record not in others:
                others.append(record)
    for ticker, company in others:
        if wanted and short_name(company) == wanted:
            deal.acquirer_ticker, deal.acquirer = ticker, company
            return
    if wanted and wanted not in ("parent", "buyer", "purchaser", "acquiror", "acquirer"):
        record = by_name.get(wanted)
        if record:
            deal.acquirer_ticker, deal.acquirer = record
            return
    if len(others) == 1:
        deal.acquirer_ticker, deal.acquirer = others[0]


def market_cap(sec: Sec, cik: str, price: float) -> float | None:
    """Price x the latest cover-page share count (small companyconcept request, not full companyfacts)."""
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik.zfill(10)}/dei/EntityCommonStockSharesOutstanding.json"
    try:
        facts = sec.get(url).json()["units"]["shares"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    latest = max(facts, key=lambda f: (f.get("end", ""), f.get("filed", "")), default=None)
    return latest["val"] * price if latest and latest.get("val") else None


def counterparty_name(deal: Deal, names: dict[str, str]) -> str:
    if deal.acquirer:
        return deal.acquirer
    for filing in deal.filings:
        for cik in filing.ciks:
            if cik != deal.cik and cik in names:
                return names[cik]
    return ""


# ---------------------------------------------------------------- rows

def consideration_text(deal: Deal) -> str:
    parts = []
    if deal.cash:
        parts.append(f"{deal.cash:.2f} cash")
    if deal.ratio:
        parts.append(f"{deal.ratio:g} {deal.acquirer_ticker or deal.acquirer or 'acquirer shares'}")
    if deal.cvr:
        parts.append(f"1 CVR (up to {deal.cvr_max:.2f})" if deal.cvr_max else "1 CVR")
    return " + ".join(parts)


def situation_type(deal: Deal) -> str:
    kind = "cash+stock" if deal.cash and deal.ratio else ("stock" if deal.ratio else "cash")
    base = "Tender offer" if deal.tender else "Merger arb"
    return f"{base} ({kind})" + (" + CVR" if deal.cvr else "")


def deal_row(deal: Deal, pulled: date) -> dict | None:
    if deal.is_acquirer or not deal.price or (deal.ratio and not deal.acquirer_price):
        return None
    if deal.acquirer_ticker == deal.ticker or (deal.acquirer and short_name(deal.acquirer) == short_name(deal.company)):
        return None
    math = deal_math(deal.price, cash=deal.cash, ratio=deal.ratio, acquirer_price=deal.acquirer_price or 0)
    value = math["consideration_value"]
    if not 0.5 <= deal.price / value <= 1.1:   # an acquirer's own price, or terms we misread
        return None
    estimated = deal.close_date is None or deal.close_date <= pulled
    close = deal.close_date if not estimated else (
        pulled + timedelta(days=DEFAULT_TENDER_DAYS if deal.tender else DEFAULT_MERGER_DAYS))
    days = max((close - pulled).days, 1)
    spread = math["gross_spread"]
    annualized = spread * 365 / days
    downside = deal.undisturbed / deal.price - 1 if deal.undisturbed else None
    if deal.terminated or spread < 0:
        tier = "Watchlist - negative spread" if spread < 0 else "Watchlist - possible termination"
    elif spread > VERIFY_SPREAD:
        tier = "Verify - unusually wide spread"
    elif deal.cvr:
        tier = "B - merger security"
    else:
        tier = "A - merger arb"
    value_text = f"{value:.2f}"
    if deal.ratio:
        value_text += f" ({deal.cash:.2f} + {deal.ratio:g} x {deal.acquirer_ticker} {deal.acquirer_price:.2f})" if deal.cash \
            else f" ({deal.ratio:g} x {deal.acquirer_ticker} {deal.acquirer_price:.2f})"
    if deal.cvr:
        value_text += " + CVR not valued"
    key_dates = f"{'Tender expires' if deal.close_basis == 'tender expiry' else 'Close'} {close:%a %m/%d/%Y}"
    key_dates += f" ({deal.close_basis})" if not estimated else f" (est. {DEFAULT_TENDER_DAYS if deal.tender else DEFAULT_MERGER_DAYS}d; no date in filing)"
    return {
        "id": f"DEAL-{deal.ticker}",
        "tier": tier,
        "ticker": deal.ticker,
        "company": deal.company,
        "situation_type": situation_type(deal),
        "counterparty": deal.acquirer or "",
        "deal_terms": consideration_text(deal),
        "price": deal.price,
        "consideration_value": value,
        "consideration_text": value_text,
        "gross_spread": spread,
        "close_date": close.isoformat(),
        "days": days,
        "annualized": annualized,
        "key_dates_catalyst": key_dates,
        "status": deal.status,
        "downside": downside,
        "undisturbed": deal.undisturbed,
        "market_cap": deal.market_cap,
        "sources": deal.terms_source,
        "estimated_close": estimated,
    }


# ---------------------------------------------------------------- workbook

TIER_ORDER = ("A - merger arb", "B - merger security", "Verify - unusually wide spread",
              "Watchlist - possible termination", "Watchlist - negative spread")
SHEET_COLUMNS = ["Tier", "Ticker", "Company", "Situation type", "Counterparty", "Deal terms", "Price",
                 "Consideration value", "Gross spread", "Annualized", "Key dates/catalyst", "Status/approvals",
                 "Bull", "Bear", "Risks", "Downside if broken", "Market cap", "Sources"]
SHEET_WIDTHS = [26, 9, 30, 24, 26, 30, 9, 30, 11, 11, 40, 44, 26, 26, 26, 18, 10, 60]
ANALYSIS_COLUMNS = ["EV rank", "Ticker", "Situation type", "Why mispriced (forced flow)", "Value estimate (method)",
                    "Catalyst + date", "Days", "Upside if right", "Downside if wrong (valuation, opinion)",
                    "Probability (opinion)", "EV (prob-weighted)", "Annualized (arb)", "Sizing note (liquidity/borrow)"]
ANALYSIS_WIDTHS = [8, 9, 24, 40, 30, 40, 7, 12, 16, 12, 14, 13, 30]
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
SECTION_FILL = PatternFill("solid", fgColor="D9E1F2")


def _fmt_cap(value):
    if not value:
        return ""
    return f"{value / 1e9:.1f}B" if value >= 1e9 else f"{value / 1e6:.0f}M"


def _header(ws, row: int, labels: list[str], widths: list[int]):
    for column, (label, width) in enumerate(zip(labels, widths), 1):
        cell = ws.cell(row=row, column=column, value=label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = ws.cell(row=row + 1, column=3)


def write_workbook(rows: list[dict], pulled: datetime, path: Path, notes: list[str]) -> Path:
    workbook = Workbook()
    ws = workbook.active
    ws.title = f"Special Situations {pulled:%Y-%m-%d}"[:31]
    ws["A1"] = f"Event-Driven Special Situations - {pulled:%a %b %d, %Y} (prices pulled {pulled:%H:%M %Z})"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = ("Research only - not investment advice. Terms extracted automatically from SEC EDGAR filings "
                "(linked in Sources; verify before acting); prices from Yahoo Finance at pull time. "
                "Annualized = gross spread x 365 / days to close. Bull/Bear/Risks are left for the analyst.")
    ws["A2"].alignment = Alignment(wrap_text=False)
    _header(ws, 4, SHEET_COLUMNS, SHEET_WIDTHS)
    r = 5
    for tier in TIER_ORDER:
        tier_rows = sorted((x for x in rows if x["tier"] == tier), key=lambda x: -x["annualized"])
        if not tier_rows:
            continue
        for column in range(1, len(SHEET_COLUMNS) + 1):
            ws.cell(row=r, column=column).fill = SECTION_FILL
        ws.cell(row=r, column=1, value=f"{tier} ({len(tier_rows)})").font = Font(bold=True)
        r += 1
        for x in tier_rows:
            values = [tier, x["ticker"], x["company"], x["situation_type"], x["counterparty"], x["deal_terms"],
                      x["price"], x["consideration_text"], x["gross_spread"], x["annualized"],
                      x["key_dates_catalyst"], x["status"], "", "", "",
                      (f"{x['downside']:+.1%} to pre-deal {x['undisturbed']:.2f}" if x["downside"] is not None else ""),
                      _fmt_cap(x["market_cap"]), x["sources"]]
            for column, value in enumerate(values, 1):
                cell = ws.cell(row=r, column=column, value=value)
                cell.alignment = Alignment(vertical="top", wrap_text=column in (6, 8, 11, 12))
                if column in (9, 10):
                    cell.number_format = "+0.0%;-0.0%"
                elif column == 7:
                    cell.number_format = "0.00"
                elif column == 18 and value:
                    cell.hyperlink = value
                    cell.font = Font(color="0563C1", underline="single")
            r += 1
    r += 1
    for note in notes:
        ws.cell(row=r, column=1, value=note).font = Font(italic=True, color="666666")
        r += 1

    an = workbook.create_sheet("Analysis - EV Ranked")
    an["A1"] = "Analysis tab - ideas ranked by probability-weighted expected value (EV)."
    an["A1"].font = Font(bold=True, size=13)
    an["A2"] = (f"EV = p x upside + (1 - p) x downside. Probability starts at a {DEFAULT_PROBABILITY:.0%} default - "
                "edit column J and EV recalculates. Downside = move back to the pre-announcement close (opinion proxy).")
    _header(an, 4, ANALYSIS_COLUMNS, ANALYSIS_WIDTHS)
    ranked = sorted((x for x in rows if x["tier"] in TIER_ORDER[:3]),
                    key=lambda x: -(DEFAULT_PROBABILITY * x["gross_spread"]
                                    + (1 - DEFAULT_PROBABILITY) * (x["downside"] if x["downside"] is not None else -0.25)))
    for i, x in enumerate(ranked, 1):
        row = 4 + i
        downside = x["downside"] if x["downside"] is not None else -0.25
        values = [i, x["ticker"], x["situation_type"], "", f"{x['consideration_value']:.2f} deal value ({x['deal_terms']})",
                  x["key_dates_catalyst"], x["days"], x["gross_spread"], downside, DEFAULT_PROBABILITY,
                  f"=J{row}*H{row}+(1-J{row})*I{row}", x["annualized"], _fmt_cap(x["market_cap"])]
        for column, value in enumerate(values, 1):
            cell = an.cell(row=row, column=column, value=value)
            cell.alignment = Alignment(vertical="top", wrap_text=column in (5, 6))
            if column in (8, 9, 11, 12):
                cell.number_format = "+0.0%;-0.0%"
            elif column == 10:
                cell.number_format = "0%"
    an.auto_filter.ref = f"A4:M{max(5, 4 + len(ranked))}"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        workbook.save(path)
    except PermissionError as error:
        raise RuntimeError(f"Close {path.name} in Excel and run again.") from error
    return path


# ---------------------------------------------------------------- run

def build_deal_sheet(days: int = 270, out_dir: Path | None = None, log=print) -> tuple[Path, list[dict]]:
    pulled = datetime.now().astimezone()
    today = pulled.date()
    sec = Sec()
    tickers = _load_sec_tickers(sec.session.headers["User-Agent"])
    by_name = {}
    for ticker, company in tickers.values():
        by_name.setdefault(short_name(company), (ticker, company))
    log(f"Searching EDGAR for deal filings since {today - timedelta(days=days)}...")
    warnings = []
    filings = search_deal_filings(sec, today - timedelta(days=days), today, warnings=warnings)
    deals = group_by_target(filings, tickers)
    log(f"  {len(filings)} filings, {len(deals)} listed companies named on them")

    names = {cik.lstrip("0"): company for cik, (_, company) in tickers.items()}

    def enrich(deal: Deal):
        read_status(sec, deal, tickers)
        if deal.closed:
            return
        read_terms(sec, deal)
        if not (deal.cash or deal.ratio):
            return
        resolve_acquirer(deal, tickers, by_name)
        deal.acquirer = deal.acquirer or counterparty_name(deal, names)
        announced = datetime.strptime(min(f.filed for f in deal.filings), "%Y-%m-%d").date()
        deal.price, deal.undisturbed = yahoo_prices(deal.ticker, announced)
        if deal.ratio:
            deal.acquirer_price, _ = yahoo_prices(deal.acquirer_ticker)
        if deal.price:
            deal.market_cap = market_cap(sec, deal.cik, deal.price)

    log("Reading filing histories, deal terms and prices (a few minutes)...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(enrich, deals.values()))
    rows = [row for row in (deal_row(d, today) for d in deals.values() if not d.closed and (d.cash or d.ratio)) if row]
    # One row per target; keep the highest-confidence read (a dated close beats an estimate).
    best = {}
    for row in rows:
        if row["ticker"] not in best or (best[row["ticker"]]["estimated_close"] and not row["estimated_close"]):
            best[row["ticker"]] = row
    rows = list(best.values())
    closed = sum(1 for d in deals.values() if d.closed)
    notes = [
        f"Coverage: SEC EDGAR deal filings {today - timedelta(days=days)} to {today} "
        f"({len(filings)} filings, {len(deals)} companies; {closed} already closed or delisted; {len(rows)} priced deals).",
        "US-listed targets only (SEC filers). UK/ASX/HKEX situations are in the dashboard's global scan, not priced here.",
        "Not covered automatically: stubs, spin-offs, liquidations, litigation status, borrow cost. Check Sources before trading.",
    ] + warnings
    for warning in warnings:
        log(f"  Warning: {warning}")
    path = (out_dir or _report_directory()) / f"special_situations_{today:%Y-%m-%d}.xlsx"
    write_workbook(rows, pulled, path, notes)

    tracked = 0
    for row in rows:
        if row["tier"] not in TIER_ORDER[:3]:
            continue
        deal = {**row, "date_pulled": pulled.isoformat(timespec="seconds"), "deal_terms": row["deal_terms"]}
        try:
            if record_tracked_deal(deal).get("tracked"):
                tracked += 1
        except RuntimeError as error:
            log(f"  Tracker not updated: {error}")
            break
    log(f"Deal sheet: {path}")
    log(f"  {len(rows)} priced deals; {tracked} at or above {TRACKER_MIN_ANNUALIZED:.0%} annualized in the tracker")
    return path, rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=270, help="look back this many days for deal announcements")
    parser.add_argument("--open", action="store_true", help="open the workbook when done")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    path, _ = build_deal_sheet(args.days)
    if args.open:
        os.startfile(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
