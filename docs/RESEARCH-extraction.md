# Research: what more can we extract from the Drobo 5N?

> **Scope / safety note.** Everything in the "What we do today" and "Read-only
> probes" sections below was verified without authenticating to, writing to, or
> changing anything on the Drobo. The proposals in "Getting real SMART data"
> would each require *installing software and/or a service on the Drobo*. Per
> the homelab denylist and this task's rules, **those steps are for the user to
> approve and perform — the agent must not do them.**

Device under study: **Drobo 5N** at `192.168.1.144`, firmware `4.3.1-8.126.117497`,
`DNASDroboAppsEnabled = 1`, 5 data bays + 1 mSATA cache slot.

---

## 1. What the NASD stream (port 5000) does and does not give us

The dashboard reads the unauthenticated `<ESATMUpdate>` XML the NASD daemon
emits on TCP `5000`. Cross-checked against `tests/sample_5n.xml`, the
`reference/drobo_exporter` struct map, and the droboports
[NASD-XML-format wiki](https://github.com/droboports/droboports.github.io/wiki/NASD-XML-format).

**Exposed (now surfaced in full on `/stats`):** device identity (name, model,
serial, arch, firmware version/date), overall + per-slot status codes, capacity
(total/used/free protected, unprotected, OS-used, PT), yellow/red thresholds,
redundancy/feature state, relayout progress, disk-pack IDs, DNAS status/config/
feature-table, DroboApps-enabled + email-alerts flags, LUN table, and per-drive
make/model, firmware rev, serial, physical/managed capacity.

**Bogus or missing per-drive health — the real gap:**

| Field | Reported | Reality |
|---|---|---|
| `mTemperature` | `0` for every drive | Not populated by this firmware. **No temperature at all.** |
| `SSDLifeRemaining` | `100` for every drive | Placeholder; not real SSD wear. |
| `RotationalSpeed` | `27` / `36` / `1` | A device code, **not RPM**. Useless as a speed. |
| Reallocated / pending / uncorrectable sectors | — | Absent. |
| Power-on hours, start/stop counts | — | Absent. |
| Per-drive read/write error rates | — | Only a coarse `mErrorCount` per slot. |

So the single most valuable thing we cannot get from port 5000 is **genuine
per-disk SMART data** (real temperature, reallocated/pending sectors, power-on
hours, and true SSD wear-out on the mSATA cache). That data lives in each
drive's own SMART firmware and can only be read *on the Drobo itself* via ATA
pass-through to the physical disks.

---

## 2. Read-only probe: other services on the Drobo

A light, connect-only presence check (single TCP connect, **no data sent, no
auth**) of a handful of well-known ports on `192.168.1.144`:

| Port | Service | Result |
|---|---|---|
| **22** | SSH (Dropbear/OpenSSH) | **OPEN** |
| 139 | SMB / NetBIOS | OPEN |
| 445 | SMB / CIFS | OPEN |
| 548 | AFP (Netatalk) | OPEN |
| 5000 | NASD status (what we use) | OPEN |
| 80 | HTTP | closed |
| 8080 | HTTP-alt (Apache DroboApp) | closed |
| 631 | (control) | closed |

**Headline finding: port 22 is already open.** That almost certainly means a
**Dropbear or OpenSSH DroboApp is already installed and running** on this Drobo.
If so, we may not need to install *anything* to get shell access — only valid
credentials (default is `root` / `root`, which should already have been
changed). SMB (139/445) and AFP (548) are the normal file-sharing services.
Apache (8080) is **not** running, so no web service is currently exposed beyond
NASD. *(No authentication was attempted against port 22 or any other port.)*

---

## 3. How DroboApps work on the Drobo 5N

Confirmed from droboports.com, the Ars Technica DroboFS deep-dive, and multiple
Drobo 5N how-tos:

- **Location.** Installed apps live under the `DroboApps` share, on disk at
  `/mnt/DroboFS/Shares/DroboApps/`. Each app is its own subdirectory
  (e.g. `.../DroboApps/dropbear/`, `.../DroboApps/git/bin/`).
- **They run as `root`.** Every DroboApp's `service.sh` is launched as root at
  boot. This is the central security fact for anything we add.
- **Listing installed apps** (once you have shell): `ls
  /mnt/DroboFS/Shares/DroboApps/` — each directory with a `service.sh`
  (`start|stop|restart|status`) is an installed app.
- **Installing an app.** Copy the app's `.tgz` into the `DroboApps` SMB share
  and reboot the Drobo; the firmware unpacks and starts it. (Or via Drobo
  Dashboard's Drobo Apps pane, for apps in the official catalog.)
- **Available relevant apps.** Dropbear SSH (official) and OpenSSH (DroboPorts)
  both provide shell on port 22 — never run both (port conflict). Apache
  (port 8080) exists but, running as root, it bypasses all share/file
  permissions — avoid. Command-line utilities such as `e2fsprogs` are packaged
  and explicitly documented as "enable SSH, then run from a shell."

### smartctl / smartmontools availability

- **There is no ready-made `smartmontools` DroboApp in the DroboPorts
  repository.** It would have to be **cross-compiled** for the 5N's toolchain
  (ARM, GCC 4.4.5 / glibc 2.11.1 per the DroboPorts "Toolchains" wiki) or
  sourced from a community build (Drobo Space forums / jhah's site).
- **Do the physical disks expose SMART?** The BeyondRAID *volume* layer is
  proprietary and cannot be reassembled off-box — but that is a **volume-layer**
  concern. The **physical disks themselves are ordinary SATA drives**; on the
  Drobo's own Linux they appear as `/dev/sda…/dev/sde` (+ the mSATA as another
  `/dev/sd*`). SMART is a per-drive feature read by sending ATA pass-through
  commands straight to the drive firmware, independent of BeyondRAID, so
  `smartctl -A /dev/sdX` should return real attributes **from within the
  Drobo**. Caveat: the Marvell SATA controller may require an explicit device
  type (`smartctl -d sat …` or `-d marvell …`); this needs to be verified on
  the box. SMART reads are read-only and do not disturb the disk pack.

---

## 4. Getting real SMART / temperature — concrete, approval-gated proposal

Goal: feed the dashboard **real per-drive temperature + key SMART attributes**
(IDs 194/190 temperature, 5 reallocated, 197 pending, 198 uncorrectable, 9
power-on hours, and SSD wear-out 177/202/233 for the mSATA cache).

### Step 0 — Verify feasibility first (read-only, no install)

Because port 22 is already open, the user can likely just SSH in and check,
installing nothing:

```sh
ssh root@192.168.1.144                      # SSH DroboApp appears already present
ls /mnt/DroboFS/Shares/DroboApps/           # which apps are installed?
ls -l /dev/sd*                              # are the physical disks exposed?
which smartctl || echo "smartctl not installed"
smartctl --scan
smartctl -A /dev/sda            || smartctl -A -d sat /dev/sda   # try plain, then SAT
```

If `smartctl -A` (with or without `-d sat`) prints a temperature and attribute
table, the whole approach is viable. If `smartctl` is absent, it must be built/
obtained (see §3).

### Step 1 — Get `smartctl` onto the Drobo (requires approval)

Either cross-compile `smartmontools` with the DroboPorts ARM toolchain and drop
the resulting `smartctl` binary under `/mnt/DroboFS/Shares/DroboApps/…/bin/`, or
install a trusted community-built `smartmontools` DroboApp `.tgz` via the
`DroboApps` share + reboot.

### Step 2 — Expose the data to the dashboard (pick ONE)

Ranked best-to-worst on the security/complexity trade-off:

- **(A) SSH-key pull (recommended).** The dashboard host runs, on a timer, a
  key-authenticated `ssh drobo 'for d in /dev/sd?; do smartctl -A -d sat $d; done'`
  and parses the output into the same JSON shape as `/api/raw`. Uses the
  **already-open, authenticated** port 22; **no new listening service** on the
  Drobo. Lock the key down with a forced command (`command="/path/smart-json.sh"`
  in `authorized_keys`) so the key can *only* run the read-only SMART dump.
  *Trade-off:* the dashboard would gain an SSH dependency (subprocess `ssh` or a
  library), which today's zero-dependency app does not have.

- **(B) Cron → JSON file on an existing share (lowest new exposure).** A root
  cron job on the Drobo runs `smartctl` every N minutes and writes
  `smart.json` into a folder already shared over SMB/AFP. The dashboard reads
  that file (mount or SMB client). **No new network port**, no live SSH from the
  dashboard. *Trade-off:* data is only as fresh as the cron interval; needs a
  share the dashboard host can read.

- **(C) Tiny read-only HTTP JSON service (simplest to consume, riskiest).** A
  small BusyBox-httpd / CGI or minimal script DroboApp that, on `GET`, runs
  `smartctl -A` per disk and returns JSON on a LAN port (e.g. `9101`). The
  dashboard fetches it exactly like `/api/raw`. This mirrors `reference/drobomon`
  (which serves `/v1/drobomon/status`). **Security cost:** it is a **new,
  unauthenticated, LAN-exposed service running as root that shells out to
  `smartctl`.** Only acceptable if bound to a trusted VLAN and/or firewalled to
  the dashboard host, with the script hard-coded to a fixed read-only command
  (no request-controlled arguments).

### Security implications (apply to all of the above)

- Anything installed as a DroboApp **runs as root**; a flaw in a script or
  service is a root-level foothold on the box holding all your data.
- Option C adds an **unauthenticated root service on the LAN** — treat it like
  the NASD port: assume anyone on the network can read it, and never let request
  input reach the shell.
- SMART reads are read-only; they do not touch BeyondRAID or the disk pack. The
  risk is in the *access mechanism*, not the SMART reads themselves.
- Default SSH creds (`root`/`root`) must already be changed; confirm before
  relying on key-only auth.

---

## 5. Prioritized recommendation

1. **Worth doing (high value):** get real per-drive **temperature** and the core
   SMART wear/error attributes (reallocated 5, pending 197, uncorrectable 198,
   power-on hours 9, SSD wear-out for the mSATA). These directly replace the
   stream's useless `mTemperature=0` / `SSDLifeRemaining=100`.
2. **Best mechanism:** **Option A (SSH-key pull with a forced read-only
   command)** — it reuses the already-open, authenticated port 22 and adds no
   new attack surface on the Drobo. **Option B (cron → JSON on an existing
   share)** is the close runner-up when you'd rather not give the dashboard an
   SSH dependency. Reserve **Option C** for a firewalled/VLAN-isolated setup
   only.
3. **First action is free:** run the Step-0 read-only checks over the existing
   SSH to confirm `smartctl` availability and that `/dev/sd*` SMART works —
   before committing to any install.
4. **Not worth doing:** standing up Apache (root, bypasses share security);
   attempting to read the physical disks / BeyondRAID from another machine;
   aggressive port scanning. None of these advance the SMART goal safely.

> **Reminder:** Steps 1–2 modify the Drobo (install software / add a
> service or cron). They require the user's explicit approval and must be
> performed by the user, not the agent.

---

## 6. VERIFIED on-device probe results (2026-07-03) — SMART is NOT obtainable

The Step-0 read-only checks above were actually run over the existing SSH
(`admin@192.168.1.144`). **The result is negative: real per-drive SMART /
temperature cannot be read on this Drobo 5N.** The optimistic assumption in §3–5
(that `/dev/sd*` would expose the physical disks) does **not** hold on this
device. Evidence:

- **Login is unprivileged.** `id` → `uid=1000(admin)`, not root. `sudo -n` →
  "a password is required"; `admin` is not a sudoer. DroboApps run as root, but
  interactive SSH does not.
- **The kernel exposes only ONE virtual disk, not the physical drives.**
  `/proc/partitions` and `/sys/block` list only `sda` (a 68 TB *sparse virtual*
  BeyondRAID volume) plus flash `mtdblockN`/`ram0`/`loop0`. There is **no
  `/dev/sdb…sdf`.** One SCSI target (`0:0:0:0`) and one generic node
  (`/dev/sg0`) exist — both the emulated Drobo volume, owned `root:root`
  `brw-rw----` (unreadable to `admin`).
- **The physical HDDs/mSATA are hidden behind the Marvell/BeyondRAID firmware.**
  `dmesg` is restricted (shows nothing but `eth0: link up`) and no `ataN`/SATA
  disk lines are visible. The 5 data drives + cache SSD are never enumerated by
  Linux; only the closed firmware sees them.
- **No smartmontools.** Installed DroboApps: `apache2, bash, openssh, plex,
  python3`. `smartctl` absent; `smartctl --scan` returns nothing.

**Consequence:** even with root and a cross-compiled `smartctl`, the only
targetable device is `/dev/sda` / `/dev/sg0` — the *virtual* Drobo disk, which
does not carry the real per-HDD SMART attributes (temperature, reallocated/
pending/uncorrectable sectors, power-on hours). The firmware that can read the
physical disks deliberately reports `mTemperature=0` / `SSDLifeRemaining=100` in
the NASD stream and offers no other userspace export. Options A/B/C above are all
moot because there is no physical-disk device node to run SMART against.

**Recommendation (updated):** do **not** pursue on-device SMART. Keep the
dashboard's honest "temperature not reported by firmware" presentation. Real
per-drive health on a Drobo 5N is simply not exposed outside the firmware.

*(Kernel: `Linux Drobo5N 3.2.96-3 armv7l`. All commands read-only; no changes
were made to the device.)*
