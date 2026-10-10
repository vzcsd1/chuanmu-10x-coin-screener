@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ================================================
echo        川沐十倍币筛选 - 一键上传 GitHub
echo ================================================
echo.

rem --- 1. 检查代理是否可用 ---
echo [1/5] 检查代理 (Clash 127.0.0.1:7897) ...
curl -s -o nul --max-time 5 -x http://127.0.0.1:7897 https://github.com
if errorlevel 1 (
    echo.
    echo   [X] 代理不通！请先打开 Clash 再重试。
    echo.
    pause
    exit /b 1
)
echo   [OK] 代理正常
echo.

rem --- 2. 显示改动 ---
echo [2/5] 本次改动文件：
git status --short
echo.
git status --porcelain | findstr /r "." >nul
if errorlevel 1 (
    echo   没有任何改动，无需上传。
    echo.
    pause
    exit /b 0
)

rem --- 3. 输入说明 ---
echo [3/5] 请输入本次改动说明（直接回车用默认）：
set "MSG="
set /p MSG="  ^> "
if "!MSG!"=="" set "MSG=更新项目文件"

rem --- 4. 提交 ---
echo.
echo [4/5] 正在提交 ...
git add -A
git commit -m "!MSG!"
if errorlevel 1 (
    echo   [X] 提交失败，请查看上方错误信息。
    pause
    exit /b 1
)
echo   [OK] 提交完成
echo.

rem --- 5. 推送 ---
echo [5/5] 正在推送到 GitHub（大改动可能需要等待）...
git push
if errorlevel 1 (
    echo.
    echo   [!] 第一次推送失败，正在用加大缓冲重试 ...
    git -c http.postBuffer=524288000 -c http.version=HTTP/1.1 push
    if errorlevel 1 (
        echo   [X] 推送仍然失败，请截图发给 AI 排查。
        pause
        exit /b 1
    )
)
echo   [OK] 推送成功！
echo.
echo ================================================
echo   完成！查看：https://github.com/vzcsd1/chuanmu-10x-coin-screener
echo ================================================
echo.
pause
