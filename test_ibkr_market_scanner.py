import unittest
from io import BytesIO
import json
import threading
from types import SimpleNamespace
from urllib.request import Request, urlopen
from unittest.mock import patch
from xml.etree import ElementTree as ET

from openpyxl import load_workbook
from ibkr_market_scanner import (
    ScanCandidate,
    ANALYSIS_COLUMNS,
    SOURCE_MODES,
    SPECIAL_SITUATIONS_COLUMNS,
    build_report,
    build_live_report,
    build_excel_report,
    build_research_leads,
    build_scan_jobs,
    check_gs_marquee_connection,
    classify_headline,
    create_live_dashboard_server,
    event_relevance_score,
    scan_factiva,
    scan_online_news,
    scan_sec_edgar,
    _scan_selected_sources,
    snapshot_market_price,
)


class MarketScannerTests(unittest.TestCase):
    def test_public_source_mode_controls_are_available(self):
        report = build_live_report(
            [],
            [],
            {
                "host": "127.0.0.1",
                "port": 4002,
                "source_mode": "public",
                "source_name": SOURCE_MODES["public"],
                "sources_used": ["SEC EDGAR", "Online news (Google News)"],
                "scans_succeeded": 0,
                "scans_total": 0,
                "scans_planned": 0,
                "contracts_found": 0,
                "news_contracts_queried": 0,
                "news_provider_names": [],
                "instruments": [],
                "locations": [],
            },
            "2026-10-28 12:00:00 PDT",
        )

        self.assertIn('id="sourceMode"', report)
        for mode, label in SOURCE_MODES.items():
            self.assertIn(f'<option value="{mode}"', report)
            self.assertIn(label, report)
        self.assertIn("source_mode:sourceMode", report)

    def test_sec_edgar_results_map_filings_to_ticker_and_source_link(self):
        import ibkr_market_scanner as scanner

        payload = {
            "hits": {
                "hits": [{
                    "_source": {
                        "adsh": "0001045810-26-000123",
                        "ciks": ["0001045810"],
                        "display_names": ["NVIDIA CORP (NVDA) (CIK 0001045810)"],
                        "form": "8-K",
                        "file_date": "2026-10-28",
                        "file_description": "Entry into material definitive agreement",
                    },
                }],
            },
        }
        response = SimpleNamespace(json=lambda: payload)
        with patch.dict("os.environ", {"SEC_USER_AGENT": ""}), \
             patch.object(scanner, "_load_sec_tickers", return_value={
                 "0001045810": ("NVDA", "NVIDIA CORP"),
             }), patch.object(scanner, "_http_get", return_value=response) as http_get:
            candidates, warnings, summary = scan_sec_edgar(days=7, limit=10)
        # EDGAR ignores a custom date range unless both ends are sent.
        self.assertIn("startdt=", http_get.call_args[0][0])
        self.assertIn("enddt=", http_get.call_args[0][0])

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].contract.symbol, "NVDA")
        self.assertEqual(candidates[0].description, "NVIDIA CORP")
        self.assertEqual(candidates[0].headlines[0]["matches"][0][0], "M&A / merger arbitrage")
        self.assertIn(
            "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000123/000104581026000123-index.htm",
            candidates[0].headlines[0]["url"],
        )
        self.assertEqual(summary["sources_used"], ["SEC EDGAR"])
        self.assertTrue(any("SEC_USER_AGENT" in warning for warning in warnings))

    def test_online_news_results_keep_publisher_link_and_classified_headline(self):
        import ibkr_market_scanner as scanner

        xml = b"""<rss><channel><item>
          <title>Example Corp (EXM) agrees to merger</title>
          <link>https://news.example.test/story/1</link>
          <pubDate>Wed, 28 Oct 2026 12:00:00 GMT</pubDate>
          <source>Example News</source>
        </item><item>
          <title>Broad market rises today</title>
          <link>https://news.example.test/story/2</link>
        </item></channel></rss>"""
        response = SimpleNamespace(content=xml)
        with patch.dict("os.environ", {
            "SEC_USER_AGENT": "TestApp/1.0 analyst@example.test",
            "TIINGO_API_KEY": "",
            "BENZINGA_API_KEY": "",
        }), \
             patch.object(
                 scanner, "_load_sec_tickers_by_symbol",
                 return_value={"EXM": ("0000000001", "Example Corporation")},
             ), patch.object(scanner, "_http_get", return_value=response) as http_get:
            candidates, warnings, summary = scan_online_news(days=7, limit=10)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].contract.symbol, "EXM")
        self.assertEqual(candidates[0].contract.secType, "STK")
        self.assertEqual(candidates[0].description, "Example Corporation")
        self.assertEqual(candidates[0].headlines[0]["provider"], "Example News")
        self.assertEqual(candidates[0].headlines[0]["url"], "https://news.example.test/story/1")
        self.assertTrue(any("secondary-source leads" in warning for warning in warnings))
        self.assertTrue(any("TIINGO_API_KEY is not configured" in warning for warning in warnings))
        self.assertTrue(any("BENZINGA_API_KEY is not configured" in warning for warning in warnings))
        self.assertFalse(any("ticker parsed from a title are unverified" in warning for warning in warnings))
        self.assertEqual(
            summary["sources_used"],
            [
                "Nasdaq Trader corporate action RSS",
                "Google News",
                "Google News OTC-focused search RSS",
                "Google News NYSE-focused search RSS",
            ],
        )
        requested_urls = [call.args[0] for call in http_get.call_args_list]
        self.assertIn(
            "https://www.nasdaqtrader.com/Rss.aspx?feed=currentheadlines&categorylist=105",
            requested_urls,
        )
        self.assertTrue(any("site%3Aotcmarkets.com" in url for url in requested_urls))
        self.assertTrue(any("site%3Anyse.com" in url for url in requested_urls))

    def test_tiingo_and_benzinga_news_apis_use_local_credentials_and_normalize_events(self):
        import ibkr_market_scanner as scanner

        now = scanner.datetime.now(scanner.timezone.utc).isoformat()
        payload = [{
            "id": "article-1",
            "title": "Example Corporation (EXM) announces merger agreement",
            "url": "https://news.example.test/article-1",
            "publishedDate": now,
            "source": "Example Wire",
            "tickers": ["EXM"],
        }]
        json_response = SimpleNamespace(json=lambda: payload)
        with patch.dict("os.environ", {
            "SEC_USER_AGENT": "TestApp/1.0 analyst@example.test",
            "TIINGO_API_KEY": "tiingo-test-key",
            "BENZINGA_API_KEY": "benzinga-test-key",
        }), patch.object(
            scanner, "_load_sec_tickers_by_symbol",
            return_value={"EXM": ("0000000001", "Example Corporation")},
        ), patch.object(
            scanner, "_http_get", return_value=SimpleNamespace(content=b"<rss><channel/></rss>")
        ), patch.object(
            scanner, "_http_get_with_headers", return_value=json_response
        ) as vendor_get:
            candidates, warnings, summary = scan_online_news(days=7, limit=10)

        self.assertIn("Tiingo News", summary["sources_used"])
        self.assertIn("Benzinga News", summary["sources_used"])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].contract.symbol, "EXM")
        self.assertEqual(candidates[0].description, "Example Corporation")
        self.assertEqual(candidates[0].headlines[0]["provider"], "Example Wire")
        self.assertEqual(vendor_get.call_count, 2)
        requests = [call.args for call in vendor_get.call_args_list]
        self.assertTrue(all("test-key" not in request[0] for request in requests))
        headers = [request[1] for request in requests]
        self.assertIn({"Authorization": "Token tiingo-test-key"}, headers)
        self.assertIn({"Authorization": "token benzinga-test-key"}, headers)
        self.assertFalse(any("TIINGO_API_KEY" in warning for warning in warnings))
        self.assertFalse(any("BENZINGA_API_KEY" in warning for warning in warnings))

    def test_vendor_research_enriches_short_interest_fundamentals_and_insider_leads(self):
        import ibkr_market_scanner as scanner

        candidate = ScanCandidate(
            contract=SimpleNamespace(
                symbol="EXM",
                localSymbol="EXM",
                secType="STK",
                conId=123,
            ),
            description="Example Corporation",
        )
        responses = {
            "insider_transactions": {
                "data": [{
                    "id": "benz-1",
                    "company_symbol": "EXM",
                    "company_name": "Example Corporation",
                    "filing_date": "2026-10-28",
                    "html_url": "https://www.sec.gov/Archives/edgar/data/1/form4.htm",
                    "owner": {"insider_name": "Example Director", "is_director": True},
                    "transactions": [{
                        "acquired_or_disposed": "A",
                        "transaction_code": "P",
                        "shares": "1000",
                        "price_per_share": "12.5",
                    }],
                }],
            },
            "shortinterest": {
                "shortInterestData": {
                    "EXM": {"data": [{
                        "recordDate": "2026-10-15",
                        "totalShortInterest": "250000",
                        "shortPercentOfFloat": 8.5,
                        "daysToCover": 2.4,
                        "settlementDate": "2026-10-14",
                    }]},
                },
            },
            "fundamentals": {
                "data": [{
                    "symbol": "EXM",
                    "marketCap": 1200000000,
                    "financials": {"revenue": 500000000},
                }],
            },
        }
        quiver_payload = [{
            "Ticker": "EXM",
            "Date": "2026-10-28",
            "Name": "Another Director",
            "TransactionCode": "S",
            "AcquiredDisposedCode": "D",
            "Shares": 50,
            "PricePerShare": 14,
            "officerTitle": "Director",
        }]

        def fake_get(url, headers):
            if "quiverquant.com" in url:
                payload = quiver_payload
            elif "insider_transactions" in url:
                payload = responses["insider_transactions"]
            elif "shortinterest" in url:
                payload = responses["shortinterest"]
            else:
                payload = responses["fundamentals"]
            return SimpleNamespace(json=lambda: payload)

        with patch.dict("os.environ", {
            "BENZINGA_API_KEY": "benzinga-test-key",
            "QUIVER_API_KEY": "quiver-test-key",
        }), patch.object(scanner, "_http_get_with_headers", side_effect=fake_get) as http_get:
            results, warnings, providers = scanner.scan_vendor_research([candidate])

        self.assertIn("Benzinga Form 4", providers)
        self.assertIn("Benzinga Short Interest", providers)
        self.assertIn("Benzinga Fundamentals", providers)
        self.assertIn("Quiver Insider Trading", providers)
        self.assertEqual(len(results), 3)
        self.assertEqual(candidate.metrics["Short interest shares"], "250000")
        self.assertEqual(candidate.metrics["Short interest % float"], "8.5")
        self.assertEqual(candidate.metrics["Benzinga marketCap"], "1,200,000,000")
        benzinga_lead = next(row for row in results if "Benzinga Form 4" in row.signals)
        self.assertEqual(benzinga_lead.contract.symbol, "EXM")
        self.assertEqual(benzinga_lead.metrics["Insider"], "Example Director")
        self.assertTrue(benzinga_lead.headlines[0]["url"].startswith("https://www.sec.gov/"))
        quiver_lead = next(row for row in results if "Quiver Insider Trading" in row.signals)
        self.assertIn("sale", quiver_lead.headlines[0]["headline"])
        self.assertEqual(http_get.call_count, 4)
        self.assertTrue(all("test-key" not in call.args[0] for call in http_get.call_args_list))
        self.assertTrue(all(call.args[1].get("Authorization") for call in http_get.call_args_list))
        self.assertEqual(warnings, [])

    def test_vendor_research_without_credentials_skips_requests_with_explicit_warnings(self):
        import ibkr_market_scanner as scanner

        with patch.dict("os.environ", {
            "BENZINGA_API_KEY": "",
            "QUIVER_API_KEY": "",
        }), patch.object(scanner, "_http_get_with_headers") as http_get:
            candidates, warnings, providers = scanner.scan_vendor_research([])

        self.assertEqual(candidates, [])
        self.assertEqual(providers, [])
        self.assertTrue(any("BENZINGA_API_KEY is not configured" in warning for warning in warnings))
        self.assertTrue(any("QUIVER_API_KEY is not configured" in warning for warning in warnings))
        http_get.assert_not_called()

    def test_news_ticker_is_not_used_when_sec_issuer_name_does_not_match(self):
        import ibkr_market_scanner as scanner

        xml = b"""<rss><channel><item>
          <title>Unrelated Company (EXM) agrees to merger</title>
          <link>https://news.example.test/story/unrelated</link>
        </item></channel></rss>"""
        with patch.object(
            scanner, "_http_get", return_value=SimpleNamespace(content=xml)
        ):
            candidates, error = scanner._read_event_rss(
                "https://news.example.test/feed",
                "Google News",
                10,
                {"EXM": ("0000000001", "Example Corporation")},
            )

        self.assertIsNone(error)
        self.assertEqual(len(candidates), 1)
        self.assertNotEqual(candidates[0].contract.symbol, "EXM")
        self.assertEqual(candidates[0].contract.secType, "NEWS")
        self.assertEqual(candidates[0].description, "Unrelated Company")
        self.assertEqual(
            scanner._headline_issuer_name(
                "Volatus Aerospace Completes Acquisition of AISPECO - True North Strategic Review",
                "True North Strategic Review",
            ),
            "Volatus Aerospace",
        )
        self.assertEqual(
            scanner._headline_issuer_name(
                "Alchemy Beverages Inc. Executes Tender Offer - GlobeNewswire",
                "GlobeNewswire",
            ),
            "Alchemy Beverages Inc.",
        )

    def test_news_card_shows_company_unverified_ticker_and_all_matched_terms(self):
        candidate = ScanCandidate(
            contract=SimpleNamespace(
                conId=123,
                symbol="NEWS-D92F52",
                localSymbol="NEWS-D92F52",
                secType="NEWS",
                primaryExchange="True North Strategic Review",
                exchange="",
                currency="",
            ),
            description="Volatus Aerospace",
            headlines=[{
                "time": "Thu, 29 Oct 2026 00:02:44 GMT",
                "provider": "True North Strategic Review",
                "article_id": "d92f5291e177",
                "headline": "Volatus Aerospace Completes Acquisition of AISPECO",
                "url": "https://news.example.test/volatus",
                "matches": [
                    ("M&A / merger arbitrage", ["Acquisition"]),
                    ("Activist / strategic review", ["Strategic Review"]),
                ],
            }],
        )

        report = build_report(
            [candidate],
            [],
            {
                "scans_succeeded": 0,
                "scans_total": 0,
                "scans_planned": 0,
                "contracts_found": 1,
                "news_contracts_queried": 0,
                "news_provider_names": ["Google News"],
                "instruments": [],
                "locations": [],
                "source_mode": "news",
            },
            include_analysis_links=True,
        )

        self.assertIn("Headline company (unverified):</strong> Volatus Aerospace", report)
        self.assertIn("Ticker:</strong> Not verified", report)
        self.assertIn("Lead ref NEWS-D92F52", report)
        self.assertIn("M&amp;A / merger arbitrage: Acquisition", report)
        self.assertIn("Activist / strategic review: Strategic Review", report)
        self.assertNotIn(">NEWS-D92F52</a>", report)

    def test_factiva_uses_api_credentials_and_maps_event_articles(self):
        import ibkr_market_scanner as scanner

        article = {
            "id": "drn:archive.newsarticle.EXAMPLE001",
            "meta": {
                "original_doc_id": "EXAMPLE001",
                "source": {"name": "Example Journal"},
            },
            "attributes": {
                "headline": {"main": {"text": "Acme Corp (ACME) agrees to merger"}},
                "publication_date": "2026-10-28",
                "snippet": {"content": [{"text": "The merger agreement was announced today."}]},
            },
        }
        responses = [
            SimpleNamespace(
                status_code=200,
                json=lambda: {"id_token": "id-token", "access_token": "session-token"},
                raise_for_status=lambda: None,
            ),
            SimpleNamespace(
                status_code=200,
                json=lambda: {"access_token": "bearer-token"},
                raise_for_status=lambda: None,
            ),
            SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [article]},
                raise_for_status=lambda: None,
            ),
        ]
        credentials = {
            "FACTIVA_CLIENTID": "test-client",
            "FACTIVA_USERNAME": "api-user@example.test",
            "FACTIVA_PASSWORD": "test-password",
        }
        with patch.dict("os.environ", credentials, clear=True), patch.object(
            scanner.requests, "post", side_effect=responses
        ) as post:
            candidates, warnings, summary = scan_factiva()

        self.assertEqual(warnings, [])
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.contract.symbol, "ACME")
        self.assertEqual(candidate.description, "Acme Corp")
        self.assertEqual(candidate.headlines[0]["provider"], "Dow Jones Factiva · Example Journal")
        self.assertEqual(candidate.headlines[0]["time"], "2026-10-28")
        self.assertEqual(candidate.headlines[0]["matches"][0][0], "M&A / merger arbitrage")
        self.assertEqual(summary["sources_used"], ["Dow Jones Factiva"])
        self.assertEqual(post.call_count, 3)
        self.assertEqual(
            post.call_args_list[0].args[0],
            "https://accounts.dowjones.com/oauth2/v1/token",
        )
        self.assertEqual(
            post.call_args_list[2].args[0],
            "https://api.dowjones.com/content/gen-ai/retrieve",
        )
        self.assertEqual(post.call_args_list[2].kwargs["headers"]["Authorization"], "Bearer bearer-token")
        self.assertFalse(post.call_args_list[0].kwargs["allow_redirects"])

    def test_factiva_without_credentials_warns_without_network_requests(self):
        import ibkr_market_scanner as scanner

        with patch.dict("os.environ", {}, clear=True), patch.object(
            scanner.requests, "post"
        ) as post:
            candidates, warnings, summary = scan_factiva()

        self.assertEqual(candidates, [])
        self.assertTrue(any("FACTIVA_CLIENTID" in warning for warning in warnings))
        self.assertEqual(summary["sources_used"], [])
        post.assert_not_called()

    def test_gs_marquee_connection_uses_client_credentials_and_profile_only(self):
        import ibkr_market_scanner as scanner

        responses = [
            SimpleNamespace(
                status_code=200,
                json=lambda: {"access_token": "temporary-access-token"},
            ),
            SimpleNamespace(
                status_code=200,
                json=lambda: {"id": "profile-id", "email": "private@example.test"},
            ),
        ]
        with patch.dict("os.environ", {
            "GS_MARQUEE_CLIENT_ID": "test-client",
            "GS_MARQUEE_CLIENT_SECRET": "test-secret",
        }, clear=True), patch.object(scanner.requests, "post", return_value=responses[0]) as post, \
             patch.object(scanner.requests, "get", return_value=responses[1]) as get:
            result = check_gs_marquee_connection()

        self.assertEqual(result, {
            "ok": True,
            "message": "Marquee authentication and profile check succeeded.",
        })
        self.assertEqual(
            post.call_args.args[0],
            "https://idfs.gs.com/as/token.oauth2",
        )
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "client_credentials")
        self.assertEqual(post.call_args.kwargs["data"]["scope"], "read_user_profile")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(
            get.call_args.args[0],
            "https://api.marquee.gs.com/v1/users/self",
        )
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"],
            "Bearer temporary-access-token",
        )
        self.assertNotIn("private@example.test", result["message"])

    def test_gs_marquee_connection_requires_local_credentials(self):
        import ibkr_market_scanner as scanner

        with patch.dict("os.environ", {}, clear=True), patch.object(
            scanner.requests, "post"
        ) as post:
            with self.assertRaisesRegex(ValueError, "GS_MARQUEE_CLIENT_ID"):
                check_gs_marquee_connection()
        post.assert_not_called()

    def test_all_sources_keeps_public_results_when_ibkr_is_unavailable(self):
        import ibkr_market_scanner as scanner

        candidate = ScanCandidate(
            contract=SimpleNamespace(
                conId=123, symbol="TEST", localSymbol="TEST", secType="FILING",
                primaryExchange="SEC EDGAR", exchange="SEC EDGAR", currency="",
            ),
            description="Test issuer",
            headlines=[{
                "headline": "Definitive merger proxy",
                "time": "2026-10-28",
                "provider": "SEC EDGAR · DEFM14A",
                "article_id": "filing-1",
                "matches": [("M&A / merger arbitrage", ["DEFM14A"])],
            }],
        )
        profiles = SimpleNamespace(get=lambda cik: {"domicile": "United States", "exchanges": ["NYSE"]})
        with patch.object(scanner, "_scan_from_ibkr", side_effect=RuntimeError("Gateway down")), \
             patch.object(scanner, "_sec_profiles", return_value=profiles), \
             patch.object(scanner, "scan_sec_edgar", return_value=(
                 [candidate], [], {"sources_used": ["SEC EDGAR"], "news_provider_names": ["SEC filings"]}
             )), \
             patch.object(scanner, "scan_online_news", return_value=(
                 [], [], {"sources_used": ["Online news (Google News)"], "news_provider_names": ["Google News RSS"]}
             )), \
             patch.object(scanner, "scan_factiva", return_value=(
                 [], [], {"sources_used": ["Dow Jones Factiva"], "news_provider_names": ["Dow Jones Factiva"]}
             )), \
             patch("global_event_sources.GLOBAL_SOURCES", ()):
            candidates, warnings, summary = _scan_selected_sources("all")

        self.assertEqual(candidates, [candidate])
        self.assertTrue(any("IBKR source unavailable" in warning for warning in warnings))
        self.assertEqual(
            summary["sources_used"],
            ["SEC EDGAR", "Online news (Google News)", "Dow Jones Factiva"],
        )
        self.assertEqual(summary["source_mode"], "all")

    def test_build_scan_jobs_uses_supported_global_locations_and_cap(self):
        parameters = """<ScannerParameters>
          <ScanTypeList>
            <ScanType><scanCode>TOP_PERC_GAIN</scanCode><instruments>STK</instruments></ScanType>
            <ScanType><scanCode>MOST_ACTIVE</scanCode><instruments>STK,BOND</instruments></ScanType>
            <ScanType><scanCode>UNRELATED</scanCode><instruments>STK</instruments></ScanType>
          </ScanTypeList>
          <LocationTree>
            <Location><locationCode>STK.US.MAJOR</locationCode><instruments>STK</instruments></Location>
            <Location><locationCode>STK.GB</locationCode><instruments>STK</instruments></Location>
            <Location><locationCode>BOND.US</locationCode><instruments>BOND</instruments></Location>
          </LocationTree>
        </ScannerParameters>"""

        jobs, total = build_scan_jobs(parameters, max_scans=2)

        self.assertEqual(total, 5)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            {(job.instrument, job.location, job.scan_code) for job in jobs},
            {
                ("BOND", "BOND.US", "MOST_ACTIVE"),
                ("STK", "STK.GB", "TOP_PERC_GAIN"),
            },
        )

    def test_classifies_event_headline_and_scores_research_relevance(self):
        headline = "Company announces merger agreement and strategic review"
        matches = classify_headline(headline)
        candidate = ScanCandidate(
            contract=SimpleNamespace(),
            signals={"STK.US.MAJOR TOP_PERC_GAIN"},
            headlines=[{"matches": matches}],
        )

        self.assertIn("M&A / merger arbitrage", [category for category, _ in matches])
        self.assertIn("Activist / strategic review", [category for category, _ in matches])
        self.assertGreaterEqual(event_relevance_score(candidate), 60)

    def test_classifies_delisting_warnings_and_ticker_symbol_changes(self):
        delisting_matches = classify_headline(
            "Exchange issues delisting warning after minimum bid price deficiency"
        )
        symbol_matches = classify_headline(
            "Example Corp ticker symbol changed from EXM to NEW"
        )
        insider_matches = classify_headline(
            "Director reports open-market purchase in Form 4"
        )
        short_interest_matches = classify_headline(
            "Short interest increases as days to cover rise"
        )

        self.assertIn(
            "Delisting / listing status",
            [category for category, _ in delisting_matches],
        )
        self.assertIn(
            "Corporate action / security change",
            [category for category, _ in symbol_matches],
        )
        self.assertIn(
            "Insider transactions",
            [category for category, _ in insider_matches],
        )
        self.assertIn(
            "Short interest / positioning",
            [category for category, _ in short_interest_matches],
        )

    def test_snapshot_price_uses_live_fields_and_research_lead_includes_capture_time(self):
        self.assertEqual(
            snapshot_market_price(SimpleNamespace(last=12.5, bid=12.4, ask=12.6, close=11.0)),
            12.5,
        )
        self.assertEqual(
            snapshot_market_price(SimpleNamespace(last=float("nan"), bid=9.9, ask=10.1)),
            10.0,
        )
        self.assertIsNone(
            snapshot_market_price(SimpleNamespace(last=None, bid=None, ask=None, close=9.0))
        )

        candidate = ScanCandidate(
            contract=SimpleNamespace(conId=123, symbol="TEST", localSymbol=""),
            description="Test issuer",
            price=12.5,
            quote_time="2026-10-28T12:00:00-07:00",
            headlines=[{
                "headline": "Merger agreement announced",
                "provider": "NEWS",
                "article_id": "story-1",
                "matches": [("M&A / merger arbitrage", ["merger"])],
            }],
            metrics={"Market capitalization": "$1.2B"},
        )
        lead = build_research_leads([candidate])[0]

        self.assertEqual(lead["price"], 12.5)
        self.assertIn("IBKR market-data snapshot 2026-10-28T12:00:00-07:00", lead["sources"])
        self.assertIn("Market capitalization=$1.2B", lead["sources"])

    def test_report_escapes_contract_and_headline_content(self):
        candidate = ScanCandidate(
            contract=SimpleNamespace(
                conId=123,
                symbol="<script>alert(1)</script>",
                localSymbol="",
                secType="STK",
                primaryExchange="NYSE",
                exchange="SMART",
                currency="USD",
            ),
            headlines=[{
                "time": "2026-10-27T12:00:00+00:00",
                "provider": "NEWS",
                "article_id": "story-1",
                "headline": "<img src=x onerror=alert(1)> restructuring offer",
                "matches": [("Restructuring / distress", ["restructur"])],
            }],
            metrics={"Short interest % of float": "12.5% <review>"},
        )

        report = build_report(
            [candidate],
            [],
            {
                "scans_succeeded": 1,
                "scans_total": 1,
                "scans_planned": 1,
                "contracts_found": 1,
                "news_contracts_queried": 1,
                "news_provider_names": ["NEWS"],
                "instruments": ["STK"],
                "locations": ["STK.US"],
            },
        )

        self.assertNotIn("<script>alert(1)</script>", report)
        self.assertNotIn("<img src=x onerror=alert(1)>", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("&lt;img", report)
        self.assertIn("Additional data:", report)
        self.assertIn("12.5% &lt;review&gt;", report)
        self.assertIn("addEventListener('change',filterCards)", report)
        self.assertIn("document.querySelectorAll('article.idea')", report)
        self.assertIn("filterStatus", report)

    def test_live_report_has_manual_refresh_and_workbook_research_tabs(self):
        report = build_live_report(
            [],
            [],
            {
                "scans_succeeded": 0,
                "scans_total": 0,
                "scans_planned": 0,
                "contracts_found": 0,
                "news_contracts_queried": 0,
                "news_provider_names": [],
                "instruments": [],
                "locations": [],
            },
            "2026-10-28 12:00:00 PDT",
        )

        self.assertIn("Refresh selected sources", report)
        self.assertIn("Test GS Marquee connection", report)
        self.assertIn("/api/gs-marquee-check", report)
        self.assertIn("Special situations &amp; EV analysis", report)
        self.assertIn("localStorage", report)
        self.assertIn("/api/export", report)
        self.assertIn("manual", report.lower())

    def test_live_scanner_name_links_to_matching_research_row(self):
        candidate = ScanCandidate(
            contract=SimpleNamespace(
                conId=123,
                symbol="TEST",
                localSymbol="",
                secType="STK",
                primaryExchange="NYSE",
                exchange="SMART",
                currency="USD",
            ),
            description="Test issuer",
            headlines=[{
                "time": "2026-10-27T12:00:00+00:00",
                "provider": "NEWS",
                "article_id": "story-1",
                "headline": "Merger agreement announced",
                "matches": [("M&A / merger arbitrage", ["merger"])],
            }],
        )
        report = build_live_report(
            [candidate],
            [],
            {
                "scans_succeeded": 1,
                "scans_total": 1,
                "scans_planned": 1,
                "contracts_found": 1,
                "news_contracts_queried": 1,
                "news_provider_names": ["NEWS"],
                "instruments": ["STK"],
                "locations": ["STK.US"],
            },
            "2026-10-28 12:00:00 PDT",
        )

        self.assertIn('href="#deal-analysis" data-analysis-id="123"', report)
        self.assertIn("querySelector('[data-tab=\"deal-analysis\"]').click()", report)
        self.assertIn("renderSelectedAnalysis(selectedAnalysisId)", report)
        self.assertIn("renderDetailFields(RESEARCH_COLUMNS,research,'research'", report)
        self.assertIn("renderDetailFields(ANALYSIS_COLUMNS,analysis,'analysis'", report)
        self.assertIn("Price", report)
        self.assertIn("Why mispriced (forced flow)", report)
        self.assertIn("Downside if wrong (valuation, opinion)", report)
        self.assertIn("Probability (opinion)", report)
        self.assertIn('id="selectedAnalysis"', report)

    def test_excel_report_matches_research_workbook_and_calculates_ev(self):
        stream = BytesIO()
        event_candidate = ScanCandidate(
            contract=SimpleNamespace(
                symbol="TEST",
                localSymbol="TEST",
                secType="STK",
                primaryExchange="NASDAQ",
                exchange="SMART",
                currency="USD",
                conId=123,
            ),
            description="Test issuer",
            headlines=[{
                "time": "2026-10-28",
                "provider": "Benzinga",
                "article_id": "story-1",
                "headline": "Test issuer reports short interest",
                "matches": [("Short interest / positioning", ["short interest"])],
            }],
            metrics={"Short interest % of float": "12.5%"},
        )
        research = {
            "123": {
                "id": "123", "tier": "Scan lead", "ticker": "TEST",
                "company": "Test issuer", "situation_type": "Merger arb",
                "price": "12.5", "sources": "Provider:story; IBKR snapshot at test time",
            }
        }
        analysis = {
            "123": {
                "id": "123", "ticker": "TEST", "situation_type": "Merger arb",
                "upside": "30", "downside": "-20", "probability": "60",
            }
        }

        build_excel_report(
            [event_candidate],
            [],
            {
                "host": "127.0.0.1",
                "port": 4002,
                "scans_succeeded": 1,
                "scans_total": 1,
                "scans_planned": 1,
                "contracts_found": 1,
                "news_contracts_queried": 1,
                "news_provider_names": ["NEWS"],
                "instruments": ["STK"],
                "locations": ["STK.US"],
            },
            stream,
            research,
            analysis,
        )
        workbook = load_workbook(stream, data_only=False)

        special = workbook["Special Situations"]
        ev = workbook["Analysis - EV Ranked"]
        event_matches = workbook["Event Matches"]
        self.assertEqual(event_matches["O1"].value, "Vendor metrics")
        self.assertEqual(event_matches["O2"].value, "Short interest % of float=12.5%")
        self.assertEqual(
            [cell.value for cell in special[1]],
            [label for _, label in SPECIAL_SITUATIONS_COLUMNS],
        )
        self.assertEqual(special["B2"].value, "TEST")
        self.assertEqual(special["G2"].value, 12.5)
        self.assertIn("IBKR snapshot at test time", special["R2"].value)
        self.assertEqual(
            [cell.value for cell in ev[1]],
            [label for _, label in ANALYSIS_COLUMNS],
        )
        self.assertEqual(ev["H2"].value, 30)
        self.assertEqual(ev["I2"].value, -20)
        self.assertEqual(ev["J2"].value, 60)
        self.assertEqual(ev["K2"].data_type, "f")
        self.assertIn("H2*J2/100+I2*(1-J2/100)", ev["K2"].value)

    def test_local_dashboard_refresh_endpoint_updates_snapshot_and_exports_edits(self):
        state = {
            "candidates": [],
            "warnings": [],
            "summary": {
                "host": "127.0.0.1",
                "port": 4002,
                "scans_succeeded": 0,
                "scans_total": 0,
                "scans_planned": 0,
                "contracts_found": 0,
                "news_contracts_queried": 0,
                "news_provider_names": [],
                "instruments": [],
                "locations": [],
            },
            "updated_at": "before refresh",
            "source_mode": "public",
            "refresh_lock": threading.Lock(),
        }

        def refresh(source_mode):
            self.assertEqual(source_mode, "news")
            state["updated_at"] = "after refresh"
            return state["updated_at"]

        server = create_live_dashboard_server(state, refresh)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = Request(
                base_url + "/api/refresh",
                data=json.dumps({"source_mode": "news"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(json.load(response)["updated_at"], "after refresh")
            with urlopen(base_url + "/") as response:
                page = response.read().decode("utf-8")
            self.assertIn("after refresh", page)

            payload = json.dumps({
                "research": [{"ticker": "TEST", "company": "Editable row"}],
                "analysis": [{"ticker": "TEST", "upside": "30", "downside": "-20", "probability": "60"}],
            }).encode("utf-8")
            request = Request(
                base_url + "/api/export",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                workbook = load_workbook(BytesIO(response.read()), data_only=False)
            self.assertEqual(workbook["Special Situations"]["B2"].value, "TEST")
            self.assertEqual(workbook["Analysis - EV Ranked"]["K2"].data_type, "f")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
