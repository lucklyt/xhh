#!/usr/bin/env pwsh
# Windows PowerShell launcher for xhh (auto venv bootstrap)
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Host "[xhh] 首次运行，正在检查 Python 环境..." -ForegroundColor Cyan
    $PythonCmd = $null
    foreach ($cand in @("py", "python3", "python")) {
        $res = Get-Command $cand -ErrorAction SilentlyContinue
        if ($res) {
            try {
                $verCheck = & $cand -c "import sys; assert sys.version_info >= (3, 9); print('ok')" 2>$null
                if ($verCheck -eq "ok") {
                    $PythonCmd = $cand
                    break
                }
            } catch {}
        }
    }

    if (-not $PythonCmd) {
        Write-Host "[xhh] 未找到 Python >= 3.9，请先安装 Python 并勾选 Add to PATH: https://www.python.org/" -ForegroundColor Red
        exit 1
    }

    Write-Host "[xhh] 正在初始化虚拟环境 ($ScriptDir\.venv)..." -ForegroundColor Cyan
    & $PythonCmd -m venv (Join-Path $ScriptDir ".venv")
    $VenvPip = Join-Path $ScriptDir ".venv\Scripts\pip.exe"
    Write-Host "[xhh] 正在安装依赖与 xhh-tui..." -ForegroundColor Cyan
    & $VenvPip install -e $ScriptDir
}

& $VenvPy -m xhh.cli $args
exit $LASTEXITCODE
