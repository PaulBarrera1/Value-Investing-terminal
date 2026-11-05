# Value-Investing-terminal
![Terminal Dashboard](dashboard.png)
Automated Python financial terminal for quantitative analysis of Special Situations and Value Investing.
 Value Investing & Special Situations Terminal

Overview
An automated Python-based financial terminal designed to streamline due diligence for **Value Investing** and **Special Situations** (Spin-offs, M&A, Restructurings, and Share Buybacks). It fetches market data, computes financial ratios, and builds an interactive institutional HTML dashboard.

 Key Features
* **Multi-Source Ingestion:** Blends 10-year historical fundamental data via Alpha Vantage API with market price trends using `yfinance`.
* **Automated Dashboard Generation:** Converts raw financial data into a clean, local HTML dashboard opened automatically in your browser.
* **Structured Financial Analysis:**
  * Interactive Price Charts with flexible timeframes.
  * Core Financials (Income Statement, Balance Sheet, Cash Flow Statement).
  * Institutional Valuation Ratios.
  * Earnings Call Transcripts for qualitative & legal risk assessment.
* **Event-Driven Idea Generator:** A special-situations playbook, source-backed candidate form, and ranked watchlist with catalyst dates, scenario returns, and downside cases. The watchlist is saved locally in the browser.

The single-company dashboard's idea generator remains a manual research workflow, now alongside the live global event scanner in the integrated terminal's sidebar. The scanner discovers leads from market scanners, filings, and subscribed news. Verify each catalyst and primary source yourself; scores and analyst-entered estimates are not investment recommendations or forecasts.

 Tech Stack
* **Language:** Python 3.x
* **Financial Data:** `yfinance`, `alpha_vantage`
* **Data Processing & Visualization:** `pandas`, `plotly`, `HTML/CSS`

Architecture & Philosophy
Built around a quantitative investment framework to eliminate manual data collection and accelerate fundamental analysis. Designed for scalable data modeling and institutional-grade visualization.

 Quick Start
1. Clone this repository.
2. Install required packages: `pip install yfinance pandas numpy requests alpha_vantage plotly ib_async openpyxl`
3. If using the single-company Alpha Vantage data, set `ALPHAVANTAGE_API_KEY` in your environment.
4. Run `python terminal_pro.py`. Choose **1** for the integrated terminal (single-company analysis plus the live global event scanner in one sidebar/navigation), **2** for the scanner by itself, or **3** for a one-time IBKR scan/export.
5. In integrated mode, the terminal starts or reuses the local scanner service and embeds it as a full-height Global Event Scanner section alongside the company dashboard, financial statements, valuation, and research idea generator. The integrated process remains open to serve the scanner; close the terminal console to stop it. The scanner defaults to all sources and keeps public results if IBKR is unavailable. Configure provider credentials locally to enable optional vendor feeds; otherwise they are skipped with explicit warnings.

### Screening standards

Both the dashboard scan and the deal sheet apply the same standards ([standards.py](standards.py)), so what reaches you is a short list you can underwrite rather than every keyword hit:

1. **Universe: MSCI developed markets.** US, Canada, UK, Ireland, eurozone DM, Switzerland, the Nordics, Israel, Japan, Australia, New Zealand, Hong Kong and Singapore, judged by where the company is domiciled (its SEC business address, or the ISIN for UK Takeover Panel names). Holding-company domiciles such as Bermuda, Cayman, Jersey and Guernsey fall back to the listing venue. OTC-only listings are out. Hong Kong listings stay in with a note, because MSCI classes mainland-China companies listed there as emerging markets.
2. **Hard catalyst from a primary source.** Kept: target-side SEC deal documents (merger proxies, SC 14D9, SC TO-T, SC 13E3), issuer tender offers (SC TO-I), Form 10 spin-offs, UK offer periods, ASX schemes, bids, demergers, renounceable issues and capital returns, HKEX privatisations, offers, spin-offs and rights issues, and liquidations. Screened out, each with its reason: news-only and vendor leads, keyword hits in 8-Ks, the buyer's 425/S-4 filings, the bidder's copy of a tender offer, 13D stakes (soft catalysts with no dated event), delistings that have already happened, trading halts, HKEX dealing disclosures and completed issues, and ASX asset deals and placements.
3. **25% risk-adjusted hurdle.** A priced deal must earn at least 25% a year after weighting for failure: (p × gross spread + (1 − p) × break loss) × 365 / days. Three deliberately conservative inputs:
   * **p, the completion probability**, starts from base rates for signed friendly deals (92% merger, 94% tender) and moves with each condition or regulator named in the terms document: financing condition, HSR second request, CFIUS, China SAMR, EU/CMA review, bank/utility/insurance regulators, a hostile approach, a pending vote, support agreements, a reverse termination fee. Every adjustment is listed with the sentence that triggered it.
   * **Break price** = the pre-announcement close moved with the S&P 500 since, less 10%, because deals usually break on bad news.
   * **Days** = the guided close plus a slip (30 days for mergers, 10 for tenders, plus the slowest regulator's typical delay).

Every surviving deal also shows the market-implied probability, the probability the hurdle needs, the latest close date that still clears it, a reward-to-risk ratio, kill criteria, and a **What could make this wrong** list. Deals that clear only because the model is much more confident than the market (by more than 15 points), or that have a spread over 40%, are put in a separate **verify** tier: a large disagreement with the market usually means it knows something the filing text does not show. A **Model size** column sizes each deal so that a break costs at most 1% of NAV (`BREAK_LOSS_BUDGET`) on a $1B book (`SPECIAL_SITS_NAV_USD`), capped by 10 days at 20% of average dollar volume and 5% of NAV. It is context for the portfolio manager, not a filter.

The probabilities are a model of filing text. They cannot see litigation, politics, an interloper or a shareholder revolt, so overwrite them in the Analysis tab or the calculator when you know better.

### Event-Driven Market Scan

The dashboard supports seven manually selected source modes: **IBKR Gateway**, **SEC EDGAR**, **Global regulators and exchanges**, **Online news + Tiingo/Benzinga + exchange RSS**, **Dow Jones Factiva**, **Public sources**, and **All sources**.

#### Global regulators and exchanges (free, no API keys)

`global_event_sources.py` reads primary-source publications directly. Public sources and All sources include all of them; SEC EDGAR mode adds the deal-form index.

| Source | What it surfaces |
|---|---|
| SEC EDGAR daily form index (last 3 business days) | Third-party and issuer tender offers (SC TO-T/TO-I, 14D-9), going-private (13E-3), merger proxies (PREM/DEFM14A/C), 425s, S-4/F-4, exchange-listed Form 10 spin-offs (10-12B), Schedule 13D, 25-NSE delistings and deregistrations. Fund and exchange filers are dropped; each filing is kept only for parties with a listed ticker, except pre-listing SpinCos. |
| [UK Takeover Panel disclosure table](https://www.thetakeoverpanel.org.uk/disclosure/disclosure-table) | Every UK company in an offer period, with offeror(s), offer-period start and Rule 2.6 "put up or shut up" deadline prefilled into the research row. |
| ASX market announcements | Takeovers, schemes of arrangement, asset acquisitions/disposals, demergers, buy-backs (not daily notices), capital returns, entitlement offers, halts, suspensions and administrations. |
| HKEXnews latest announcements (English) | Privatisations, general offers and Takeovers Code announcements, major and very substantial transactions, spin-offs, rights and open offers, halts, delistings and winding-up. |
| [Nasdaq trade halts](https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts) | News-pending (T1–T3), corporate-action, additional-information and regulatory halts; volatility pauses are excluded. |

These are leads from regulator and exchange notices, not verified deal terms. A feed that fails or changes layout is reported in the warnings, and the rest of the scan continues. Japan (EDINET), Korea (DART) and Canada (SEDAR+) are not yet included: EDINET and DART need a free registration key, and SEDAR+ has no public API.

#### Deal calculator and SEC financials

Open any lead's research detail to find:

* **Deal calculator**: enter cash per share, stock ratio and acquirer price, a CVR estimate, an FX rate, days to close, your completion probability and the break price if the deal fails. Consideration value = (cash + ratio × acquirer price + CVR estimate) × FX; annualized = gross spread × 365 / days, the simple convention used in the special situations workbook. Risk-adjusted annualized = (p × spread + (1 − p) × break loss) × 365 / days; the calculator also shows the probability the market is implying and the probability the hurdle needs. **Apply to workbook row** fills consideration value, gross spread, annualized and deal terms, plus the EV analysis upside, downside, probability and days.
* **Pull date**: every scan is stamped with the time its data was pulled. It appears in the dashboard banner, the page title, the static report, the workbook's Run Summary and the exported filename. Each research row has a read-only **Date pulled** column holding the date the lead was first pulled; it is kept across rescans. With an **Expected close date**, days to close and annualized returns are counted from that row's pull date.
* **Tracking workbook**: applying the calculator to a deal at or above 25% **risk-adjusted** annualized (`TRACKER_MIN_ANNUALIZED`, default `0.25`) adds it to `reports\special_situations_tracker.xlsx`; without a probability and break price nothing is sent. There is one row per lead, holding the date added, date pulled, last updated date, status, terms, price at pull, consideration, spread, expected close, days, gross annualized return, catalyst, sources, completion probability, break price and risk-adjusted annualized return (the last three columns are added to an existing tracker automatically). A tracked deal that later falls below the threshold stays in the workbook and is marked `Below threshold since <date>`; a deal that never clears is not added. The threshold is applied by the local server, not the browser. Close the workbook in Excel before applying, or the update is refused with a message saying so.
* **Load SEC financials**: latest SEC XBRL facts for US filers (shares outstanding, cash, investments, debt, revenue, operating income, net income, operating cash flow and capex), with market cap, net cash, enterprise value, net cash per share, free cash flow and FCF yield derived from the scanner price. Each fact shows its period end, form and XBRL concept; facts more than two years older than the newest are treated as discontinued and omitted. **All sources** is the default: it scans IBKR and also runs SEC EDGAR and online/RSS news, retaining available results if IBKR or another provider is unavailable. Tiingo News ([API endpoint](https://api.tiingo.com/tiingo/news)) and the [Benzinga News API](https://docs.benzinga.com/api-reference/news-api/get-news-items.md) are included when their API keys are configured locally; without them, the public RSS feeds continue and the dashboard explains which optional feeds were skipped. Factiva is included when All sources is selected; it requires Dow Jones' [Factiva Retrieval API](https://github.com/dowjones/factiva-retrievalapi-demo), with API access and entitlements separate from an app.dowjones.com website login. Configure the API client ID, service-account username, and password in local environment variables before using Factiva. Factiva requests may count against account API usage or subscription limits. Each provider's coverage and issuer mapping remain subject to its own limitations.

The **Test GS Marquee connection** control separately verifies production Marquee OAuth access against `/v1/users/self`. It is an account connectivity check only: Marquee is not currently used for scanner leads or price enrichment. The check requests only the `read_user_profile` scope and does not display or store the API token or profile response.

SEC EDGAR mode searches recent event-related 8-K/6-K/10-K/10-Q filings and delisting-related Form 25/25-NSE/15 filings, mapping issuers to SEC's ticker directory when possible; it is not a complete market/security universe. Event matching includes delisting/listing-status warnings, symbol/name/CUSIP changes, insider-transaction language, and short-interest/positioning language. Online-news mode includes the [Nasdaq Trader corporate-actions RSS feed](https://www.nasdaqtrader.com/Rss.aspx?feed=currentheadlines&categorylist=105), general Google News RSS search, and Google News RSS searches scoped to OTC Markets and NYSE domains. Optional Tiingo and Benzinga news APIs augment these feeds when configured. In **All sources**, **Public sources**, and **Online news** modes, configured Benzinga/Quiver credentials also enable recent Form 4 insider leads; Benzinga adds FINRA-reported short-interest and fundamentals context for up to 50 discovered stock symbols per scan. These are credential- and entitlement-gated and report data only when the vendor returns it. Benzinga's FINRA-reported short-interest snapshots are periodic settlement-date data, not daily short-sale volume. The Quiver integration uses its documented live-insiders endpoint for recent transactions. No standalone FINRA API adapter for the LEAN `FINRAShortInterest` dataset or Brain filing-language metrics adapter is claimed: the LEAN examples are not direct endpoint specifications, and the public schemas/access for those feeds could not be verified. We could not verify a working official OTC Markets or NYSE event RSS endpoint (the NYSE RSS URLs tested returned 404), so the focused Google News RSS queries are fallbacks, not exchange-issued feeds. News cards show the company inferred from the headline when available, mark unresolved tickers as **Not verified**, and display the exact matched event categories and phrases. **Online-news tickers** are treated as verified only when they map uniquely to the SEC issuer directory and the headline's company name matches; otherwise the row remains a news lead and the generated `NEWS-*` value is shown only as a lead reference. Vendor-provided transaction symbols come from the corresponding data provider. Online headlines are secondary-source leads, so confirm reported events against issuer filings or other primary sources. Factiva search uses a recent date range and a fixed event-driven query; article tickers are only mapped when explicitly shown in the headline, and Factiva results are still leads for analyst verification. The scanner does not fabricate deal terms or valuation. SEC fair access requires an identifying User-Agent with contact details; configure `SEC_USER_AGENT` as described below.

Choose **2** for the loopback-only browser dashboard. It opens even if a selected source is unavailable and shows connection/request warnings with a retry button. Scans are manual; changing sources does not start automatic polling. The research tab follows the attached workbook's Special Situations and probability-weighted EV columns. Click a filtered name in the scanner feed to open its focused detail with editable fields from both workbook sections. When IBKR is selected and a usable quote is returned, Price uses a one-shot snapshot (last trade or bid/ask midpoint); capture time and source are recorded in Sources. Public sources do not provide an IBKR quote, so Price remains blank unless supplied by another quote source. Deal terms, counterparties, break cases, probabilities, and valuation fields remain analyst-research inputs. Edits are saved in that browser's local storage. **Export research workbook** downloads the research tabs with event matches, scanner contracts, and run summary; successful scans are archived in `reports\`.

On Windows, double-click **`Open Special Situations Dashboard.cmd`** to start the same dashboard without the menu. It starts on **Public sources** (free regulator, exchange and news feeds, which load in a few minutes); choose IBKR or All sources from the dashboard when needed, as the full IBKR scanner sweep takes over an hour. It uses the live IB Gateway port `4001` unless `IBKR_PORT` is already set. Close its window to stop the dashboard.

### Finished deal sheet

Double-click **`Build Deal Sheet.cmd`** (or run `py deal_sheet.py --open`) for a finished workbook, `reports\special_situations_YYYY-MM-DD.xlsx`, in the layout of the analyst sheet. It takes about 4 minutes:

- Finds pending US-listed mergers and tender offers in SEC EDGAR filings from the last 270 days (`--days` changes this) and drops deals that have closed or been delisted.
- Reads the terms from the filings: cash per share, exchange ratio and acquirer, CVRs, and the guided close or tender expiry.
- Prices the target and acquirer (Yahoo Finance), with 20-day dollar volume and the S&P 500's move since the announcement, and applies the [screening standards](#screening-standards).
- **Special Situations** tab: only deals that clear the 25% risk-adjusted hurdle, in two tiers: **1 - Clears risk-adjusted hurdle**, and **2 - Clears, but verify: market disagrees**. Each row has model and market-implied probability, break price, the probability and latest close the hurdle needs, bull and bear cases, the risk flags found in the filing (quoted), kill criteria, what could make the thesis wrong, and a model position size.
- **Analysis - EV Ranked** tab: every priced deal in the universe, including those below the hurdle. Probability starts at the model estimate (its basis is shown); edit it and EV, risk-adjusted annualized return and **Clears hurdle?** recalculate in Excel.
- **Screened Out** tab: every other priced deal, with the reason: outside the universe, possible termination, negative spread, or below the hurdle and by how much.
- Tier 1 deals are added to `reports\special_situations_tracker.xlsx`. Verify-tier deals are not added until you confirm their terms in the dashboard calculator. Tracked deals that no longer meet the standards are marked below threshold.

Terms and risk flags are pattern-matched from filing text, so every row links its source filing. Non-SEC targets (UK, ASX, HKEX) are not priced here: they appear on the dashboard's hard-catalyst list for you to price in the calculator.

Close the terminal running option 2 to stop the local dashboard. `IBKR_DASHBOARD_PORT` optionally changes its default local web port (`8765`). Refresh is manual by design.

Optional environment settings (defaults shown):

| Variable | Default | Purpose |
|---|---:|---|
| `IBKR_HOST` | `127.0.0.1` | Gateway/TWS host |
| `IBKR_PORT` | `4002` | IB Gateway paper port (`4001` for live; TWS commonly `7497`/`7496`) |
| `IBKR_CLIENT_ID` | `37` | API client ID; use a distinct ID for this app |
| `IBKR_MAX_SCANS` | `0` | Maximum scanner combinations per run; `0` requests every combination in IBKR's catalog |
| `IBKR_MAX_NEWS_CONTRACTS` | `150` | Maximum discovered contracts whose headlines are checked |
| `IBKR_NEWS_DAYS` | `7` | Headline lookback |
| `IBKR_NEWS_RESULTS_PER_CONTRACT` | `10` | Maximum headlines retrieved per contract |
| `TRACKER_MIN_ANNUALIZED` | `0.25` | Risk-adjusted annualized hurdle for the deal sheet and tracker |
| `SPECIAL_SITS_NAV_USD` | `1000000000` | NAV used for the model position size |
| `BREAK_LOSS_BUDGET` | `0.01` | Largest share of NAV the model lets one deal break cost |
| `IBKR_SCAN_SOURCE` | `all` | Initial dashboard source: `ibkr`, `sec`, `news`, `factiva`, `public`, or `all` |
| `SEC_USER_AGENT` | unset | SEC-compliant app identification and contact, e.g. `MyResearchApp/1.0 analyst@example.com`; also enables verified news ticker matching |
| `TIINGO_API_KEY` | unset | Optional Tiingo News API key |
| `BENZINGA_API_KEY` | unset | Optional Benzinga News, Form 4, FINRA-reported short-interest, and fundamentals endpoints (separate entitlements may apply) |
| `QUIVER_API_KEY` | unset | Optional Quiver live-insiders API token (endpoint requires the applicable plan) |
| `FACTIVA_CLIENTID` | unset | Dow Jones Factiva Retrieval API client ID |
| `FACTIVA_USERNAME` | unset | Dow Jones Factiva service-account username |
| `FACTIVA_PASSWORD` | unset | Dow Jones Factiva service-account password |
| `FACTIVA_METRICS_EMAIL` | `FACTIVA_USERNAME` | Optional email hashed for Factiva API usage metrics; the email itself is not sent in the metrics payload |
| `GS_MARQUEE_CLIENT_ID` | unset | Goldman Sachs Marquee client ID for the production connection check |
| `GS_MARQUEE_CLIENT_SECRET` | unset | Goldman Sachs Marquee client secret for the production connection check |

Set Tiingo, Benzinga, Quiver, Factiva, and Marquee credentials only in your local environment; do not put them in source code or send them in chat. Tiingo, Benzinga, and Quiver API keys are sent only in HTTPS Authorization headers to their documented endpoints, not in URLs. Factiva credentials are sent only to Dow Jones' HTTPS authentication endpoint and its bearer token only to the fixed Dow Jones Retrieval API endpoint. Marquee credentials are sent only to its production OAuth endpoint and its bearer token only to the fixed profile endpoint for the manual connection check. No IBKR username, password, or API key should be put in the code or report. IBKR authentication and data entitlements are managed by the local IB Gateway/TWS session.

   ---
##  Contact & Professional Profile
* **Developer:** Paul Barrera
* **Focus:** Quantitative Finance Tools, Financial Engineering & LegalTech, Regtech 
* **LinkedIn:** www.linkedin.com/in/paulbarrera1-25364b431
