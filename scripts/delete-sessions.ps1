<#
.SYNOPSIS
    ウォッチ（実機/エミュレーター）のアプリ内部ストレージから、指定したセッションの CSV・イベント CSV・meta を削除する。

.DESCRIPTION
    debug ビルドのみ対応（adb の run-as を使うため）。削除は元に戻せない。
    - 削除前に対象ファイルを一覧表示し、確認を求める（-Force で省略、-WhatIf で表示のみ）
    - PC 側（-LocalDir）に同じサイズのコピーが無いファイルは削除しない（-SkipBackupCheck で省略）

.EXAMPLE
    .\scripts\delete-sessions.ps1 session_20261018_154843 session_20261025_034220
    .\scripts\delete-sessions.ps1 session_20261018_154843 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true, Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Session,
    [string]$LocalDir = (Join-Path $PSScriptRoot "..\captures\sessions"),
    [string]$Serial = "",
    [string]$Adb = "",
    [switch]$SkipBackupCheck,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$package = "net.akakitune87.fishingcastcounter"
$remoteDir = "files/sessions"

if (-not $Adb) {
    $cmd = Get-Command adb -ErrorAction SilentlyContinue
    if ($cmd) {
        $Adb = $cmd.Source
    } else {
        # local.properties の sdk.dir から adb を探す
        $localProps = Join-Path $PSScriptRoot "..\local.properties"
        $sdkLine = Get-Content $localProps | Where-Object { $_ -match '^sdk\.dir=' } | Select-Object -First 1
        $sdkDir = ($sdkLine -replace '^sdk\.dir=', '') -replace '\\:', ':' -replace '\\\\', '\'
        $Adb = Join-Path $sdkDir "platform-tools\adb.exe"
    }
}
$serialArgs = if ($Serial) { @("-s", $Serial) } else { @() }

# "session_20261018_154843", "session_20261018_154843.csv" などのどれでも受け付ける。
# rm に渡すため、形式外の名前は拒否する。
$names = foreach ($s in $Session) {
    $name = [System.IO.Path]::GetFileName($s) -replace '\.meta\.txt$', '' -replace '\.events\.csv$', '' -replace '\.csv$', ''
    if ($name -notmatch '^session_\d{8}_\d{6}$') {
        throw "セッション名の形式が不正です: $s（例: session_20261018_154843）"
    }
    $name
}

$remoteFiles = & $Adb @serialArgs shell run-as $package ls $remoteDir |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -ne "" }

$targets = @()
foreach ($name in $names) {
    $found = $remoteFiles | Where-Object { $_ -eq "$name.csv" -or $_ -eq "$name.events.csv" -or $_ -eq "$name.meta.txt" }
    if (-not $found) {
        Write-Warning "ウォッチ上に見つかりません: $name"
        continue
    }
    foreach ($file in $found) {
        $remoteSize = [long]((& $Adb @serialArgs shell run-as $package stat -c %s "$remoteDir/$file") -join "").Trim()
        $local = Join-Path $LocalDir $file
        $localSize = if (Test-Path $local) { (Get-Item $local).Length } else { -1 }
        $backedUp = $localSize -eq $remoteSize
        if (-not $backedUp -and -not $SkipBackupCheck) {
            Write-Warning "PC 側にコピーが無いかサイズが違うため削除しません: $file（remote=$remoteSize, local=$localSize）。先に pull-sessions.ps1 を実行してください"
            continue
        }
        $targets += [pscustomobject]@{ File = $file; Bytes = $remoteSize; BackedUp = $backedUp }
    }
}

if (-not $targets) {
    Write-Host "削除対象はありません。"
    exit 0
}

Write-Host "削除対象（ウォッチ上の $remoteDir）:"
$targets | Format-Table -AutoSize | Out-String | Write-Host

$deleted = 0
foreach ($t in $targets) {
    if ($Force -or $PSCmdlet.ShouldProcess("$remoteDir/$($t.File)", "ウォッチから削除")) {
        & $Adb @serialArgs shell run-as $package rm "$remoteDir/$($t.File)"
        if ($LASTEXITCODE -ne 0) {
            throw "削除に失敗しました: $($t.File)"
        }
        Write-Host "削除: $($t.File)"
        $deleted++
    }
}
Write-Host "$deleted 件削除しました。"
