# 观潮 · 个人选币工作台 —— 启动脚本
# 用法：在本目录的 PowerShell 里执行：
#   powershell -ExecutionPolicy Bypass -File .\start_ui.ps1          # 真实模式
#   powershell -ExecutionPolicy Bypass -File .\start_ui.ps1 -Demo    # 演示模式（内置样例）
# 停止：在本窗口按 Ctrl+C，或直接关闭本窗口。
param(
    [switch]$Demo
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = "py"
try {
    & $py -3.10 --version | Out-Null
} catch {
    Write-Host "未找到 Python 3.10（py -3.10）。请先安装 Python 3.10。" -ForegroundColor Red
    exit 1
}

& $py -3.10 -c "import flask" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少界面依赖 flask。请运行下面这一条命令安装后重试：" -ForegroundColor Yellow
    Write-Host "    py -3.10 -m pip install -r ui\requirements-ui.txt" -ForegroundColor Cyan
    exit 1
}

$port = 8765
$env:CHUANMU_UI_PORT = "$port"
if ($Demo) { $env:CHUANMU_UI_DEMO = "1" } else { Remove-Item Env:CHUANMU_UI_DEMO -ErrorAction SilentlyContinue }

# 若端口已被占用，多半是上一轮服务还在跑：直接打开页面，不重复启动。
$portBusy = $false
try {
    $probe = New-Object System.Net.Sockets.TcpClient
    $probe.Connect("127.0.0.1", $port)
    $probe.Close()
    $portBusy = $true
} catch { $portBusy = $false }

if ($portBusy) {
    Write-Host "检测到 $port 端口已有服务在运行，直接打开页面（未重复启动）。" -ForegroundColor Yellow
    Start-Process "http://127.0.0.1:$port"
    exit 0
}

Write-Host "正在启动观潮工作台（$(if ($Demo) {'演示模式'} else {'真实模式'})）..." -ForegroundColor Green

$proc = Start-Process -FilePath $py -ArgumentList "-3.10", "ui\app.py" `
    -WorkingDirectory $root -PassThru -NoNewWindow

# 等服务就绪（最多 20 秒），未就绪不假报成功。
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if ($proc.HasExited) { break }
    try {
        $probe = New-Object System.Net.Sockets.TcpClient
        $probe.Connect("127.0.0.1", $port)
        $probe.Close()
        $ready = $true
        break
    } catch { }
}

if (-not $ready) {
    Write-Host "服务未能启动。请在本目录手动运行查看原因：" -ForegroundColor Red
    Write-Host "    py -3.10 ui\app.py" -ForegroundColor Cyan
    if (-not $proc.HasExited) { $proc.Kill() }
    exit 1
}

Write-Host "已就绪：http://127.0.0.1:$port" -ForegroundColor Green
Start-Process "http://127.0.0.1:$port"
Write-Host "服务运行中（扫描进度会显示在下面）。按 Ctrl+C 或关闭本窗口停止服务。"

try {
    Wait-Process -Id $proc.Id
} finally {
    if (-not $proc.HasExited) { $proc.Kill() }
}
