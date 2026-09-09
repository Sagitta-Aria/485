$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:IDF_PATH = "D:\esp32\esp32-idf\.espressif\v5.5.4\esp-idf"
$buildDirectory = Join-Path $PSScriptRoot "build"
$idfArguments = @($args)
$eimToolsRoot = "C:\Espressif\tools"
$eimPythonEnvironment = Join-Path $eimToolsRoot "python\v5.5.4\venv"
$eimPython = Join-Path $eimPythonEnvironment "Scripts\python.exe"
if (Test-Path -LiteralPath $eimPython) {
    # Match the environment already used by this project's in-tree build cache.
    $env:IDF_TOOLS_PATH = $eimToolsRoot
    $env:IDF_PYTHON_ENV_PATH = $eimPythonEnvironment
    $romElfDirectory = Join-Path $eimToolsRoot "esp-rom-elfs\20241011"
    if (Test-Path -LiteralPath $romElfDirectory) { $env:ESP_ROM_ELF_DIR = $romElfDirectory }
    $toolBins = @(
        (Join-Path $eimToolsRoot "cmake\3.30.2\bin"),
        (Join-Path $eimToolsRoot "ninja\1.12.1"),
        (Join-Path $eimToolsRoot "xtensa-esp-elf\esp-14.2.0_20260121\xtensa-esp-elf\bin")
    )
    $env:PATH = ($toolBins -join ";") + ";" + $env:PATH
    Set-Location $PSScriptRoot
    & $eimPython (Join-Path $env:IDF_PATH "tools\idf.py") -B $buildDirectory --no-ccache @idfArguments
    exit $LASTEXITCODE
}
$exportScript = Join-Path $env:IDF_PATH "export.ps1"
if (-not (Test-Path -LiteralPath $exportScript)) {
    throw "ESP-IDF export script not found: $exportScript"
}
. $exportScript | Out-Null
Set-Location $PSScriptRoot
& idf.py -B $buildDirectory --no-ccache @idfArguments
exit $LASTEXITCODE
