#requires -Version 5.1
[CmdletBinding()]
param(
    # 未指定时处理本脚本所在目录及其所有子目录。
    [string]$Root = (Split-Path -Parent $MyInvocation.MyCommand.Definition)
)

$ErrorActionPreference = 'Stop'
$prefix = '【哲风壁纸】'
$renamed = 0
$skipped = 0

try {
    $rootPath = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
    $files = Get-ChildItem -LiteralPath $rootPath -File -Recurse -Force

    foreach ($file in $files) {
        if (-not $file.Name.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            continue
        }

        $newName = $file.Name.Substring($prefix.Length)
        if ([string]::IsNullOrWhiteSpace($newName)) {
            Write-Host ('跳过空文件名：' + $file.FullName) -ForegroundColor Yellow
            $skipped++
            continue
        }

        $targetPath = Join-Path $file.DirectoryName $newName
        if (Test-Path -LiteralPath $targetPath) {
            Write-Host ('跳过重名文件：' + $targetPath) -ForegroundColor Yellow
            $skipped++
            continue
        }

        Move-Item -LiteralPath $file.FullName -Destination $targetPath
        Write-Host ($file.Name + ' -> ' + $newName) -ForegroundColor Green
        $renamed++
    }

    Write-Host ''
    Write-Host ('处理目录：' + $rootPath)
    Write-Host ('已重命名：' + $renamed + ' 个文件') -ForegroundColor Green
    Write-Host ('已跳过：' + $skipped + ' 个文件') -ForegroundColor Yellow
} catch {
    Write-Host ('执行失败：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

if ($Host.Name -match 'ConsoleHost') {
    Read-Host '按回车键退出'
}
