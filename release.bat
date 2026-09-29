@echo off
REM Double-click this file to publish the GitHub Release.
REM It only calls release_upload.py -- all the logic lives there.
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================
echo   Publishing bTool Release
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] python not found on PATH.
  echo         Install Python or run release_upload.py manually.
  echo.
  pause
  exit /b 1
)

python release_upload.py
set RC=%errorlevel%

echo.
if "%RC%"=="0" (
  echo ============================================
  echo   Done. Press any key to close.
  echo ============================================
) else (
  echo ============================================
  echo   FAILED with code %RC% -- copy the text above.
  echo ============================================
)
echo.
pause
