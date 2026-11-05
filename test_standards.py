import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import standards
from ibkr_market_scanner import ScanCandidate, apply_standards, build_research_leads


def headline(provider, text="", labels=()):
    return {"provider": provider, "headline": text, "article_id": text, "matches": [("x", list(labels))]}


class UniverseTests(unittest.TestCase):
    def test_edgar_addresses_map_to_countries(self):
        self.assertEqual(standards.edgar_country("NY", "NY"), "United States")
        self.assertEqual(standards.edgar_country("A6", "Ontario, Canada"), "Canada")
        self.assertEqual(standards.edgar_country("K3", "Hong Kong"), "Hong Kong")
        self.assertEqual(standards.edgar_country("D0", "Bermuda"), "Bermuda")
        self.assertEqual(standards.edgar_country("R5", "Peru"), "Peru")
        self.assertEqual(standards.edgar_country("P8", "Netherlands Antilles"), "Curacao")

    def test_msci_developed_markets_by_domicile_then_listing(self):
        self.assertTrue(standards.market_status("Canada")[0])
        self.assertFalse(standards.market_status("Peru", "United States")[0])
        self.assertFalse(standards.market_status("China", "United States")[0])
        ok, basis = standards.market_status("Bermuda", "United States")
        self.assertTrue(ok)
        self.assertIn("offshore", basis)
        self.assertFalse(standards.market_status("United States", otc_only=True)[0])
        self.assertIn("mainland-China", standards.market_status("", "Hong Kong")[1])

    def test_sec_profile_otc_only_is_out(self):
        self.assertFalse(standards.sec_market_status({"domicile": "United States", "exchanges": ["OTC"]})[0])
        self.assertTrue(standards.sec_market_status({"domicile": "United States", "exchanges": ["Nasdaq", "OTC"]})[0])

    def test_listing_from_isin_or_suffix(self):
        self.assertEqual(standards.listing_country("GB00BF3SQB83"), "United Kingdom")
        self.assertEqual(standards.listing_country("GG00BMHG0H12"), "Guernsey")
        self.assertEqual(standards.listing_country("FXG.AX"), "Australia")
        self.assertEqual(standards.listing_country("1788.HK"), "Hong Kong")


class CatalystTests(unittest.TestCase):
    def test_sec_target_documents_are_hard_and_keyword_hits_are_not(self):
        self.assertEqual(standards.headline_catalyst(headline("SEC EDGAR · DEFM14A"))[0], standards.MERGER)
        self.assertEqual(standards.headline_catalyst(headline("SEC EDGAR · 10-12B"))[0], standards.SPIN)
        self.assertEqual(standards.headline_catalyst(headline("SEC EDGAR · SC TO-I"))[0], standards.ISSUER_TENDER)
        for form in ("8-K", "425", "SC 13D/A", "25-NSE", "SC TO-T (bidder)"):
            kind, reason = standards.headline_catalyst(headline(f"SEC EDGAR · {form}"))
            self.assertIsNone(kind, form)
            self.assertTrue(reason)

    def test_exchange_feeds_keep_offers_and_drop_routine_items(self):
        self.assertEqual(standards.headline_catalyst(headline("UK Takeover Panel"))[0], standards.MERGER)
        self.assertEqual(standards.headline_catalyst(
            headline("ASX announcements", labels=["Scheme of Arrangement"]))[0], standards.MERGER)
        self.assertIsNone(standards.headline_catalyst(headline("ASX announcements", labels=["Trading Halt"]))[0])
        self.assertIsNone(standards.headline_catalyst(headline("ASX announcements", labels=["Asset Acquisition"]))[0])
        self.assertIsNone(standards.headline_catalyst(
            headline("HKEXnews", "Disclosure of Dealings under Rule 22", ["Takeovers Code - dealing disclosures"]))[0])
        self.assertIsNone(standards.headline_catalyst(
            headline("HKEXnews", "RESULTS OF THE RIGHTS ISSUE", ["Allotment Results / Rights Issue"]))[0])
        self.assertEqual(standards.headline_catalyst(
            headline("HKEXnews", "Proposed privatisation by way of scheme", ["Announcement by Offeree Company"]))[0],
            standards.MERGER)
        self.assertIsNone(standards.headline_catalyst(headline("Nasdaq trade halts"))[0])
        self.assertIsNone(standards.headline_catalyst(headline("Google News · Reuters"))[0])


class BidderTests(unittest.TestCase):
    def test_shared_tender_filing_marks_the_non_subject_party_as_bidder(self):
        import global_event_sources as sources

        def party(cik, symbol):
            return ScanCandidate(contract=SimpleNamespace(conId=cik, symbol=symbol), description=symbol, headlines=[{
                "provider": "SEC EDGAR · SC TO-T", "article_id": "0001140361-26-041224",
                "headline": f"{symbol}: Third-party tender offer (SC TO-T)", "matches": []}])

        target, bidder = party(1396814, "PCRX"), party(1792044, "VTRS")
        header = SimpleNamespace(text="<SUBJECT-COMPANY>\n<COMPANY-DATA>\n<CONFORMED-NAME>Pacira\n<CIK>0001396814\n")
        with patch.object(sources, "_http_get", return_value=header):
            sources._mark_bidders([target, bidder], "agent", [])
        self.assertEqual(target.headlines[0]["provider"], "SEC EDGAR · SC TO-T")
        self.assertEqual(bidder.headlines[0]["provider"], "SEC EDGAR · SC TO-T (bidder)")
        self.assertIsNone(standards.screen_candidate(bidder)[0])
        self.assertEqual(standards.screen_candidate(target)[0], standards.MERGER)


class RiskModelTests(unittest.TestCase):
    def test_flags_read_conditions_and_respect_negation(self):
        text = ("The transaction is not subject to a financing condition. Buyer received a second request from the "
                "FTC. Completion requires approval from SAMR. Holders of 30% signed voting agreements. The outside "
                "date is March 31, 2027. A termination fee of $45 million is payable.")
        keys = {f["key"] for f in standards.risk_flags(text)}
        self.assertTrue({"no_financing", "second_request", "samr", "support", "outside_date", "termination_fee"} <= keys)
        self.assertNotIn("financing", keys)

    def test_boilerplate_does_not_raise_false_flags(self):
        text = ("Target's lead asset is in a Phase 2 clinical trial. The agreement restricts soliciting any "
                "unsolicited proposal, subject to fiduciary duties.")
        keys = {f["key"] for f in standards.risk_flags(text)}
        self.assertNotIn("eu_phase2", keys)
        self.assertNotIn("hostile", keys)

    def test_probability_moves_with_flags_and_is_bounded(self):
        flags = standards.risk_flags("A second request was received. SAMR approval is required.")
        p, basis = standards.completion_probability(False, flags)
        self.assertAlmostEqual(p, 0.92 - 0.08 - 0.08)
        self.assertIn("HSR second request", basis)
        self.assertEqual(standards.completion_probability(True, [])[0], 0.94)

    def test_wide_spread_clears_but_flags_a_market_disagreement(self):
        risk = standards.evaluate_deal(price=90, value=100, days_guided=60, tender=False, flags=[],
                                       undisturbed=75, pulled=date(2026, 11, 3))
        self.assertAlmostEqual(risk["break_price"], 67.5)
        self.assertEqual(risk["days_expected"], 90)
        self.assertAlmostEqual(risk["risk_adjusted_annualized"], (0.92 * (100 / 90 - 1) + 0.08 * (67.5 / 90 - 1)) * 365 / 90)
        self.assertTrue(risk["clears_hurdle"])
        self.assertAlmostEqual(risk["market_probability"], 0.25 / (100 / 90 - 0.75), places=6)
        self.assertEqual(risk["verdict"], "Clears, but verify: market disagrees")
        self.assertTrue(any("more confident than the market" in w for w in risk["what_could_be_wrong"]))
        self.assertLessEqual(risk["model_size"], standards.NAV_USD * standards.MAX_POSITION)

    def test_tight_spread_fails_and_says_what_it_would_need(self):
        risk = standards.evaluate_deal(price=97, value=100, days_guided=60, tender=False, flags=[], undisturbed=75)
        self.assertFalse(risk["clears_hurdle"])
        self.assertTrue(risk["verdict"].startswith("Below hurdle"))
        self.assertGreater(risk["probability_needed"], risk["probability"])

    def test_stock_below_its_pre_deal_price_is_not_a_free_option(self):
        # Pre-deal 60, market up 10%: the anchor (59.4 after haircut) is above the 50 price.
        risk = standards.evaluate_deal(price=50, value=80, days_guided=60, tender=False, flags=[],
                                       undisturbed=60, market_drift=1.10)
        self.assertLess(risk["break_loss"], 0)
        self.assertAlmostEqual(risk["break_price"], 50 * (1 - standards.FALLBACK_BREAK_LOSS))
        self.assertEqual(risk["verdict"], "Clears, but verify: market disagrees")
        self.assertTrue(any("below its pre-deal price" in w for w in risk["what_could_be_wrong"]))
        self.assertIsNotNone(risk["market_probability"])
        self.assertGreaterEqual(risk["market_probability"], 0)

    def test_deal_off_language_is_flagged_but_termination_clauses_are_not(self):
        off = standards.risk_flags("Seer to Remain an Independent Public Company Following Conclusion of Review.")
        self.assertIn("deal_off", {f["key"] for f in off})
        boilerplate = standards.risk_flags("The merger agreement may be terminated by either party if the merger "
                                           "is not completed by the End Date, and at the time of such termination, "
                                           "Buyer could have terminated the Merger Agreement. If the merger is not "
                                           "completed, UniFirst will remain an independent public company. The board "
                                           "evaluated the plan were it to remain an independent public company, "
                                           "including forecasts.")
        self.assertNotIn("deal_off", {f["key"] for f in boilerplate})

    def test_analyst_probability_overrides_the_model(self):
        risk = standards.evaluate_deal(price=90, value=100, days_guided=60, tender=False, flags=[],
                                       undisturbed=75, probability=0.5)
        self.assertEqual(risk["probability"], 0.5)
        self.assertEqual(risk["model_probability"], 0.92)
        self.assertFalse(risk["clears_hurdle"])


class ScannerStandardsTests(unittest.TestCase):
    def candidate(self, symbol, provider, sec_type="STK", exchange="", labels=(), cik=""):
        contract = SimpleNamespace(conId=1, symbol=symbol, localSymbol=symbol, secType=sec_type,
                                   primaryExchange=exchange, exchange=exchange, currency="")
        return ScanCandidate(contract=contract, description=symbol, research={"cik": cik} if cik else {},
                             headlines=[{**headline(provider, f"{symbol} event", labels), "time": "2026-11-03"}])

    def test_keeps_hard_catalysts_in_developed_markets_with_reasons_for_the_rest(self):
        profiles = {"1": {"domicile": "United States", "exchanges": ["NYSE"]},
                    "2": {"domicile": "Peru", "exchanges": ["NYSE"]}}
        leads = [
            self.candidate("ACME", "SEC EDGAR · DEFM14A", "FILING", "SEC EDGAR", cik="1"),
            self.candidate("CPAC", "SEC EDGAR · DEFM14A", "FILING", "SEC EDGAR", cik="2"),
            self.candidate("ULBI", "SEC EDGAR · 8-K", "FILING", "SEC EDGAR", cik="1"),
            self.candidate("FXG.AX", "ASX announcements", labels=["Trading Halt"]),
            self.candidate("GB00BF3SQB83", "UK Takeover Panel", exchange="UK Takeover Panel"),
            self.candidate("Unverified", "Google News · Law firm"),
        ]
        with patch("ibkr_market_scanner._sec_profiles",
                   return_value=SimpleNamespace(get=lambda cik: profiles[cik])):
            kept, screened = apply_standards(leads)
        self.assertEqual([c.description for c in kept], ["ACME", "GB00BF3SQB83"])
        reasons = {row["ticker"]: row["reason"] for row in screened}
        self.assertIn("Peru", reasons["CPAC"])
        self.assertIn("8-K", reasons["ULBI"])
        self.assertIn("Trading halt", reasons["FXG.AX"].replace("Trading Halt", "Trading halt"))
        self.assertIn("Secondary source", reasons["Unverified"])
        lead = build_research_leads(kept)[0]
        self.assertEqual(lead["tier"], "Hard catalyst · Merger / takeover")
        self.assertIn("risk-adjusted hurdle", lead["risks"])


if __name__ == "__main__":
    unittest.main()
