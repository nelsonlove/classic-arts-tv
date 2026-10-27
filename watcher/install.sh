#!/bin/sh
# Install or update the watcher on a Debian or Ubuntu host. Run as root from this directory.
set -e
apt-get -qq update
DEBIAN_FRONTEND=noninteractive apt-get -qq install -y ffmpeg tesseract-ocr python3 python3-pil python3-venv
id cas >/dev/null 2>&1 || useradd --system --home /var/lib/cas-watcher --shell /usr/sbin/nologin cas
install -d -o cas -g cas /var/lib/cas-watcher
install -d /opt/cas-watcher
install -m 0755 cas_watcher.py /opt/cas-watcher/cas_watcher.py
[ -x /opt/cas-watcher/venv/bin/python ] || python3 -m venv --system-site-packages /opt/cas-watcher/venv
/opt/cas-watcher/venv/bin/pip -q install --upgrade anthropic
install -m 0644 cas-watcher.service /etc/systemd/system/cas-watcher.service
[ -f /etc/cas-watcher.env ] || install -m 0644 cas-watcher.env.example /etc/cas-watcher.env
systemctl daemon-reload
systemctl enable --now cas-watcher
systemctl restart cas-watcher
systemctl --no-pager --lines=3 status cas-watcher
