# 小黑盒社区 TUI (xhh) Windows PowerShell 一键安装脚本
[CmdletBinding()]
param(
    [switch]$Local,
    [switch]$Uninstall,
    [string]$Branch = "main",
    [string]$RepoUrl = "https://github.com/lucklyt/xhh",
    [string]$BinDir = "$HOME\.local\bin",
    [string]$InstallDir = "$env:LOCALAPPDATA\xhh\venv"
)

$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "[xhh] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[✓] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[✗] $msg" -ForegroundColor Red }

# 卸载逻辑
if ($Uninstall) {
    Write-Info "正在卸载 xhh..."
    $targets = @(
        (Join-Path $BinDir "xhh.cmd"),
        (Join-Path $BinDir "xhh.ps1"),
        (Join-Path $BinDir "xhh.exe")
    )
    foreach ($f in $targets) {
        if (Test-Path $f) {
            Remove-Item -Force $f
            Write-Ok "已删除 $f"
        }
    }
    $parentDir = Split-Path -Parent $InstallDir
    if (Test-Path $parentDir) {
        Remove-Item -Recurse -Force $parentDir
        Write-Ok "已删除 $parentDir"
    }
    Write-Ok "卸载完成（用户配置 ~/.config/xhh 已保留）"
    exit 0
}

# 检查 Python
Write-Info "正在检查 Python 环境..."
$PythonCmd = $null
foreach ($cand in @("py", "python3", "python")) {
    $res = Get-Command $cand -ErrorAction SilentlyContinue
    if ($res) {
        try {
            $verCheck = & $cand -c "import sys; assert sys.version_info >= (3, 9); print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
            if ($verCheck) {
                $PythonCmd = $cand
                $PythonVer = $verCheck.Trim()
                break
            }
        } catch {}
    }
}

if (-not $PythonCmd) {
    Write-Err "未找到可用的 Python >= 3.9！"
    Write-Warn "请先安装 Python（官网下载或运行 winget install Python.Python.3.12），安装时请务必勾选 'Add python.exe to PATH'。"
    exit 1
}

Write-Info "使用 Python $PythonVer ($PythonCmd)"

# 准备安装包路径
$TempDir = $null
$SrcDir = $null

try {
    if ($Local) {
        $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
        if (Test-Path (Join-Path $ScriptDir "pyproject.toml")) {
            $SrcDir = $ScriptDir
        } elseif (Test-Path (Join-Path (Get-Location) "pyproject.toml")) {
            $SrcDir = (Get-Location).Path
        } else {
            Write-Err "本地目录未找到 pyproject.toml"
            exit 1
        }
        Write-Info "使用本地源码目录: $SrcDir"
    } else {
        $TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("xhh_install_" + [System.Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $TempDir -Force | Out-Null
        $SrcDir = Join-Path $TempDir "xhh-src"
        
        Write-Info "正在下载 xhh 源码 ($RepoUrl)..."
        $cloned = $false
        if (Get-Command git -ErrorAction SilentlyContinue) {
            try {
                git clone --depth 1 --branch $Branch "$RepoUrl.git" $SrcDir 2>$null
                if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $SrcDir "pyproject.toml"))) {
                    $cloned = $true
                }
            } catch {}
        }
        
        if (-not $cloned) {
            $zipUrl = "$RepoUrl/archive/refs/heads/$Branch.zip"
            $zipPath = Join-Path $TempDir "xhh.zip"
            Write-Info "使用 WebClient 下载 zip 压缩包..."
            Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
            Expand-Archive -Path $zipPath -DestinationPath $TempDir -Force
            $extracted = Get-ChildItem -Directory $TempDir | Where-Object { $_.Name -like "xhh-*" } | Select-Object -First 1
            if ($extracted) {
                $SrcDir = $extracted.FullName
            } else {
                Write-Err "解压源码包失败"
                exit 1
            }
        }
    }

    # 创建虚拟环境
    if (-not (Test-Path $InstallDir)) {
        Write-Info "正在创建独立虚拟环境 ($InstallDir)..."
        & $PythonCmd -m venv $InstallDir
    }

    $VenvPip = Join-Path $InstallDir "Scripts\pip.exe"
    $VenvXhh = Join-Path $InstallDir "Scripts\xhh.exe"

    Write-Info "正在安装 xhh 及依赖..."
    & $VenvPip install --upgrade pip --quiet
    & $VenvPip install --upgrade $SrcDir

    # 导出可执行包装脚本到 BinDir
    if (-not (Test-Path $BinDir)) {
        New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    }

    $ShimCmd = Join-Path $BinDir "xhh.cmd"
    $ShimPs1 = Join-Path $BinDir "xhh.ps1"

    @"
@echo off
"$VenvXhh" %*
"@ | Out-File -FilePath $ShimCmd -Encoding ascii -Force

    @"
& "$VenvXhh" `$args
exit `$LASTEXITCODE
"@ | Out-File -FilePath $ShimPs1 -Encoding utf8 -Force

    Write-Ok "已生成可执行命令: $ShimCmd"

    # 检查 PATH 环境变量
    $currentPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $binDirNormalized = (Resolve-Path $BinDir).Path.TrimEnd('\')
    $pathParts = $currentPath -split ';' | ForEach-Object { $_.TrimEnd('\') }

    $pathUpdated = $false
    if ($pathParts -notcontains $binDirNormalized) {
        Write-Info "正在将 $BinDir 添加到用户 PATH 环境变量..."
        $newPath = "$currentPath;$binDirNormalized".TrimStart(';')
        [System.Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
        $env:PATH = "$env:PATH;$binDirNormalized"
        $pathUpdated = $true
        Write-Ok "已写入用户 PATH"
    }

    Write-Host ""
    Write-Host "🎉 小黑盒社区 TUI (xhh) 安装成功！" -ForegroundColor Green
    Write-Host ""

    if ($pathUpdated) {
        Write-Warn "PATH 环境变量已更新，新开一个 PowerShell / Windows Terminal 窗口即可全局使用 xhh。"
        Write-Host "当前窗口可临时运行: `$env:PATH += `";$BinDir`"" -ForegroundColor Gray
    }

    Write-Host @"
快速开始:
  xhh              # 启动全屏交互式 TUI
  xhh login        # 终端扫码登录小黑盒
  xhh feed         # 查看首页推荐流
  xhh hot          # 查看黑盒热议 / 热搜 / 热门游戏榜
  xhh topic CS2    # 查看指定板块帖子
"@ -ForegroundColor Cyan

} finally {
    if ($TempDir -and (Test-Path $TempDir)) {
        Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
    }
}
