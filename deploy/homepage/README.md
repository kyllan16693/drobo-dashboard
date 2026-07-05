# Homepage customapi widget

[gethomepage](https://gethomepage.dev/) has no native Drobo widget. Use the
**customapi** widget against `GET /api/widget` on the drobo-dashboard container.

## Apply on homelab (CT 402)

1. SSH to homelab1, enter CT 402: `pct exec 402 -- bash`
2. Backup first:
   ```sh
   cp /root/docker_vols/homepage/config/services.yaml \
      /root/docker_vols/homepage/config/services.yaml.bak.$(date +%s)
   ```
3. Edit `config/services.yaml` — add the snippet from
   [`widget.example.yaml`](widget.example.yaml) to the **Storage** group.
   Replace `192.168.1.72` with the drobo-dashboard host IP.
4. Homepage hot-reloads YAML; no container restart needed unless `.env` changed.
5. Verify from CT 402:
   ```sh
   curl -s http://<drobo-dashboard-ip>:8765/api/widget
   ```
6. Open Homepage and confirm the Drobo tile shows capacity %, uptime, load,
   and up/down speeds.

## API contract

`GET /api/widget` returns flat JSON (unauthenticated, LAN-only):

| Field | Type | Source |
|---|---|---|
| `used_pct` | float | NASD capacity |
| `used_human` | string | NASD capacity |
| `free_human` | string | NASD capacity |
| `status_label` | string | NASD overall status |
| `uptime_human` | string | SSH `/proc/uptime` |
| `load1` | float | SSH `/proc/loadavg` |
| `rx_human` | string | SSH network throughput |
| `tx_human` | string | SSH network throughput |
| `reachable` | bool | NASD poller reachable |

If SSH is down, capacity fields still populate from NASD; uptime/load/speeds
may be null.
