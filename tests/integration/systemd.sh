#!/usr/bin/env bash
# Run only on a disposable Linux CI host with systemd, as root.
set -euo pipefail
python="$1"
cleanup() {
    systemctl stop ahab || true
    rm -f /etc/systemd/system/ahab.service.d/integration.conf
    systemctl daemon-reload
}
trap cleanup EXIT

bash install.sh --dev --systemd --python "$python"
systemd-analyze verify /etc/systemd/system/ahab.service
! systemctl is-active --quiet ahab
! systemctl is-enabled --quiet ahab

# The real CLI must reject incomplete configuration before enabling the service.
if bash install.sh --dev --systemd --start --python "$python"; then
    echo 'Incomplete configuration unexpectedly passed preflight.' >&2
    exit 1
fi
! systemctl is-active --quiet ahab
! systemctl is-enabled --quiet ahab

# Ordinary users cannot read secrets or invoke the privileged launcher.
if runuser -u nobody -- /usr/local/bin/ahab doctor > /tmp/ahab-rejection.log 2>&1; then
    exit 1
fi
grep -q 'sudo ahab' /tmp/ahab-rejection.log
! runuser -u nobody -- test -r /etc/ahab/ahab.toml
! runuser -u nobody -- test -r /etc/ahab/notifications.env

printf 'test credentials\n' > /var/lib/ahab/.oci/config
printf 'test private key\n' > /var/lib/ahab/.oci/key.pem
printf 'test public key\n' > /var/lib/ahab/.ssh/id_ed25519.pub
printf 'TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_CHAT_ID=123\n' > /etc/ahab/notifications.env
chown -R ahab:ahab /var/lib/ahab
chmod 600 /var/lib/ahab/.oci/* /var/lib/ahab/.ssh/*
chmod 640 /etc/ahab/notifications.env

# Exercise the installed launcher and CLI, including env-file loading.
printf 'INVALID_SETTING=secret\n' >> /etc/ahab/notifications.env
if /usr/local/bin/ahab doctor > /tmp/ahab-env-error.log 2>&1; then exit 1; fi
grep -q 'Invalid notification setting' /tmp/ahab-env-error.log
! grep -q secret /tmp/ahab-env-error.log
printf 'TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_CHAT_ID=123\n' > /etc/ahab/notifications.env

install -m 644 tests/integration/service_probe.py /opt/ahab/service_probe.py
runuser -u ahab -- env HOME=/var/lib/ahab /opt/ahab/.venv/bin/python /opt/ahab/service_probe.py
rm /var/lib/ahab/state/integration-ok

# Keep the production unit's identity and hardening; replace only its workload.
mkdir -p /etc/systemd/system/ahab.service.d
cat > /etc/systemd/system/ahab.service.d/integration.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/opt/ahab/.venv/bin/python /opt/ahab/service_probe.py
IPAddressDeny=any
EOF
systemctl daemon-reload
systemctl start ahab
for _ in {1..30}; do
    if [[ -f /var/lib/ahab/state/integration-ok ]] && ! systemctl is-active --quiet ahab; then
        break
    fi
    sleep 1
done
test "$(systemctl show ahab -p ExecMainStatus --value)" = 0
test "$(systemctl show ahab -p Result --value)" = success
test "$(stat -c %U /var/lib/ahab/state/integration-ok)" = ahab
echo 'Systemd integration passed: identity, secrets, state, preflight and sandbox.'
