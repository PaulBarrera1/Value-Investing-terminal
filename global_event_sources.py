"""Free primary-source event feeds beyond EDGAR full-text search, plus deal math and SEC financials.

Every source here is a regulator or exchange publication that needs no API key:
  * SEC EDGAR daily form index: tender offers, going-private, merger proxies, 425s, S-4/F-4,
    Form 10 spin-offs, Schedule 13D, delistings and deregistrations.
  * UK Takeover Panel disclosure table: every UK company currently in an offer period.
  * ASX market announcements: takeovers, schemes, halts, suspensions, buy-backs, rights issues.
  * HKEXnews latest announcements: privatisations, offers, major/very substantial transactions,
    spin-offs, rights issues, halts and suspensions.
  * Nasdaq trade halts RSS: news-pending, regulatory and SEC halts.
  * SEC XBRL company facts: balance-sheet and cash-flow snapshot for the analysis panel.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import os
import re
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import requests

from ibkr_market_scanner import (
    ScanCandidate,
    _http_get,
    _load_sec_tickers,
    _load_sec_tickers_by_symbol,
)


USER_AGENT_FALLBACK = "Value-Investing-Terminal research scanner (set SEC_USER_AGENT with contact details)"

# Form type -> (event category, situation label). Form names are as they appear in the daily index.
EDGAR_DEAL_FORMS = {
    "SC TO-T": ("M&A / merger arbitrage", "Third-party tender offer"),
    "SC TO-T/A": ("M&A / merger arbitrage", "Third-party tender offer (amended)"),
    "SC 14D9": ("M&A / merger arbitrage", "Target response to tender offer"),
    "SC 14D9/A": ("M&A / merger arbitrage", "Target response to tender offer (amended)"),
    "SC TO-I": ("Tender / buyback / redemption", "Issuer tender offer"),
    "SC TO-I/A": ("Tender / buyback / redemption", "Issuer tender offer (amended)"),
    "SC 13E3": ("M&A / merger arbitrage", "Going-private transaction"),
    "SC 13E3/A": ("M&A / merger arbitrage", "Going-private transaction (amended)"),
    "PREM14A": ("M&A / merger arbitrage", "Preliminary merger proxy"),
    "DEFM14A": ("M&A / merger arbitrage", "Definitive merger proxy"),
    "PREM14C": ("M&A / merger arbitrage", "Preliminary merger information statement"),
    "DEFM14C": ("M&A / merger arbitrage", "Definitive merger information statement"),
    "425": ("M&A / merger arbitrage", "Business-combination communication"),
    "S-4": ("M&A / merger arbitrage", "Stock-deal registration"),
    "S-4/A": ("M&A / merger arbitrage", "Stock-deal registration (amended)"),
    "F-4": ("M&A / merger arbitrage", "Stock-deal registration (foreign issuer)"),
    "F-4/A": ("M&A / merger arbitrage", "Stock-deal registration (foreign issuer, amended)"),
    "10-12B": ("Spin-off / separation", "Form 10 spin-off registration"),
    "10-12B/A": ("Spin-off / separation", "Form 10 spin-off registration (amended)"),
    "10-12G": ("Spin-off / separation", "Form 10 registration"),
    "10-12G/A": ("Spin-off / separation", "Form 10 registration (amended)"),
    "SCHEDULE 13D": ("Activist / strategic review", "Schedule 13D (5%+ holder with intent)"),
    "SCHEDULE 13D/A": ("Activist / strategic review", "Schedule 13D (amended)"),
    "SC 13D": ("Activist / strategic review", "Schedule 13D (5%+ holder with intent)"),
    "SC 13D/A": ("Activist / strategic review", "Schedule 13D (amended)"),
    "25-NSE": ("Delisting / listing status", "Exchange delisting notice"),
    "15-12B": ("Delisting / listing status", "Deregistration"),
    "15-12G": ("Delisting / listing status", "Deregistration"),
}

# ASX announcement types worth surfacing; daily buy-back returns and routine admin are excluded.
ASX_TYPE_RULES = (
    (re.compile(r"takeover|bidder|target'?s statement|withdrawal of offer", re.I), "M&A / merger arbitrage"),
    (re.compile(r"scheme of arrangement", re.I), "M&A / merger arbitrage"),
    (re.compile(r"asset acquisition|asset disposal", re.I), "M&A / merger arbitrage"),
    (re.compile(r"demerger", re.I), "Spin-off / separation"),
    (re.compile(r"^(?!daily).*buy-?back", re.I), "Tender / buyback / redemption"),
    (re.compile(r"return of capital|capital reduction", re.I), "Tender / buyback / redemption"),
    (re.compile(r"pro rata|renounceable|entitlement", re.I), "Rights / recapitalization"),
    (re.compile(r"trading halt(?! lifted)|suspension from official quotation|reinstatement", re.I),
     "Corporate action / security change"),
    (re.compile(r"delisting|removal from official list", re.I), "Delisting / listing status"),
    (re.compile(r"^(?!company administration).*(administrat|receiver|liquidat)", re.I),
     "Restructuring / distress"),
)

# HKEXnews headline-category rules (English feed). Daily share-buyback returns are excluded.
HKEX_RULES = (
    (re.compile(r"privati[sz]ation|scheme of arrangement|general offer|offeror|takeovers code|"
                r"composite (?:offer )?document|rule 3\.[57]", re.I), "M&A / merger arbitrage"),
    (re.compile(r"very substantial (?:acquisition|disposal)|major transaction|reverse takeover", re.I),
     "M&A / merger arbitrage"),
    (re.compile(r"spin-?off|separate listing|distribution in specie", re.I), "Spin-off / separation"),
    (re.compile(r"rights issue|open offer", re.I), "Rights / recapitalization"),
    (re.compile(r"repurchase offer|off-market (?:share )?buy-?back", re.I), "Tender / buyback / redemption"),
    (re.compile(r"trading halt|suspension|resumption", re.I), "Corporate action / security change"),
    (re.compile(r"withdrawal of listing|delisting|cancellation of listing", re.I), "Delisting / listing status"),
    (re.compile(r"winding[- ]up|liquidat|provisional liquidator|restructuring", re.I), "Restructuring / distress"),
)

# Nasdaq halt reason codes that point to news, a corporate action or a regulatory problem.
# Volatility pauses (LUDP, T5) and market-wide halts are excluded.
NASDAQ_HALT_CODES = {
    "T1": ("Corporate action / security change", "Halt - news pending"),
    "T2": ("Corporate action / security change", "Halt - news released"),
    "T3": ("Corporate action / security change", "Halt - news and resumption times"),
    "T6": ("Corporate action / security change", "Halt - extraordinary market activity"),
    "T12": ("Delisting / listing status", "Halt - additional information requested by Nasdaq"),
    "M1": ("Corporate action / security change", "Halt - corporate action"),
    "H4": ("Delisting / listing status", "Halt - non-compliance"),
    "H9": ("Delisting / listing status", "Halt - not current in required filings"),
    "H10": ("Delisting / listing status", "Halt - SEC trading suspension"),
    "H11": ("Delisting / listing status", "Halt - regulatory concern"),
}


def _sec_user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", USER_AGENT_FALLBACK)


def _summary(name: str | None, count: int) -> dict:
    return {
        "sources_used": [name] if name else [],
        "news_provider_names": [name] if name else [],
        "contracts_found": count,
    }


def _contract(con_id: int, symbol: str, sec_type: str, exchange: str, currency: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        conId=con_id, symbol=symbol, localSymbol=symbol, secType=sec_type,
        primaryExchange=exchange, exchange=exchange, currency=currency,
    )


def _stable_id(*parts: str) -> int:
    return int(hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12], 16)


# ---------------------------------------------------------------- SEC EDGAR daily form index

def business_days_back(end: date, count: int) -> list[date]:
    days, current = [], end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return sorted(days)


def parse_edgar_form_index(text: str) -> list[dict]:
    """Parse a form.YYYYMMDD.idx file into rows. Columns are fixed-width; the form type can contain spaces."""
    rows, started = [], False
    for line in text.splitlines():
        if line.startswith("-----"):
            started = True
            continue
        if not started or not line.strip():
            continue
        match = re.match(r"^(.+?)\s{2,}(.+?)\s{2,}(\d{1,10})\s+(\d{8})\s+(edgar/\S+)\s*$", line)
        if match:
            form, company, cik, filed, path = match.groups()
            rows.append({
                "form": form.strip(), "company": company.strip(), "cik": cik.zfill(10),
                "date": f"{filed[:4]}-{filed[4:6]}-{filed[6:]}", "path": path,
            })
    return rows


FUND_NAME = re.compile(
    r"\b(funds?|trust|portfolios?|etfs?|municipal|bdc|private (?:income|credit|equity)|"
    r"interval|capital (?:income|credit) (?:fund|corp))\b", re.I)
EXCHANGE_FILERS = re.compile(r"\b(nasdaq stock market|nyse|new york stock exchange|cboe|exchange,? inc)\b", re.I)


def _keep_edgar_row(row: dict, has_ticker: bool) -> bool:
    """Drop fund and exchange noise. Each filing is indexed once per party (filer and subject company);
    only parties with a listed ticker are kept, except exchange-listed Form 10 spin-offs (10-12B),
    whose SpinCo has no ticker until it trades."""
    if EXCHANGE_FILERS.search(row["company"]) or FUND_NAME.search(row["company"]):
        return False
    return has_ticker or row["form"].startswith("10-12B")


def scan_edgar_deal_forms(business_days: int = 3, today: date | None = None) -> tuple[list[ScanCandidate], list[str], dict]:
    """Tender offers, merger proxies, spin-off Form 10s, 13Ds and delistings from EDGAR's daily form index."""
    warnings = []
    user_agent = _sec_user_agent()
    today = today or datetime.now(timezone.utc).date()
    try:
        tickers = _load_sec_tickers(user_agent)
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        tickers = {}
        warnings.append(f"SEC ticker directory unavailable; EDGAR deal forms are shown by CIK: {error}")
    candidates_by_cik: dict[str, ScanCandidate] = {}
    seen = set()
    succeeded = False
    for day in business_days_back(today, business_days):
        url = (f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/"
               f"QTR{(day.month - 1) // 3 + 1}/form.{day:%Y%m%d}.idx")
        try:
            rows = parse_edgar_form_index(_http_get(url, user_agent).text)
            succeeded = True
        except requests.RequestException as error:
            if day < today:   # today's index is posted overnight
                warnings.append(f"EDGAR daily index {day} unavailable: {error}")
            continue
        for row in rows:
            spec = EDGAR_DEAL_FORMS.get(row["form"])
            if not spec or (row["cik"], row["path"]) in seen:
                continue
            seen.add((row["cik"], row["path"]))
            category, label = spec
            symbol, company = tickers.get(row["cik"], ("", ""))
            if not _keep_edgar_row(row, bool(symbol)):
                continue
            company = company or row["company"]
            candidate = candidates_by_cik.get(row["cik"])
            if candidate is None:
                candidate = ScanCandidate(
                    contract=_contract(int(row["cik"]), symbol or f"CIK{int(row['cik'])}", "FILING", "SEC EDGAR", "USD"),
                    description=company,
                    research={"cik": row["cik"]},
                )
                candidates_by_cik[row["cik"]] = candidate
            accession = row["path"].rsplit("/", 1)[-1].replace(".txt", "")
            folder = accession.replace("-", "")
            candidate.headlines.append({
                "time": row["date"],
                "provider": f"SEC EDGAR · {row['form']}",
                "article_id": accession,
                "headline": f"{company}: {label} ({row['form']})",
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/{folder}/{accession}-index.htm",
                "matches": [(category, [row["form"]])],
            })
            candidate.signals.add(f"SEC filing · {row['form']}")
    candidates = list(candidates_by_cik.values())
    for candidate in candidates:
        forms = sorted({h["provider"].split(" · ", 1)[1] for h in candidate.headlines})
        candidate.research.setdefault("status_approvals", "EDGAR filings: " + ", ".join(forms))
    return candidates, warnings, _summary("SEC EDGAR deal forms" if succeeded else None, len(candidates))


# ---------------------------------------------------------------- UK Takeover Panel

def parse_takeover_panel(page_html: str) -> list[dict]:
    """Return offeree companies (with offerors) from the Panel's full disclosure table, skipping the day's change log."""
    text = html.unescape(re.sub(r"<br\s*/?>", " ", page_html))
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    # The first "DISCLOSURE TABLE" heading is the page title; the full table follows the second.
    markers = [i for i, line in enumerate(lines) if line.upper().endswith("DISCLOSURE TABLE")]
    start = markers[1] + 1 if len(markers) > 1 else (markers[0] + 1 if markers else 0)
    offerees, current, party = [], None, None
    for line in lines[start:]:
        if line.startswith("OFFEREE:"):
            current = {"offeree": line[8:].strip(), "isin": "", "offer_period": "", "offerors": [], "lei": ""}
            offerees.append(current)
            party = current
        elif current is None:
            continue
        elif line.startswith("OFFEROR:"):
            party = {"name": line[8:].strip(), "identified": "", "rule_26": ""}
            current["offerors"].append(party)
        elif line.startswith("Offer period commenced:"):
            current["offer_period"] = line.split(":", 1)[1].strip()
        elif line.startswith("Offeror identified:") and party is not current:
            party["identified"] = line.split(":", 1)[1].strip()
        elif line.startswith("Rule 2.6 deadline:") and party is not current:
            party["rule_26"] = line.split(":", 1)[1].strip()
        elif line.startswith("ISIN:") and party is current and not current["isin"]:
            current["isin"] = line[5:].strip()
        elif line.startswith("LEI:") and party is current and not current["lei"]:
            current["lei"] = line[4:].strip()
        elif "accept" in line.lower() and "cookies" in line.lower():
            break
    return offerees


def scan_uk_takeover_panel() -> tuple[list[ScanCandidate], list[str], dict]:
    url = "https://www.thetakeoverpanel.org.uk/disclosure/disclosure-table"
    try:
        offerees = parse_takeover_panel(_http_get(url, _sec_user_agent()).text)
    except requests.RequestException as error:
        return [], [f"UK Takeover Panel disclosure table request failed: {error}"], _summary(None, 0)
    if not offerees:
        return [], ["UK Takeover Panel disclosure table returned no offerees; the page layout may have changed."], _summary(None, 0)
    candidates = []
    for entry in offerees:
        offerors = entry["offerors"]
        names = "; ".join(o["name"] for o in offerors) or "Offeror not yet identified"
        deadlines = "; ".join(
            f"{o['name']}: Rule 2.6 {o['rule_26']}" for o in offerors if o["rule_26"] and o["rule_26"] != "N/A"
        )
        key_dates = f"Offer period since {entry['offer_period']}" + (f"; {deadlines}" if deadlines else "")
        symbol = entry["isin"] or entry["offeree"]
        candidates.append(ScanCandidate(
            contract=_contract(_stable_id("ukpanel", symbol), symbol, "STK", "UK Takeover Panel", "GBP"),
            description=entry["offeree"],
            signals={"UK Takeover Panel offer period"},
            headlines=[{
                "time": entry["offer_period"],
                "provider": "UK Takeover Panel",
                "article_id": entry["isin"] or entry["lei"],
                "headline": f"{entry['offeree']} in offer period; offeror(s): {names}",
                "url": url,
                "matches": [("M&A / merger arbitrage", ["offer period"])],
            }],
            research={
                "situation_type": "UK takeover (offer period)",
                "counterparty": names,
                "key_dates_catalyst": key_dates,
                "status_approvals": "In offer period under the UK Takeover Code" + (
                    " - Rule 2.6 'put up or shut up' deadline pending" if deadlines else ""),
            },
        ))
    return candidates, [], _summary("UK Takeover Panel", len(candidates))


# ---------------------------------------------------------------- ASX

def _classify(rules, text: str) -> str | None:
    return next((category for pattern, category in rules if pattern.search(text)), None)


def scan_asx_announcements(pages: int = 10) -> tuple[list[ScanCandidate], list[str], dict]:
    """Event-type announcements from the ASX market-wide feed (about one trading day per 800 items)."""
    warnings, items = [], []
    for page in range(1, pages + 1):
        url = f"https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements?itemsPerPage=100&page={page}"
        try:
            batch = _http_get(url, _sec_user_agent()).json()["data"]["items"]
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            warnings.append(f"ASX announcements page {page} unavailable: {error}")
            break
        if not batch:
            break
        items.extend(batch)
    candidates_by_symbol: dict[str, ScanCandidate] = {}
    for item in items:
        types = item.get("announcementTypes") or []
        matches = [(category, [kind]) for kind in types if (category := _classify(ASX_TYPE_RULES, kind))]
        symbol = str(item.get("symbol") or "").strip()
        if not matches or not symbol or len(item.get("companies") or []) > 3:   # multi-fund notices are routine
            continue
        info = (item.get("companyInfo") or [{}])[0]
        candidate = candidates_by_symbol.get(symbol)
        if candidate is None:
            candidate = ScanCandidate(
                contract=_contract(_stable_id("asx", symbol), f"{symbol}.AX", "STK", "ASX", "AUD"),
                description=info.get("displayName", "") or symbol,
                research={"situation_type": "ASX: " + "; ".join(t for t in types)},
            )
            candidates_by_symbol[symbol] = candidate
        key = item.get("documentKey", "")
        candidate.headlines.append({
            "time": item.get("date", ""),
            "provider": "ASX announcements" + (" · price-sensitive" if item.get("isPriceSensitive") else ""),
            "article_id": key,
            "headline": f"{symbol}: {item.get('headline', '')}",
            "url": f"https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/{key}" if key else "",
            "matches": matches,
        })
        candidate.signals.add("ASX announcement")
    candidates = list(candidates_by_symbol.values())
    return candidates, warnings, _summary("ASX announcements" if items else None, len(candidates))


# ---------------------------------------------------------------- HKEX

def scan_hkex_announcements() -> tuple[list[ScanCandidate], list[str], dict]:
    """Event headline categories from HKEXnews' latest Main Board announcements (English)."""
    url = "https://www1.hkexnews.hk/ncms/json/eds/lcisehk1relsde_1.json"
    try:
        news = _http_get(url, _sec_user_agent()).json()["newsInfoLst"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        return [], [f"HKEXnews announcements request failed: {error}"], _summary(None, 0)
    candidates_by_code: dict[str, ScanCandidate] = {}
    for item in news:
        text = f"{item.get('lTxt', '')} {item.get('title', '')}"
        category = _classify(HKEX_RULES, text)
        stocks = item.get("stock") or []
        if not category or not stocks:
            continue
        code, name = stocks[0].get("sc", ""), stocks[0].get("sn", "")
        if not code:
            continue
        candidate = candidates_by_code.get(code)
        if candidate is None:
            candidate = ScanCandidate(
                contract=_contract(_stable_id("hkex", code), f"{code.lstrip('0') or code}.HK", "STK", "HKEX", "HKD"),
                description=name.title(),
            )
            candidates_by_code[code] = candidate
        tag = re.sub(r"^.*?\[|\]$", "", item.get("lTxt", ""))
        candidate.headlines.append({
            "time": item.get("relTime", ""),
            "provider": "HKEXnews",
            "article_id": str(item.get("newsId", "")),
            "headline": f"{name.title()}: {' '.join(str(item.get('title', '')).split())} [{tag}]",
            "url": "https://www1.hkexnews.hk" + item.get("webPath", ""),
            "matches": [(category, [tag])],
        })
        candidate.signals.add("HKEX announcement")
        candidate.research.setdefault("situation_type", "HKEX: " + tag)
    candidates = list(candidates_by_code.values())
    return candidates, [], _summary("HKEXnews", len(candidates))


# ---------------------------------------------------------------- Nasdaq halts

def scan_nasdaq_halts() -> tuple[list[ScanCandidate], list[str], dict]:
    url = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
    try:
        root = ET.fromstring(_http_get(url, _sec_user_agent()).content)
    except requests.RequestException as error:
        return [], [f"Nasdaq trade halts RSS request failed: {error}"], _summary(None, 0)
    except ET.ParseError as error:
        return [], [f"Nasdaq trade halts RSS was not valid XML: {error}"], _summary(None, 0)

    def field_text(item, name: str) -> str:
        node = next((child for child in item if child.tag.rsplit("}", 1)[-1] == name), None)
        return (node.text or "").strip() if node is not None else ""

    candidates = []
    for item in root.iter("item"):
        code = field_text(item, "ReasonCode")
        if code not in NASDAQ_HALT_CODES:
            continue
        symbol = field_text(item, "IssueSymbol")
        category, label = NASDAQ_HALT_CODES[code]
        halted = f"{field_text(item, 'HaltDate')} {field_text(item, 'HaltTime')}".strip()
        resumed = field_text(item, "ResumptionTradeTime")
        candidates.append(ScanCandidate(
            contract=_contract(_stable_id("halt", symbol, halted), symbol, "STK", field_text(item, "Market"), "USD"),
            description=field_text(item, "IssueName"),
            signals={f"Nasdaq halt {code}"},
            headlines=[{
                "time": halted,
                "provider": "Nasdaq trade halts",
                "article_id": f"{symbol}-{code}",
                "headline": f"{symbol}: {label} ({code})" + (f"; resumed {resumed}" if resumed else "; not yet resumed"),
                "url": "https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts",
                "matches": [(category, [code])],
            }],
            research={"key_dates_catalyst": f"Halted {halted} ET ({label})"},
        ))
    return candidates, [], _summary("Nasdaq trade halts", len(candidates))


# ---------------------------------------------------------------- Deal math

def deal_math(price: float, cash: float = 0.0, ratio: float = 0.0, acquirer_price: float = 0.0,
              cvr: float = 0.0, fx: float = 1.0, days: float = 0.0) -> dict:
    """Consideration value, gross spread and simple annualized return for a cash/stock/CVR offer.

    fx converts the offer currency into the target's quote currency (e.g. EUR offer, USD line: EURUSD).
    The CVR value is an analyst estimate, not a market price. Annualized = spread x 365 / days (simple),
    matching the workbook convention; it is None when days is not positive.
    """
    if price <= 0:
        raise ValueError("Target price must be positive.")
    value = (cash + ratio * acquirer_price + cvr) * fx
    spread = value / price - 1
    return {
        "consideration_value": value,
        "gross_spread": spread,
        "annualized": spread * 365 / days if days > 0 else None,
    }


# ---------------------------------------------------------------- SEC financial snapshot

FINANCIAL_CONCEPTS = {
    "shares_outstanding": (("dei", "EntityCommonStockSharesOutstanding"),),
    "cash": (("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
             ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")),
    "short_term_investments": (("us-gaap", "ShortTermInvestments"), ("us-gaap", "MarketableSecuritiesCurrent")),
    "total_debt_current": (("us-gaap", "LongTermDebtCurrent"), ("us-gaap", "DebtCurrent")),
    "total_debt_noncurrent": (("us-gaap", "LongTermDebtNoncurrent"), ("us-gaap", "LongTermDebt")),
    "revenue": (("us-gaap", "Revenues"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),),
    "net_income": (("us-gaap", "NetIncomeLoss"),),
    "operating_cash_flow": (("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),),
    "capex": (("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),),
    "total_assets": (("us-gaap", "Assets"),),
    "stockholders_equity": (("us-gaap", "StockholdersEquity"),),
}
FLOW_CONCEPTS = {"revenue", "operating_income", "net_income", "operating_cash_flow", "capex"}


def _latest_fact(facts: dict, concepts, annual: bool) -> dict | None:
    best = None
    for taxonomy, name in concepts:
        units = facts.get(taxonomy, {}).get(name, {}).get("units", {})
        for unit, values in units.items():
            if unit not in ("USD", "shares"):
                continue
            for value in values:
                if annual and value.get("fp") != "FY":
                    continue
                if annual and value.get("start") and value.get("end"):
                    length = (date.fromisoformat(value["end"]) - date.fromisoformat(value["start"])).days
                    if not 330 <= length <= 400:
                        continue
                key = (value.get("end", ""), value.get("filed", ""))
                if best is None or key > (best["end"], best["filed"]):
                    best = {"value": value["val"], "end": value.get("end", ""), "filed": value.get("filed", ""),
                            "form": value.get("form", ""), "concept": f"{taxonomy}:{name}", "accn": value.get("accn", "")}
    # Companies switch tags over time (e.g. Revenues -> RevenueFromContract...), so the newest
    # period across all candidate tags wins rather than the first tag with any data.
    return best


def financial_snapshot_from_facts(company_facts: dict, price: float | None = None) -> dict:
    facts = company_facts.get("facts", {})
    snapshot = {name: _latest_fact(facts, concepts, annual=name in FLOW_CONCEPTS)
                for name, concepts in FINANCIAL_CONCEPTS.items()}
    # A fact more than two years older than the newest one is a discontinued line item, not current data.
    newest = max((fact["end"] for fact in snapshot.values() if fact and fact["end"]), default="")
    if newest:
        cutoff = (date.fromisoformat(newest) - timedelta(days=730)).isoformat()
        snapshot = {name: fact if fact and fact["end"] >= cutoff else None for name, fact in snapshot.items()}

    def value(name):
        return snapshot[name]["value"] if snapshot.get(name) else None

    debt = [v for v in (value("total_debt_current"), value("total_debt_noncurrent")) if v is not None]
    cash = [v for v in (value("cash"), value("short_term_investments")) if v is not None]
    derived = {
        "total_debt": sum(debt) if debt else None,
        "cash_and_investments": sum(cash) if cash else None,
    }
    derived["net_cash"] = (derived["cash_and_investments"] - (derived["total_debt"] or 0)
                           if derived["cash_and_investments"] is not None else None)
    shares = value("shares_outstanding")
    derived["market_cap"] = price * shares if price and shares else None
    derived["enterprise_value"] = (derived["market_cap"] - derived["net_cash"]
                                   if derived["market_cap"] is not None and derived["net_cash"] is not None else None)
    derived["net_cash_per_share"] = derived["net_cash"] / shares if derived["net_cash"] is not None and shares else None
    fcf = (value("operating_cash_flow") - (value("capex") or 0)) if value("operating_cash_flow") is not None else None
    derived["free_cash_flow"] = fcf
    derived["fcf_yield"] = fcf / derived["market_cap"] if fcf is not None and derived["market_cap"] else None
    return {
        "entity": company_facts.get("entityName", ""),
        "cik": str(company_facts.get("cik", "")).zfill(10),
        "facts": snapshot,
        "derived": derived,
        "price_used": price,
        "source": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{str(company_facts.get('cik', '')).zfill(10)}.json",
    }


def sec_financial_snapshot(ticker: str = "", cik: str = "", price: float | None = None) -> dict:
    """Latest SEC XBRL facts for a US filer, looked up by CIK or ticker."""
    user_agent = _sec_user_agent()
    if not cik and ticker:
        record = _load_sec_tickers_by_symbol(user_agent).get(ticker.upper().split(".")[0])
        cik = record[0] if record else ""
    if not re.fullmatch(r"\d{1,10}", cik or ""):
        raise ValueError("No SEC filer found for this name; SEC financials cover US-registered filers only.")
    response = _http_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json", user_agent)
    return financial_snapshot_from_facts(response.json(), price)


GLOBAL_SOURCES = (
    scan_edgar_deal_forms,
    scan_uk_takeover_panel,
    scan_asx_announcements,
    scan_hkex_announcements,
    scan_nasdaq_halts,
)
