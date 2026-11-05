import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

import deal_sheet as ds


class ExtractTermsTests(unittest.TestCase):
    def test_cash_deal_with_guided_close(self):
        text = ("Acme Corp. today announced a definitive agreement under which Buyer will acquire Acme for "
                "$70.00 per share in cash. The transaction is expected to close in the first half of 2027, "
                "subject to regulatory approvals. Acme stockholders will receive $70.00 per share in cash.")
        terms = ds.extract_terms(text)
        self.assertEqual(terms["cash"], 70.0)
        self.assertEqual(terms["ratio"], 0.0)
        self.assertEqual(terms["close_date"], date(2027, 6, 30))
        self.assertFalse(terms["tender"])

    def test_cash_and_stock_with_acquirer(self):
        text = ("each share of UniFirst common stock will be converted into the right to receive $155.00 in cash "
                "and 0.7720 shares of Cintas common stock. The merger is expected to close in the second quarter of 2027.")
        terms = ds.extract_terms(text)
        self.assertEqual(terms["cash"], 155.0)
        self.assertEqual(terms["ratio"], 0.772)
        self.assertEqual(terms["acquirer"], "Cintas")
        self.assertEqual(terms["close_date"], date(2027, 6, 30))

    def test_tender_with_cvr(self):
        text = ("The Offer to Purchase is for $8.00 per share in cash plus one contingent value right, which "
                "represents the right to receive up to $3.00 per share upon milestones. The tender offer will "
                "expire at one minute after 11:59 p.m., Eastern Time, on November 17, 2026, unless extended.")
        terms = ds.extract_terms(text)
        self.assertEqual(terms["cash"], 8.0)
        self.assertTrue(terms["cvr"])
        self.assertEqual(terms["cvr_max"], 3.0)
        self.assertTrue(terms["tender"])
        self.assertEqual(terms["close_date"], date(2026, 11, 17))


class DealRoleTests(unittest.TestCase):
    TEXT = ("Viatris Inc. today announced that it has agreed to acquire Pacira BioSciences, Inc. for $36.50 per "
            "share in cash. Pacira stockholders will receive $36.50 per share in cash for each share of Pacira common stock.")

    def test_target_and_acquirer_are_told_apart(self):
        self.assertEqual(ds.deal_role(self.TEXT, "Pacira BioSciences, Inc."), "target")
        self.assertEqual(ds.deal_role(self.TEXT, "Viatris Inc"), "acquirer")

    def test_short_name(self):
        self.assertEqual(ds.short_name("TRICO BANCSHARES /"), "trico")
        self.assertEqual(ds.short_name("Maplebear Inc."), "maplebear")
        self.assertEqual(ds.short_name("C. H. ROBINSON WORLDWIDE, INC."), "robinson")


class DealRowTests(unittest.TestCase):
    def _deal(self, **kwargs):
        deal = ds.Deal("123", "BHF", "Brighthouse Financial")
        for key, value in {"cash": 70.0, "price": 44.73, "close_date": date(2026, 12, 6),
                           "close_basis": "guided", "undisturbed": 40.0, "domicile": "United States",
                           "exchanges": ["Nasdaq"], **kwargs}.items():
            setattr(deal, key, value)
        return deal

    def test_cash_deal_matches_workbook_spread(self):
        row = ds.deal_row(self._deal(), date(2026, 10, 28))
        self.assertAlmostEqual(row["gross_spread"], 0.565, delta=0.001)
        self.assertEqual(row["days"], 39)
        self.assertEqual(row["tier"], ds.TIER_VERIFY)   # a 56% spread means the market sees real risk
        self.assertAlmostEqual(row["downside"], 40 / 44.73 - 1)
        self.assertEqual(row["risk"]["days_expected"], 39 + 30)   # merger slip

    def test_acquirer_price_is_rejected(self):
        self.assertIsNone(ds.deal_row(self._deal(price=150.0), date(2026, 10, 28)))

    def test_negative_spread_is_screened_and_missing_date_is_estimated(self):
        row = ds.deal_row(self._deal(price=72.0, close_date=None), date(2026, 10, 28))
        self.assertEqual(row["tier"], "Screened out")
        self.assertIn("Negative spread", row["screen_reason"])
        self.assertTrue(row["estimated_close"])
        self.assertEqual(row["days"], ds.DEFAULT_MERGER_DAYS)

    def test_tight_spread_fails_the_risk_adjusted_hurdle(self):
        row = ds.deal_row(self._deal(price=69.0, undisturbed=50.0), date(2026, 10, 28))
        self.assertGreater(row["annualized"], 0.10)
        self.assertEqual(row["tier"], "Screened out")
        self.assertTrue(row["screen_reason"].startswith("Below hurdle"))

    def test_deal_off_in_the_filing_is_screened_out(self):
        flags = ds.standards.risk_flags("Acme to Remain an Independent Public Company Following Strategic Review.")
        row = ds.deal_row(self._deal(flags=flags), date(2026, 10, 28))
        self.assertEqual(row["tier"], "Screened out")
        self.assertIn("deal is off", row["screen_reason"])

    def test_emerging_market_domicile_is_screened_out(self):
        row = ds.deal_row(self._deal(domicile="Peru", exchanges=["NYSE"]), date(2026, 10, 28))
        self.assertEqual(row["tier"], "Screened out")
        self.assertIn("Peru is not an MSCI developed market", row["screen_reason"])

    def test_stock_deal_needs_acquirer_price(self):
        deal = self._deal(cash=0.0, ratio=1.5401, acquirer_ticker="INDV", acquirer_price=None, price=40.96)
        self.assertIsNone(ds.deal_row(deal, date(2026, 10, 28)))
        deal.acquirer_price = 34.72
        row = ds.deal_row(deal, date(2026, 10, 28))
        self.assertAlmostEqual(row["consideration_value"], 53.47, delta=0.01)
        self.assertIn("1.5401 x INDV", row["consideration_text"])


class WorkbookTests(unittest.TestCase):
    def test_layout_and_ev_formula(self):
        row = ds.deal_row(DealRowTests()._deal(), date(2026, 10, 28))
        with tempfile.TemporaryDirectory() as folder:
            path = ds.write_workbook([row], datetime(2026, 10, 28, 18, 0).astimezone(), Path(folder) / "x.xlsx", ["note"])
            workbook = load_workbook(path)
        sheet, analysis, screened = workbook.worksheets
        self.assertEqual(sheet.title, "Special Situations 2026-10-28")
        self.assertEqual([c.value for c in sheet[4]], ds.SHEET_COLUMNS)
        self.assertEqual(sheet["A5"].value, f"{ds.TIER_VERIFY} (1)")
        self.assertEqual(sheet["B6"].value, "BHF")
        self.assertIn("Termination", sheet.cell(row=6, column=22).value)   # kill criteria
        self.assertEqual(analysis.title, "Analysis - EV Ranked")
        self.assertEqual(analysis["K5"].value, "=J5*H5+(1-J5)*I5")
        self.assertEqual(analysis["L5"].value, "=K5*365/G5")
        self.assertEqual(analysis["M5"].value, '=IF(L5>=0.25,"Yes","No")')
        self.assertEqual(screened.title, "Screened Out")

    def test_screened_rows_go_to_their_own_tab_with_reasons(self):
        row = ds.deal_row(DealRowTests()._deal(domicile="Peru"), date(2026, 10, 28))
        with tempfile.TemporaryDirectory() as folder:
            path = ds.write_workbook([row], datetime(2026, 10, 28, 18, 0).astimezone(), Path(folder) / "x.xlsx", [])
            workbook = load_workbook(path)
        sheet, analysis, screened = workbook.worksheets
        self.assertIn("No priced deal clears", sheet["A5"].value)
        self.assertIsNone(analysis["B5"].value)
        self.assertEqual(screened["A4"].value, "BHF")
        self.assertIn("Outside universe", screened["D4"].value)


if __name__ == "__main__":
    unittest.main()
