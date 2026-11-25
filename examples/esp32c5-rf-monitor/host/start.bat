@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo   esp-sdr Host (PySide6 + WebView)
echo ==========================================
echo   dir   : %CD%
echo   board : COM56 (real, gain 60)  ^|  add --mock for the fake firmware
echo   engine: host (per-window)      ^|  add --engine device for device sweep
echo.

python -c "import PySide6, serial" >nul 2>&1
if errorlevel 1 (
    echo [FIRST RUN] Installing dependencies, please wait...
    python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [ERROR] Dependency install failed.
        pause
        exit /b 1
    )
)

echo [START] Launching host app...
python app.py %*
if "%*"=="" goto ran
goto check

:ran
echo.
echo [DONE] App exited. Examples:
echo        python app.py                            ^(real board COM56, gain 60^)
echo        python app.py --mock --scenario wifi_busy ^(no board^)
echo        python app.py --engine device             ^(device-side sweep^)
echo        python app.py --lang en                   ^(English UI^)
goto end

:check
if errorlevel 1 (
    echo.
    echo [ERROR] App exited with error. See message above.
    pause
)

:end
endlocal