#!/bin/bash
set -euo pipefail

APP_DIR=/opt/carel-supervisor
SERVICE_NAME=carel-supervisor.service
SERVICE_PATH=/etc/systemd/system/$SERVICE_NAME

echo "Creating install directory..."
sudo mkdir -p $APP_DIR
sudo chown $USER:$USER $APP_DIR

echo "Copying files..."
cp -r app $APP_DIR/
cp requirements.txt $APP_DIR/

echo "Creating Python virtual environment..."
python3 -m venv $APP_DIR/venv

echo "Installing Python dependencies..."
$APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt

echo "Installing systemd service..."
sudo cp $SERVICE_NAME $SERVICE_PATH
sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE_NAME

if id -nG pi | grep -qw dialout; then
	echo "Verified: user 'pi' is in the dialout group."
else
	echo "Warning: user 'pi' is not in the dialout group. Serial access to /dev/ttyACM0 may fail."
fi

echo "Installation complete."
echo "Application directory: $APP_DIR"
echo "Service installed: $SERVICE_PATH"