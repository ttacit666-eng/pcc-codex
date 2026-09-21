param([string]$Python='py')
$ErrorActionPreference='Stop'
$pccRoot=$PSScriptRoot
$pccPython=Join-Path $pccRoot '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $pccPython)){
    if($Python -eq 'py'){ & $Python -3 -m venv (Join-Path $pccRoot '.venv') }
    else { & $Python -m venv (Join-Path $pccRoot '.venv') }
    if($LASTEXITCODE -ne 0){throw 'Python 3.11+ required. Pass -Python with its full executable path.'}
}
& $pccPython -m pip install -r (Join-Path $pccRoot 'requirements.lock.txt')
if($LASTEXITCODE -ne 0){throw 'Project dependency installation failed'}
$pccState=Join-Path $pccRoot 'state'
New-Item -ItemType Directory -Path $pccState -Force | Out-Null
if(-not(Test-Path -LiteralPath (Join-Path $pccState 'disabled.flag'))){
    Set-Content -LiteralPath (Join-Path $pccState 'disabled.flag') -Value 'Dispatch paused until owner configures and grants a project.'
}
Write-Output 'Installed project dependencies; dispatch paused. No login, model, service, tunnel or global PATH change.'
