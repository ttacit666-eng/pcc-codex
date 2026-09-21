@echo off
setlocal
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -B -m pcc %*
set "PCC_EXIT=%ERRORLEVEL%"
popd
if not "%PCC_EXIT%"=="0" echo PCC stopped. No automatic retry. Exit=%PCC_EXIT%
exit /b %PCC_EXIT%
