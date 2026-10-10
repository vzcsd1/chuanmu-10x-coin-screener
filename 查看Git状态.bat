@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo        川沐十倍币筛选 - Git 状态查看
echo ================================================
echo.

echo --- 1. 当前改动 ---
git status --short
echo.
git status --porcelain | findstr /r "." >nul
if errorlevel 1 echo   （工作区干净，没有未提交的改动）
echo.

echo --- 2. 最近 10 次提交 ---
git log --oneline -10
echo.

echo --- 3. 本地与云端差异 ---
git fetch --quiet 2>nul
for /f %%i in ('git rev-list --count origin/main..HEAD 2^>nul') do set AHEAD=%%i
echo   本地领先云端: %AHEAD% 次提交
if "%AHEAD%"=="0" echo   （本地与云端一致）
echo.

echo --- 4. 仓库地址 ---
git remote -v
echo.

pause
