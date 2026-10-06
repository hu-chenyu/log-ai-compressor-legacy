@echo off
REM ============================================================
REM  log-ai-compressor - one-click launcher (double-click)
REM
REM  Starts a LOCAL web service and opens your browser.
REM  The log data never leaves this machine.
REM
REM  IMPORTANT: this file is intentionally ASCII-only and MUST use
REM  CRLF line endings.
REM    - cmd.exe parses batch files byte-by-byte against the console
REM      code page. Any non-ASCII byte desynchronizes the parser and
REM      commands get silently truncated (e.g. "python -m ..." would
REM      be seen as just its tail).
REM    - cmd.exe also mis-parses bare-LF batch files.
REM  All user-facing Chinese text lives in the web UI, not here.
REM ============================================================
setlocal
title log-ai-compressor

cd /d "%~dp0"

REM ---- locate a usable Python: python first, then the py launcher ----
REM the -c probe also rejects the Microsoft Store stub python
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
    py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [ERROR] Python 3.9+ was not found on this machine.
    echo         Install it from https://www.python.org/downloads/
    echo         and tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)

REM ---- dependencies: install once when anything is missing ----
%PY% -c "import fastapi, uvicorn, yaml, sse_starlette, mcp" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Installing dependencies, this takes 1-3 minutes...
    %PY% -m pip install fastapi "uvicorn[standard]" sse-starlette mcp PyYAML
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        echo         Please run manually: pip install -r requirements.txt
        pause
        exit /b 1
    )
)

REM ---- start the local web service (opens your browser) ----
REM the port is auto-picked if 8765 is already taken
%PY% -m log_ai_compressor web
if errorlevel 1 (
    echo.
    echo [ERROR] The service failed to start. Please send the error above.
    pause
)
endlocal
