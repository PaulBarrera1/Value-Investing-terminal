# Value-Investing-terminal
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

 Tech Stack
* **Language:** Python 3.x
* **Financial Data:** `yfinance`, `alpha_vantage`
* **Data Processing & Visualization:** `pandas`, `plotly`, `HTML/CSS`

Architecture & Philosophy
Built around a quantitative investment framework to eliminate manual data collection and accelerate fundamental analysis. Designed for scalable data modeling and institutional-grade visualization.

 Quick Start
1. Clone this repository.
2. Install required packages: `pip install yfinance pandas alpha_vantage plotly`
3. Add your Alpha Vantage API key to `AV_KEY`.
4. Run the script, enter your target ticker (e.g., `META`, `JNJ`), and view your report.
