@echo off
REM install.bat — One-time setup for bot-mua-hang (Windows)
setlocal enabledelayedexpansion

echo.
echo ============================================
echo    bot-mua-hang -- Install ^& Setup
echo ============================================
echo.

REM ── 1. Check Python ─────────────────────────────────────────────
echo [..] Checking Python version...

python --version >nul 2>&1
if errorlevel 1 (
    echo [!!] Python not found.
    echo      Download and install Python 3.11+ from https://www.python.org/downloads/
    echo      Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VERSION=%%v
for /f "tokens=1,2 delims=." %%a in ("!PY_VERSION!") do (
    set PY_MAJOR=%%a
    set PY_MINOR=%%b
)

if !PY_MAJOR! LSS 3 (
    echo [!!] Python !PY_VERSION! found, but 3.11+ is required.
    echo      Download from https://www.python.org/downloads/
    pause
    exit /b 1
)
if !PY_MAJOR! EQU 3 if !PY_MINOR! LSS 11 (
    echo [!!] Python !PY_VERSION! found, but 3.11+ is required.
    echo      Download from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python !PY_VERSION!

REM ── 2. Check pip ────────────────────────────────────────────────
echo [..] Checking pip...
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [!!] pip not found. Run: python -m ensurepip --upgrade
    pause
    exit /b 1
)
echo [OK] pip available

REM ── 3. Install Python packages ───────────────────────────────────
echo [..] Installing Python packages from requirements.txt...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [!!] Failed to install packages. Check the error above.
    pause
    exit /b 1
)
echo [OK] Python packages installed

REM ── 4. Copy .env if missing ──────────────────────────────────────
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo [..] Created .env from .env.example
        echo      The setup wizard will guide you through adding your API key.
    )
)

REM ── Done ─────────────────────────────────────────────────────────
echo.
echo ============================================
echo [OK] Installation complete!
echo ============================================
echo.
echo Starting bot-mua-hang...
echo Open http://localhost:8081 in your browser.
echo The setup wizard will guide you through the rest.
echo.
python main.py
pause
