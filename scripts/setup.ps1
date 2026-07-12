[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$DevRequirements = Join-Path $ProjectRoot "requirements-dev.txt"
$RequirementsMarker = Join-Path $VenvDir "requirements.sha256"
$EnvExample = Join-Path $ProjectRoot ".env.example"
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

Set-Location $ProjectRoot
Write-Host "[1/4] 检查 Python 3.10+..." -ForegroundColor Cyan
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "未找到 Python：$Python。请先安装 Python 3.10 或更高版本。"
}

$VersionText = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) {
    throw "无法执行 Python：$Python"
}
$PythonVersion = [version]$VersionText.Trim()
if ($PythonVersion -lt [version]"3.10") {
    throw "Python 版本过低：$PythonVersion；最低需要 3.10。"
}

Write-Host "[2/4] 准备项目虚拟环境..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    if (Test-Path -LiteralPath $VenvDir) {
        throw "$VenvDir 已存在但不是有效虚拟环境。请确认其中没有需要保留的数据后手动删除，再重试。"
    }
    Invoke-NativeCommand $Python -m venv $VenvDir
}

Write-Host "[3/4] 安装项目依赖..." -ForegroundColor Cyan
Invoke-NativeCommand $VenvPython -m pip install --disable-pip-version-check -r $Requirements
if ($Dev) {
    Invoke-NativeCommand $VenvPython -m pip install --disable-pip-version-check -r $DevRequirements
}

$RuntimeHash = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($RequirementsMarker, $RuntimeHash + [Environment]::NewLine)

Write-Host "[4/4] 准备本机配置..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Write-Host "已创建 .env。首次运行前需填写 QMT userdata_mini 路径和资金账号。" -ForegroundColor Yellow
} else {
    Write-Host "保留已有 .env，不覆盖本机配置。"
}

Write-Host ""
Write-Host "部署完成。" -ForegroundColor Green
Write-Host "模拟运行：.\scripts\run.ps1"
Write-Host "实盘运行：.\scripts\run.ps1 -Mode live"
Write-Host "XTQuant 不在常规 QMT 目录时，请在 .env 设置 XTQUANT_PYTHONPATH。"
