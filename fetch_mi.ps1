<#
批量从 MI (Manufacturing Intelligence) 拉取测试数据。

认证方式：集成 Windows 认证 (Negotiate)，用当前登录用户身份，无需任何令牌。

用法：
  .\fetch_mi.ps1                  # 拉全部
  .\fetch_mi.ps1 -Limit 5         # 只拉前 5 个（先试跑）
  .\fetch_mi.ps1 -Force           # 忽略缓存重新拉

输出：data\raw\<报表>\<序列号>.json
#>
param(
    [int]$Limit = 0,
    [switch]$Force,
    [string]$FromDate = "2026-08-01 00:00",
    [string]$ToDate   = "2026-10-01 00:00"
)

$ErrorActionPreference = 'Continue'
$MiHost  = "https://apps.mesmi.internal.ericsson.com"
$Root    = $PSScriptRoot
$RawDir  = Join-Path $Root "data\raw"

# ---------- 工具函数 ----------

function Save-Json {
    <# 写成不带 BOM 的 UTF-8 并 gzip 压缩。
       不带 BOM：Python 的 json.load 遇到 BOM 会报 "Unexpected UTF-8 BOM"。
       gzip：单个测试点汇总约 3MB，压缩后约 1/10。 #>
    param([string]$Path, [string]$Json)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $bytes = $utf8NoBom.GetBytes($Json)
    $fs = [IO.File]::Create($Path)
    try {
        $gz = New-Object IO.Compression.GZipStream($fs, [IO.Compression.CompressionLevel]::Optimal)
        try { $gz.Write($bytes, 0, $bytes.Length) } finally { $gz.Dispose() }
    } finally { $fs.Dispose() }
}

function Get-FilterFields {
    <# 从报表页 HTML 里读出 filter-id，这些就是请求载荷的字段名 #>
    param([string]$ReportPage)
    $url = "$MiHost/MIReports/$ReportPage"
    $html = (Invoke-WebRequest -Uri $url -TimeoutSec 60 -UseBasicParsing -UseDefaultCredentials).Content
    $ids = [regex]::Matches($html, 'filter-id="([^"]+)"') |
           ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique
    $api = if ($html -match '"APIGetData1":"([^"]+)"') { $Matches[1] } else { $null }
    return [pscustomobject]@{ Fields = $ids; Api = $api }
}

function Invoke-MiReport {
    <# 向 MI 报表接口发一次只读查询，返回解析后的对象；失败返回 $null #>
    param([string]$ApiPath, [hashtable]$Filter, [int]$TimeoutSec = 180)
    $payload = @{ reportFilter = $Filter; pageSetting = $null } | ConvertTo-Json -Depth 8 -Compress
    try {
        $r = Invoke-WebRequest -Uri ($MiHost + $ApiPath) -Method Post -Body $payload `
             -ContentType 'application/json' -TimeoutSec $TimeoutSec `
             -UseBasicParsing -UseDefaultCredentials
        return $r.Content
    } catch {
        $resp = $_.Exception.Response
        $code = if ($resp) { [int]$resp.StatusCode } else { 'ERR' }
        Write-Host ("      !! 查询失败 status=$code " + $_.Exception.Message.Substring(0,[Math]::Min(80,$_.Exception.Message.Length))) -ForegroundColor DarkYellow
        return $null
    }
}

function New-Filter {
    <# 把所有字段填 null，再用指定值覆盖 —— 服务端要求字段齐全 #>
    param([string[]]$Fields, [hashtable]$Values)
    $f = @{}
    foreach ($k in $Fields) { $f[$k] = $null }
    foreach ($k in $Values.Keys) { $f[$k] = $Values[$k] }
    return $f
}

# ---------- 准备 ----------

$serialsFile = Join-Path $Root "data\serials.json"
if (-not (Test-Path $serialsFile)) {
    Write-Host "缺少 data\serials.json，请先运行 extract_serials.py" -ForegroundColor Red
    exit 1
}
$serials = Get-Content -LiteralPath $serialsFile -Raw -Encoding UTF8 | ConvertFrom-Json

$allSn = @()
foreach ($s in $serials.all_radio_sn)   { $allSn += [pscustomobject]@{ sn=$s; kind='radio' } }
foreach ($s in $serials.all_antenna_sn) { $allSn += [pscustomobject]@{ sn=$s; kind='antenna' } }
if ($Limit -gt 0) { $allSn = $allSn | Select-Object -First $Limit }

Write-Host "=== MI 批量取数 ===" -ForegroundColor Cyan
Write-Host "身份     : $([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
Write-Host "序列号数 : $($allSn.Count)  (整机 $($serials.all_radio_sn.Count) / 天线 $($serials.all_antenna_sn.Count))"
Write-Host "日期范围 : $FromDate  ~  $ToDate"
Write-Host ""

Write-Host "读取报表字段定义..." -ForegroundColor Gray
$snlog = Get-FilterFields "SerialNumberLog"
$tpsum = Get-FilterFields "TestPointSummary"
Write-Host "  SerialNumberLog  : $($snlog.Fields.Count) 个字段 -> $($snlog.Api)"
Write-Host "  TestPointSummary : $($tpsum.Fields.Count) 个字段 -> $($tpsum.Api)"
Write-Host ""

New-Item -ItemType Directory -Force -Path (Join-Path $RawDir "SerialNumberLog")  | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RawDir "TestPointSummary") | Out-Null

# ---------- 第一遍：序列号日志（顺带取到产品号） ----------

$productMap = @{}
$i = 0
foreach ($item in $allSn) {
    $i++
    $sn = $item.sn
    $out = Join-Path $RawDir "SerialNumberLog\$sn.json.gz"
    if ((Test-Path $out) -and -not $Force) {
        $cached = $null
        try {
            $fsIn = [IO.File]::OpenRead($out)
            $gzIn = New-Object IO.Compression.GZipStream($fsIn, [IO.Compression.CompressionMode]::Decompress)
            $srIn = New-Object IO.StreamReader($gzIn)
            $cached = $srIn.ReadToEnd() | ConvertFrom-Json
            $srIn.Dispose(); $gzIn.Dispose(); $fsIn.Dispose()
        } catch { $cached = $null }
        if (-not $cached) { Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue }
        if ($cached.data.product) { $productMap[$sn] = $cached.data.product }
        Write-Host ("[{0,3}/{1}] {2} {3,-12} 已缓存 产品={4}" -f $i,$allSn.Count,$item.kind,$sn,$cached.data.product) -ForegroundColor DarkGray
        continue
    }
    $flt = New-Filter $snlog.Fields @{ SerialNumber = $sn }
    $json = Invoke-MiReport $snlog.Api $flt
    if ($json) {
        Save-Json $out $json
        $o = $json | ConvertFrom-Json
        $prod = $o.data.product
        if ($prod) { $productMap[$sn] = $prod }
        $nrows = @($o.data.tableResultSet.rows).Count
        Write-Host ("[{0,3}/{1}] {2} {3,-12} 产品={4,-22} 事件={5}" -f $i,$allSn.Count,$item.kind,$sn,$prod,$nrows) -ForegroundColor Green
    } else {
        Write-Host ("[{0,3}/{1}] {2} {3,-12} 无数据" -f $i,$allSn.Count,$item.kind,$sn) -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "识别到的产品号:" -ForegroundColor Cyan
$productMap.Values | Group-Object | Sort-Object Count -Descending |
    ForEach-Object { Write-Host ("  {0,-26} {1} 个序列号" -f $_.Name, $_.Count) }
Write-Host ""

# ---------- 第二遍：测试点汇总（每个序列号的测量值） ----------

$i = 0
foreach ($item in $allSn) {
    $i++
    $sn = $item.sn
    $prod = $productMap[$sn]
    if (-not $prod) {
        Write-Host ("[{0,3}/{1}] {2,-12} 跳过（无产品号）" -f $i,$allSn.Count,$sn) -ForegroundColor DarkGray
        continue
    }
    # 产品号里去掉尾部的 R-state（如 "KRD 901 330/11 R1A" -> "KRD 901 330/11"）
    $prodNum = ($prod -replace '\s+R\d+[A-Z]?$','').Trim()

    $out = Join-Path $RawDir "TestPointSummary\$sn.json.gz"
    if ((Test-Path $out) -and -not $Force) {
        Write-Host ("[{0,3}/{1}] {2,-12} 已缓存" -f $i,$allSn.Count,$sn) -ForegroundColor DarkGray
        continue
    }
    $flt = New-Filter $tpsum.Fields @{
        ProductNumber   = $prodNum
        SerialNumber    = $sn
        FromDate        = $FromDate
        ToDate          = $ToDate
        TestCount       = "[All]"
        TestPointResult = "[All]"
        IncludeOutliers = "No"
        MedianBase      = "[All]"
    }
    $json = Invoke-MiReport $tpsum.Api $flt
    if ($json) {
        Save-Json $out $json
        $o = $json | ConvertFrom-Json
        $ncol = @($o.data.columns).Count
        $nrow = @($o.data.data).Count
        Write-Host ("[{0,3}/{1}] {2,-12} {3,-22} 测试点={4,3}  列={5}" -f $i,$allSn.Count,$sn,$prodNum,$nrow,$ncol) -ForegroundColor Green
    } else {
        Write-Host ("[{0,3}/{1}] {2,-12} 查询失败" -f $i,$allSn.Count,$sn) -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=== 完成 ===" -ForegroundColor Cyan
foreach ($d in @("SerialNumberLog","TestPointSummary")) {
    $n = (Get-ChildItem -Path (Join-Path $RawDir $d) -Filter *.json.gz -ErrorAction SilentlyContinue).Count
    Write-Host ("  data\raw\{0,-18} {1} 个文件" -f $d, $n)
}

