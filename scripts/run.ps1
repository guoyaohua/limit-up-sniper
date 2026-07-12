[CmdletBinding()]
param(
    [ValidateSet("simulation", "live")]
    [string]$Mode = "simulation",
    [switch]$EnableShadow,
    [switch]$RefreshSector,
    [switch]$PreflightOnly,
    [switch]$NoBootstrap
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$SetupScript = Join-Path $PSScriptRoot "setup.ps1"
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$RequirementsMarker = Join-Path $ProjectRoot ".venv\requirements.sha256"
$EnvFile = Join-Path $ProjectRoot ".env"

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败（退出码 $LASTEXITCODE）：$Command $($Arguments -join ' ')"
    }
}

function Import-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    foreach ($RawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $Line = $RawLine.Trim()
        if (-not $Line -or $Line.StartsWith("#")) {
            continue
        }

        $Separator = $Line.IndexOf("=")
        if ($Separator -le 0) {
            throw "无效的 .env 配置行：$RawLine"
        }

        $Name = $Line.Substring(0, $Separator).Trim()
        $Value = $Line.Substring($Separator + 1).Trim()
        if ($Name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            throw "无效的 .env 变量名：$Name"
        }
        if ($Value.Length -ge 2) {
            $First = $Value[0]
            $Last = $Value[$Value.Length - 1]
            if (($First -eq '"' -and $Last -eq '"') -or
                ($First -eq "'" -and $Last -eq "'")) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }
        }

        # Shell variables deliberately take precedence over .env.
        if ($null -eq [Environment]::GetEnvironmentVariable($Name, "Process")) {
            [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
        }
    }
}

function Add-PythonPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Path "xtquant") -PathType Container)) {
        return $false
    }

    $Current = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    $Parts = @($Path)
    if ($Current) {
        $Parts += $Current
    }
    [Environment]::SetEnvironmentVariable("PYTHONPATH", ($Parts -join [IO.Path]::PathSeparator), "Process")
    return $true
}

function Resolve-XtQuant {
    param([Parameter(Mandatory = $true)][string]$ClientPath)

    $ExplicitPath = [Environment]::GetEnvironmentVariable("XTQUANT_PYTHONPATH", "Process")
    if ($ExplicitPath -and (Add-PythonPath $ExplicitPath)) {
        return $ExplicitPath
    }

    if (-not (Test-Path -LiteralPath $ClientPath -PathType Container)) {
        return $null
    }

    $ClientDirectory = (Resolve-Path -LiteralPath $ClientPath).Path
    $InstallRoot = Split-Path -Parent $ClientDirectory
    $Candidates = @(
        (Join-Path $InstallRoot "bin.x64\Lib\site-packages"),
        (Join-Path $InstallRoot "bin\Lib\site-packages"),
        (Join-Path $InstallRoot "Lib\site-packages"),
        (Join-Path $ClientDirectory "Lib\site-packages")
    )
    foreach ($Candidate in $Candidates) {
        if (Add-PythonPath $Candidate) {
            return $Candidate
        }
    }
    return $null
}

Set-Location $ProjectRoot

$NeedsBootstrap = (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) -or
    (-not (Test-Path -LiteralPath $RequirementsMarker -PathType Leaf))
if (-not $NeedsBootstrap) {
    $ExpectedHash = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash.ToLowerInvariant()
    $InstalledHash = (Get-Content -LiteralPath $RequirementsMarker -Raw).Trim()
    $NeedsBootstrap = $ExpectedHash -ne $InstalledHash
}
if ($NeedsBootstrap) {
    if ($NoBootstrap) {
        throw "虚拟环境不存在或依赖已更新；请先执行 .\scripts\setup.ps1。"
    }
    Write-Host "检测到首次运行或依赖变化，开始自动部署..." -ForegroundColor Cyan
    & $SetupScript
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    if ($NoBootstrap) {
        throw "缺少 .env；请复制 .env.example 并填写本机配置。"
    }
    & $SetupScript
}
Import-DotEnv $EnvFile

# CLI mode is authoritative. An accidental live value in .env cannot make the
# default command send real orders.
$env:LIMIT_UP_EXECUTION_MODE = $Mode
if ($EnableShadow) {
    $env:LIMIT_UP_ENABLE_SHADOW_SIGNAL = "true"
}

$ClientName = [Environment]::GetEnvironmentVariable("LIMIT_UP_CLIENT_NAME", "Process")
if (-not $ClientName) {
    $ClientName = "GJ_SIM"
    $env:LIMIT_UP_CLIENT_NAME = $ClientName
}
if ($ClientName -notin @("GJ_SIM", "CICC_LIVE")) {
    throw "LIMIT_UP_CLIENT_NAME 只能是 GJ_SIM 或 CICC_LIVE，当前值：$ClientName"
}

$Prefix = if ($ClientName -eq "CICC_LIVE") { "CICC" } else { "GJ_SIM" }
$ClientPath = [Environment]::GetEnvironmentVariable("${Prefix}_QMT_CLIENT_PATH", "Process")
$StockAccount = [Environment]::GetEnvironmentVariable("${Prefix}_STOCK_ACCOUNT", "Process")
if (-not $ClientPath) {
    throw "缺少 ${Prefix}_QMT_CLIENT_PATH；请在 .env 填写 QMT userdata_mini 路径。"
}
if (-not (Test-Path -LiteralPath $ClientPath -PathType Container)) {
    throw "QMT userdata_mini 路径不存在：$ClientPath"
}
if (-not $StockAccount) {
    throw "缺少 ${Prefix}_STOCK_ACCOUNT；请在 .env 填写资金账号。"
}

$XtQuantPath = Resolve-XtQuant $ClientPath
& $VenvPython -c "from xtquant import xtdata; print('XTQuant SDK：可用')"
if ($LASTEXITCODE -ne 0) {
    $Hint = if ($XtQuantPath) { $XtQuantPath } else { "未自动找到" }
    throw "XTQuant SDK 无法导入（定位结果：$Hint）。请在 .env 设置 XTQUANT_PYTHONPATH，指向包含 xtquant 文件夹的 site-packages。"
}

$RequiredSectorMappings = @(
    (Join-Path $ProjectRoot "output\concept_sector_data\THS\stock_to_concept_mapping.json"),
    (Join-Path $ProjectRoot "output\industry_sector_data\THS\stock_to_industry_mapping.json"),
    (Join-Path $ProjectRoot "output\concept_sectors\THS\sector_to_stocks_mapping_latest.json"),
    (Join-Path $ProjectRoot "output\industry_sectors\THS\sector_to_stocks_mapping_latest.json")
)
$MissingSectorMappings = @(
    $RequiredSectorMappings | Where-Object {
        (-not (Test-Path -LiteralPath $_ -PathType Leaf)) -or
        ((Get-Item -LiteralPath $_ -ErrorAction SilentlyContinue).Length -eq 0)
    }
)
if ($MissingSectorMappings.Count -gt 0 -and -not $RefreshSector) {
    throw "缺少问财概念/行业板块映射。首次运行请使用 -RefreshSector；首次抓取可能打开 Edge，需登录问财。"
}

$ModeLabel = if ($Mode -eq "live") { "实盘" } else { "模拟" }
Write-Host "启动前检查通过：模式=$ModeLabel，客户端=$ClientName，账号已配置（不回显）。" -ForegroundColor Green
if ($Mode -eq "live") {
    Write-Host "警告：下一步可能发送真实委托；main.py 仍会要求输入 yes 二次确认。" -ForegroundColor Red
}
if ($RefreshSector) {
    Write-Host "正在刷新问财板块映射..." -ForegroundColor Cyan
    Invoke-NativeCommand $VenvPython (Join-Path $ProjectRoot "scraper\ths_sector_parser.py") --auto-download
}
$MissingSectorMappings = @(
    $RequiredSectorMappings | Where-Object {
        (-not (Test-Path -LiteralPath $_ -PathType Leaf)) -or
        ((Get-Item -LiteralPath $_ -ErrorAction SilentlyContinue).Length -eq 0)
    }
)
if ($MissingSectorMappings.Count -gt 0) {
    throw "问财板块刷新完成后仍缺少概念/行业映射，拒绝启动。请检查浏览器登录状态和抓取日志。"
}
Write-Host "检查上一交易日涨停/首板清单..." -ForegroundColor Cyan
Invoke-NativeCommand $VenvPython (Join-Path $ProjectRoot "scripts\prepare_market_data.py")
if ($PreflightOnly) {
    exit 0
}

Invoke-NativeCommand $VenvPython (Join-Path $ProjectRoot "main.py")
