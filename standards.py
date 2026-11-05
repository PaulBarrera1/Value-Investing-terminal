"""Screening standards for the special-situations book.

Three gates decide what reaches the analyst:

1. Universe - MSCI developed markets only, judged by where the company is domiciled (its
   business address with the SEC, or its listing venue when the domicile is a holding-company
   jurisdiction such as Bermuda or Cayman). OTC/pink-sheet listings are out.
2. Hard catalyst - a dated, primary-source event that can be underwritten: a merger, scheme or
   tender offer document, a spin-off registration, an issuer tender, a rights issue or capital
   return, a liquidation. Keyword hits, trading halts, 13Ds, dealing disclosures and news-only
   leads are screened out with a reason.
3. Risk-adjusted hurdle - for a priced deal, the probability-weighted return
   p x gross spread + (1 - p) x break loss, annualized over a realistic (slipped) close, must
   clear HURDLE (25%). p comes from a transparent base-rate model of the filing text and is
   always shown next to the probability the market is implying, so the analyst can overrule it.

Every number here is a research estimate for an analyst to challenge, not a recommendation.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

HURDLE = float(os.environ.get("TRACKER_MIN_ANNUALIZED", "0.25"))

# Portfolio lens: the model sizes each deal so that a break costs at most BREAK_LOSS_BUDGET of NAV,
# capped by what can be bought at ADV_PARTICIPATION of daily volume over BUILD_DAYS and by
# MAX_POSITION of NAV. Shown for context; size does not screen anything out.
NAV_USD = float(os.environ.get("SPECIAL_SITS_NAV_USD", "1000000000"))
BREAK_LOSS_BUDGET = float(os.environ.get("BREAK_LOSS_BUDGET", "0.01"))
MAX_POSITION = 0.05
ADV_PARTICIPATION = 0.20
BUILD_DAYS = 10

# Deals that break usually break on bad news, so the stock lands below its market-adjusted
# pre-deal price. The model takes a further 10% off rather than assume a clean round trip.
BREAK_HAIRCUT = 0.10
# If the model is this much more confident than the market, the market probably knows something.
EDGE_VERIFY = 0.15
VERIFY_SPREAD = 0.40
# Break loss assumed when there is no usable pre-deal price.
FALLBACK_BREAK_LOSS = 0.30

# ---------------------------------------------------------------- universe

DM_COUNTRIES = {
    "United States", "Canada", "United Kingdom", "Ireland", "Germany", "France", "Netherlands",
    "Belgium", "Austria", "Finland", "Italy", "Spain", "Portugal", "Switzerland", "Sweden", "Norway",
    "Denmark", "Israel", "Japan", "Australia", "New Zealand", "Hong Kong", "Singapore",
}
# Holding-company jurisdictions: the domicile says nothing about the economy, so the listing decides.
OFFSHORE = {
    "Bermuda", "Cayman Islands", "British Virgin Islands", "Virgin Islands, British", "Jersey", "Guernsey",
    "Isle of Man", "Luxembourg", "Marshall Islands", "Gibraltar", "Curacao", "Bahamas", "Panama",
}
COUNTRY_ALIASES = {"england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
                   "great britain": "United Kingdom", "northern ireland": "United Kingdom"}
US_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR VI GU".split()
)
SUFFIX_COUNTRY = {
    ".AX": "Australia", ".HK": "Hong Kong", ".L": "United Kingdom", ".TO": "Canada", ".V": "Canada",
    ".PA": "France", ".DE": "Germany", ".F": "Germany", ".AS": "Netherlands", ".BR": "Belgium",
    ".VI": "Austria", ".HE": "Finland", ".MI": "Italy", ".MC": "Spain", ".LS": "Portugal",
    ".SW": "Switzerland", ".ST": "Sweden", ".OL": "Norway", ".CO": "Denmark", ".TA": "Israel",
    ".T": "Japan", ".NZ": "New Zealand", ".SI": "Singapore", ".IR": "Ireland",
}
ISIN_COUNTRY = {
    "US": "United States", "CA": "Canada", "GB": "United Kingdom", "IE": "Ireland", "DE": "Germany",
    "FR": "France", "NL": "Netherlands", "BE": "Belgium", "AT": "Austria", "FI": "Finland", "IT": "Italy",
    "ES": "Spain", "PT": "Portugal", "CH": "Switzerland", "SE": "Sweden", "NO": "Norway", "DK": "Denmark",
    "IL": "Israel", "JP": "Japan", "AU": "Australia", "NZ": "New Zealand", "HK": "Hong Kong", "SG": "Singapore",
    "BM": "Bermuda", "KY": "Cayman Islands", "VG": "British Virgin Islands", "JE": "Jersey", "GG": "Guernsey",
    "IM": "Isle of Man", "LU": "Luxembourg",
}
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
HK_NOTE = ("MSCI classes mainland-China companies listed in Hong Kong as emerging markets; "
           "confirm the domicile before underwriting.")


def edgar_country(code: str, description: str) -> str:
    """Country from an SEC address: a US state code, or a description such as 'Ontario, Canada'."""
    code, description = (code or "").strip().upper(), (description or "").strip()
    if code in US_STATES:
        return "United States"
    lowered = description.lower()
    if "antilles" in lowered:
        return "Curacao"
    for name in sorted(OFFSHORE | DM_COUNTRIES, key=len, reverse=True):
        if name.lower() in lowered:
            return name
    for alias, name in COUNTRY_ALIASES.items():
        if alias in lowered:
            return name
    return description.split(",")[-1].strip().title() if description else ""


def listing_country(symbol: str) -> str:
    symbol = (symbol or "").upper()
    if ISIN_RE.match(symbol):
        return ISIN_COUNTRY.get(symbol[:2], symbol[:2])
    suffix = "." + symbol.rsplit(".", 1)[1] if "." in symbol else ""
    return SUFFIX_COUNTRY.get(suffix, "")


def market_status(domicile: str = "", listing: str = "", otc_only: bool = False) -> tuple[bool, str]:
    """(in the universe, reason) under the MSCI developed-markets standard."""
    if otc_only:
        return False, "OTC/pink-sheet listing only"
    if domicile and domicile not in OFFSHORE:
        if domicile in DM_COUNTRIES:
            return True, f"Domicile {domicile}" + (f". {HK_NOTE}" if domicile == "Hong Kong" else "")
        return False, f"Domicile {domicile} is not an MSCI developed market"
    if listing in DM_COUNTRIES:
        basis = f"offshore domicile ({domicile})" if domicile else "domicile not stated"
        note = f". {HK_NOTE}" if listing == "Hong Kong" else ""
        return True, f"Listed in {listing}; {basis}{note}"
    if domicile or listing:
        return False, f"Domicile {domicile or 'unknown'}, listing {listing or 'unknown'}: not an MSCI developed market"
    return False, "Domicile and listing unknown"


class SecProfiles:
    """Domicile and listing venues from the SEC submissions file, cached per CIK and rate limited."""

    def __init__(self, user_agent: str):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.cache: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.last = 0.0

    def get(self, cik: str) -> dict:
        cik = str(cik).lstrip("0")
        if cik in self.cache:
            return self.cache[cik]
        with self.lock:
            wait = self.last + 0.13 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
        try:
            response = self.session.get(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json", timeout=20)
            response.raise_for_status()
            profile = profile_from_submissions(response.json())
        except (requests.RequestException, ValueError, KeyError, TypeError):
            profile = {"domicile": "", "exchanges": [], "submissions": None}
        self.cache[cik] = profile
        return profile


def profile_from_submissions(submissions: dict) -> dict:
    business = (submissions.get("addresses") or {}).get("business") or {}
    domicile = edgar_country(business.get("stateOrCountry", ""), business.get("stateOrCountryDescription", ""))
    if not domicile:
        domicile = edgar_country(submissions.get("stateOfIncorporation", ""),
                                 submissions.get("stateOfIncorporationDescription", ""))
    return {"domicile": domicile, "exchanges": [e for e in submissions.get("exchanges") or [] if e],
            "submissions": submissions}


def sec_market_status(profile: dict) -> tuple[bool, str]:
    exchanges = profile.get("exchanges") or []
    otc_only = bool(exchanges) and all(e.upper() == "OTC" for e in exchanges)
    # A US-exchange listing is the fallback for offshore holding companies.
    return market_status(profile.get("domicile", ""), "United States" if exchanges and not otc_only else "", otc_only)


# ---------------------------------------------------------------- hard catalysts (scanner leads)

MERGER, ISSUER_TENDER, SPIN, RIGHTS, LIQUIDATION = (
    "Merger / takeover", "Issuer tender offer", "Spin-off / demerger", "Rights issue / capital return",
    "Liquidation / restructuring")
# Target-side SEC documents only: 425s and S-4s are usually filed by the buyer.
HARD_SEC_FORMS = {
    "SC TO-T": MERGER, "SC TO-T/A": MERGER, "SC 14D9": MERGER, "SC 14D9/A": MERGER,
    "SC 13E3": MERGER, "SC 13E3/A": MERGER, "PREM14A": MERGER, "DEFM14A": MERGER,
    "PREM14C": MERGER, "DEFM14C": MERGER, "SC TO-I": ISSUER_TENDER, "SC TO-I/A": ISSUER_TENDER,
    "10-12B": SPIN, "10-12B/A": SPIN, "10-12G": SPIN, "10-12G/A": SPIN,
}
SEC_REJECT_REASONS = {
    "425": "Business-combination filing, usually the buyer's (US mergers are priced on the deal sheet)",
    "S-4": "Stock-deal registration, filed by the buyer (US mergers are priced on the deal sheet)",
    "S-4/A": "Stock-deal registration, filed by the buyer (US mergers are priced on the deal sheet)",
    "F-4": "Stock-deal registration, filed by the buyer (US mergers are priced on the deal sheet)",
    "F-4/A": "Stock-deal registration, filed by the buyer (US mergers are priced on the deal sheet)",
    "25-NSE": "Delisting notice: the event has already happened",
    "15-12B": "Deregistration: the event has already happened",
    "15-12G": "Deregistration: the event has already happened",
}
ASX_HARD = (
    (re.compile(r"scheme of arrangement|bidder'?s statement|target'?s statement|off-market bid|takeover|"
                r"withdrawal of offer", re.I), MERGER),
    (re.compile(r"demerger", re.I), SPIN),
    (re.compile(r"off-market buy-?back", re.I), ISSUER_TENDER),
    (re.compile(r"renounceable|entitlement|pro rata|return of capital|capital reduction", re.I), RIGHTS),
    (re.compile(r"^(?!company administration).*(administrat|receiver|liquidat)", re.I), LIQUIDATION),
)
HKEX_REJECT = re.compile(r"dealing disclosure|results? of (?:the )?(?:rights issue|open offer)|allotment results|"
                         r"major transaction|very substantial|discloseable|connected transaction", re.I)
HKEX_HARD = (
    (re.compile(r"privati[sz]ation|scheme of arrangement|general offer|composite (?:offer )?document|"
                r"offer(?:ee|or) company|rule 3\.[57]", re.I), MERGER),
    (re.compile(r"spin-?off|separate listing|distribution in specie", re.I), SPIN),
    (re.compile(r"repurchase offer|off-market (?:share )?buy-?back", re.I), ISSUER_TENDER),
    (re.compile(r"rights issue|open offer", re.I), RIGHTS),
    (re.compile(r"winding[- ]up|liquidat|provisional liquidator", re.I), LIQUIDATION),
)


def _match(rules, text: str) -> str | None:
    return next((kind for pattern, kind in rules if pattern.search(text)), None)


def headline_catalyst(item: dict) -> tuple[str | None, str]:
    """(hard-catalyst kind or None, reason) for one headline from any scanner source."""
    provider = item.get("provider", "")
    labels = " ".join(" ".join(terms) for _, terms in item.get("matches", []))
    text = f"{item.get('headline', '')} {labels}"
    if provider.startswith("SEC EDGAR · "):
        form = provider.split(" · ", 1)[1].strip()
        if form.endswith("(bidder)"):
            return None, "Bidder's side of a tender offer or going-private filing"
        if form in HARD_SEC_FORMS:
            return HARD_SEC_FORMS[form], f"SEC {form}"
        if form.upper().startswith(("SC 13D", "SCHEDULE 13D")):
            return None, "13D stake: activism is a soft catalyst with no dated event"
        return None, SEC_REJECT_REASONS.get(form, f"Keyword match in an SEC {form}, not a deal document")
    if provider == "UK Takeover Panel":
        return MERGER, "UK Takeover Panel offer period"
    if provider.startswith("ASX announcements"):
        kind = _match(ASX_HARD, labels)
        return (kind, f"ASX {labels}") if kind else (None, f"ASX {labels or 'announcement'}: not an event to underwrite")
    if provider == "HKEXnews":
        if HKEX_REJECT.search(text):
            return None, "HKEX dealing disclosure, completed issue or own-account transaction"
        kind = _match(HKEX_HARD, text)
        return (kind, f"HKEX {labels}") if kind else (None, f"HKEX {labels or 'announcement'}: not an event to underwrite")
    if provider == "Nasdaq trade halts":
        return None, "Trading halt: no event to underwrite until the news is out"
    return None, "Secondary source only (news/vendor): no primary filing"


def screen_candidate(candidate) -> tuple[str | None, str]:
    """Best hard-catalyst kind among a lead's headlines, or (None, the first reason it failed)."""
    reasons = []
    for item in candidate.headlines:
        kind, reason = headline_catalyst(item)
        if kind:
            return kind, reason
        reasons.append(reason)
    return None, (reasons[0] if reasons else "No event headline")


# ---------------------------------------------------------------- deal risk model

@dataclass(frozen=True)
class RiskRule:
    key: str
    label: str
    pattern: re.Pattern
    p_adjust: float          # added to the completion probability
    slip_days: int           # regulatory reviews run in parallel, so the longest slip applies
    kill: str = ""           # the event that would end the thesis
    negated_by: re.Pattern | None = None


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


RISK_RULES = (
    RiskRule("financing", "Financing condition", _rx(r"financing condition|conditioned (?:up)?on (?:the )?(?:receipt of )?"
             r"(?:debt )?financing|subject to (?:the )?(?:receipt of |obtaining )?(?:debt )?financing"), -0.08, 0,
             "Financing commitments pulled or credit markets shut", _rx(r"not (?:subject to|conditioned on|contingent on) "
             r"(?:a |any )?(?:financing|the receipt of financing)|no financing condition")),
    RiskRule("second_request", "HSR second request", _rx(r"second request"), -0.08, 120,
             "FTC/DOJ sues to block, or demands remedies the buyer refuses"),
    RiskRule("cfius", "CFIUS review", _rx(r"\bCFIUS\b|Committee on Foreign Investment"), -0.04, 60,
             "CFIUS refers the deal to the President or requires mitigation the buyer will not accept"),
    RiskRule("samr", "China SAMR approval", _rx(r"\bSAMR\b|State Administration for Market Regulation|"
             r"PRC (?:anti-?trust|anti-?monopoly)|China'?s? anti-?(?:trust|monopoly)"), -0.08, 90,
             "SAMR review runs past the outside date (US-China tension risk)"),
    # "Phase 2" alone would catch every biotech trial; only a review or investigation counts.
    RiskRule("eu_phase2", "Phase II/2 competition review", _rx(r"phase (?:II|2) (?:review|investigation|inquiry|proceedings)|"
             r"in-depth (?:investigation|review)"), -0.10, 120,
             "Commission prohibition or remedies beyond the agreed limit"),
    RiskRule("eu", "European Commission approval", _rx(r"European Commission"), -0.02, 45,
             "Commission opens a Phase II investigation"),
    RiskRule("cma", "UK CMA review", _rx(r"\bCMA\b|Competition and Markets Authority"), -0.03, 60,
             "CMA refers to Phase 2 or blocks"),
    RiskRule("bank", "Bank regulatory approval", _rx(r"Federal Reserve|Comptroller of the Currency|\bOCC\b|\bFDIC\b"),
             -0.02, 45, "Bank regulator delays approval past the outside date"),
    RiskRule("utility", "Utility/telecom/insurance regulator", _rx(r"\bFCC\b|Federal Communications Commission|\bFERC\b|"
             r"public (?:utility|utilities|service) commission|insurance (?:department|commissioner|regulator)"), -0.03, 90,
             "State or sector regulator conditions or rejects the deal"),
    # Not "unsolicited proposal": every friendly merger agreement's no-shop clause says it.
    RiskRule("hostile", "Hostile / board recommends rejecting", _rx(r"\bhostile (?:bid|offer|tender|takeover)|"
             r"recommends? that (?:the )?(?:stockholders|shareholders) reject"), -0.25, 60,
             "Target board refuses and shareholders do not tender"),
    RiskRule("vote", "Target shareholder vote required", _rx(r"(?:stockholder|shareholder) approval|approval of (?:the )?"
             r"(?:company'?s )?(?:stockholders|shareholders)|special meeting of (?:stockholders|shareholders)"), -0.01, 0,
             "Shareholders vote it down (watch for activist opposition)"),
    RiskRule("support", "Voting/support agreements signed", _rx(r"voting (?:and support )?agreements?|support agreements?|"
             r"irrevocable undertakings?"), 0.02, 0),
    RiskRule("reverse_fee", "Reverse termination fee", _rx(r"reverse termination fee"), 0.02, 0),
    RiskRule("no_financing", "No financing condition", _rx(r"not (?:subject to|conditioned on|contingent on) (?:a |any )?"
             r"(?:financing|the receipt of financing)|no financing condition"), 0.01, 0),
    RiskRule("go_shop", "Go-shop period (bump optionality)", _rx(r"go-shop"), 0.0, 0),
    RiskRule("minority", "Controlling holder / majority-of-minority", _rx(r"majority of the minority|majority-of-the-minority|"
             r"special committee"), 0.0, 0, "Minority holders reject the price (bump or walk)"),
)
# The document announces the deal is off. Only announcement language: proxies are full of "may be
# terminated", "could have terminated the merger agreement" and, if the merger fails, "X will remain
# an independent public company".
DEAL_OFF_RE = re.compile(r"to remain an independent(?: public)? company (?:following|after)|mutually agreed to terminate|"
                         r"announc\w* (?:the )?termination of (?:the|its) (?:merger|transaction|arrangement) agreement|"
                         r"(?:offer|bid) (?:has been|was) withdrawn", re.I)
OUTSIDE_DATE_RE = re.compile(
    r"(?:outside|end|termination) date[^.;]{0,100}?((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December) \d{1,2}, 20\d\d)", re.I)
TERMINATION_FEE_RE = re.compile(r"termination fee of (?:approximately )?\$\s?([\d.,]+)\s*(million|billion)", re.I)
BASE_PROBABILITY = {"tender": 0.94, "merger": 0.92}
BASE_SLIP_DAYS = {"tender": 10, "merger": 30}


def risk_flags(text: str) -> list[dict]:
    """Deal conditions and regulators named in the filing, each with the sentence that names it."""
    flags = []
    for rule in RISK_RULES:
        match = rule.pattern.search(text)
        if not match or (rule.negated_by and rule.negated_by.search(text)):
            continue
        start = max(0, text.rfind(".", 0, match.start()) + 1)
        end = text.find(".", match.end())
        evidence = text[start:end if end > 0 else match.end() + 160].strip()[:240]
        flags.append({"key": rule.key, "label": rule.label, "p_adjust": rule.p_adjust,
                      "slip_days": rule.slip_days, "kill": rule.kill, "evidence": evidence})
    off = DEAL_OFF_RE.search(text)
    if off:
        flags.append({"key": "deal_off", "label": "Filing says the deal is off", "p_adjust": 0.0, "slip_days": 0,
                      "kill": "", "evidence": text[max(0, off.start() - 80):off.end() + 80].strip()})
    if any(f["key"] == "eu_phase2" for f in flags):
        flags = [f for f in flags if f["key"] != "eu"]
    outside = OUTSIDE_DATE_RE.search(text)
    if outside:
        flags.append({"key": "outside_date", "label": f"Outside date {outside.group(1)}", "p_adjust": 0.0,
                      "slip_days": 0, "kill": f"Approvals not in hand by the outside date ({outside.group(1)})",
                      "evidence": outside.group(0)[:240]})
    fee = TERMINATION_FEE_RE.search(text)
    if fee:
        flags.append({"key": "termination_fee", "label": f"Termination fee ${fee.group(1)} {fee.group(2)}",
                      "p_adjust": 0.0, "slip_days": 0, "kill": "", "evidence": fee.group(0)})
    return flags


def completion_probability(tender: bool, flags: list[dict], vote_held: bool = False) -> tuple[float, str]:
    """Base rate for signed, friendly deals, moved by each flag; returns (p, how it was built)."""
    kind = "tender" if tender else "merger"
    p = BASE_PROBABILITY[kind]
    steps = [f"base {p:.0%} ({kind})"]
    for flag in flags:
        if flag["key"] == "vote" and vote_held:
            continue
        if flag["p_adjust"]:
            p += flag["p_adjust"]
            steps.append(f"{flag['p_adjust']:+.0%} {flag['label']}")
    if vote_held:
        p += 0.02
        steps.append("+2% shareholder vote held")
    p = min(0.97, max(0.05, p))
    return p, "; ".join(steps)


def evaluate_deal(*, price: float, value: float, days_guided: int, tender: bool, flags: list[dict],
                  undisturbed: float | None = None, market_drift: float = 1.0, vote_held: bool = False,
                  stock_deal: bool = False, cvr: bool = False, adv_usd: float | None = None,
                  pulled: date | None = None, probability: float | None = None) -> dict:
    """Risk-adjusted view of one priced deal: the hurdle test plus the ways the thesis can be wrong."""
    pulled = pulled or date.today()
    gross = value / price - 1
    if undisturbed:
        break_price = undisturbed * market_drift * (1 - BREAK_HAIRCUT)
        break_basis = (f"pre-deal {undisturbed:.2f} x market move {market_drift - 1:+.1%} "
                       f"less {BREAK_HAIRCUT:.0%} for breaking on bad news")
    else:
        break_price = price * (1 - FALLBACK_BREAK_LOSS)
        break_basis = f"no pre-deal price; assumes a {FALLBACK_BREAK_LOSS:.0%} fall"
    # A target already below its market-adjusted pre-deal price is not pricing a deal: the market
    # doubts it, or the terms were misread. Do not let a stale anchor turn a break into a gain.
    below_anchor = break_price >= price
    if below_anchor:
        break_basis = (f"trades below its market-adjusted pre-deal price ({break_price:.2f}); "
                       f"assumes a {FALLBACK_BREAK_LOSS:.0%} fall from here")
        break_price = price * (1 - FALLBACK_BREAK_LOSS)
    loss = break_price / price - 1
    model_p, p_basis = completion_probability(tender, flags, vote_held)
    p = model_p if probability is None else probability
    slip = BASE_SLIP_DAYS["tender" if tender else "merger"] + max([f["slip_days"] for f in flags] + [0])
    days = max(days_guided, 1) + slip
    expected = p * gross + (1 - p) * loss
    annualized = expected * 365 / days
    # Undefined when the stock already trades at or below the modelled break price.
    market_p = -loss / (gross - loss) if gross > loss and loss < 0 else None
    needed = (HURDLE * days / 365 - loss) / (gross - loss) if gross > loss else None
    latest_days = int(expected * 365 / HURDLE) if expected > 0 else None
    clears = annualized >= HURDLE
    edge = p - market_p if market_p is not None else None

    if not clears:
        if needed is not None and needed > 1:
            verdict = "Below hurdle: cannot clear even if completion is certain"
        else:
            verdict = f"Below hurdle: needs p >= {needed:.0%} (model {p:.0%})" if needed is not None else "Below hurdle"
    elif below_anchor or gross > VERIFY_SPREAD or (edge is not None and edge > EDGE_VERIFY):
        verdict = "Clears, but verify: market disagrees"
    else:
        verdict = "Clears risk-adjusted hurdle"

    size_risk = NAV_USD * BREAK_LOSS_BUDGET / abs(loss) if loss < 0 else NAV_USD * MAX_POSITION
    capacity = adv_usd * ADV_PARTICIPATION * BUILD_DAYS if adv_usd else None
    size = min(x for x in (size_risk, capacity, NAV_USD * MAX_POSITION) if x is not None)

    positives = [f["label"] for f in flags if f["p_adjust"] > 0 or f["key"] == "go_shop"]
    negatives = [f["label"] for f in flags if f["p_adjust"] < 0]
    bull = (f"{gross:+.1%} to {value:.2f} in ~{days}d; model p {p:.0%} vs market-implied "
            f"{market_p:.0%}" if market_p is not None else f"{gross:+.1%} to {value:.2f} in ~{days}d")
    if positives:
        bull += ". " + ", ".join(positives)
    bear = f"Break to ~{break_price:.2f} ({loss:+.1%}): risks {abs(loss) / gross:.1f}x the spread" if gross > 0 else \
        f"Break to ~{break_price:.2f} ({loss:+.1%}) with no spread to earn"
    if negatives:
        bear += ". " + ", ".join(negatives)
    kills = ["Termination or a target 8-K Item 1.02", "Spread widens past the market-implied break-even (new information)"]
    kills += [f["kill"] for f in flags if f["kill"]]
    if tender:
        kills.append("Minimum tender condition missed or repeated extensions")
    if stock_deal:
        kills.append("Acquirer shares cannot be borrowed to hedge, or the borrow fee eats the spread")
    wrong = [f"Break price is a model ({break_basis}); a target that deteriorated since signing falls further",
             f"Close slips: the hurdle fails if the deal closes after {pulled + timedelta(days=latest_days):%d %b %Y}"
             if latest_days else "Close timing: no positive expected return to protect",
             "Probability comes from filing text: it cannot see litigation, politics, an interloper or a shareholder revolt"]
    if below_anchor:
        wrong.append("Trades below its pre-deal price: the market doubts the deal, or the terms here are misread - "
                     "check the source filing first")
    if edge is not None and edge > 0.05:
        wrong.append(f"Model is {edge:.0%} more confident than the market: find what the market is pricing before sizing")
    if stock_deal:
        wrong.append("Stock consideration: return depends on the hedge; unhedged, the spread moves with the acquirer")
    if cvr:
        wrong.append("CVR valued at zero: upside only if milestones are met")
    return {
        "gross_spread": gross, "break_price": break_price, "break_loss": loss, "break_basis": break_basis,
        "model_probability": model_p, "probability": p, "probability_basis": p_basis,
        "market_probability": market_p, "edge": edge, "days_expected": days, "slip_days": slip,
        "expected_return": expected, "risk_adjusted_annualized": annualized, "clears_hurdle": clears,
        "probability_needed": needed, "latest_close": (pulled + timedelta(days=latest_days)).isoformat() if latest_days else "",
        "reward_to_risk": gross / abs(loss) if loss < 0 else None, "verdict": verdict,
        "size_risk_budget": size_risk, "capacity": capacity, "model_size": size,
        "bull": bull, "bear": bear, "kill_criteria": kills, "what_could_be_wrong": wrong,
        "flags": flags,
    }
