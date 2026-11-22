@echo off
REM ============================================
REM MatSynth Deploy Script - SSH Key Automation
REM Auto-generates SSH key if missing
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
set SSH_KEY=%USERPROFILE%\.ssh\id_matsynth

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

REM [STEP 0] Auto-setup chiave SSH se mancante
if not exist "%SSH_KEY%" (
    echo [0/4] SSH key not found - creating new key...
    echo    ^> Generating ed25519 key at %SSH_KEY%
    
    REM Crea directory .ssh se non esiste
    if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
    
    REM Genera chiave (no passphrase per automazione)
    ssh-keygen -t ed25519 -f "%SSH_KEY%" -N "" -C "matsynth-deploy" >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo ERROR: Failed to generate SSH key
        exit /b 1
    )
    echo    ^> Key generated successfully
    
    REM Upload chiave pubblica al Pi (RICHIEDE PASSWORD UNA SOLA VOLTA)
    echo    ^> Uploading public key to %TARGET%
    echo    ^> [WILL PROMPT FOR PASSWORD - LAST TIME]
    type "%SSH_KEY%.pub" | ssh %TARGET% "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && chmod 700 ~/.ssh"
    if %ERRORLEVEL% NEQ 0 (
        echo ERROR: Failed to upload SSH key
        exit /b 1
    )
    echo    ^> SSH key setup complete - future deploys will be passwordless
    
    REM Breve pausa per permettere al server SSH di aggiornare authorized_keys
    timeout /t 2 /nobreak >nul 2>&1
    echo.
)

REM Test connessione SSH con chiave esplicita
echo [1/4] Testing SSH connection...
ssh -i "%SSH_KEY%" -o ConnectTimeout=5 %TARGET% "echo OK" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: SSH connection failed!
    echo Key exists at: %SSH_KEY%
    echo Try manually: ssh -i "%SSH_KEY%" %TARGET%
    echo.
    exit /b 1
)
echo    ^> Connected successfully (key-based auth)

REM Crea directory remota
echo [2/4] Preparing remote directory...
ssh -i "%SSH_KEY%" %TARGET% "mkdir -p %DEST_DIR%" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Cannot create remote directory
    exit /b 1
)
echo    ^> Remote directory ready

REM Transfer con chiave esplicita e compressione
echo [3/4] Transferring files...
scp -i "%SSH_KEY%" -o Compression=yes -r -p "%SOURCE_DIR%\*" %TARGET%:%DEST_DIR%/ 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: File transfer failed
    exit /b 1
)
echo    ^> Files transferred successfully

REM Restart service (opzionale)
echo [4/4] Service management...
if /i "%AUTO_RESTART%"=="y" (
    echo    ^> Restarting MatSynth service...
    ssh -i "%SSH_KEY%" %TARGET% "sudo systemctl restart matsynth.service" 2>nul
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
echo - SSH into Pi: ssh -i "%SSH_KEY%" %TARGET%
echo - Check logs: journalctl -u matsynth.service -f
echo - Web interface: http://%TARGET:*@=%:5000
echo.

endlocal
exit /b 0