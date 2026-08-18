#!/usr/bin/env bash
# Run THIS SCRIPT ON 192.168.91.11 (Linux-1), not on the 91.13 hub.
# Lets the ComfyNuke control panel (192.168.91.13:8600) ping and proxy
# ComfyUI on 8166 / 8177.
set -euo pipefail
HUB_NET="${HUB_NET:-192.168.91.0/24}"
PORTS="${PORTS:-8166 8177}"

echo "Opening ComfyUI ports ${PORTS} from ${HUB_NET} on $(hostname) $(hostname -I)"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi 'Status: active'; then
  for p in $PORTS; do
    ufw allow from "$HUB_NET" to any port "$p" proto tcp
  done
  ufw status numbered | head -40
elif command -v firewall-cmd >/dev/null 2>&1; then
  for p in $PORTS; do
    firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${HUB_NET} port protocol=tcp port=${p} accept"
  done
  firewall-cmd --reload
else
  for p in $PORTS; do
    iptables -C INPUT -p tcp -s "$HUB_NET" --dport "$p" -j ACCEPT 2>/dev/null \
      || iptables -I INPUT -p tcp -s "$HUB_NET" --dport "$p" -j ACCEPT
  done
  echo "iptables INPUT rules added (not persisted). Save with: iptables-save"
  iptables -L INPUT -n | head -20
fi

ss -tln | grep -E '8166|8177' || echo "NOTE: ComfyUI must listen on 0.0.0.0 not 127.0.0.1"
echo "From 192.168.91.13 you should now: curl http://$(hostname -I | awk '{print $1}'):8166/system_stats"
