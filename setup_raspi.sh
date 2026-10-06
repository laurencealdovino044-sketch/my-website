#!/bin/bash
# Raspberry Pi Setup Script for HI-TECH EggPlant
# Run this on your Raspberry Pi to set up the command executor

echo "🚀 HI-TECH EggPlant Raspberry Pi Setup"
echo "======================================"

# Check if running on Raspberry Pi
if ! grep -q "Raspberry Pi" /proc/cpuinfo; then
    echo "⚠️  Warning: This script is designed for Raspberry Pi"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Update system packages
echo ""
echo "📦 Updating system packages..."
sudo apt-get update
sudo apt-get upgrade -y

# Install Python 3 and pip
echo ""
echo "🐍 Installing Python 3 and pip..."
sudo apt-get install -y python3-pip python3-dev

# Install required Python packages
echo ""
echo "📚 Installing Python dependencies..."
pip3 install --upgrade pip
pip3 install firebase-admin
pip3 install gpiozero
pip3 install adafruit-circuitpython-dht

# Configure GPIO permissions
echo ""
echo "🔐 Configuring GPIO permissions..."
sudo usermod -a -G gpio pi
sudo usermod -a -G dialout pi

# Create working directory
echo ""
echo "📁 Creating working directory..."
mkdir -p ~/hitech-executor
cd ~/hitech-executor

# Download command executor script
echo ""
echo "📥 Setting up command executor..."
# Note: You'll need to copy the script manually or have it in the repo
# For now, we'll create a placeholder
touch raspi_command_executor.py

# Create firebase service account placeholder
echo ""
echo "🔑 Firebase Service Account"
echo "   Copy your firebase-service-account.json to: ~/hitech-executor/"
echo "   Example: scp firebase-service-account.json pi@raspberrypi.local:~/hitech-executor/"

# Create systemd service
echo ""
echo "⚙️  Creating systemd service..."
read -p "Enter your user ID (from Django app): " user_id
read -p "Enter raspberrypi hostname (default: raspberrypi): " hostname
hostname=${hostname:-raspberrypi}

sudo tee /etc/systemd/system/hitech-executor.service > /dev/null <<EOF
[Unit]
Description=HI-TECH EggPlant Command Executor
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/hitech-executor
Environment="HITECH_USER_ID=${user_id}"
ExecStart=/usr/bin/python3 /home/pi/hitech-executor/raspi_command_executor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Enable the service
echo ""
echo "✓ Enabling systemd service..."
sudo systemctl daemon-reload
sudo systemctl enable hitech-executor

# Display next steps
echo ""
echo "=============================================="
echo "✅ Setup Complete!"
echo "=============================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Copy your files:"
echo "   scp raspi_command_executor.py pi@${hostname}.local:~/hitech-executor/"
echo "   scp firebase-service-account.json pi@${hostname}.local:~/hitech-executor/"
echo ""
echo "2. Verify GPIO permissions (may need reboot):"
echo "   groups pi"
echo ""
echo "3. Start the service:"
echo "   sudo systemctl start hitech-executor"
echo ""
echo "4. Check status:"
echo "   sudo systemctl status hitech-executor"
echo ""
echo "5. View logs:"
echo "   sudo journalctl -u hitech-executor -f"
echo ""
echo "⚠️  Don't forget to:"
echo "   - Copy the Python script to ~/hitech-executor/"
echo "   - Copy firebase-service-account.json to ~/hitech-executor/"
echo "   - Restart the Raspberry Pi for GPIO group changes to take effect"
echo ""
echo "=============================================="
