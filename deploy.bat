@echo off
REM ============================================
REM MatSynth Deploy Script - Optimized
REM Auto-configures SSH key (no password needed)
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
echo Target:       %TARGET%
echo Auto-restart: %AUTO_RESTART%
echo SSH Key:      %SSH_KEY%
echo.

REM Verifica directory sorgente
if not exist "%SOURCE_DIR%" (
    echo ERROR: Source directory not found: %SOURCE_DIR%
    exit /b 1
)

REM ---- STEP 0: Configura chiave SSH se necessario ----
echo [0/4] Configuring SSH key authentication...

REM Crea ~/.ssh se non esiste
if not exist "%USERPROFILE%\.ssh\" mkdir "%USERPROFILE%\.ssh"

REM Genera coppia di chiavi se non esiste
if not exist "%SSH_KEY%" (
    echo [0/3] Configuring SSH key authentication...
    echo    ^> Key not found. Generating Ed25519 key pair...
    ssh-keygen -t ed25519 -f "%SSH_KEY%" -N "" -C "matsynth-deploy-key"
    if !ERRORLEVEL! NEQ 0 (
        echo ERROR: ssh-keygen failed. Ensure OpenSSH Client is installed.
        echo        Settings ^> Apps ^> Optional Features ^> OpenSSH Client
        exit /b 1
    )
    echo    ^> Key pair created: %SSH_KEY%
)

REM Opzioni SSH/SCP condivise (chiave + no-password)
set SSH_OPTS=-i "%SSH_KEY%" -o BatchMode=yes -o ConnectTimeout=10

REM Testa se la chiave e' gia' autorizzata sul server
ssh %SSH_OPTS% %TARGET% "echo OK" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo [0/3] Configuring SSH key authentication...
    echo    ^> Key not yet authorized on Pi. Copying public key...
    echo    ^> ^(You will be prompted for the Pi password ONE last time^)
    type "%SSH_KEY%.pub" | ssh %TARGET% "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    if !ERRORLEVEL! NEQ 0 (
        echo ERROR: Failed to copy public key to %TARGET%.
        echo        Check network connectivity and SSH access on the Pi.
        exit /b 1
    )
    echo    ^> Public key installed on Pi.

    REM Verifica finale che la chiave funzioni
    ssh %SSH_OPTS% %TARGET% "echo OK" >nul 2>&1
    if !ERRORLEVEL! NEQ 0 (
        echo ERROR: Key authentication still failing after key copy.
        echo        Debug with: ssh -i "%SSH_KEY%" -v %TARGET%
        exit /b 1
    )
    echo    ^> SSH key authentication OK
)

REM ---- STEP 1: Prepara directory remota ----
echo [1/3] Preparing remote directory...
ssh %SSH_OPTS% %TARGET% "mkdir -p %DEST_DIR%"
if !ERRORLEVEL! NEQ 0 (
    echo ERROR: Cannot create remote directory
    exit /b 1
)
echo    ^> Remote directory ready

REM ---- STEP 2: Trasferimento file ----
echo [2/3] Transferring files...
scp %SSH_OPTS% -o Compression=yes -r -p "%SOURCE_DIR%\*" %TARGET%:%DEST_DIR%/
if !ERRORLEVEL! NEQ 0 (
    echo ERROR: File transfer failed
    exit /b 1
)
echo    ^> Files transferred successfully

REM ---- STEP 3: Restart servizio (opzionale) ----
echo [3/3] Service management...
if /i "%AUTO_RESTART%"=="y" (
    echo    ^> Restarting MatSynth service...
    ssh %SSH_OPTS% %TARGET% "sudo systemctl restart matsynth.service"
    if !ERRORLEVEL! EQU 0 (
        echo    ^> Service restarted successfully
    ) else (
        echo    ^> WARNING: Service restart failed ^(check sudo permissions on Pi^)
    )
) else (
    echo    ^> Skipped ^(use 'deploy.bat %TARGET% y' to auto-restart^)
)

echo.
echo ================================================
echo   DEPLOY COMPLETED
echo ================================================
echo.
echo Next steps:
echo - SSH into Pi:  ssh -i "%SSH_KEY%" %TARGET%
echo - Check logs:   ssh %TARGET% "journalctl -u matsynth.service -f"
echo - Web UI:       http://%TARGET:*@=%:5000
echo.

endlocal
exit /b 0