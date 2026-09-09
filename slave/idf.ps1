$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:IDF_PATH = "D:\esp32\esp32-idf\.espressif\v5.5.4\esp-idf"
$buildDirectory = Join-Path $PSScriptRoot "build"
$exportScript = Join-Path $env:IDF_PATH "export.ps1"
if (-not (Test-Path -LiteralPath $exportScript)) {
    throw "ESP-IDF export script not found: $exportScript"
}
. $exportScript | Out-Null
Set-Location $PSScriptRoot
& idf.py -B $buildDirectory --no-ccache @args
exit $LASTEXITCODE
