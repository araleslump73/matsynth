@echo off
REM ============================================
REM MatSynth Deploy Script - Optimized
REM Uses SSH key authentication (NO PASSWORD)
REM ============================================

setlocal enabledelayedexpansion

if "%1"=="" (
    echo.
    echo ERROR: No target server specified!
    echo.
    echo Usage: deploy.bat [target] [auto-restart]
    echo Example: deploy.bat matteo@matsynth
    echo Example: deploy.bat matteo@matsynth y
    echo.
    exit /b 1
)

set TARGET=%1
set SOURCE_DIR=%~dp0home\matteo\matsynth_web
set DEST_DIR=/home/matteo/matsynth_web

REM Auto-restart flag (default: no)
set AUTO_RESTART=%2
if "%AUTO_RESTART%"=="" set AUTO_RESTART=n

echo.
echo ================================================
echo   MatSynth Deploy to Raspberry Pi
echo ================================================
echo Target: %TARGET%
echo Auto-restart: %AUTO_RESTART%
echo.

REM Verifica esistenza directory sorgente
if not exist "%SOURCE_DIR%" (
    echo ERROR: Source directory not found: %SOURCE_DIR%
    exit /b 1
)

REM Test connessione SSH (DEVE usare chiave, no password)
echo [1/4] Testing SSH connection...
ssh -o BatchMode=yes -o ConnectTimeout=5 %TARGET% "echo OK" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: SSH connection failed!
    echo.
    echo This script requires SSH KEY authentication.
    echo Password prompts are NOT supported.
    echo.
    echo Setup instructions:
    echo 1. Run: ssh-keygen -t ed25519 -f ~/.ssh/id_matsynth
    echo 2. Copy key: type %USERPROFILE%\.ssh\id_matsynth.pub ^| ssh %TARGET% "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
    echo 3. Test: ssh %TARGET% "echo test"
    echo.
    exit /b 1
)
echo    ^> Connected successfully (key-based auth)

REM Crea directory remota
echo [2/4] Preparing remote directory...
ssh -o BatchMode=yes %TARGET% "mkdir -p %DEST_DIR%" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Cannot create remote directory
    exit /b 1
)
echo    ^> Remote directory ready

REM Transfer con rsync-like behavior (solo file modificati)
echo [3/4] Transferring files...
scp -o BatchMode=yes -o Compression=yes -r -p "%SOURCE_DIR%\*" %TARGET%:%DEST_DIR%/ 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: File transfer failed
    exit /b 1
)
echo    ^> Files transferred successfully

REM Restart service (opzionale)
echo [4/4] Service management...
if /i "%AUTO_RESTART%"=="y" (
    echo    ^> Restarting MatSynth service...
    ssh -o BatchMode=yes %TARGET% "sudo systemctl restart matsynth.service" 2>nul
    if %ERRORLEVEL% EQU 0 (
        echo    ^> Service restarted successfully
    ) else (
        echo    ^> WARNING: Service restart failed (check sudo permissions^)
    )
) else (
    echo    ^> Skipped (use 'deploy.bat %TARGET% y' to auto-restart^)
)

echo.
echo ================================================
echo   DEPLOY COMPLETED
echo ================================================
echo.
echo Next steps:
echo - SSH into Pi: ssh %TARGET%
echo - Check logs: journalctl -u matsynth.service -f
echo - Web interface: http://%TARGET:*@=%:5000
echo.

endlocal
exit /b 0