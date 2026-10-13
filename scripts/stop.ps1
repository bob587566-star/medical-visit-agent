$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $projectRoot 'logs\server.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Output '没有本项目的启动记录。'; exit 0 }
$serviceId = [int](Get-Content -LiteralPath $pidPath)
$serviceInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$serviceId"
$expectedPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if ($serviceInfo) {
    if ($serviceInfo.ExecutablePath -ne $expectedPython -or $serviceInfo.CommandLine -notlike '*backend.main:app*') { throw '进程身份不匹配，未停止其他应用。' }
    $expectedBasePython = (& $expectedPython -c 'import sys; print(sys._base_executable)').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $expectedBasePython) { throw '无法核实 Python 子进程，未停止服务。' }
    $interpreterChildren = @()
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$serviceId")) {
        if ($child.Name -eq 'conhost.exe' -and $child.ExecutablePath -eq (Join-Path $env:WINDIR 'system32\conhost.exe')) { continue }
        if ($child.ExecutablePath -ne $expectedBasePython -or $child.CommandLine -notlike '*backend.main:app*') { throw '子进程身份不匹配，未停止其他应用。' }
        $interpreterChildren += $child
    }
    foreach ($child in $interpreterChildren) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.ProcessId)"
        if ($current) {
            if ($current.ExecutablePath -ne $expectedBasePython -or $current.CommandLine -notlike '*backend.main:app*') { throw '子进程身份已变化，未停止其他应用。' }
            Stop-Process -Id $current.ProcessId
        }
    }
    if (Get-Process -Id $serviceId -ErrorAction SilentlyContinue) { Stop-Process -Id $serviceId }
}
Remove-Item -LiteralPath $pidPath
Write-Output '知禾服务已停止。'
