#requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$script:ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location -LiteralPath $script:ProjectRoot

$script:SourcePath = Join-Path $script:ProjectRoot 'get_wallpapers.py'
$script:SpecPath = Join-Path $script:ProjectRoot 'get_wallpapers.spec'
$script:VenvRoot = Join-Path $script:ProjectRoot '.venv'
$script:VenvPython = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
$script:BrowserExe = Join-Path $script:ProjectRoot 'browser\chrome-win64\chrome.exe'
$script:BrowserDll = Join-Path $script:ProjectRoot 'browser\chrome-win64\chrome.dll'
$script:HostPythonPath = $null
$script:HostPythonPrefix = @()

function Write-CheckResult {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Passed,
        [string]$Detail = ''
    )

    if ($Passed) {
        $mark = '[OK]'
        $color = 'Green'
    } else {
        $mark = '[缺少]'
        $color = 'Yellow'
    }
    Write-Host ('{0,-14} {1} {2}' -f $Name, $mark, $Detail) -ForegroundColor $color
}

# 检测可用的 Python 解释器，优先使用 Python 3.13。
function Find-HostPython {
    $candidates = New-Object System.Collections.Generic.List[object]
    $pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $pyCommand) {
        $candidates.Add([pscustomobject]@{
            Path = $pyCommand.Source
            Prefix = @('-3.13')
        })
        $candidates.Add([pscustomobject]@{
            Path = $pyCommand.Source
            Prefix = @()
        })
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        $candidates.Add([pscustomobject]@{
            Path = $pythonCommand.Source
            Prefix = @()
        })
    }

    foreach ($candidate in $candidates) {
        try {
            $arguments = @()
            if ($candidate.Prefix.Count -gt 0) {
                $arguments += $candidate.Prefix
            }
            $arguments += '--version'
            $versionText = (& $candidate.Path @arguments 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) {
                continue
            }
            if ($versionText -match 'Python\s+(\d+)\.(\d+)\.(\d+)') {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if (($major -gt 3) -or (($major -eq 3) -and ($minor -ge 10))) {
                    $pythonArguments = @()
                    if ($candidate.Prefix.Count -gt 0) {
                        $pythonArguments += $candidate.Prefix
                    }
                    $pythonArguments += @('-c', 'import sys; print(sys.executable)')
                    $resolvedPath = (& $candidate.Path @pythonArguments 2>$null | Out-String).Trim()
                    if (Test-Path -LiteralPath $resolvedPath -PathType Leaf) {
                        return [pscustomobject]@{
                            Path = $resolvedPath
                            Prefix = @()
                            Version = $versionText
                        }
                    }
                    return [pscustomobject]@{
                        Path = $candidate.Path
                        Prefix = $candidate.Prefix
                        Version = $versionText
                    }
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Invoke-HostPython {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $invokeArguments = @()
    if ($script:HostPythonPrefix.Count -gt 0) {
        $invokeArguments += $script:HostPythonPrefix
    }
    $invokeArguments += $Arguments
    & $script:HostPythonPath @invokeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令执行失败，退出码：$LASTEXITCODE"
    }
}

function Invoke-VenvPython {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & $script:VenvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "虚拟环境 Python 命令执行失败，退出码：$LASTEXITCODE"
    }
}

function Test-VenvPython {
    if (-not (Test-Path -LiteralPath $script:VenvPython -PathType Leaf)) {
        return $false
    }
    $oldErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $null = & $script:VenvPython --version 2>&1
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $oldErrorAction
    }
}

# 检查虚拟环境中的 Python 包和 PyInstaller。
function Get-EnvironmentStatus {
    $sourceOk = Test-Path -LiteralPath $script:SourcePath -PathType Leaf
    $specOk = Test-Path -LiteralPath $script:SpecPath -PathType Leaf
    $hostPythonOk = $null -ne $script:HostPythonPath
    $venvOk = Test-VenvPython
    $dependenciesOk = $false
    $pyinstallerOk = $false

    if ($venvOk) {
        $oldErrorAction = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $null = & $script:VenvPython -c 'import Crypto, websocket' 2>&1
            $dependenciesOk = $LASTEXITCODE -eq 0
            $null = & $script:VenvPython -m PyInstaller --version 2>&1
            $pyinstallerOk = $LASTEXITCODE -eq 0
        } finally {
            $ErrorActionPreference = $oldErrorAction
        }
    }

    $browserOk = (Test-Path -LiteralPath $script:BrowserExe -PathType Leaf) -and
        (Test-Path -LiteralPath $script:BrowserDll -PathType Leaf)
    $missing = @()
    if (-not $sourceOk) { $missing += 'get_wallpapers.py' }
    if (-not $specOk) { $missing += 'get_wallpapers.spec' }
    if (-not $hostPythonOk) { $missing += 'Python 3.10+' }
    if (-not $venvOk) { $missing += '.venv' }
    if (-not $dependenciesOk) { $missing += 'Python 依赖包' }
    if (-not $pyinstallerOk) { $missing += 'PyInstaller' }
    if (-not $browserOk) { $missing += '内置 Chromium' }

    return [pscustomobject]@{
        Source = $sourceOk
        Spec = $specOk
        HostPython = $hostPythonOk
        Venv = $venvOk
        Dependencies = $dependenciesOk
        PyInstaller = $pyinstallerOk
        Browser = $browserOk
        Complete = $missing.Count -eq 0
        Missing = $missing
    }
}

function Show-EnvironmentStatus {
    param([Parameter(Mandatory = $true)]$Status)

    Write-Host ''
    Write-Host '========== 环境检查 ==========' -ForegroundColor Cyan
    Write-CheckResult '源代码' $Status.Source
    Write-CheckResult '打包配置' $Status.Spec
    Write-CheckResult 'Python' $Status.HostPython $(if ($script:HostPythonPath) { $script:HostPythonPath } else { '未找到 Python 3.10+' })
    Write-CheckResult '虚拟环境' $Status.Venv
    Write-CheckResult 'Python 依赖' $Status.Dependencies
    Write-CheckResult 'PyInstaller' $Status.PyInstaller
    Write-CheckResult '内置 Chromium' $Status.Browser
    Write-Host ''
}

function Show-PythonDownloadPrompt {
    Write-Host '未检测到 Python 3.10 或更高版本。' -ForegroundColor Yellow
    Write-Host '官方下载地址：https://www.python.org/downloads/windows/'
    $answer = Read-Host '是否打开 Python 下载页面？[Y/N]'
    if ($answer -match '^(Y|y)$') {
        Start-Process 'https://www.python.org/downloads/windows/'
    }
    throw '请安装 Python 后重新运行本脚本。'
}

# 下载并解压 Chrome for Testing，作为程序内置浏览器。
function Install-BundledBrowser {
    $metadataUrl = 'https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json'
    $metadata = Invoke-RestMethod -Uri $metadataUrl -TimeoutSec 30
    $stable = $metadata.channels.Stable
    $download = $stable.downloads.chrome |
        Where-Object { $_.platform -eq 'win64' } |
        Select-Object -First 1
    if ($null -eq $download) {
        throw '未找到 Chrome for Testing win64 下载地址。'
    }

    $browserRoot = Join-Path $script:ProjectRoot 'browser'
    New-Item -ItemType Directory -Force -Path $browserRoot | Out-Null
    $zipPath = Join-Path ([System.IO.Path]::GetTempPath()) ('get-wallpapers-chrome-' + $stable.version + '.zip')
    try {
        Write-Host ('正在下载内置 Chromium ' + $stable.version + ' ...') -ForegroundColor Cyan
        $curlCommand = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($null -ne $curlCommand) {
            & $curlCommand.Source -L --fail --retry 5 --retry-delay 3 --retry-all-errors -o $zipPath $download.url
            if ($LASTEXITCODE -ne 0) {
                throw "Chromium 下载失败，退出码：$LASTEXITCODE"
            }
        } else {
            Invoke-WebRequest -Uri $download.url -OutFile $zipPath -UseBasicParsing
        }

        Expand-Archive -LiteralPath $zipPath -DestinationPath $browserRoot -Force
        if (-not (Test-Path -LiteralPath $script:BrowserExe -PathType Leaf)) {
            throw 'Chromium 解压完成，但未找到 browser\chrome-win64\chrome.exe。'
        }
        Write-Host '内置 Chromium 已准备完成。' -ForegroundColor Green
    } finally {
        if (Test-Path -LiteralPath $zipPath -PathType Leaf) {
            [System.IO.File]::Delete($zipPath)
        }
    }
}

# 补齐虚拟环境、Python 依赖和打包工具。
function Install-MissingEnvironment {
    if (-not (Test-VenvPython)) {
        if (Test-Path -LiteralPath $script:VenvRoot -PathType Container) {
            Write-Host '检测到残缺虚拟环境，正在重新创建 ...' -ForegroundColor Yellow
            [System.IO.Directory]::Delete($script:VenvRoot, $true)
        }
        Write-Host '正在创建 .venv ...' -ForegroundColor Cyan
        Invoke-HostPython @('-m', 'venv', '.venv')
        if (-not (Test-VenvPython)) {
            throw '虚拟环境创建后无法启动 .venv\Scripts\python.exe。'
        }
    }

    $status = Get-EnvironmentStatus
    if (-not $status.Dependencies -or -not $status.PyInstaller) {
        Write-Host '正在安装 Python 依赖和 PyInstaller ...' -ForegroundColor Cyan
        Invoke-VenvPython @(
            '-m', 'pip', 'install', '--disable-pip-version-check',
            'pycryptodome==3.23.0',
            'websocket-client==1.9.2',
            'pyinstaller==6.22.3'
        )
    }

    $status = Get-EnvironmentStatus
    if (-not $status.Browser) {
        Install-BundledBrowser
    }
}

function Run-SourceProgram {
    Write-Host '正在启动源码程序 ...' -ForegroundColor Cyan
    Invoke-VenvPython @($script:SourcePath)
}

function Build-Executable {
    $distPath = Join-Path $script:ProjectRoot 'dist_single'
    $workPath = Join-Path $script:ProjectRoot 'build_single'
    Write-Host '正在构建单文件 exe ...' -ForegroundColor Cyan
    Invoke-VenvPython @(
        '-m', 'PyInstaller',
        '--clean',
        '--noconfirm',
        '--distpath', $distPath,
        '--workpath', $workPath,
        $script:SpecPath
    )

    $exePath = Join-Path $distPath 'get_wallpapers.exe'
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw 'PyInstaller 执行完成，但没有找到 dist_single\get_wallpapers.exe。'
    }
    Write-Host ''
    Write-Host ('构建完成：' + $exePath) -ForegroundColor Green
}

try {
    $hostPython = Find-HostPython
    if ($null -eq $hostPython) {
        Show-PythonDownloadPrompt
    }
    $script:HostPythonPath = $hostPython.Path
    $script:HostPythonPrefix = $hostPython.Prefix

    $status = Get-EnvironmentStatus
    Show-EnvironmentStatus $status
    if (-not $status.Complete) {
        Write-Host ('缺少环境：' + ($status.Missing -join '、')) -ForegroundColor Yellow
        $setupAnswer = Read-Host '是否自动补齐环境？[Y/N]'
        if ($setupAnswer -notmatch '^(Y|y)$') {
            throw '环境未补齐，程序已停止。'
        }
        Install-MissingEnvironment
        $status = Get-EnvironmentStatus
        Show-EnvironmentStatus $status
        if (-not $status.Complete) {
            throw ('环境仍不完整：' + ($status.Missing -join '、'))
        }
    }

    Write-Host '请选择操作：' -ForegroundColor Cyan
    Write-Host '  1. 直接运行'
    Write-Host '  2. 构建单文件 exe'
    Write-Host '  Q. 退出'
    $action = (Read-Host '请选择 [1/2/Q]').Trim().ToUpperInvariant()
    switch ($action) {
        '1' { Run-SourceProgram }
        '2' { Build-Executable }
        default { Write-Host '已退出。' }
    }
} catch {
    Write-Host ('执行失败：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
