@echo off
rem Daily IBKR event scan, run by Windows Task Scheduler. Reports and log go to reports\.
cd /d "%~dp0"
if not exist reports mkdir reports
rem Live IB Gateway port (read-only connection); paper Gateway is 4002.
if not defined IBKR_PORT set "IBKR_PORT=4001"
set "PYTHON=C:\Users\KLevy\AppData\Local\Python\pythoncore-3.15-64\python.exe"
echo ==== %date% %time% ====>> reports\scan_log.txt
"%PYTHON%" ibkr_market_scanner.py >> reports\scan_log.txt 2>&1
