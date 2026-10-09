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

# ── 后台模式：用 Start-Process 启独立进程 ───────────────────────────────
# 以前用 Start-Job，但 Start-Job 启的进程绑在当前 PowerShell 会话里，
# 关 shell 服务会跟着死。改成 Start-Process -WindowStyle Hidden 后：
#   - 启的是独立操作系统进程，和本 shell 无血缘关系
#   - 关 shell、注销桌面都不影响服务
#   - 代价：Hidden 窗口的 stdout/stderr 默认会被丢掉，必须显式重定向到文件

# 1. 启动前先看端口是不是已经被占了。如果占的是本服务，直接重用；
#    是别的进程占的就报错让用户决定。
$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -EA SilentlyContinue |
            Select-Object -First 1
if ($existing) {
    $ownerPid = $existing.OwningProcess
    try {
        $h = Invoke-RestMethod "$url/api/health" -TimeoutSec 3
        if ($h.status -eq 'ok') {
            Write-Host "  端口 $Port 已经在用，而且是本服务(PID=$ownerPid)。" -ForegroundColor Yellow
            Write-Host "  直接用现在的那个即可。要重启：Stop-Process -Id $ownerPid -Force 再跑本脚本"
            if (-not $NoBrowser) { Start-Process $url }
            exit 0
        }
    } catch { }
    Write-Host "  [错误] 端口 $Port 被别的进程占用(PID=$ownerPid)。" -ForegroundColor Red
    Write-Host "         先停掉它：Stop-Process -Id $ownerPid -Force"
    exit 1
}

# 2. 日志目录。每次启动都从头写(覆盖，而非追加)——方便看最新一次日志。
$logDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item $logDir -ItemType Directory -Force | Out-Null }
$outLog = Join-Path $logDir 'uvicorn.out.log'
$errLog = Join-Path $logDir 'uvicorn.err.log'

# 3. 启独立进程。-PassThru 让 Start-Process 返回 Process 对象，拿它的 .Id
#    当 PID 用。-WorkingDirectory 保证 python 以项目根为工作目录。
$proc = Start-Process -FilePath $py -PassThru -WindowStyle Hidden `
    -WorkingDirectory $PSScriptRoot `
    -ArgumentList @('-m','uvicorn','api:app','--host','127.0.0.1','--port',$Port) `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError  $errLog

# 4. 轮询 /api/health 等就绪(最多 ~21 秒)
$ready = $false
foreach ($i in 1..30) {
    Start-Sleep -Milliseconds 700
    if ($proc.HasExited) { break }      # 进程挂了就不用再等
    try {
        $h = Invoke-RestMethod "$url/api/health" -TimeoutSec 3
        if ($h.status -eq 'ok') { $ready = $true; break }
    } catch { }
}

if (-not $ready) {
    Write-Host "  [错误] 服务没起来。错误日志末尾：" -ForegroundColor Red
    if (Test-Path $errLog) { Get-Content $errLog -Tail 20 }
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -EA SilentlyContinue
    }
    exit 1
}

Write-Host "  服务已启动(独立进程 PID=$($proc.Id))" -ForegroundColor Green
Write-Host "  这是独立进程，关掉本 PowerShell 窗口服务不会死。" -ForegroundColor Green
Write-Host ""
Write-Host "  停止：      Stop-Process -Id $($proc.Id) -Force"
Write-Host "  健康检查：  Invoke-RestMethod $url/api/health"
Write-Host "  实时日志：  Get-Content '$outLog' -Tail 30 -Wait"
Write-Host "  错误日志：  Get-Content '$errLog' -Tail 30"
Write-Host ""
if (-not $NoBrowser) { Start-Process $url }
