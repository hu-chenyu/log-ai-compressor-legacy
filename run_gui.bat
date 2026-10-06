@echo off
REM ============================================================
REM  log-ai-compressor - GUI launcher for Windows (double-click)
REM
REM  IMPORTANT: this file is intentionally ASCII-only.
REM  cmd.exe parses batch files byte-by-byte against the console
REM  code page. Any non-ASCII byte (Chinese comments or messages)
REM  desynchronizes the parser when the console code page does not
REM  match the file encoding, and commands get silently truncated
REM  (for example "%PY% run_gui.py" is seen as "ui.py").
REM  All user-facing Chinese text lives in the GUI itself, which is
REM  unaffected. This file MUST also use CRLF line endings - cmd.exe
REM  mis-parses bare-LF batch files and drops the head of every line.
REM ============================================================
setlocal
title log-ai-compressor

REM ---- always work from the script directory so run_gui.py resolves ----
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

REM ---- dependency check: install once when anything is missing ----
%PY% -c "import customtkinter, matplotlib, yaml, tkinterdnd2" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Installing dependencies, this takes 1-2 minutes...
    %PY% -m pip install customtkinter matplotlib PyYAML tkinterdnd2
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        echo         Please run manually: pip install -r requirements.txt
        pause
        exit /b 1
    )
)

REM ---- launch the GUI ----
%PY% run_gui.py
if errorlevel 1 (
    echo.
    echo [ERROR] The GUI failed to start. Please send the error above.
    pause
)
endlocal
