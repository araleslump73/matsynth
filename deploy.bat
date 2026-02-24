@echo off
REM ============================================
REM MatSynth Deploy Script (Batch version)
REM Transfer files to Raspberry Pi using SCP
REM ============================================

setlocal enabledelayedexpansion

if "%1"=="" (
    echo.
    echo ERROR: No target server specified!
    echo.
    echo Usage: deploy.bat [target]
    echo Example: deploy.bat matteo@matsynth
    echo Example: deploy.bat matteo@192.168.1.50
    echo.
    exit /b 1
)

set TARGET=%1
set SOURCE_DIR=%~dp0home\matteo\matsynth_web
set DEST_DIR=/home/matteo/matsynth_web

echo.
echo ================================================
echo   MatSynth Deploy to Raspberry Pi
echo ================================================
echo.
echo Source: %SOURCE_DIR%
echo Target: %TARGET%
echo Destination: %DEST_DIR%
echo.

REM Check if source directory exists
if not exist "%SOURCE_DIR%" (
    echo ERROR: Source directory not found: %SOURCE_DIR%
    exit /b 1
)

echo Testing SSH connection...
echo.
ssh %TARGET% "echo 'Connection successful'"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Cannot connect to %TARGET%
    echo Please check:
    echo - Network connectivity
    echo - SSH credentials
    echo - Target address
    echo.
    exit /b 1
)

echo.
echo Creating remote directory...
ssh %TARGET% "mkdir -p %DEST_DIR%"

echo.
echo Transferring files...
echo (You may be prompted for password multiple times)
echo.

REM Transfer files with scp
scp -r -p "%SOURCE_DIR%\*" %TARGET%:%DEST_DIR%/

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ================================================
    echo   SUCCESS: Files transferred successfully!
    echo ================================================
    echo.
    
    set /p RESTART="Restart MatSynth service? (y/n): "
    
    if /i "!RESTART!"=="y" (
        echo.
        echo Restarting service...
        ssh %TARGET% "sudo systemctl restart matsynth.service"
        echo Service restarted!
    )
    
    echo.
    echo Deployment completed!
    echo.
) else (
    echo.
    echo ERROR: File transfer failed!
    echo Please check network connectivity and SSH access.
    echo.
    exit /b 1
)

endlocal
