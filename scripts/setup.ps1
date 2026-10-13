$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $basePython = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $basePython -or $basePython.Source -like '*WindowsApps*') { throw '请先安装 Python 3.11+，不要使用 Microsoft Store 占位命令。' }
    & $basePython.Source -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
}
& $pythonPath -m pip install -r (Join-Path $projectRoot 'backend\requirements.txt') -c (Join-Path $projectRoot 'backend\requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
$npmCommand = Get-Command npm.cmd -ErrorAction Stop
Push-Location (Join-Path $projectRoot 'frontend')
try {
    & $npmCommand.Source ci
    if ($LASTEXITCODE -ne 0) { throw '网页依赖安装失败。' }
    & $npmCommand.Source run build
    if ($LASTEXITCODE -ne 0) { throw '网页构建失败。' }
} finally { Pop-Location }
$envPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) { Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $envPath }
Write-Output '安装完成。无需模型密钥也能本机使用；运行 scripts\start.ps1 启动。'
