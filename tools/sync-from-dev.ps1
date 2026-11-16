#Requires -Version 5.1
<#
.SYNOPSIS
    BidGenie 开发源 → GitHub 发布仓库的同步工具。

.DESCRIPTION
    开发以 D:\ProJects\bidgenie-flow\plugin 为唯一源；发布仓库只保留运行时代码、
    技能、模板、资产、README/LICENSE/tools，不包含 tests/docs/pylibs/开发产物。
    本脚本把开发源中的运行时代码同步到发布仓库，保留发布仓库独有的文件
    （README.md、LICENSE、package.json、tools/、封面图等）。

    默认只检查并同步，不提交、不推送。

.PARAMETER Repo
    发布仓库根目录，默认 D:\ProJects\dsh-bidgenie。

.PARAMETER DevPlugin
    开发插件根目录，默认 D:\ProJects\bidgenie-flow\plugin。

.PARAMETER Check
    只比较/列出差异，不写入。

.PARAMETER Commit
    同步后 git add/commit；需同时提供 -Message。

.PARAMETER Push
    同步并提交后推送 origin main。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\sync-from-dev.ps1 -Check
    powershell -ExecutionPolicy Bypass -File tools\sync-from-dev.ps1 -Commit -Message "fix: ..." -Push
#>
[CmdletBinding()]
param(
    [string]$Repo = 'D:\ProJects\dsh-bidgenie',
    [string]$DevPlugin = 'D:\ProJects\bidgenie-flow\plugin',
    [switch]$Check,
    [switch]$Commit,
    [string]$Message = '',
    [switch]$Push
)

$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$Text) Write-Host "`n== $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  [WARN] $Text" -ForegroundColor Yellow }

if (-not (Test-Path (Join-Path $DevPlugin 'index.js'))) { throw "开发插件目录无效：$DevPlugin" }
if (-not (Test-Path (Join-Path $Repo 'package.json'))) { throw "发布仓库目录无效：$Repo" }
if ($Commit -and -not $Message) { throw "-Commit 需要同时提供 -Message" }

$runtimeFiles = @('index.js', 'cordis.patch.yml')
$runtimeDirs = @('client', 'lib', 'libexec', 'skills', 'templates', 'assets')

Write-Step '源与目标'
Write-Host "  开发源：$DevPlugin"
Write-Host "  发布仓库：$Repo"

$diff = @()
foreach ($file in $runtimeFiles) {
    $s = Join-Path $DevPlugin $file
    $r = Join-Path $Repo $file
    if (-not (Test-Path $s)) { throw "开发源缺少 $file" }
    if (-not (Test-Path $r)) { $diff += "新增 $file" }
    elseif ((Get-FileHash $s).Hash -ne (Get-FileHash $r).Hash) { $diff += "更新 $file" }
}
foreach ($dir in $runtimeDirs) {
    $s = Join-Path $DevPlugin $dir
    if (-not (Test-Path $s)) { continue }
    Get-ChildItem $s -Recurse -File | Where-Object {
        $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -notin @('.pyc', '.pyo')
    } | ForEach-Object {
        $rel = $_.FullName.Substring($s.Length + 1)
        $r = Join-Path (Join-Path $Repo $dir) $rel
        if (-not (Test-Path $r)) { $diff += "新增 $dir/$rel" }
        elseif ((Get-FileHash $_.FullName).Hash -ne (Get-FileHash $r).Hash) { $diff += "更新 $dir/$rel" }
    }
}

if ($diff.Count -eq 0) {
    Write-Ok '运行时代码已与发布仓库一致，无差异。'
    exit 0
}

Write-Step "差异（$($diff.Count) 项）"
$diff | Select-Object -First 60 | ForEach-Object { Write-Host "  $_" }
if ($diff.Count -gt 60) { Write-Host "  ... 其余 $($diff.Count - 60) 项" }

if ($Check) {
    Write-Warn '仅检查模式：未写入。'
    exit 0
}

Write-Step '同步运行时代码（保留发布仓库独有文件）'
foreach ($file in $runtimeFiles) {
    Copy-Item (Join-Path $DevPlugin $file) (Join-Path $Repo $file) -Force
}
# 用 robocopy 只增量复制运行目录；不清理目标中的 README/封面/tools 等发布独有文件。
foreach ($dir in $runtimeDirs) {
    $s = Join-Path $DevPlugin $dir
    $r = Join-Path $Repo $dir
    if (-not (Test-Path $s)) { continue }
    $null = robocopy $s $r /E /XD __pycache__ .pytest_cache .mypy_cache /XF *.pyc *.pyo *.bak *.tmp *.lock /NFL /NDL /NJH /NJS
}
Write-Ok '运行时代码已同步。'

if ($Commit -or $Push) {
    Push-Location $Repo
    try {
        Write-Step '发布仓库差异'
        git status --short
        git add -A
        if ($Commit) {
            git commit -m $Message
            Write-Ok "已提交：$Message"
        }
        if ($Push) {
            git push origin main
            Write-Ok '已推送 origin main'
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host ''
Write-Host '完成。发布仓库未包含 tests/docs/pylibs；依赖由 tools/repair-env.cmd 安装。' -ForegroundColor Green
