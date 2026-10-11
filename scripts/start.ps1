param([int]$Port = 8270)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '先运行 scripts\setup.ps1 安装环境。' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\dist\index.html'))) { throw '先运行 scripts\setup.ps1 构建页面。' }
$activeListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($activeListener) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 3
        if ($health.service -eq 'zhihe-medical-visit-agent') { Write-Output "知禾已运行：http://127.0.0.1:$Port/"; exit 0 }
    } catch { }
    throw "端口 $Port 已被其他服务占用，未关闭其他程序。"
}
$logRoot = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$serviceProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', "$Port") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'server.stdout.log') -RedirectStandardError (Join-Path $logRoot 'server.stderr.log')
$serviceProcess.Id | Set-Content -LiteralPath (Join-Path $logRoot 'server.pid') -Encoding ascii
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 300
    if ($serviceProcess.HasExited) { throw '服务启动失败，查看 logs\server.stderr.log；日志不含输入正文或密钥。' }
    try {
        $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/readyz" -TimeoutSec 2
        if ($ready.status -eq 'ready') { Write-Output "知禾已启动：http://127.0.0.1:$Port/"; exit 0 }
    } catch { }
}
throw '服务启动检查超时，查看 logs\server.stderr.log。'
