<#
.SYNOPSIS
    ウォッチ（実機/エミュレーター）のアプリ内部ストレージから計測セッションの CSV を PC にコピーする。

.DESCRIPTION
    debug ビルドのみ対応（adb の run-as を使うため）。
    PowerShell 5.1 の `>` リダイレクトは出力を UTF-16 に変換してしまうため、adb の標準出力をバイト列のままファイルへ書き出す。

.EXAMPLE
    .\scripts\pull-sessions.ps1
    .\scripts\pull-sessions.ps1 -OutDir D:\fishing\sessions -Serial emulator-5554
#>
param(
    [string]$OutDir = (Join-Path $PSScriptRoot "..\captures\sessions"),
    [string]$Serial = "",
    [string]$Adb = ""
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

$files = & $Adb @serialArgs shell run-as $package ls $remoteDir |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -ne "" }
if (-not $files) {
    Write-Host "セッションファイルがありません。"
    exit 0
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

foreach ($name in $files) {
    $dest = Join-Path $OutDir $name
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Adb
    $psi.Arguments = (($serialArgs + @("exec-out", "run-as", $package, "cat", "$remoteDir/$name")) -join " ")
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute = $false
    $process = [System.Diagnostics.Process]::Start($psi)
    $stream = [System.IO.File]::Create($dest)
    try {
        $process.StandardOutput.BaseStream.CopyTo($stream)
    } finally {
        $stream.Close()
    }
    $process.WaitForExit()

    $remoteSize = [long]((& $Adb @serialArgs shell run-as $package stat -c %s "$remoteDir/$name") -join "").Trim()
    $localSize = (Get-Item $dest).Length
    $status = if ($remoteSize -eq $localSize) { "OK" } else { "サイズ不一致 (remote=$remoteSize)" }
    Write-Host ("{0}  {1} bytes  {2}" -f $name, $localSize, $status)
}
Write-Host "保存先: $OutDir"
