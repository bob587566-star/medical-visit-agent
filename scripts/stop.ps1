$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $projectRoot 'logs\server.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Output '没有本项目的启动记录。'; exit 0 }
$serviceId = [int](Get-Content -LiteralPath $pidPath)
$serviceInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$serviceId"
$expectedPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if ($serviceInfo) {
    if ($serviceInfo.ExecutablePath -ne $expectedPython -or $serviceInfo.CommandLine -notlike '*backend.main:app*') { throw '进程身份不匹配，未停止其他应用。' }
    Stop-Process -Id $serviceId
}
Remove-Item -LiteralPath $pidPath
Write-Output '知禾服务已停止。'
