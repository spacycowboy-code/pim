# 在自己电脑上启动 PIM 查询网页
#
# 用法：在 PowerShell 里执行
#     .\start-local.ps1
# 然后浏览器会自动打开 http://127.0.0.1:8080
#
# 数据一律实时从 MI / RTD 拉取，没有"读本地快照"这种模式。
# 所以必须在有 Windows 域身份的机器上跑（MI 用集成 Windows 认证）。

param(
    [int]$Port = 8080,
    [switch]$NoBrowser,          # 不自动打开浏览器
    [switch]$Foreground,         # 前台运行，Ctrl+C 停止
    [switch]$Reload              # 改代码自动重启（开发时用，需配合 -Foreground）
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

Write-Host ""
Write-Host "=================================================="
Write-Host "  PIM 测试数据查询 —— 本机实时模式"
Write-Host "=================================================="

# 1. 检查虚拟环境。没有就用 uv 建一个
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "  首次运行，正在装依赖（约 1 分钟）..."
    $uv = "$env:USERPROFILE\.local\bin\uv.exe"
    if (-not (Test-Path $uv)) {
        Write-Host "  [错误] 找不到 uv：$uv" -ForegroundColor Red
        Write-Host "  装一下：irm https://astral.sh/uv/install.ps1 | iex"
        exit 1
    }
    & $uv sync --extra dev
    if ($LASTEXITCODE -ne 0) { Write-Host "  [错误] 装依赖失败" -ForegroundColor Red; exit 1 }
}

# 2. 确认能访问 MI。这一步失败的话后面查询都会失败，早点发现早点报
Write-Host "  检查 MI 连接..." -NoNewline
$ok = & $py -c @"
import sys
try:
    import requests
    from requests_negotiate_sspi import HttpNegotiateAuth
    s = requests.Session(); s.auth = HttpNegotiateAuth()
    r = s.get('https://apps.mesmi.internal.ericsson.com/MIReports', timeout=30)
    print('OK' if r.status_code == 200 else f'HTTP {r.status_code}')
except Exception as e:
    print(f'FAIL {type(e).__name__}')
"@ 2>$null
if ($ok -eq 'OK') {
    Write-Host " 通" -ForegroundColor Green
} else {
    Write-Host " $ok" -ForegroundColor Yellow
    Write-Host "  连不上 MI。确认：①连着公司网络或 VPN ②用域账号登录的 Windows"
    Write-Host "  服务仍会启动，但**查任何数据都会失败** —— 没有本地缓存可以退回。" -ForegroundColor Yellow
}

# 3. 服务只绑 127.0.0.1（只有本机能访问），所以不需要密码。
#    （PIM_SOURCE 这个开关已经没有了——数据只能实时查，没有第二种模式。）
Remove-Item Env:\PIM_AUTH_USER -EA SilentlyContinue
Remove-Item Env:\PIM_AUTH_PASS -EA SilentlyContinue

$url = "http://127.0.0.1:$Port"
Write-Host ""
Write-Host "  模式    实时查 MI（任意产品号、任意日期）"
Write-Host "  地址    $url"
Write-Host "  绑定    127.0.0.1（只有本机能访问，所以不需要密码）"
Write-Host ""

if ($Foreground) {
    # 前台模式：服务直接在当前终端跑，日志实时打印，Ctrl+C 停止。
    # -Reload 时监视代码文件，保存改动后自动重启加载新代码（开发时很方便）。
    # 注意：--reload 只在前台用，放进后台 Start-Job 里重启会不干净。
    $uvicornArgs = @("-m", "uvicorn", "api:app", "--host", "127.0.0.1",
                     "--port", $Port)
    if ($Reload) {
        $uvicornArgs += "--reload"
        Write-Host "  已开启自动重载：改完代码保存，服务会自己重启" -ForegroundColor Green
    }
    Write-Host "  前台运行，按 Ctrl+C 停止"
    Write-Host ""
    if (-not $NoBrowser) { Start-Process $url }
    & $py @uvicornArgs
    exit $LASTEXITCODE
}

if ($Reload) {
    # 后台模式下 --reload 不可靠，直接拦住并引导走前台
    Write-Host "  [提示] -Reload 需要配合 -Foreground 使用。" -ForegroundColor Yellow
    Write-Host "         改用：.\start-local.ps1 -Foreground -Reload"
    exit 1
}

# 后台起，然后等它就绪
$job = Start-Job -ScriptBlock {
    Set-Location $using:PSScriptRoot
    & $using:py -m uvicorn api:app --host 127.0.0.1 --port $using:Port 2>&1
}

$ready = $false
foreach ($i in 1..30) {
    Start-Sleep -Milliseconds 700
    try {
        $h = Invoke-RestMethod "$url/api/health" -TimeoutSec 3
        if ($h.status -eq 'ok') { $ready = $true; break }
    } catch { }
}

if (-not $ready) {
    Write-Host "  [错误] 服务没起来。日志：" -ForegroundColor Red
    Receive-Job $job | Select-Object -Last 20
    Stop-Job $job -EA SilentlyContinue; Remove-Job $job -Force -EA SilentlyContinue
    exit 1
}

Write-Host "  服务已启动（后台任务 Id=$($job.Id)）" -ForegroundColor Green
Write-Host ""
Write-Host "  停止：Stop-Job $($job.Id); Remove-Job $($job.Id)"
Write-Host "  看日志：Receive-Job $($job.Id)"
Write-Host ""
if (-not $NoBrowser) { Start-Process $url }
