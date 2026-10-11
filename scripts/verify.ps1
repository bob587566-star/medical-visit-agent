param([string]$BaseUrl = 'http://127.0.0.1:8270', [switch]$Browser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$testTemp = Join-Path $projectRoot ('.pytest-tmp\verify-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testTemp -Force | Out-Null
Push-Location $projectRoot
try {
    & $pythonPath -m pytest backend/tests -q --basetemp=$testTemp
    if ($LASTEXITCODE -ne 0) { throw '后端测试失败。' }
    & $pythonPath tests/model_boundary.py
    if ($LASTEXITCODE -ne 0) { throw '模拟模型边界测试失败。' }
    & $pythonPath tests/acceptance.py --base-url $BaseUrl
    if ($LASTEXITCODE -ne 0) { throw 'HTTP 验收失败，请确认服务已经启动。' }
    Push-Location (Join-Path $projectRoot 'frontend')
    try {
        & npm.cmd test
        if ($LASTEXITCODE -ne 0) { throw '页面测试失败。' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw '页面构建失败。' }
        if ($Browser) {
            & npm.cmd run test:e2e
            if ($LASTEXITCODE -ne 0) { throw '浏览器验收失败。' }
        }
    } finally { Pop-Location }
} finally { Pop-Location }
Write-Output '指定的检查全部通过。验收使用独立的虚构测试记录。'
