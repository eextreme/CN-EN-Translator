@echo off
setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PYW=.venv\Scripts\pythonw.exe"

rem --- 1. Make sure the virtual environment exists ---
if not exist "%VENV_PY%" (
    echo Virtual environment not found. Creating .venv ...
    where py >nul 2>&1 && (py -3 -m venv .venv) || (python -m venv .venv)
    if not exist "%VENV_PY%" (
        echo.
        echo ERROR: Could not create .venv. Is Python 3 installed and on PATH?
        goto :fail
    )
)

rem --- 2. Make sure dependencies are installed ---
"%VENV_PY%" -c "import PySide6, opencc" >nul 2>&1
if errorlevel 1 (
    echo Missing dependencies. Installing from requirements.txt ...
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: pip install failed. See the output above.
        goto :fail
    )
    "%VENV_PY%" -c "import PySide6, opencc" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo ERROR: Dependencies still cannot be imported after install.
        goto :fail
    )
)

rem --- 3. Make sure the app imports cleanly (catches errors pythonw would hide) ---
"%VENV_PY%" -c "import ln_translator.__main__" >nul 2>"%TEMP%\ln_translator_check.txt"
if errorlevel 1 (
    echo ERROR: ln_translator failed to import:
    echo.
    type "%TEMP%\ln_translator_check.txt"
    goto :fail
)

rem --- 4. Launch without a console window ---
start "" "%VENV_PYW%" -m ln_translator
exit /b 0

:fail
echo.
pause
exit /b 1
