@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo Forward collection (M13 spread / M14 liquidation / 5m OI).
echo This process must keep running - every minute it is off is data lost forever.
echo   Data   : data\forward\
echo   Health : data\forward\_health\YYYY-MM-DD.jsonl
echo   Status : py -3.10 tools\forward_collect_ds.py status
echo   Stop   : Ctrl+C in this window
echo.
echo Known limitation on this machine: futures WebSocket does not deliver
echo push frames through the proxy, so the liquidations lane will log
echo "unavailable" every 30 min and collect nothing. That is expected.
echo The bookticker and oi5m lanes are unaffected.
echo.

py -3.10 tools\forward_collect_ds.py run --lanes bookticker,oi5m,liquidations

echo.
echo Exited. Press any key to close.
pause >nul
endlocal
