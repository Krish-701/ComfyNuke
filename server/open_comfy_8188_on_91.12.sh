#!/usr/bin/env bash
# Run THIS on 192.168.91.12 (the extra ComfyUI box), not on the 91.11 hub.
# Lets ComfyNuke on 192.168.91.11:8600 proxy jobs to ComfyUI :8188.
set -euo pipefail
HUB_NET="${HUB_NET:-192.168.91.0/24}"
PORT="${PORT:-8188}"

echo "Host: $(hostname) $(hostname -I 2>/dev/null || true)"
echo "Allow ${HUB_NET} → TCP ${PORT}"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi 'Status: active'; then
  ufw allow from "$HUB_NET" to any port "$PORT" proto tcp
  ufw status numbered | head -40
elif command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${HUB_NET} port protocol=tcp port=${PORT} accept"
  firewall-cmd --reload
else
  iptables -C INPUT -p tcp -s "$HUB_NET" --dport "$PORT" -j ACCEPT 2>/dev/null \
    || iptables -I INPUT -p tcp -s "$HUB_NET" --dport "$PORT" -j ACCEPT
  echo "iptables rule added (not persisted). Save with: sudo iptables-save"
  iptables -L INPUT -n | head -25
fi

echo
echo "ComfyUI must listen on 0.0.0.0:${PORT} (not 127.0.0.1):"
ss -tlnp | grep ":${PORT}" || echo "NOT LISTENING on ${PORT}"
echo
echo "From hub 192.168.91.11 test:  curl -m 5 http://192.168.91.12:${PORT}/system_stats"
