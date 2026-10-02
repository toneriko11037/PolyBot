@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] creating virtualenv .venv ...
    python -m venv .venv || goto :err
)

echo [setup] installing/updating dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
".venv\Scripts\python.exe" patch_sigtype.py || goto :err

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [setup] created .env from .env.example
    echo [setup] fill in PRIVATE_KEY and FUNDER_ADDRESS if needed, then run again.
    pause
    goto :end
)

echo.
echo [run] python constellation.py
echo [info] see CONSTELLATION.md
echo ------------------------------------------------------------
".venv\Scripts\python.exe" constellation.py
set "RC=%ERRORLEVEL%"
echo ------------------------------------------------------------
echo [done] exit code: %RC%
goto :end

:err
set "RC=1"
echo.
echo [error] something failed, check the messages above.

:end
echo.
pause