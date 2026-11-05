import unittest
from datetime import date
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import global_event_sources as sources
from ibkr_market_scanner import (
    ScanCandidate,
    _scan_selected_sources,
    build_live_report,
    build_research_leads,
    create_live_dashboard_server,
    merge_duplicate_candidates,
)


EDGAR_INDEX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
10-12B           SpinCo Holdings Inc.                                          2200001     20261029    edgar/data/2200001/0002200001-26-000001.txt
10-12G           Sagard Private Income Fund Inc.                               2133018     20261029    edgar/data/2133018/0002133018-26-000002.txt
25-NSE           NYSE ARCA, INC.                                               1143362     20261029    edgar/data/1143362/0001143362-26-000003.txt
425              RANGE RESOURCES CORP                                          315852      20261029    edgar/data/315852/0000315852-26-000004.txt
425              RANGE RESOURCES CORP                                          315852      20261029    edgar/data/315852/0000315852-26-000004.txt
8-K              RANGE RESOURCES CORP                                          315852      20261029    edgar/data/315852/0000315852-26-000005.txt
SC TO-T          Target Co                                                     1000001     20261029    edgar/data/1000001/0001000002-26-000006.txt
SC TO-T          Bidder Private LLC                                            1000002     20261029    edgar/data/1000001/0001000002-26-000006.txt
SCHEDULE 13D     Target Co                                                     1000001     20261029    edgar/data/1000001/0001000003-26-000007.txt
"""

PANEL_HTML = """<html><body><table><tr><td><strong>THE TAKEOVER PANEL &#8211; DISCLOSURE TABLE</strong></td></tr></table>
<table><tr><td><strong>AMENDMENTS</strong></td></tr></table>
<table><tr><td><strong>OFFEREE: SEGRO plc </strong></td><td>LEI: 213800XC35KGM9NFC641</td></tr>
<tr><td>Offer period commenced: 07:00 24-Jun-2026</td></tr><tr><td>ISIN: GB00B5ZN1N88</td></tr></table>
<table><tr><td><strong>DISCLOSURE TABLE</strong></td></tr></table>
<table><tr><td><strong>OFFEREE: SEGRO plc </strong></td><td>LEI: 213800XC35KGM9NFC641</td></tr>
<tr><td>Offer period commenced: 07:00 24-Jun-2026</td></tr>
<tr><td>10p ordinary</td><td>ISIN: GB00B5ZN1N88</td><td>NSI: 1,354,122,167</td></tr>
<tr><td><strong>OFFEROR: Prologis, Inc. </strong></td><td>LEI:<br />529900DFH19P073LZ636</td></tr>
<tr><td>Offeror identified: 07:00 24-Jun-2026</td></tr><tr><td>Rule 2.6 deadline: N/A</td></tr>
<tr><td>USD 0.01 common</td><td>ISIN: US74340W1036</td></tr></table>
<table><tr><td><strong>OFFEREE: SThree plc </strong></td><td>LEI: 2138003NEBX5VRP3EX50</td></tr>
<tr><td>Offer period commenced: 15:19 09-Sep-2026</td></tr><tr><td>ISIN: GB00B0KM9T71</td></tr>
<tr><td><strong>OFFEROR: Circle8 Group Inc. </strong></td><td>LEI:</td></tr>
<tr><td>Rule 2.6 deadline: 17:00 18-Nov-2026</td></tr></table>
<p>This site uses cookies. Accept Decline</p></body></html>"""

HALTS_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>
<item><title>SUPN</title><ndaq:HaltDate>10/30/2026</ndaq:HaltDate><ndaq:HaltTime>16:00:00.000</ndaq:HaltTime>
<ndaq:IssueSymbol>SUPN</ndaq:IssueSymbol><ndaq:IssueName>Supernus Pharmaceuticals</ndaq:IssueName>
<ndaq:Market>NASDAQ</ndaq:Market><ndaq:ReasonCode>T12</ndaq:ReasonCode><ndaq:ResumptionTradeTime /></item>
<item><title>VOL</title><ndaq:IssueSymbol>VOL</ndaq:IssueSymbol><ndaq:ReasonCode>LUDP</ndaq:ReasonCode></item>
</channel></rss>"""


def _text_response(text):
    return SimpleNamespace(text=text, content=text.encode("utf-8") if isinstance(text, str) else text)


class EdgarDealFormTests(unittest.TestCase):
    def test_parses_index_and_keeps_listed_deal_parties_and_unlisted_spincos(self):
        tickers = {"0000315852": ("RRC", "RANGE RESOURCES CORP"), "0001000001": ("TGT1", "Target Co")}
        with patch.object(sources, "_load_sec_tickers", return_value=tickers), \
             patch.object(sources, "_http_get", return_value=_text_response(EDGAR_INDEX)):
            candidates, warnings, summary = sources.scan_edgar_deal_forms(business_days=1, today=date(2026, 10, 29))

        by_name = {c.description: c for c in candidates}
        # Fund Form 10s, exchange 25-NSE filers, unlisted bidders and non-deal 8-Ks are dropped.
        self.assertEqual(set(by_name), {"SpinCo Holdings Inc.", "RANGE RESOURCES CORP", "Target Co"})
        self.assertEqual(len(by_name["RANGE RESOURCES CORP"].headlines), 1)   # duplicate index row deduped
        target = by_name["Target Co"]
        self.assertEqual(target.contract.symbol, "TGT1")
        self.assertEqual({h["matches"][0][0] for h in target.headlines},
                         {"M&A / merger arbitrage", "Activist / strategic review"})
        self.assertIn("SC TO-T", target.research["status_approvals"])
        self.assertEqual(target.research["cik"], "0001000001")
        self.assertEqual(
            target.headlines[0]["url"],
            "https://www.sec.gov/Archives/edgar/data/1000001/000100000226000006/0001000002-26-000006-index.htm",
        )
        self.assertEqual(by_name["SpinCo Holdings Inc."].contract.symbol, "CIK2200001")
        self.assertEqual(summary["sources_used"], ["SEC EDGAR deal forms"])
        self.assertEqual(warnings, [])

    def test_business_days_skip_weekends(self):
        self.assertEqual(
            sources.business_days_back(date(2026, 11, 2), 3),   # Monday
            [date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2)],
        )


class TakeoverPanelTests(unittest.TestCase):
    def test_reads_full_table_not_change_log_and_prefills_counterparty_and_deadline(self):
        offerees = sources.parse_takeover_panel(PANEL_HTML)
        self.assertEqual([o["offeree"] for o in offerees], ["SEGRO plc", "SThree plc"])
        self.assertEqual(offerees[0]["isin"], "GB00B5ZN1N88")   # offeree ISIN, not the offeror's
        self.assertEqual(offerees[1]["offerors"][0]["rule_26"], "17:00 18-Nov-2026")

        with patch.object(sources, "_http_get", return_value=_text_response(PANEL_HTML)):
            candidates, warnings, summary = sources.scan_uk_takeover_panel()
        sthree = candidates[1]
        self.assertEqual(sthree.research["counterparty"], "Circle8 Group Inc.")
        self.assertIn("Rule 2.6 17:00 18-Nov-2026", sthree.research["key_dates_catalyst"])
        self.assertNotIn("Rule 2.6", candidates[0].research["key_dates_catalyst"])   # N/A deadline omitted
        lead = build_research_leads([sthree])[0]
        self.assertEqual(lead["counterparty"], "Circle8 Group Inc.")
        self.assertEqual(lead["situation_type"], "UK takeover (offer period)")
        self.assertEqual(summary["sources_used"], ["UK Takeover Panel"])

    def test_layout_change_is_reported_not_silent(self):
        with patch.object(sources, "_http_get", return_value=_text_response("<html>maintenance</html>")):
            candidates, warnings, _ = sources.scan_uk_takeover_panel()
        self.assertEqual(candidates, [])
        self.assertIn("layout may have changed", warnings[0])


class ExchangeFeedTests(unittest.TestCase):
    def test_asx_keeps_event_types_and_skips_routine_notices(self):
        items = [
            {"symbol": "OML", "announcementTypes": ["Scheme of Arrangement"], "companies": [{}],
             "companyInfo": [{"displayName": "OOH!MEDIA LIMITED"}], "headline": "Scheme timetable",
             "documentKey": "2924-1", "isPriceSensitive": True, "date": "2026-10-30T01:00:00Z"},
            {"symbol": "BHP", "announcementTypes": ["Daily Share Buy-Back Notice"], "companies": [{}]},
            {"symbol": "XYZ", "announcementTypes": ["Company Administration - Other"], "companies": [{}]},
            {"symbol": "ABC", "announcementTypes": ["Trading Halt Lifted"], "companies": [{}]},
        ]
        pages = [SimpleNamespace(json=lambda: {"data": {"items": items}}),
                 SimpleNamespace(json=lambda: {"data": {"items": []}})]
        with patch.object(sources, "_http_get", side_effect=pages):
            candidates, warnings, summary = sources.scan_asx_announcements(pages=3)
        self.assertEqual([c.contract.symbol for c in candidates], ["OML.AX"])
        self.assertEqual(candidates[0].headlines[0]["matches"], [("M&A / merger arbitrage", ["Scheme of Arrangement"])])
        self.assertIn("price-sensitive", candidates[0].headlines[0]["provider"])
        self.assertTrue(candidates[0].headlines[0]["url"].endswith("/file/2924-1"))

    def test_hkex_classifies_offers_and_skips_daily_buyback_returns(self):
        payload = {"newsInfoLst": [
            {"newsId": 1, "lTxt": "Announcements and Notices - [Share Buyback]", "title": "NEXT DAY DISCLOSURE",
             "stock": [{"sc": "00005", "sn": "HSBC HOLDINGS"}], "webPath": "/a.pdf"},
            {"newsId": 2, "lTxt": "Announcements and Notices - [Privatisation / Scheme of Arrangement]",
             "title": "JOINT ANNOUNCEMENT\nPROPOSED PRIVATISATION", "stock": [{"sc": "03774", "sn": "FAMEGLOW"}],
             "webPath": "/listedco/b.pdf", "relTime": "30/10/2026 22:59"},
        ]}
        with patch.object(sources, "_http_get", return_value=SimpleNamespace(json=lambda: payload)):
            candidates, _, summary = sources.scan_hkex_announcements()
        self.assertEqual([c.contract.symbol for c in candidates], ["3774.HK"])
        self.assertEqual(candidates[0].headlines[0]["url"], "https://www1.hkexnews.hk/listedco/b.pdf")
        self.assertEqual(candidates[0].headlines[0]["matches"][0][0], "M&A / merger arbitrage")

    def test_nasdaq_halts_keep_news_and_regulatory_codes_only(self):
        with patch.object(sources, "_http_get", return_value=SimpleNamespace(content=HALTS_XML)):
            candidates, _, _ = sources.scan_nasdaq_halts()
        self.assertEqual([c.contract.symbol for c in candidates], ["SUPN"])
        self.assertIn("T12", candidates[0].headlines[0]["headline"])
        self.assertIn("not yet resumed", candidates[0].headlines[0]["headline"])


class DealMathTests(unittest.TestCase):
    """Cases from the 2026-10-28 special situations workbook."""

    def test_stock_deal(self):   # SUPN: 1.5401 INDV per share, INDV 34.72, SUPN 40.96
        result = sources.deal_math(price=40.96, ratio=1.5401, acquirer_price=34.72)
        self.assertAlmostEqual(result["consideration_value"], 53.47, places=2)
        self.assertAlmostEqual(result["gross_spread"], 0.305, places=3)
        self.assertIsNone(result["annualized"])

    def test_cash_deal_annualized_is_simple(self):   # BHF: 70 cash vs 44.73, 40 days -> about +515%
        result = sources.deal_math(price=44.73, cash=70, days=40)
        self.assertAlmostEqual(result["gross_spread"], 0.565, places=3)
        self.assertAlmostEqual(result["annualized"], 5.15, delta=0.01)

    def test_foreign_currency_offer(self):   # QGEN: EUR 46.50 at 1.1131 vs 50.17
        result = sources.deal_math(price=50.17, cash=46.50, fx=1.1131)
        self.assertAlmostEqual(result["consideration_value"], 51.76, places=2)
        self.assertAlmostEqual(result["gross_spread"], 0.032, places=3)

    def test_cash_plus_stock_plus_cvr(self):   # UNF: 155 + 0.7720 CTAS @ 214.80 vs 250.40
        result = sources.deal_math(price=250.40, cash=155, ratio=0.7720, acquirer_price=214.80)
        self.assertAlmostEqual(result["consideration_value"], 320.83, places=2)
        with self.assertRaises(ValueError):
            sources.deal_math(price=0, cash=10)


class FinancialSnapshotTests(unittest.TestCase):
    def test_newest_tag_wins_stale_facts_dropped_and_values_derived(self):
        def fact(val, end, start=None, fp="FY", form="10-K"):
            return {"val": val, "end": end, "start": start, "fp": fp, "form": form, "filed": end, "accn": "x"}

        facts = {"cik": 1, "entityName": "Example Corp", "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [fact(10_000_000, "2026-08-01", fp="Q2")]}}},
            "us-gaap": {
                "Revenues": {"units": {"USD": [fact(400, "2017-12-31", "2017-01-01")]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [fact(900, "2025-12-31", "2025-01-01"), fact(200, "2026-06-30", "2026-04-01", fp="Q2")]}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [fact(300_000_000, "2026-06-30", fp="Q2")]}},
                "MarketableSecuritiesCurrent": {"units": {"USD": [fact(170_000_000, "2016-09-30", fp="Q3")]}},
                "LongTermDebtNoncurrent": {"units": {"USD": [fact(100_000_000, "2026-06-30", fp="Q2")]}},
                "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [fact(80_000_000, "2025-12-31", "2025-01-01")]}},
                "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [fact(5_000_000, "2025-12-31", "2025-01-01")]}},
            }}}
        snapshot = sources.financial_snapshot_from_facts(facts, price=50.0)
        self.assertEqual(snapshot["facts"]["revenue"]["value"], 900)   # newest annual, not the 2017 tag or a quarter
        self.assertIsNone(snapshot["facts"]["short_term_investments"])  # 2016 fact is a discontinued line
        derived = snapshot["derived"]
        self.assertEqual(derived["market_cap"], 500_000_000)
        self.assertEqual(derived["net_cash"], 200_000_000)
        self.assertEqual(derived["enterprise_value"], 300_000_000)
        self.assertEqual(derived["net_cash_per_share"], 20)
        self.assertEqual(derived["free_cash_flow"], 75_000_000)
        self.assertEqual(snapshot["cik"], "0000000001")

    def test_non_us_names_get_a_clear_error(self):
        with patch.object(sources, "_load_sec_tickers_by_symbol", return_value={}):
            with self.assertRaisesRegex(ValueError, "US-registered"):
                sources.sec_financial_snapshot(ticker="3380.HK")


class ScannerIntegrationTests(unittest.TestCase):
    def _candidate(self, headline_id, provider, research=None):
        return ScanCandidate(
            contract=SimpleNamespace(conId=315852, symbol="RRC", localSymbol="RRC", secType="FILING",
                                     primaryExchange="SEC EDGAR", exchange="SEC EDGAR", currency="USD"),
            description="RANGE RESOURCES CORP",
            signals={provider},
            headlines=[{"time": "2026-10-29", "provider": provider, "article_id": headline_id,
                        "headline": "Merger", "matches": [("M&A / merger arbitrage", ["merger"])]}],
            research=research or {},
        )

    def test_same_filer_from_two_sec_sources_becomes_one_lead_with_cik(self):
        merged = merge_duplicate_candidates([
            self._candidate("a", "SEC EDGAR · 8-K"),
            self._candidate("b", "SEC EDGAR · 425", {"status_approvals": "EDGAR filings: 425"}),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0].headlines), 2)
        lead = build_research_leads(merged)[0]
        self.assertEqual(lead["cik"], "0000315852")
        self.assertEqual(lead["status_approvals"], "EDGAR filings: 425")

    def test_global_mode_survives_a_failing_feed(self):
        import ibkr_market_scanner as scanner

        good = self._candidate("a", "UK Takeover Panel")

        def broken():
            raise RuntimeError("feed down")

        profiles = SimpleNamespace(get=lambda cik: {"domicile": "United States", "exchanges": ["NYSE"]})
        with patch.object(scanner, "_sec_profiles", return_value=profiles), patch.object(sources, "GLOBAL_SOURCES", (
            lambda: ([good], ["minor"], {"sources_used": ["UK Takeover Panel"], "news_provider_names": []}),
            broken,
        )):
            candidates, warnings, summary = _scan_selected_sources("global")
        self.assertEqual(candidates, [good])
        self.assertIn("minor", warnings)
        self.assertTrue(any("feed down" in warning for warning in warnings))
        self.assertEqual(summary["sources_used"], ["UK Takeover Panel"])

    def test_live_report_includes_deal_calculator_and_financials_tools(self):
        page = build_live_report([self._candidate("a", "SEC EDGAR · 425")], [], {
            "host": "127.0.0.1", "port": 4001, "scans_succeeded": 0, "scans_total": 0, "scans_planned": 0,
            "contracts_found": 1, "news_contracts_queried": 0, "news_provider_names": [], "instruments": [],
            "locations": [], "source_mode": "global",
        }, "now")
        self.assertIn("function dealMath(row)", page)
        self.assertIn("data-load-financials", page)
        self.assertIn('<option value="global" selected>', page)

    def test_financials_endpoint_returns_snapshot_and_rejects_non_us(self):
        state = {"candidates": [], "warnings": [], "summary": {}, "updated_at": "", "source_mode": "public",
                 "refresh_lock": threading.Lock()}
        server = create_live_dashboard_server(state, lambda mode: "")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(body):
            return urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/financials",
                                   data=json.dumps(body).encode("utf-8"),
                                   headers={"Content-Type": "application/json"}, method="POST"))

        try:
            with patch.object(sources, "sec_financial_snapshot", return_value={"entity": "Range", "derived": {}}) as snap:
                with post({"ticker": "RRC", "cik": "0000315852", "price": "56.38"}) as response:
                    self.assertEqual(json.load(response)["entity"], "Range")
            snap.assert_called_once_with(ticker="RRC", cik="0000315852", price=56.38)
            with patch.object(sources, "sec_financial_snapshot", side_effect=ValueError("US-registered filers only")):
                with self.assertRaises(HTTPError) as error:
                    post({"ticker": "3380.HK"})
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "tracker.xlsx"

    def tearDown(self):
        self.folder.cleanup()

    def deal(self, annualized, **overrides):
        return {"id": "315852", "ticker": "BHF", "company": "Brighthouse Financial", "price": "44.73",
                "consideration_value": 70, "gross_spread": 0.565, "close_date": "2026-12-06", "days": 36,
                "annualized": annualized, "risk_adjusted_annualized": annualized, "probability": 0.9,
                "break_price": 36.0, "date_pulled": "2026-10-31", "deal_terms": "=70.00 cash", **overrides}

    def test_hurdle_uses_risk_adjusted_return_and_standards(self):
        from ibkr_market_scanner import record_tracked_deal

        gross_only = record_tracked_deal(self.deal(5.73, risk_adjusted_annualized=0.12), self.path, today="2026-10-31")
        self.assertFalse(gross_only["tracked"])   # a wide gross spread alone does not clear
        no_probability = record_tracked_deal(self.deal(5.73, risk_adjusted_annualized=None), self.path, today="2026-10-31")
        self.assertFalse(no_probability["tracked"])
        outside = record_tracked_deal(self.deal(5.73, meets_standards=False), self.path, today="2026-10-31")
        self.assertFalse(outside["tracked"])
        self.assertFalse(self.path.exists())

    def test_threshold_is_applied_in_code_and_rows_upsert_by_lead(self):
        from openpyxl import load_workbook
        from ibkr_market_scanner import record_tracked_deal

        below = record_tracked_deal(self.deal(0.249), self.path, today="2026-10-31")
        self.assertFalse(below["tracked"])
        self.assertFalse(self.path.exists())   # nothing written for a deal that never cleared

        added = record_tracked_deal(self.deal(5.73), self.path, today="2026-10-31")
        self.assertTrue(added["tracked"])
        self.assertFalse(added["updated"])
        updated = record_tracked_deal(self.deal(6.10, price="44.10"), self.path, today="2026-11-02")
        self.assertTrue(updated["updated"])

        sheet = load_workbook(self.path).active
        self.assertEqual(sheet.max_row, 2)   # one row per lead
        row = {sheet.cell(row=1, column=c).value: sheet.cell(row=2, column=c) for c in range(1, sheet.max_column + 1)}
        self.assertEqual(row["Date added"].value, "2026-10-31")   # first-added date kept on update
        self.assertEqual(row["Last updated"].value, "2026-11-02")
        self.assertEqual(row["Date pulled"].value, "2026-10-31")
        self.assertEqual(row["Status"].value, "Active")
        self.assertEqual(row["Annualized"].value, 6.10)
        self.assertEqual(row["Annualized"].number_format, "0.0%")
        self.assertEqual(row["Risk-adj. annualized (hurdle)"].value, 6.10)
        self.assertEqual(row["Completion probability"].number_format, "0%")
        self.assertEqual(row["Price at pull"].value, 44.10)
        self.assertEqual(row["Deal terms"].data_type, "s")   # "=..." text is not stored as a formula

        dropped = record_tracked_deal(self.deal(0.10), self.path, today="2026-11-05")
        self.assertFalse(dropped["tracked"])
        sheet = load_workbook(self.path).active
        self.assertEqual(sheet.cell(row=2, column=5).value, "Below threshold since 2026-11-05")

    def test_track_endpoint_writes_the_configured_workbook(self):
        state = {"candidates": [], "warnings": [], "summary": {}, "updated_at": "", "source_mode": "public",
                 "refresh_lock": threading.Lock(), "tracker_path": str(self.path)}
        server = create_live_dashboard_server(state, lambda mode: "")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/api/track",
                              data=json.dumps(self.deal(0.5)).encode("utf-8"),
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request) as response:
                result = json.load(response)
            self.assertTrue(result["tracked"])
            self.assertTrue(self.path.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_pull_date_is_stamped_on_rows_and_page(self):
        candidate = ScanCandidate(
            contract=SimpleNamespace(conId=1, symbol="X", localSymbol="X", secType="STK",
                                     primaryExchange="", exchange="", currency=""),
            headlines=[{"time": "", "provider": "p", "article_id": "a", "headline": "Merger",
                        "matches": [("M&A / merger arbitrage", ["merger"])]}],
        )
        self.assertEqual(build_research_leads([candidate], "2026-10-31")[0]["date_pulled"], "2026-10-31")
        page = build_live_report([candidate], [], {
            "host": "127.0.0.1", "port": 4001, "scans_succeeded": 0, "scans_total": 0, "scans_planned": 0,
            "contracts_found": 1, "news_contracts_queried": 0, "news_provider_names": [], "instruments": [],
            "locations": [], "pulled_at": "2026-10-31T03:04:16-07:00",
        }, "now")
        self.assertIn("Data pulled: <strong>Sat Oct 31, 2026 03:04", page)
        self.assertIn('const SCAN_DATE="2026-10-31"', page)
        self.assertIn("<title>2026-10-31 ·", page)


if __name__ == "__main__":
    unittest.main()
