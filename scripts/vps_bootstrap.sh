#!/usr/bin/env bash
# Hetzner CX22 (Ubuntu 24.04) initial bootstrap. Run as root on a fresh box.
# Idempotent — safe to re-run.

set -euo pipefail

DOMAIN="${DOMAIN:-recruiter-ai.example.com}"
SSH_USER="${SSH_USER:-recruiter}"
SSH_PUBKEY_FILE="${SSH_PUBKEY_FILE:-/root/.ssh/authorized_keys}"

echo "==> apt update + base packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    ca-certificates curl gnupg ufw fail2ban git jq age \
    htop iotop tmux unzip apt-transport-https

echo "==> create non-root user $SSH_USER"
if ! id -u "$SSH_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo "$SSH_USER"
    echo "$SSH_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$SSH_USER"
    chmod 0440 "/etc/sudoers.d/$SSH_USER"
    mkdir -p "/home/$SSH_USER/.ssh"
    if [[ -f "$SSH_PUBKEY_FILE" ]]; then
        cp "$SSH_PUBKEY_FILE" "/home/$SSH_USER/.ssh/authorized_keys"
    fi
    chown -R "$SSH_USER:$SSH_USER" "/home/$SSH_USER/.ssh"
    chmod 700 "/home/$SSH_USER/.ssh"
    chmod 600 "/home/$SSH_USER/.ssh/authorized_keys" || true
fi

echo "==> ssh hardening"
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl reload ssh

echo "==> ufw"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> fail2ban"
systemctl enable --now fail2ban

echo "==> install docker"
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    usermod -aG docker "$SSH_USER"
    systemctl enable --now docker
fi

echo "==> sops"
if ! command -v sops >/dev/null 2>&1; then
    SOPS_VERSION="v3.9.1"
    curl -fsSL -o /usr/local/bin/sops \
        "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.amd64"
    chmod +x /usr/local/bin/sops
fi

echo "==> done. Reboot recommended."
echo "    Next: clone repo as $SSH_USER, run scripts/sops_init.sh, set up Caddy, docker compose up."
