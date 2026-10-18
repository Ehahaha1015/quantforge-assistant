param([switch]$Build)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $showcaseCache = Join-Path $PSScriptRoot 'local-tools/cache'
    foreach ($folder in @('temp','uv','npm')) {
        New-Item -ItemType Directory -Path (Join-Path $showcaseCache $folder) -Force | Out-Null
    }
    $env:TEMP = Join-Path $showcaseCache 'temp'
    $env:TMP = $env:TEMP
    $env:UV_CACHE_DIR = Join-Path $showcaseCache 'uv'
    $env:npm_config_cache = Join-Path $showcaseCache 'npm'
    $showcaseArgs = @('compose', '-f', 'compose.showcase.yaml', 'up', '-d', '--wait')
    if ($Build) { $showcaseArgs += '--build' }
    & docker @showcaseArgs
    if ($LASTEXITCODE -ne 0) { throw 'Offline showcase startup failed' }
    Write-Output 'Offline synthetic showcase: http://127.0.0.1:4182/#chart-reports'
    Write-Output 'Uses quantforge-showcase_showcase-data; production account and volumes are not mounted.'
} finally { Pop-Location }
