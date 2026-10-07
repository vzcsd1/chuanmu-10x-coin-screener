@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo Archive collection (v2 queue, chunked, auto-resume).
echo Re-run this file at any time to continue from where it stopped.
echo   Queue : reports\archive_download_ds_full\queue_v2.csv
echo   Log   : reports\archive_download_ds_full\run_v2.log
echo   PID   : reports\archive_download_ds_full\download_v2.pid
echo   State : data\archive_raw\_state.json
echo   Stop  : taskkill /PID ^<download_v2.pid^>
echo   Note  : if you must restart within 5 min of a stop, first delete
echo           data\archive_raw\.archive_download.lock
echo.

py -3.10 tools\archive_queue_ds.py run --version v2 --chunk 2000 --interval-sec 0.02 --max-minutes 0 --min-free-gib 20 --workers 4

echo.
echo Exited. Press any key to close.
pause >nul
endlocal
