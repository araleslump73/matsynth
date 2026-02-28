#!/bin/bash

# ============================================
# SSH Key Setup Script for MatSynth
# Configure passwordless SSH authentication
# ============================================

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

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
echo "║   SSH Passwordless Setup for MatSynth Deploy  ║"
echo "╚════════════════════════════════════════════════╝"
echo -e "${NC}"
echo ""
echo -e "${CYAN}🎯 Target server: $TARGET${NC}"
echo ""

# Check if SSH key already exists
SSH_KEY="$HOME/.ssh/id_ed25519"
SSH_PUB_KEY="$HOME/.ssh/id_ed25519.pub"

if [ -f "$SSH_KEY" ]; then
    echo -e "${GREEN}✓ SSH key already exists: $SSH_KEY${NC}"
else
    echo -e "${YELLOW}⚠ SSH key not found. Generating new Ed25519 key...${NC}"
    echo ""
    
    # Generate SSH key (Ed25519 is more secure and faster than RSA)
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "matsynth-deploy-key"
    
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ ERROR: Failed to generate SSH key${NC}"
        exit 1
    fi
    
    echo ""
    echo -e "${GREEN}✓ SSH key generated successfully!${NC}"
fi

echo ""
echo -e "${CYAN}📤 Copying SSH key to $TARGET...${NC}"
echo -e "${YELLOW}   (You will be prompted for your password ONE LAST TIME)${NC}"
echo ""

# Copy SSH key to target server
ssh-copy-id -i "$SSH_PUB_KEY" "$TARGET"

if [ $? -ne 0 ]; then
    echo ""
    echo -e "${RED}❌ ERROR: Failed to copy SSH key to $TARGET${NC}"
    echo ""
    echo -e "${YELLOW}Troubleshooting:${NC}"
    echo -e "${YELLOW}  1. Check network connectivity${NC}"
    echo -e "${YELLOW}  2. Verify username and hostname${NC}"
    echo -e "${YELLOW}  3. Ensure SSH is enabled on Raspberry Pi${NC}"
    echo ""
    exit 1
fi

echo ""
echo -e "${CYAN}🔍 Testing passwordless connection...${NC}"

# Test passwordless connection
ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" "echo 'SUCCESS'" 2>/dev/null

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ Passwordless SSH connection is working!${NC}"
    echo ""
    echo -e "${GREEN}╔════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║            Setup Complete! 🎉                  ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${CYAN}You can now run ./deploy.sh without entering a password:${NC}"
    echo -e "${YELLOW}  ./deploy.sh $TARGET${NC}"
    echo ""
else
    echo -e "${RED}❌ ERROR: Passwordless connection test failed${NC}"
    echo ""
    echo -e "${YELLOW}The key was copied, but authentication is not working.${NC}"
    echo -e "${YELLOW}Try connecting manually to debug:${NC}"
    echo -e "${YELLOW}  ssh -v $TARGET${NC}"
    echo ""
    exit 1
fi

exit 0
