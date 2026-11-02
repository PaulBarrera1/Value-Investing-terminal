@echo off
rem Double-click to build today's special-situations deal sheet and open it in Excel.
rem Finds pending US mergers and tender offers in SEC filings, prices them, and adds deals at or
rem above 25% annualized to reports\special_situations_tracker.xlsx. Takes about 4 minutes.
cd /d "%~dp0"
if not exist reports mkdir reports
set "PYTHONIOENCODING=utf-8"
set "PYTHON=C:\Users\KLevy\AppData\Local\Python\pythoncore-3.15-64\python.exe"
if not exist "%PYTHON%" set "PYTHON=py"
"%PYTHON%" deal_sheet.py --open
if errorlevel 1 pause
