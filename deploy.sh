#!/bin/bash

# ============================================
# MatSynth Deploy Script (Bash version)
# Transfer files to Raspberry Pi using SCP
# ============================================

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/home/matteo/matsynth_web"
DEST_DIR="/home/matteo/matsynth_web"

# SSH ControlMaster configuration for single password prompt
SSH_CONTROL_DIR="$HOME/.ssh/control"
SSH_CONTROL_PATH="$SSH_CONTROL_DIR/%r@%h:%p"

# Create SSH control directory if it doesn't exist
mkdir -p "$SSH_CONTROL_DIR"

# Check arguments
if [ -z "$1" ]; then
    echo -e "${RED}ERROR: No target server specified!${NC}"
    echo ""
    echo "Usage: $0 [target]"
    echo "Example: $0 matteo@matsynth"
    echo "Example: $0 matteo@192.168.1.50"
    echo ""
    exit 1
fi

TARGET="$1"

echo -e "${CYAN}"
echo "╔════════════════════════════════════════════════╗"
echo "║     MatSynth Deploy Script for Raspberry Pi   ║"
echo "╚════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check if source directory exists
if [ ! -d "$SOURCE_DIR" ]; then
    echo -e "${RED}❌ ERROR: Source directory not found: $SOURCE_DIR${NC}"
    exit 1
fi

echo -e "${CYAN}📁 Source directory: $SOURCE_DIR${NC}"
echo -e "${CYAN}🎯 Target server:    $TARGET${NC}"
echo -e "${CYAN}📂 Destination:      $DEST_DIR${NC}"
echo ""

# Test SSH connection (will prompt for password)
echo -e "${CYAN}🔐 Testing SSH connection...${NC}"
echo -e "${YELLOW}   (You will be prompted for your password once)${NC}"
echo ""

# Establish SSH ControlMaster connection (prompts for password)
ssh -o ControlMaster=yes -o ControlPath="$SSH_CONTROL_PATH" -o ControlPersist=60s -f -N "$TARGET" 2>/dev/null

if [ $? -ne 0 ]; then
    echo -e "${RED}❌ ERROR: Cannot connect to $TARGET${NC}"
    echo -e "${YELLOW}   Please check:${NC}"
    echo -e "${YELLOW}   - Network connectivity${NC}"
    echo -e "${YELLOW}   - SSH credentials${NC}"
    echo -e "${YELLOW}   - Target address${NC}"
    echo ""
    exit 1
fi

# Test the connection
ssh -o ControlPath="$SSH_CONTROL_PATH" "$TARGET" "echo 'Connection successful'" 2>/dev/null

if [ $? -ne 0 ]; then
    echo -e "${RED}❌ ERROR: SSH connection test failed${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Connection established! (password not required for remaining operations)${NC}"
echo ""

# Confirm before proceeding
echo -e "${YELLOW}⚠️  This will overwrite files on the target server!${NC}"
read -p "Do you want to continue? (yes/no): " confirmation

if [[ ! "$confirmation" =~ ^(yes|y)$ ]]; then
    echo -e "${YELLOW}❌ Deployment cancelled by user.${NC}"
    # Close SSH connection
    ssh -o ControlPath="$SSH_CONTROL_PATH" -O exit "$TARGET" 2>/dev/null
    exit 0
fi

echo -e "${CYAN}🚀 Starting file transfer...${NC}"
echo ""

# Create destination directory on remote server
echo -e "${CYAN}📦 Ensuring destination directory exists...${NC}"
ssh -o ControlPath="$SSH_CONTROL_PATH" "$TARGET" "mkdir -p $DEST_DIR" 2>/dev/null

if [ $? -ne 0 ]; then
    echo -e "${YELLOW}⚠️  Warning: Could not verify/create remote directory${NC}"
    echo -e "${YELLOW}   Continuing anyway...${NC}"
    echo ""
fi

# Transfer files using SCP
echo -e "${CYAN}📤 Transferring files (this may take a moment)...${NC}"
echo ""

scp -o ControlPath="$SSH_CONTROL_PATH" -r -p -C "$SOURCE_DIR"/* "${TARGET}:${DEST_DIR}/"

if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}✅ SUCCESS: All files transferred successfully!${NC}"
    echo ""
    
    # Optional: Restart the service
    read -p "🔄 Do you want to restart the MatSynth service? (yes/no): " restart_confirm
    
    if [[ "$restart_confirm" =~ ^(yes|y)$ ]]; then
        echo ""
        echo -e "${CYAN}🔄 Restarting MatSynth service...${NC}"
        ssh -o ControlPath="$SSH_CONTROL_PATH" "$TARGET" "sudo systemctl restart matsynth.service"
        
        if [ $? -eq 0 ]; then
            echo -e "${GREEN}✅ Service restarted successfully!${NC}"
        else
            echo -e "${YELLOW}⚠️  Warning: Could not restart service. You may need to restart manually.${NC}"
        fi
    fi
    
    # Close SSH connection
    ssh -o ControlPath="$SSH_CONTROL_PATH" -O exit "$TARGET" 2>/dev/null
    
    echo ""
    echo -e "${GREEN}╔════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║          Deployment completed! 🎉              ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════╝${NC}"
    echo ""
else
    echo ""
    echo -e "${RED}❌ ERROR: File transfer failed!${NC}"
    echo -e "${YELLOW}   Please check:${NC}"
    echo -e "${YELLOW}   - Network connectivity to $TARGET${NC}"
    echo -e "${YELLOW}   - SSH access and credentials${NC}"
    echo -e "${YELLOW}   - Remote directory permissions${NC}"
    echo ""
    
    # Close SSH connection
    ssh -o ControlPath="$SSH_CONTROL_PATH" -O exit "$TARGET" 2>/dev/null
    exit 1
fi
