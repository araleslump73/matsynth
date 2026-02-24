#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Deploy MatSynth Web to Raspberry Pi
.DESCRIPTION
    Transfers all MatSynth web files to the Raspberry Pi using SCP
.PARAMETER Target
    Target server address (e.g., matteo@matsynth or matteo@192.168.1.100)
.EXAMPLE
    .\deploy.ps1 matteo@matsynth
.EXAMPLE
    .\deploy.ps1 matteo@192.168.1.50
#>

param(
    [Parameter(Mandatory=$true, Position=0, HelpMessage="Target server (e.g., matteo@matsynth)")]
    [string]$Target
)

# Colors for output
$ErrorColor = "Red"
$SuccessColor = "Green"
$InfoColor = "Cyan"
$WarningColor = "Yellow"

# Configuration
$SourceDir = Join-Path $PSScriptRoot "home\matteo\matsynth_web"
$DestDir = "/home/matteo/matsynth_web"
$SshControlPath = "/tmp/matsynth_deploy_%r@%h:%p"
$SshOpts = @("-o", "ControlMaster=auto", "-o", "ControlPath=$SshControlPath", "-o", "ControlPersist=10m")

Write-Host "`n╔════════════════════════════════════════════════╗" -ForegroundColor $InfoColor
Write-Host "║     MatSynth Deploy Script for Raspberry Pi   ║" -ForegroundColor $InfoColor
Write-Host "╚════════════════════════════════════════════════╝`n" -ForegroundColor $InfoColor

# Check if source directory exists
if (-not (Test-Path $SourceDir)) {
    Write-Host "❌ ERROR: Source directory not found: $SourceDir" -ForegroundColor $ErrorColor
    exit 1
}

Write-Host "📁 Source directory: $SourceDir" -ForegroundColor $InfoColor
Write-Host "🎯 Target server:    $Target" -ForegroundColor $InfoColor
Write-Host "📂 Destination:      $DestDir`n" -ForegroundColor $InfoColor

# Test SSH connection (will prompt for password)
Write-Host "🔐 Testing SSH connection and establishing session..." -ForegroundColor $InfoColor
Write-Host "   (You will be prompted for your password ONCE)`n" -ForegroundColor $WarningColor

$testResult = ssh @SshOpts $Target "echo 'Connection successful'" 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ ERROR: Cannot connect to $Target" -ForegroundColor $ErrorColor
    Write-Host "   Please check:" -ForegroundColor $WarningColor
    Write-Host "   - Network connectivity" -ForegroundColor $WarningColor
    Write-Host "   - SSH credentials" -ForegroundColor $WarningColor
    Write-Host "   - Target address`n" -ForegroundColor $WarningColor
    exit 1
}

Write-Host "✅ Connection established! Password will be reused for all operations.`n" -ForegroundColor $SuccessColor

# Check if scp is available
try {
    $scpVersion = scp 2>&1
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {
        Write-Host "❌ ERROR: SCP not found. Please install OpenSSH Client." -ForegroundColor $ErrorColor
        Write-Host "   You can install it via: Settings > Apps > Optional Features > OpenSSH Client" -ForegroundColor $WarningColor
        exit 1
    }
} catch {
    Write-Host "❌ ERROR: SCP not found. Please install OpenSSH Client." -ForegroundColor $ErrorColor
    exit 1
}

# Confirm before proceeding
Write-Host "⚠️  This will overwrite files on the target server!" -ForegroundColor $WarningColor
$confirmation = Read-Host "Do you want to continue? (yes/no)"

if ($confirmation -ne "yes" -and $confirmation -ne "y") {
    Write-Host "`n❌ Deployment cancelled by user.`n" -ForegroundColor $WarningColor
    exit 0
}

Write-Host "`n🚀 Starting file transfer...`n" -ForegroundColor $InfoColor

# Create destination directory on remote server (if it doesn't exist)
Write-Host "📦 Ensuring destination directory exists..." -ForegroundColor $InfoColor
ssh $Target "mkdir -p $DestDir" 2>&1 | Out-Null

if (@SshOpts $LASTEXITCODE -ne 0) {
    Write-Host "⚠️  Warning: Could not verify/create remote directory" -ForegroundColor $WarningColor
    Write-Host "   Continuing anyway...`n" -ForegroundColor $WarningColor
}

# Transfer files using SCP
Write-Host "📤 Transferring files (this may take a moment)...`n" -ForegroundColor $InfoColor

# Use scp with recursive option and SSH multiplexing
# -r: recursive
# -p: preserve modification times and modes
# -C: enable compression
$scpArgs = $SshOpts + @("-r", "-p", "-C", "$SourceDir\*", "${Target}:${DestDir}/")
& scp @scpArgs

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n✅ SUCCESS: All files transferred successfully!`n" -ForegroundColor $SuccessColor
    
    # Optional: Restart the service
    Write-Host "🔄 Do you want to restart the MatSynth service? (yes/no)" -ForegroundColor $InfoColor
    $restartConfirm = Read-Host
    
    if ($restartConfirm -eq "yes" -or $restartConfirm -eq "y") {
        Writ@SshOpts $Target "sudo systemctl restart matsynth.service"
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✅ Service restarted successfully!`n" -ForegroundColor $SuccessColor
        } else {
            Write-Host "⚠️  Warning: Could not restart service. You may need to restart manually.`n" -ForegroundColor $WarningColor
        }
    }
    
    Write-Host "🔌 Closing SSH connection..." -ForegroundColor $InfoColor
    ssh @SshOpts -O exit $Target 2>&1 | Out-Null   }
    }
    
    Write-Host "╔════════════════════════════════════════════════╗" -ForegroundColor $SuccessColor
    Write-Host "║          Deployment completed! 🎉              ║" -ForegroundColor $SuccessColor
    Write-Host "╚════════════════════════════════════════════════╝`n" -ForegroundColor $SuccessColor
    
} else {
    Write-Host "`n❌ ERROR: File transfer failed!" -ForegroundColor $ErrorColor
    Write-Host "   Please check:" -ForegroundColor $WarningColor
    Write-Host "   - Network connectivity to $Target" -ForegroundColor $WarningColor
    Write-Host "   - SSH access and credentials" -ForegroundColor $WarningColor
    Write-Host "   - Remote directory permissions`n" -ForegroundColor $WarningColor
    exit 1
}
