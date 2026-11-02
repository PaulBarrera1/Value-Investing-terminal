@echo off
rem Double-click to open the Special Situations dashboard in your browser.
rem Close this window to stop the dashboard.
cd /d "%~dp0"
if not exist reports mkdir reports
rem Free regulator and exchange feeds load in a few minutes; the full IBKR scanner
rem sweep (465 requests) takes over an hour, so pick it from the dashboard when needed.
set "IBKR_SCAN_SOURCE=public"
rem Live IB Gateway port, matching run_daily_scan.cmd; paper Gateway is 4002.
if not defined IBKR_PORT set "IBKR_PORT=4001"
if not defined IBKR_DASHBOARD_PORT set "IBKR_DASHBOARD_PORT=8765"
set "PYTHONIOENCODING=utf-8"
set "PYTHON=C:\Users\KLevy\AppData\Local\Python\pythoncore-3.15-64\python.exe"
if not exist "%PYTHON%" set "PYTHON=py"
"%PYTHON%" -c "import ibkr_market_scanner as s; s.run_live_dashboard(open_browser=True)"
pause
