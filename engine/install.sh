#!/bin/bash
# First-boot installer for the My Dear Academy editing engine (Ubuntu 24.04).
# Run by cloud-init as root. Safe to re-run.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ffmpeg python3 python3-pip python3-venv git unzip fontconfig

# Poppins font for captions
mkdir -p /usr/local/share/fonts/poppins
if [ ! -f /usr/local/share/fonts/poppins/Poppins-Bold.ttf ]; then
  for w in Bold Black SemiBold; do
    curl -fsSL "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-$w.ttf" -o "/usr/local/share/fonts/poppins/Poppins-$w.ttf"
  done
  fc-cache -f
fi

# Code
mkdir -p /opt/mda
if [ -d /opt/mda/repo/.git ]; then
  git -C /opt/mda/repo pull --ff-only
else
  git clone --depth 1 https://github.com/MyDearEditor/mda-upload.git /opt/mda/repo
fi

# Python env
if [ ! -d /opt/mda/venv ]; then python3 -m venv /opt/mda/venv; fi
/opt/mda/venv/bin/pip install --upgrade pip
/opt/mda/venv/bin/pip install faster-whisper numpy scipy anthropic requests

# Env file (secrets are pasted in by Mason with configure.sh)
if [ ! -f /opt/mda/.env ]; then
cat > /opt/mda/.env <<'EOF'
MDA_API_URL=https://script.google.com/macros/s/AKfycbykCMfDGOVnsSxB3T-GQcQKHUbj_4-5hLVSbGFcB3UJVc-Gnd4gnp8Cw2_XN3rCKMrF/exec
MDA_ENGINE_SECRET=PASTE_ME
ANTHROPIC_API_KEY=PASTE_ME
CLAUDE_MODEL=claude-sonnet-4-5
WHISPER_MODEL=small.en
MDA_WORK_DIR=/opt/mda/work
MDA_POLL_SECONDS=120
EOF
chmod 600 /opt/mda/.env
fi

# Helper: paste secrets without them landing in shell history
cat > /opt/mda/configure.sh <<'EOF'
#!/bin/bash
set -e
echo "My Dear Academy engine setup. Paste each value and press Enter (nothing is echoed)."
read -rsp "ENGINE_SECRET (from Apps Script > Project Settings > Script Properties): " ES; echo
read -rsp "ANTHROPIC_API_KEY (from console.anthropic.com): " AK; echo
sed -i "s|^MDA_ENGINE_SECRET=.*|MDA_ENGINE_SECRET=$ES|" /opt/mda/.env
sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$AK|" /opt/mda/.env
systemctl restart mda-engine
sleep 3
systemctl --no-pager status mda-engine | head -5
echo "Done. Watch it with:  journalctl -u mda-engine -f"
EOF
chmod 700 /opt/mda/configure.sh

# Download the Whisper model once so the first call isn't slow
/opt/mda/venv/bin/python3 -c "from faster_whisper import WhisperModel; WhisperModel('small.en', device='cpu', compute_type='int8')" || true

# Service
cat > /etc/systemd/system/mda-engine.service <<'EOF'
[Unit]
Description=My Dear Academy editing engine
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/opt/mda/.env
WorkingDirectory=/opt/mda/repo/engine
ExecStart=/opt/mda/venv/bin/python3 -I /opt/mda/repo/engine/engine.py
Restart=always
RestartSec=30
Nice=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable mda-engine
# Only start if secrets are present (otherwise it would loop on "bad secret")
if ! grep -q PASTE_ME /opt/mda/.env; then systemctl restart mda-engine; fi

# Updater: pull the latest engine code and restart
cat > /opt/mda/update.sh <<'EOF'
#!/bin/bash
set -e
git -C /opt/mda/repo pull --ff-only
systemctl restart mda-engine
echo "updated and restarted"
EOF
chmod 700 /opt/mda/update.sh

echo "MDA engine install complete" > /opt/mda/INSTALLED
