@echo off
rem Guan Chao - personal coin-picking workbench - double-click launcher.
rem
rem This file is deliberately ASCII-ONLY. Do not add Chinese here:
rem cmd.exe reads .bat as GBK, and UTF-8 Chinese bytes can swallow the
rem line break, merging the next line into this one (that is a real bug
rem we already hit once). All Chinese output lives in start_ui.ps1,
rem which is saved as UTF-8 with BOM so PowerShell reads it correctly.
rem
rem Usage:
rem   double-click              -> real mode (calls the real screener)
rem   from a terminal, add      -> -Demo   (built-in samples, no network)
rem
rem Stop: close this window, or press Ctrl+C.
rem As long as this window stays open, the server keeps running.

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_ui.ps1" %*

if errorlevel 1 (
    echo.
    echo [START FAILED] See the reason above. Press any key to close.
    pause >nul
)
