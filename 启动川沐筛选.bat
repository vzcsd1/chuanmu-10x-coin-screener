@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Please install Python 3 and try again.
    goto :end
)

python "%~dp0run_chuanmu.py"
echo.
if errorlevel 1 echo The program exited with an error.
:end
echo Press any key to close this window.
pause >nul
endlocal
