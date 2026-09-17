#!/bin/bash

# ============================================================================
# Virtual Lab - Lab Pi Installation Script
# ============================================================================
# This script sets up a Raspberry Pi as a Lab Pi (Experiment Node)
# It clones the repository and configures the Lab Pi to connect to Master Pi
#
# Usage:
#   Run from inside your remote_lab_pi checkout (or an empty directory, which
#   the script will clone the repository into):
#   ./install-lab-pi.sh
#
# Required Environment Variables:
#   LAB_PI_ID       - Unique ID for this Lab Pi (e.g., lab-001)
#   LAB_PI_NAME     - Display name (e.g., "LED Blinky Lab")
#   LAB_PI_MAC      - MAC address of the Pi
#   EXPERIMENT_ID   - Which experiment this Pi handles
#   MASTER_URL      - URL of Master Pi (e.g., http://192.168.1.100:5000)
#   MASTER_API_KEY  - Optional API key for authentication
# ============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Resolve these up front so every later step (including the ustreamer build,
# which cd's back into the project dir) can rely on them being set.
# The project dir is wherever this script is run from, not a hardcoded
# path -- this way .env, the venv, and the systemd services all end up
# next to the code you actually have checked out.
PROJECT_DIR="$(pwd)"
CURRENT_USER=$(whoami)
CURRENT_HOME=$(eval echo ~"$CURRENT_USER")

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Virtual Lab - Lab Pi Setup${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo -e "${YELLOW}⚠️  Running as root is not recommended. Run as regular user with sudo.${NC}"
fi

# Get MAC address if not provided
get_mac_address() {
    # Try to get MAC address from eth0 or wlan0
    for iface in eth0 wlan0 end0; do
        if ip link show "$iface" &>/dev/null; then
            mac=$(ip link show "$iface" | grep link | awk '{print $2}')
            if [ -n "$mac" ]; then
                echo "$mac"
                return 0
            fi
        fi
    done
    echo ""
    return 1
}

# Detect hostname
get_hostname() {
    hostname -s 2>/dev/null || echo "raspberrypi"
}

# Prompt on the controlling terminal rather than this script's own stdin.
# This keeps prompts working even when the script itself is invoked
# non-interactively (e.g. `curl ... | bash`, `ssh host bash install-lab-pi.sh`,
# a cron/CI job). Without this, `read -p` reads EOF immediately in those
# cases and, combined with `set -e`, aborts the whole script on the very
# first prompt — leaving .env missing or half-written instead of populated
# with the defaults. If there's no controlling terminal at all, the read is
# skipped (rather than killing the script) and the caller's own default
# takes over.
prompt_read() {
    local __prompt="$1" __var="$2"
    # HAS_TTY (checked once, up front) already confirms /dev/tty opens
    # cleanly, so no need to silence stderr here -- read -p writes its
    # prompt text to stderr, and doing so would make it invisible.
    [ "$HAS_TTY" = "1" ] || return 0
    read -r -p "$__prompt" "$__var" < /dev/tty || true
}

# ============================================================================
# Step 1: Get Configuration
# ============================================================================
echo -e "${YELLOW}Step 1: Configuration${NC}"
echo ""

# -r/-w on /dev/tty only check permission bits, not whether a controlling
# terminal actually exists to open — actually try it, with stderr silenced
# so a headless run doesn't print "/dev/tty: No such device or address".
if { : < /dev/tty; } 2>/dev/null; then
    HAS_TTY=1
else
    HAS_TTY=0
fi

if [ "$HAS_TTY" = "0" ]; then
    echo -e "${YELLOW}⚠️  No interactive terminal detected — configuration prompts will be skipped.${NC}"
    echo -e "${YELLOW}   Any value not already set via environment variable will use its default${NC}"
    echo -e "${YELLOW}   (or be left blank for optional fields). To customize, run this script${NC}"
    echo -e "${YELLOW}   directly in a terminal, or pre-export: LAB_PI_ID, LAB_PI_NAME, LAB_PI_MAC,${NC}"
    echo -e "${YELLOW}   EXPERIMENT_ID, MASTER_URL, MASTER_API_KEY, LOCATION.${NC}"
    echo ""
fi

# Get hostname for default values
DETECTED_HOSTNAME=$(get_hostname)

# Lab Pi ID - use hostname as default
if [ -z "$LAB_PI_ID" ]; then
    prompt_read "Enter Lab Pi ID (default: lab-$DETECTED_HOSTNAME): " LAB_PI_ID
    LAB_PI_ID=${LAB_PI_ID:-lab-$DETECTED_HOSTNAME}
fi

# Lab Pi Name - use hostname as default
if [ -z "$LAB_PI_NAME" ]; then
    prompt_read "Enter Lab Pi Name (default: Lab Pi $DETECTED_HOSTNAME): " LAB_PI_NAME
    LAB_PI_NAME=${LAB_PI_NAME:-Lab Pi $DETECTED_HOSTNAME}
fi

# MAC Address
if [ -z "$LAB_PI_MAC" ]; then
    echo "MAC address not provided. Attempting to detect..."
    DETECTED_MAC=$(get_mac_address)
    if [ -n "$DETECTED_MAC" ]; then
        echo -e "Detected MAC: ${GREEN}$DETECTED_MAC${NC}"
        prompt_read "Use this MAC address? (Y/n): " USE_DETECTED
        if [ "$USE_DETECTED" != "n" ] && [ "$USE_DETECTED" != "N" ]; then
            LAB_PI_MAC="$DETECTED_MAC"
        fi
    fi
    if [ -z "$LAB_PI_MAC" ]; then
        prompt_read "Enter MAC address (optional, press Enter to skip): " LAB_PI_MAC
    fi
fi

# Experiment ID - default to 1
if [ -z "$EXPERIMENT_ID" ]; then
    prompt_read "Enter Experiment ID (default: 1): " EXPERIMENT_ID
    EXPERIMENT_ID=${EXPERIMENT_ID:-1}
fi

# Master URL - use common default
if [ -z "$MASTER_URL" ]; then
    prompt_read "Enter Master Pi URL (default: http://10.114.62.73:5000): " MASTER_URL
    MASTER_URL=${MASTER_URL:-http://10.114.62.73:5000}
fi

# Master API Key (optional)
if [ -z "$MASTER_API_KEY" ]; then
    prompt_read "Enter Master API Key (optional, press Enter to skip): " MASTER_API_KEY
fi

# Location (optional)
if [ -z "$LOCATION" ]; then
    prompt_read "Enter Location (optional, e.g., Lab Room 101): " LOCATION
fi

echo ""
echo -e "${GREEN}Configuration Summary:${NC}"
echo "  Lab Pi ID: $LAB_PI_ID"
echo "  Lab Pi Name: $LAB_PI_NAME"
echo "  MAC Address: $LAB_PI_MAC"
echo "  Experiment ID: $EXPERIMENT_ID"
echo "  Master URL: $MASTER_URL"
echo "  Location: $LOCATION"
echo ""

# ============================================================================
# Step 2: Update System
# ============================================================================
echo -e "${YELLOW}Step 2: Updating system packages...${NC}"
sudo apt update && sudo apt upgrade -y

# ============================================================================
# Step 3: Install Dependencies
# ============================================================================
echo -e "${YELLOW}Step 3: Installing system dependencies...${NC}"
sudo apt update

# Remove conflicting esptool packages
echo "Fixing esptool installation..."
sudo apt remove -y esptool python3-esptool 2>/dev/null || true

sudo apt install -y \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    curl \
    wget \
    swig \
    liblgpio-dev \
    portaudio19-dev \
    libasound2-dev \
    libjpeg-dev \
    libevent-dev \
    libbsd-dev \
    avrdude \
    openocd \
    gdb-multiarch \
    alsa-utils \
    libportaudio2 \
    ffmpeg

# Install esptool as Python module ONLY (more reliable)
echo "Installing esptool Python module..."
pip3 install --break-system-packages esptool

# Install lgpio for GPIO control
echo "Installing lgpio for GPIO control..."
pip3 install --break-system-packages lgpio

# Install ustreamer from source (more reliable)
echo "Installing ustreamer from source..."
cd /tmp
if [ ! -d "ustreamer" ]; then
    git clone https://github.com/pikvm/ustreamer.git
fi
cd ustreamer
make -j$(nproc)
sudo make install
cd "$PROJECT_DIR"

# ============================================================================
# Step 5: DFRobot UPS will be installed after project setup
# ============================================================================
echo -e "${YELLOW}Step 5: DFRobot UPS will be installed after project setup...${NC}"

# ============================================================================
# Step 6: Set up the project in the current directory
# ============================================================================
echo -e "${YELLOW}Step 6: Setting up project...${NC}"

# PROJECT_DIR is `pwd`, so it always exists -- the question is whether it's
# already a checkout of this repo, empty (bootstrap: clone into it), or
# neither (refuse rather than guess).
if [ -d "$PROJECT_DIR/.git" ]; then
    echo "Already inside a git checkout at $PROJECT_DIR -- using it as-is."
    echo "(Not auto git-pulling/resetting, so this doesn't clobber local changes" \
         "you may have here -- pull manually first if you want the latest code.)"
    cd "$PROJECT_DIR"
elif [ -z "$(find "$PROJECT_DIR" -mindepth 1 -maxdepth 1 ! -name "$(basename "$0")" -print -quit 2>/dev/null)" ]; then
    echo "Current directory is empty -- cloning repository into it..."
    # Replace with your actual repository URL
    REPO_URL=${REPO_URL:-"https://github.com/Abhilash1575/remote_lab_pi.git"}
    # `git clone` refuses a target dir that already holds this script, so
    # clone to a scratch dir and move the checkout (dotfiles included) up.
    CLONE_TMP="$(mktemp -d)"
    git clone "$REPO_URL" "$CLONE_TMP"
    shopt -s dotglob
    mv "$CLONE_TMP"/* "$PROJECT_DIR"/
    shopt -u dotglob
    rmdir "$CLONE_TMP"
    cd "$PROJECT_DIR"
else
    echo -e "${RED}❌ $PROJECT_DIR is not a git repository and isn't empty.${NC}"
    echo "Run this script from inside your remote_lab_pi checkout, or from an empty directory."
    exit 1
fi

# ============================================================================
# Step 7: Install DFRobot UPS support (Raspberry Pi only)
# ============================================================================
echo -e "${YELLOW}Step 7: Installing DFRobot UPS support...${NC}"

# Check if we're running on Raspberry Pi
if [ "$(uname -m)" = "armv7l" ] || [ "$(uname -m)" = "aarch64" ]; then
    if [ -f "/proc/device-tree/model" ] && grep -q "Raspberry" "/proc/device-tree/model"; then
        echo -e "${GREEN}✅ Detected Raspberry Pi - Installing DFRobot UPS support${NC}"
        
        # Copy UPS script
        if [ -f "$PROJECT_DIR/install/rpi_dfrobot_ups_all_in_one.sh" ]; then
            # Run in its own subshell so a failure here (e.g. no rpi-eeprom-config
            # on this image, I2C not supported) can't abort the rest of this
            # script via set -e — the venv, systemd services, etc. below are
            # required, the UPS support is optional.
            bash "$PROJECT_DIR/install/rpi_dfrobot_ups_all_in_one.sh" || \
                echo -e "${YELLOW}⚠️ DFRobot UPS setup failed - continuing with remaining setup${NC}"
        else
            echo -e "${YELLOW}⚠️ DFRobot UPS script not found${NC}"
        fi
    else
        echo -e "${YELLOW}⚠️ Not a Raspberry Pi - Skipping DFRobot UPS installation${NC}"
    fi
else
    echo -e "${YELLOW}⚠️ Not ARM architecture - Skipping DFRobot UPS installation${NC}"
fi

# ============================================================================
# Step 8: Create Configuration
# ============================================================================
echo -e "${YELLOW}Step 8: Creating Lab Pi configuration...${NC}"

# Create .env file for Lab Pi
cat > "$PROJECT_DIR/.env" << EOF
# Lab Pi Configuration
VLAB_PI_TYPE=lab
VLAB_PI_ID=$LAB_PI_ID
VLAB_PI_NAME="$LAB_PI_NAME"
VLAB_PI_MAC="$LAB_PI_MAC"
EXPERIMENT_ID=$EXPERIMENT_ID
MASTER_URL=$MASTER_URL
MASTER_API_KEY=$MASTER_API_KEY
LOCATION="$LOCATION"

# Session Poller - Admin Pi URL for session polling
ADMIN_PI_URL=$MASTER_URL

# Server settings
LAB_PORT=5001
LAB_HOST=0.0.0.0
LAB_DEBUG=False
EOF

echo "Configuration saved to $PROJECT_DIR/.env"

# ============================================================================
# Step 6: Setup Python Environment
# ============================================================================
echo -e "${YELLOW}Step 8: Setting up Python environment...${NC}"

cd "$PROJECT_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
# Install core dependencies one by one without av
pip install Flask Flask-SocketIO Flask-SQLAlchemy Flask-Bcrypt Flask-Login Flask-Mail eventlet pyserial lgpio Werkzeug python-dateutil psutil smbus2 gpiozero requests pyaudio esptool aiohttp numpy scipy python-dotenv greenlet "pygdbmi>=0.11" || echo "Warning: Some dependencies failed to install."
# Try to install pre-built wheel first, otherwise build from source
echo "Installing PyAudio for audio capture..."
if ! pip show pyaudio >/dev/null 2>&1; then
    # Install system dependencies for PyAudio
    sudo apt-get update -qq
    sudo apt-get install -y -qq portaudio19-dev python3-pyaudio libasound2-dev 2>/dev/null || true
    pip install pyaudio || echo "Warning: PyAudio installation failed. Audio features will be disabled."
fi

# Install FFmpeg and WebRTC dependencies for audio streaming
echo "Installing FFmpeg and WebRTC dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev libavfilter-dev libswscale-dev libswresample-dev ffmpeg 2>/dev/null || true

# Install aiortc without letting pip auto-resolve its dependencies (keeps
# it from pulling in a newer av than what's actually compatible), then
# install av explicitly, pinned to aiortc's declared supported range.
# av is a HARD requirement for real WebRTC audio (aiortc imports it at
# module load time) -- prebuilt aarch64 wheels exist on PyPI, no need to
# build from source, but the FFmpeg dev headers above are kept as a
# fallback in case a future Python version has no prebuilt wheel yet.
echo "Installing aiortc..."
pip install --no-deps aiortc || echo "Warning: aiortc installation failed."
echo "Installing av (required by aiortc for real audio encoding)..."
pip install "av>=14.0.0,<17.0.0" || echo "Warning: av installation failed. WebRTC audio will not work."

# Install other required packages
pip install aioice cryptography google-crc32c pyee pylibsrtp pyopenssl || echo "Warning: Some aiortc dependencies failed."

# Re-assert the same core dependencies as above (belt-and-suspenders in case
# externally-managed-environment blocked the first pass) -- NOTE: this list
# is hand-maintained, not actually read from requirements.txt, so it must be
# kept in sync with it by hand whenever a new dependency is added there.
echo "Installing other Python dependencies..."
pip install --break-system-packages Flask Flask-SocketIO Flask-SQLAlchemy Flask-Bcrypt Flask-Login Flask-Mail eventlet pyserial lgpio Werkzeug python-dateutil psutil smbus2 gpiozero requests pyaudio esptool aiohttp numpy scipy python-dotenv "pygdbmi>=0.11" 2>/dev/null || pip install Flask Flask-SocketIO Flask-SQLAlchemy Flask-Bcrypt Flask-Login Flask-Mail eventlet pyserial lgpio Werkzeug python-dateutil psutil smbus2 gpiozero requests pyaudio esptool aiohttp numpy scipy python-dotenv "pygdbmi>=0.11" 2>/dev/null || echo "Warning: Some dependencies failed to install."

# ============================================================================
# Step 7: Create Systemd Service
# ============================================================================
echo -e "${YELLOW}Step 9: Creating systemd service...${NC}"

sudo tee /etc/systemd/system/vlab-lab-pi.service > /dev/null << EOF
[Unit]
Description=Virtual Lab - Lab Pi Node
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/app.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/vlab-lab-pi.log
StandardError=append:/var/log/vlab-lab-pi.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable vlab-lab-pi.service

# ============================================================================
# Step 8b: Setup Session Poller Service (for hardware control)
# ============================================================================
echo -e "${YELLOW}Step 9b: Setting up session poller service...${NC}"

# Copy session poller files if they exist
if [ -f "$PROJECT_DIR/lab_pi_session_poller.py" ]; then
    # Create service file with resolved paths
    sudo tee /etc/systemd/system/lab_pi_session_poller.service > /dev/null << EOFSERVICE
[Unit]
Description=Lab Pi Session Poller
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=/usr/bin/python3 $PROJECT_DIR/lab_pi_session_poller.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOFSERVICE
    
    sudo systemctl daemon-reload
    sudo systemctl enable lab_pi_session_poller || true
    echo -e "${GREEN}✅ Session poller service installed (User: $CURRENT_USER)${NC}"
else
    echo -e "${YELLOW}⚠️ Session poller not found - will use main app for session control${NC}"
fi

# ============================================================================
# Step 8: Hardware Setup (GPIO)
# ============================================================================
echo -e "${YELLOW}Step 10: Setting up GPIO permissions...${NC}"
sudo usermod -a -G gpio $USER

# ============================================================================
# Step 11: Install Audio/Video Streaming Services
# ============================================================================
echo -e "${YELLOW}Step 11: Setting up Audio/Video streaming services...${NC}"

# Copy service files
if [ -f "$PROJECT_DIR/systemd/mjpg-streamer.service" ]; then
    sudo cp "$PROJECT_DIR/systemd/mjpg-streamer.service" /etc/systemd/system/
    # Replace %i with actual username
    sudo sed -i "s|%i|$CURRENT_USER|g" /etc/systemd/system/mjpg-streamer.service
    echo "Copied and configured mjpg-streamer.service"
fi

if [ -f "$PROJECT_DIR/systemd/audio_stream.service" ]; then
    sudo cp "$PROJECT_DIR/systemd/audio_stream.service" /etc/systemd/system/
    sudo sed -i "s|__PROJECT_DIR__|$PROJECT_DIR|g" /etc/systemd/system/audio_stream.service
    sudo sed -i "s|%i|$CURRENT_USER|g" /etc/systemd/system/audio_stream.service
    echo "Copied and configured audio_stream.service"
fi

# Reload systemd
sudo systemctl daemon-reload

# Enable and start services
echo "Enabling video streaming service..."
sudo systemctl enable mjpg-streamer.service 2>/dev/null || true
# restart (not start) so re-running this script after a git pull actually
# picks up new code -- start is a no-op on an already-running service, which
# leaves it running deleted files indefinitely after a refactor/upgrade.
sudo systemctl restart mjpg-streamer.service 2>/dev/null || true

echo "Enabling audio streaming service..."
sudo systemctl enable audio_stream.service 2>/dev/null || true
sudo systemctl restart audio_stream.service 2>/dev/null || true

# ============================================================================
# Step 12: Install DFRobot UPS Service
# ============================================================================
echo -e "${YELLOW}Step 12: Setting up UPS monitoring service...${NC}"

# Copy UPS service file
if [ -f "$PROJECT_DIR/systemd/dfrobot-ups.service" ]; then
    sudo cp "$PROJECT_DIR/systemd/dfrobot-ups.service" /etc/systemd/system/
    echo "Copied dfrobot-ups.service"
    
    # Update the service file with correct paths
    sudo sed -i "s|%h|$CURRENT_HOME|g" /etc/systemd/system/dfrobot-ups.service
    sudo sed -i "s|%i|$CURRENT_USER|g" /etc/systemd/system/dfrobot-ups.service
    sudo chmod 644 /etc/systemd/system/dfrobot-ups.service
    
    # Enable and start UPS service
    sudo systemctl daemon-reload
    sudo systemctl enable dfrobot-ups.service 2>/dev/null || true
    sudo systemctl restart dfrobot-ups.service 2>/dev/null || true
    echo "UPS monitoring service enabled"
else
    echo "Warning: dfrobot-ups.service not found"
fi

echo -e "${GREEN}Audio/Video services installed${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Installation Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Lab Pi has been configured with:"
echo "  - ID: $LAB_PI_ID"
echo "  - Name: $LAB_PI_NAME"
echo "  - Experiment: $EXPERIMENT_ID"
echo "  - Master: $MASTER_URL"
echo ""
echo "To start the Lab Pi service:"
echo "  sudo systemctl start vlab-lab-pi.service"
echo ""
echo "To check status:"
echo "  sudo systemctl status vlab-lab-pi.service"
echo ""
echo "To view logs:"
echo "  journalctl -u vlab-lab-pi.service -f"
echo ""
echo -e "${YELLOW}IMPORTANT: After starting, check Master Pi to verify registration!${NC}"
