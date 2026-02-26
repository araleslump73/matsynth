#!/bin/bash
# MatSynth - Quick Setup for Raspberry Pi Zero 2W Optimization
# This script applies recommended system optimizations
# Run with: sudo ./setup_pi_zero_optimizations.sh

set -e

echo "============================================"
echo "MatSynth - Pi Zero 2W Optimization Setup"
echo "============================================"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "❌ Error: Please run as root (sudo)"
    exit 1
fi

# Backup existing configs
echo "📁 Creating backups..."
mkdir -p /root/matsynth_backups
cp /etc/dphys-swapfile /root/matsynth_backups/dphys-swapfile.bak 2>/dev/null || true
cp /boot/config.txt /root/matsynth_backups/config.txt.bak 2>/dev/null || true
echo "✅ Backups created in /root/matsynth_backups/"
echo ""

# 1. Configure Swap
echo "💾 Configuring swap file (512MB)..."
if grep -q "CONF_SWAPSIZE=512" /etc/dphys-swapfile; then
    echo "   Swap already configured"
else
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
    sed -i 's/^#CONF_SWAPFACTOR=.*/CONF_SWAPFACTOR=2/' /etc/dphys-swapfile
    sed -i 's/^#CONF_MAXSWAP=.*/CONF_MAXSWAP=1024/' /etc/dphys-swapfile
    dphys-swapfile swapoff
    dphys-swapfile setup
    dphys-swapfile swapon
    echo "✅ Swap configured and activated"
fi
echo ""

# 2. Install cpufrequtils if not present
echo "⚙️  Installing CPU frequency tools..."
if ! command -v cpufreq-set &> /dev/null; then
    apt-get update -qq
    apt-get install -y cpufrequtils bc
    echo "✅ CPU frequency tools installed"
else
    echo "   Already installed"
fi
echo ""

# 3. Configure CPU Governor
echo "🚀 Setting CPU governor to 'performance'..."
echo 'GOVERNOR="performance"' > /etc/default/cpufrequtils
systemctl restart cpufrequtils 2>/dev/null || true
echo "✅ CPU governor configured"
echo ""

# 4. Optimize boot config
echo "🔧 Optimizing /boot/config.txt..."

# Add MatSynth optimization section if not present
if ! grep -q "# MatSynth Optimizations" /boot/config.txt; then
    cat >> /boot/config.txt << 'EOF'

# MatSynth Optimizations for Pi Zero 2W
gpu_mem=16              # Minimal GPU memory (headless)
audio_pwm_mode=2        # Improved audio quality

# Uncomment for moderate overclocking (monitor temperature!)
# arm_freq=1200
# over_voltage=2

# Uncomment to disable WiFi/Bluetooth if not needed (saves ~50MB RAM)
# dtoverlay=disable-wifi
# dtoverlay=disable-bt
EOF
    echo "✅ Boot config optimized"
else
    echo "   Already configured"
fi
echo ""

# 5. Disable unnecessary services
echo "🛑 Disabling unnecessary services..."

services_to_disable=(
    "bluetooth"
    "hciuart"
    "avahi-daemon"
    "triggerhappy"
)

for service in "${services_to_disable[@]}"; do
    if systemctl is-enabled "$service" &> /dev/null; then
        systemctl disable "$service" 2>/dev/null || true
        systemctl stop "$service" 2>/dev/null || true
        echo "   ✓ Disabled $service"
    fi
done
echo "✅ Services optimized"
echo ""

# 6. Check current status
echo "📊 Current System Status:"
echo "-------------------------------------------"
echo -n "   RAM Total: "
free -h | grep Mem | awk '{print $2}'
echo -n "   Swap Total: "
free -h | grep Swap | awk '{print $2}'
echo -n "   CPU Frequency: "
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null | awk '{print $1/1000 " MHz"}' || echo "N/A"
echo -n "   CPU Governor: "
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "N/A"
echo -n "   Temperature: "
vcgencmd measure_temp 2>/dev/null || echo "N/A"
echo "-------------------------------------------"
echo ""

# 7. Final recommendations
echo "✅ Optimization complete!"
echo ""
echo "📋 Next Steps:"
echo "   1. Reboot to apply all changes: sudo reboot"
echo "   2. After reboot, verify with: free -h && uptime"
echo "   3. Use SF3 soundfonts (not SF2) for best performance"
echo "   4. Monitor performance: ./monitor_performance.sh"
echo ""
echo "⚠️  Important Notes:"
echo "   - Temperature monitoring recommended (vcgencmd measure_temp)"
echo "   - For overclocking, edit /boot/config.txt and uncomment lines"
echo "   - Backups saved in: /root/matsynth_backups/"
echo ""

read -p "Reboot now? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "🔄 Rebooting..."
    reboot
else
    echo "ℹ️  Remember to reboot later: sudo reboot"
fi
