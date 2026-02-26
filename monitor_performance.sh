#!/bin/bash
# MatSynth Performance Monitor for Raspberry Pi Zero 2W
# Usage: ./monitor_performance.sh [interval_seconds]

INTERVAL=${1:-5}  # Default 5 seconds between checks

echo "==================================="
echo "MatSynth Performance Monitor"
echo "Raspberry Pi Zero 2W"
echo "==================================="
echo "Press Ctrl+C to stop"
echo ""

# Check if FluidSynth is running
if ! pgrep -x "fluidsynth" > /dev/null; then
    echo "⚠️  WARNING: FluidSynth is not running!"
    echo ""
fi

# Check if Flask is running
if ! pgrep -f "matsynth_web/app.py" > /dev/null; then
    echo "⚠️  WARNING: Flask app is not running!"
    echo ""
fi

# Header
printf "%-8s | %-10s | %-10s | %-8s | %-12s | %-12s | %-8s\n" \
    "Time" "Temp(°C)" "CPU(MHz)" "Load" "FluidRAM" "FlaskRAM" "Swap"
printf "%s\n" "----------------------------------------------------------------------------------------"

while true; do
    # Get timestamp
    TIMESTAMP=$(date +"%H:%M:%S")
    
    # Get CPU temperature
    TEMP=$(vcgencmd measure_temp 2>/dev/null | grep -oP '\d+\.\d+' || echo "N/A")
    
    # Get CPU frequency (convert to MHz)
    FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null)
    if [ -n "$FREQ" ]; then
        FREQ_MHZ=$(echo "scale=0; $FREQ / 1000" | bc)
    else
        FREQ_MHZ="N/A"
    fi
    
    # Get load average (1 minute)
    LOAD=$(uptime | awk -F'load average:' '{print $2}' | awk '{print $1}' | tr -d ',')
    
    # Get FluidSynth memory usage (in MB)
    FLUID_MEM=$(ps aux | grep fluidsynth | grep -v grep | awk '{print $6}')
    if [ -n "$FLUID_MEM" ]; then
        FLUID_MB=$(echo "scale=0; $FLUID_MEM / 1024" | bc)
    else
        FLUID_MB="N/A"
    fi
    
    # Get Flask (Python) memory usage (in MB)
    FLASK_MEM=$(ps aux | grep "python3.*app.py" | grep -v grep | awk '{print $6}')
    if [ -n "$FLASK_MEM" ]; then
        FLASK_MB=$(echo "scale=0; $FLASK_MEM / 1024" | bc)
    else
        FLASK_MB="N/A"
    fi
    
    # Get swap usage
    SWAP_USED=$(free | grep Swap | awk '{print $3}')
    SWAP_TOTAL=$(free | grep Swap | awk '{print $2}')
    if [ "$SWAP_TOTAL" -gt 0 ]; then
        SWAP_PCT=$(echo "scale=0; ($SWAP_USED * 100) / $SWAP_TOTAL" | bc)
        SWAP_INFO="${SWAP_PCT}%"
    else
        SWAP_INFO="N/A"
    fi
    
    # Color coding for temperature
    if [ "$TEMP" != "N/A" ]; then
        TEMP_FLOAT=$(echo $TEMP | cut -d. -f1)
        if [ "$TEMP_FLOAT" -gt 75 ]; then
            TEMP_DISPLAY="🔴 $TEMP"
        elif [ "$TEMP_FLOAT" -gt 65 ]; then
            TEMP_DISPLAY="🟡 $TEMP"
        else
            TEMP_DISPLAY="🟢 $TEMP"
        fi
    else
        TEMP_DISPLAY="N/A"
    fi
    
    # Print row
    printf "%-8s | %-10s | %-10s | %-8s | %-10sMB | %-10sMB | %-8s\n" \
        "$TIMESTAMP" "$TEMP_DISPLAY" "$FREQ_MHZ" "$LOAD" "$FLUID_MB" "$FLASK_MB" "$SWAP_INFO"
    
    sleep $INTERVAL
done
