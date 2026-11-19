#Requires -Version 5.1
<#
.SYNOPSIS
    标代精灵（BidGenie）pylibs 应急修复脚本（原型）。

.DESCRIPTION
    症状：环境检查里「插件脚本完整性」未通过（bg_convert.py --selftest 失败）或
          「PDF 读取组件」降级（提示缺少 fitz / pymupdf4llm / pdfplumber），
          但插件的 pylibs 目录看起来"文件都在"。

    根因：插件包里 pylibs 目录缺少全部编译扩展（.pyd），fitz 一 import 就报
          cannot import name '_extra' from partially initialized module 'pymupdf'。

    做法：按 libexec/requirements.txt 的锁定版本装到
          <DSH_HOME>/data/bidgenie/pylibs.new -> 逐个 import 验证 -> 原子替换为
          <DSH_HOME>/data/bidgenie/pylibs（旧目录保留 pylibs.broken-<时间戳>）-> 在插件目录建立
          pylibs 链接指向该目录 -> 复跑 bg_env_check.py 单项复检。

    可重复执行：已健康时直接返回 0，不做任何改动。

.PARAMETER DryRun
    只打印计划，不做任何改动。
.PARAMETER Mirror
    自定义 pip 源（默认清华 https://pypi.tuna.tsinghua.edu.cn/simple；http 源自动加 --trusted-host）。
.PARAMETER NoMirror
    不使用镜像，走 pip 默认源（通常是 pypi.org）。
.PARAMETER NoOfficialFallback
    镜像安装失败时不回退官方源（默认会回退 https://pypi.org/simple）。
.PARAMETER Rollback
    把最近一次 pylibs.broken-* 换回 pylibs（用于验证/应急）。
.PARAMETER Force
    即使当前 pylibs 看起来健康，也重新安装。
#>
[CmdletBinding()]
param(
    [string]$PluginRoot = '',
    [string]$Python = '',
    [string]$DataHome = '',
    [string]$Workspace = (Get-Location).Path,
    [string]$Mirror = '',
    [switch]$NoMirror,
    [switch]$NoOfficialFallback,
    [switch]$Force,
    [switch]$Rollback,
    [switch]$DryRun
)

#: 默认国内镜像（清华）与官方回退源
$script:DefaultMirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'
$script:OfficialIndex = 'https://pypi.org/simple'

$ErrorActionPreference = 'Stop'
$script:Fail = $false

function Write-Step { param([string]$Text) Write-Host "`n== $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  [WARN] $Text" -ForegroundColor Yellow }
function Write-Err  { param([string]$Text) Write-Host "  [FAIL] $Text" -ForegroundColor Red; $script:Fail = $true }

# 调用外部程序：临时放开 ErrorActionPreference，避免 PowerShell 5.1 把 stderr 当成
# 终止错误（pip 的依赖冲突提示、python 的告警都会走 stderr），统一只看退出码。
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$StdinText = $null
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($null -ne $StdinText) {
            $out = $StdinText | & $Exe @Arguments 2>&1
        }
        else {
            $out = & $Exe @Arguments 2>&1
        }
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ ExitCode = $code; Output = @($out) }
}

# ---------------------------------------------------------------- 定位插件
function Resolve-PluginRoot {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    if ($env:BIDGENIE_PLUGIN_ROOT) { $candidates += $env:BIDGENIE_PLUGIN_ROOT }
    $candidates += (Join-Path $env:USERPROFILE '.dsh\profiles\desktop\node_modules\dsh-bidgenie')
    if (Test-Path (Join-Path $env:USERPROFILE '.dsh\profiles')) {
        Get-ChildItem (Join-Path $env:USERPROFILE '.dsh\profiles') -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates += (Join-Path $_.FullName 'node_modules\dsh-bidgenie') }
    }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c 'libexec\requirements.txt'))) {
            return (Resolve-Path -LiteralPath $c).Path
        }
    }
    throw "找不到已安装的 dsh-bidgenie（请用 -PluginRoot 指定插件根目录）"
}

# ---------------------------------------------------------------- 定位 Python
function Resolve-Python {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    if ($env:BIDGENIE_PYTHON) { $candidates += $env:BIDGENIE_PYTHON }
    if ($env:DSH_PYTHON) { $candidates += $env:DSH_PYTHON }

    # 1) DSH 自带运行时（优先，避免装到系统 Python）
    $dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
    $runtimeRoot = Join-Path $dshHome 'dsh-runtimes\dsh-primary-runtime\dependencies\python'
    $candidates += (Join-Path $runtimeRoot 'python.exe')
    $candidates += (Join-Path $runtimeRoot 'Scripts\python.exe')
    # DSH Desktop 安装目录里的只读运行时载荷：全新机器上 dsh-runtimes 尚未生成，
    # 但 resources\runtime\primary-runtime\dependencies\python\python.exe 一直存在。
    $desktopExe = $env:DSH_NODE_EXECUTABLE
    if ($desktopExe -and (Test-Path -LiteralPath $desktopExe)) {
        $installRoot = Split-Path -Parent $desktopExe
        $payloadRoot = Join-Path $installRoot 'resources\runtime\primary-runtime\dependencies\python'
        $candidates += (Join-Path $payloadRoot 'python.exe')
        $candidates += (Join-Path $payloadRoot 'Scripts\python.exe')
    }
    # 兜底扫描安装目录（兼容旧版布局 dsh-runtimes 与新版 primary-runtime 载荷）
    $programs = Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness'
    if (Test-Path $programs) {
        $candidates += (Join-Path $programs 'resources\runtime\primary-runtime\dependencies\python\python.exe')
        Get-ChildItem $programs -Recurse -Filter 'python.exe' -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '(dsh-runtimes|primary-runtime).*dependencies\\python' } |
            Select-Object -First 3 | ForEach-Object { $candidates += $_.FullName }
    }

    # 2) PATH / 系统 Python
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }

    # 3) 本地 Python 安装
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')

    foreach ($c in $candidates) {
        # Windows 商店别名只是指向 WindowsApps 的符号链接，执行它会拉起商店，不是真解释器。
        if ($c -and $c -match '\\WindowsApps\\') { continue }
        if ($c -and (Test-Path $c)) {
            $v = & $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $parts = $v.Trim().Split('.')
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) { return $c }
            }
        }
    }
    throw "找不到 Python >= 3.10（请用 -Python 指定解释器路径）"
}

function Get-PipIndexArgs {
    param([string]$IndexUrl)
    $result = @('--retries', '3', '--timeout', '60')
    if (-not $IndexUrl) { return $result }
    $result += @('--index-url', $IndexUrl)
    try {
        $uri = [System.Uri]$IndexUrl
        if ($uri.Scheme -eq 'http') { $result += @('--trusted-host', $uri.Host) }
    }
    catch { }
    return $result
}

function Get-MirrorLabel {
    param([string]$IndexUrl)
    if (-not $IndexUrl) { return 'pip 默认源（未显式指定，通常是 pypi.org）' }
    if ($IndexUrl -eq $script:OfficialIndex) { return "官方源 $IndexUrl" }
    return "镜像 $IndexUrl"
}

# ---------------------------------------------------------------- 数据目录 / 插件链接
function Resolve-DataHome {
    param([string]$Explicit)
    if ($Explicit) { $base = $Explicit }
    elseif ($env:DSH_HOME) { $base = Join-Path $env:DSH_HOME 'data\bidgenie' }
    else { $base = Join-Path $env:USERPROFILE '.dsh\data\bidgenie' }
    if (-not (Test-Path $base)) { New-Item -ItemType Directory -Force -Path $base | Out-Null }
    return (Resolve-Path -LiteralPath $base).Path
}

function Get-LinkTarget {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return '' }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.LinkType -in @('Junction', 'SymbolicLink')) {
        $target = @($item.Target)
        if ($target.Count -gt 0 -and $target[0]) { return [System.IO.Path]::GetFullPath($target[0]) }
    }
    return ''
}

function Ensure-PylibsLink {
    param([string]$Plugin, [string]$Target, [switch]$DryRun)
    $link = Join-Path $Plugin 'pylibs'
    $targetFull = [System.IO.Path]::GetFullPath($Target)
    $current = Get-LinkTarget $link
    if ($current -and ($current.TrimEnd('\') -eq $targetFull.TrimEnd('\'))) {
        Write-Ok "插件已链接到数据目录：$link -> $Target"
        return
    }
    if (Test-Path $link) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $legacy = Join-Path (Split-Path $Target) "plugin-pylibs.legacy-$stamp"
        if ($DryRun) { Write-Host "  [DRY] 将把现有 $link 移到 $legacy，再建立链接 -> $Target"; return }
        Move-Item -LiteralPath $link -Destination $legacy
        Write-Warn "原插件 pylibs 已移至 $legacy（未删除）"
    }
    if ($DryRun) { Write-Host "  [DRY] 将建立链接 $link -> $Target"; return }
    New-Item -ItemType Junction -Path $link -Target $Target | Out-Null
    Write-Ok "已建立链接：$link -> $Target"
}

# ---------------------------------------------------------------- 健康检查
function Test-PylibsHealthy {
    param([string]$Dir, [string]$Py, [string]$Libexec)
    if (-not (Test-Path $Dir)) { return @{ Ok = $false; Reason = '目录不存在' } }
    $pyd = @(Get-ChildItem $Dir -Recurse -Filter *.pyd -File -ErrorAction SilentlyContinue)
    if ($pyd.Count -eq 0) { return @{ Ok = $false; Reason = '目录里没有任何 .pyd（编译扩展全部缺失）' } }

    # 必须按插件运行时的同一套规则挂载：sys.path 子目录 + pywin32 的 DLL 搜索目录。
    # 只 sys.path.insert 会让 win32com 找不到 pywintypes312.dll，被误判为导入失败。
    $probe = @'
import sys
sys.path.insert(0, sys.argv[2])
from _lib import deps
deps.ensure_dependency_path(base=sys.argv[1])
mods = ["fitz", "pymupdf", "pymupdf4llm", "pdfplumber", "PIL.Image", "cryptography", "win32com.client", "docx", "lxml.etree"]
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as exc:
        bad.append(f"{m}: {type(exc).__name__}: {exc}")
print("\n".join(bad))
'@
    $result = Invoke-Native -Exe $Py -Arguments @('-', $Dir, $Libexec) -StdinText $probe
    $out = @($result.Output | ForEach-Object { "$_" } | Where-Object { $_ -and $_.Trim() })
    if ($result.ExitCode -ne 0) { return @{ Ok = $false; Reason = "导入探测进程失败（exit=$($result.ExitCode)）：$($out -join '; ')" } }
    if ($out.Count -gt 0) { return @{ Ok = $false; Reason = ($out -join '; ') } }
    return @{ Ok = $true; Reason = "$($pyd.Count) 个 .pyd，全部导入通过" }
}

# ---------------------------------------------------------------- 安全路径
function Assert-UnderPlugin {
    param([string]$Path, [string]$Root)
    $full = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $full.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作插件目录之外的路径：$full"
    }
    return $full
}

# ---------------------------------------------------------------- 主流程
try {
    $plugin = Resolve-PluginRoot -Explicit $PluginRoot
    $libexec = Join-Path $plugin 'libexec'
    $py = Resolve-Python -Explicit $Python
    $req = Join-Path $plugin 'libexec\requirements.txt'
    $dataHome = Resolve-DataHome -Explicit $DataHome
    $live = Join-Path $dataHome 'pylibs'
    $new = Join-Path $dataHome 'pylibs.new'

    # ---- 镜像解析：-NoMirror > -Mirror > BIDGENIE_PIP_INDEX_URL > 默认清华
    $mirrorUrl = $null
    $mirrorSource = ''
    if ($NoMirror) {
        $mirrorUrl = $null
        $mirrorSource = '-NoMirror'
    }
    elseif ($Mirror) {
        $mirrorUrl = $Mirror.Trim()
        $mirrorSource = '-Mirror'
    }
    elseif ($env:BIDGENIE_PIP_INDEX_URL) {
        $mirrorUrl = $env:BIDGENIE_PIP_INDEX_URL.Trim()
        $mirrorSource = 'BIDGENIE_PIP_INDEX_URL'
    }
    else {
        $mirrorUrl = $script:DefaultMirror
        $mirrorSource = '默认（清华镜像）'
    }

    # 尝试顺序：镜像 →（可选）官方回退；-NoMirror 时用 pip 默认源
    $attempts = @()
    if ($mirrorUrl) {
        $attempts += @{ Label = (Get-MirrorLabel $mirrorUrl); Index = $mirrorUrl }
        if (-not $NoOfficialFallback -and $mirrorUrl -ne $script:OfficialIndex) {
            $attempts += @{ Label = "官方回退 $script:OfficialIndex"; Index = $script:OfficialIndex }
        }
    }
    else {
        $attempts += @{ Label = (Get-MirrorLabel $null); Index = $null }
    }

    Write-Step "环境"
    Write-Host "  插件：$plugin"
    Write-Host "  数据目录：$dataHome"
    Write-Host "  Python：$py（优先 DSH 自带运行时）"
    Write-Host "  源：$(Get-MirrorLabel $mirrorUrl)（来源：$mirrorSource）"
    Write-Host "  回退：$(if ($attempts.Count -gt 1) { '镜像失败时尝试官方源（-NoOfficialFallback 可关闭）' } else { '无（-NoMirror 或已指定官方源）' })"
    Write-Host "  工作区：$Workspace"

    # ---- 回滚模式
    if ($Rollback) {
        Write-Step "回滚：把最近的 pylibs.broken-* 换回 pylibs"
        $backup = Get-ChildItem $dataHome -Directory -Filter 'pylibs.broken-*' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | Select-Object -First 1
        if (-not $backup) { throw "没有找到 pylibs.broken-* 备份（$dataHome）" }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $aside = Join-Path $dataHome "pylibs.replaced-$stamp"
        if ($DryRun) { Write-Host "  [DRY] 将把 $($backup.FullName) -> $live（当前 pylibs -> $aside）"; Ensure-PylibsLink -Plugin $plugin -Target $live -DryRun; exit 0 }
        if (Test-Path $live) { Move-Item -LiteralPath $live -Destination $aside }
        Move-Item -LiteralPath $backup.FullName -Destination $live
        Ensure-PylibsLink -Plugin $plugin -Target $live
        Write-Ok "已回滚到 $($backup.Name)（原目录存为 pylibs.replaced-$stamp）"
        exit 0
    }

    # ---- 现状
    Write-Step "现状检查"
    $health = Test-PylibsHealthy -Dir $live -Py $py -Libexec $libexec
    $linked = (Get-LinkTarget (Join-Path $plugin 'pylibs')) -eq ([System.IO.Path]::GetFullPath($live))
    if ($health.Ok -and $linked -and -not $Force) {
        Write-Ok "pylibs 健康：$($health.Reason)"
        Write-Host "`n无需修复（要强制重装请加 -Force）。" -ForegroundColor Green
        exit 0
    }
    if ($health.Ok -and -not $linked) {
        Write-Warn "依赖已可用，但插件尚未链接到数据目录"
        Ensure-PylibsLink -Plugin $plugin -Target $live
        exit 0
    }
    if ($health.Ok) {
        Write-Warn "pylibs 当前可用（$($health.Reason)），按 -Force 强制重装"
    }
    else {
        Write-Warn "pylibs 不健康：$($health.Reason)"
    }

    # ---- 计划
    Write-Step "修复计划"
    Write-Host "  1. 安装到临时目录：$new"
    $primaryArgs = @('-m', 'pip', 'install', '--target', $new, '--no-cache-dir', '--disable-pip-version-check', '-r', $req) + (Get-PipIndexArgs $attempts[0].Index)
    Write-Host "     命令：`"$py`" $($primaryArgs -join ' ')"
    Write-Host "     尝试顺序：$((($attempts | ForEach-Object { $_.Label }) -join ' → '))"
    Write-Host "     预计下载约 90 MB（PyMuPDF / pymupdf4llm / pdfplumber / Pillow / pywin32 / cryptography 等）"
    Write-Host "  2. 在临时目录逐个 import 验证（fitz/pymupdf/pdfplumber/PIL.Image/cryptography/win32com/lxml）"
    Write-Host "  3. 原子替换：pylibs -> pylibs.broken-<时间戳>，pylibs.new -> pylibs"
    Write-Host "  4. 在插件目录建立链接：$plugin\pylibs -> $live"
    Write-Host "  5. 复跑环境检查：bg_env_check.py --apply --item python_packages --item pdf_libraries --item scripts_integrity"

    if (-not $NoOfficialFallback -and $mirrorUrl -and $mirrorUrl -ne $script:OfficialIndex) {
        $fallbackArgs = @('-m', 'pip', 'install', '--target', $new, '--no-cache-dir', '--disable-pip-version-check', '-r', $req) + (Get-PipIndexArgs $script:OfficialIndex)
        Write-Host "     镜像失败时回退命令：`"$py`" $($fallbackArgs -join ' ')"
    }

    if ($DryRun) { Write-Host "`n[DRY] 只打印计划，未做任何改动。" -ForegroundColor Yellow; Ensure-PylibsLink -Plugin $plugin -Target $live -DryRun; exit 0 }

    # ---- 1. 安装（镜像失败自动回退官方，除非 -NoOfficialFallback）
    Write-Step "安装依赖到临时目录"
    $install = $null
    for ($i = 0; $i -lt $attempts.Count; $i++) {
        $attempt = $attempts[$i]
        if ($i -gt 0) {
            Write-Warn "上一个源安装失败，回退：$($attempt.Label)"
            if (Test-Path $new) { Remove-Item -LiteralPath $new -Recurse -Force }
        }
        if (Test-Path $new) {
            Write-Host "  清理旧的临时目录：$new"
            Remove-Item -LiteralPath $new -Recurse -Force
        }
        $pipArgs = @('-m', 'pip', 'install', '--target', $new, '--no-cache-dir', '--disable-pip-version-check', '-r', $req) + (Get-PipIndexArgs $attempt.Index)
        Write-Host "  尝试：$($attempt.Label)"
        $install = Invoke-Native -Exe $py -Arguments $pipArgs
        @($install.Output) | Select-Object -Last 12 | ForEach-Object { Write-Host "    $_" }
        if ($install.ExitCode -ne 0) {
            Write-Warn "安装失败（exit=$($install.ExitCode)）：$($attempt.Label)"
        }
    }
    if ($null -eq $install -or $install.ExitCode -ne 0) {
        throw "pip 安装失败（所有源均失败）；可换源重试：-Mirror <url>、-NoMirror、设置 BIDGENIE_PIP_INDEX_URL，或检查网络/代理"
    }
    if (@($install.Output) -match 'dependency conflicts') {
        Write-Warn "上面 pip 的 'dependency conflicts' 是系统 Python 里另一份 pymupdf 的冲突提示，不影响本次 --target 安装"
    }
    Write-Ok "安装完成"

    # ---- 2. 验证
    Write-Step "验证临时目录"
    $newHealth = Test-PylibsHealthy -Dir $new -Py $py -Libexec $libexec
    if (-not $newHealth.Ok) { throw "临时目录验证失败：$($newHealth.Reason)" }
    Write-Ok "验证通过：$($newHealth.Reason)"

    # ---- 3. 替换
    Write-Step "原子替换"
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bak = Join-Path $dataHome "pylibs.broken-$stamp"
    if (Test-Path $live) { Move-Item -LiteralPath $live -Destination $bak; Write-Ok "旧目录已备份：$bak" }
    Move-Item -LiteralPath $new -Destination $live
    Write-Ok "新目录已就位：$live"

    $finalHealth = Test-PylibsHealthy -Dir $live -Py $py -Libexec $libexec
    if (-not $finalHealth.Ok) { throw "替换后验证失败：$($finalHealth.Reason)（可用 -Rollback 回滚）" }
    Write-Ok "替换后验证通过：$($finalHealth.Reason)"
    Ensure-PylibsLink -Plugin $plugin -Target $live

    # ---- 4. 复检
    Write-Step "复跑环境检查"
    $envCheck = Join-Path $plugin 'libexec\bg_env_check.py'
    if (Test-Path $envCheck) {
        $check = Invoke-Native -Exe $py -Arguments @(
            '-X', 'utf8', $envCheck, '--workspace', $Workspace,
            '--apply', '--item', 'python_packages', '--item', 'pdf_libraries', '--item', 'scripts_integrity'
        )
        @($check.Output) | ForEach-Object { Write-Host "    $_" }
        if ($check.ExitCode -ne 0) { Write-Warn "环境检查退出码非 0（$($check.ExitCode)），请看上面的输出" }
        Write-Host "`n  提示：面板上点『重新检查』即可看到最新结果，或运行"
        Write-Host "        `"$py`" `"$envCheck`" --workspace `"$Workspace`" --apply"
    }
    else {
        Write-Warn "找不到 bg_env_check.py，已跳过复检"
    }

    Write-Host "`n修复完成。" -ForegroundColor Green
    exit 0
}
catch {
    Write-Host "`n[FAIL] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
