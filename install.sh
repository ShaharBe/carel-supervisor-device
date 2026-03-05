#!/bin/bash
set -e

APP_DIR=/opt/carel-supervisor

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

echo "Installation complete."
echo "Application directory: $APP_DIR"