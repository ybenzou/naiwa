@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if exist "%~dp0.venv\Scripts\python.exe" goto venv
if exist "%USERPROFILE%\miniconda3\envs\naiwa\python.exe" goto conda
python -m naiwa %*
exit /b %errorlevel%
:venv
"%~dp0.venv\Scripts\python.exe" -m naiwa %*
exit /b %errorlevel%
:conda
"%USERPROFILE%\miniconda3\envs\naiwa\python.exe" -m naiwa %*
exit /b %errorlevel%
