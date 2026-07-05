# Drobo 5N — Field Knowledge Base

Everything we've learned about this specific **Drobo 5N** while building the
dashboard: hardware, protocols, the data it exposes (and hides), the BeyondRAID
capacity math, and how it behaves on drive failure. This is the single source of
truth — read it before touching the device or the parsing/capacity code.

> Companion deep-dives: [`../RESEARCH-extraction.md`](../RESEARCH-extraction.md)
> (can we get more data / SMART?) and [`../RESEARCH-settings.md`](../RESEARCH-settings.md)
> (the port-5001 control protocol). This file is the consolidated summary.

---

## 1. TL;DR

- The 5N is **network-only** (no USB/eSATA block device), so `drobo-utils` and
  the physical-disk approach don't apply. All data comes over the network.
- **Primary data source:** an unauthenticated **XML status stream on TCP 5000**
  (the `nasd` daemon). Everything the old Drobo Dashboard showed is in there.
- **Control:** an unauthenticated binary command channel on **TCP 5001**
  (DIRNETTM) — used for identify/restart only.
- **Extra telemetry:** the box runs full Linux; we **SSH in** (`admin` account)
  and read `/proc` for CPU, RAM, load, uptime, and disk I/O.
- **Real per-drive SMART and temperature are NOT obtainable** — the physical
  disks are hidden behind the controller. Verified three ways (see §7).
- The company (Drobo/StorCentric) was **liquidated in 2023** — no firmware, no
  support. But a **drive-swap rebuild is autonomous** and needs no software (§9).

---

## 2. Hardware & firmware

| Property | Value |
|---|---|
| Model | Drobo 5N |
| IP (this unit) | `192.168.1.144` |
| SoC | Marvell Armada XP `MV88F5570`, PJ4Bv7 (ARMv7l) |
| CPU cores | **3** |
| RAM | ~868 MiB (`MemTotal` 889248 kB) |
| Kernel | `Linux 3.2.96-3 armv7l SMP` (built 2021-12-13) |
| Userland | BusyBox v1.25.0 |
| Firmware | `4.3.1-8.126.117497` (Apr 25 2022) — the last release |
| Bays | 5 × 3.5" SATA data bays + **1 mSATA cache slot** (slot 5) |
| DroboApps | enabled (`DNASDroboAppsEnabled = 1`) |

### Installed drives on this unit (from the NASD stream)

| Bay (`mSlotNumber`) | Drive | Capacity | Notes |
|---|---|---|---|
| 0 | WD Red `WD40EFRX` | 4 TB | 5400-class |
| 1 | Seagate IronWolf `ST8000VN004` | 8 TB | 7200 |
| 2 | Seagate IronWolf `ST8000VN004` | 8 TB | **`mErrorCount = 7`** (still "Healthy") |
| 3 | WD Red `WD30EFRX` | 3 TB | smallest data drive |
| 4 | WD Red `WD40EFRX` | 4 TB | 5400-class |
| 5 | Kingston `SKC600` mSATA | 256 GB | **cache accelerator**, not part of the data pack |

Redundancy: **dual-disk** (`mFirmwareFeatureStates = 7`). Raw data-bay capacity
≈ **27.01 TB**; protected/usable ≈ **10.87 TB** (see §8).

---

## 3. Network services (ports)

Confirmed by a connect-only probe and by `netstat` on the box:

| Port | Service | Use |
|---|---|---|
| **5000** | `nasd` status XML | **primary data source** (unauthenticated) |
| **5001** | DIRNETTM control | identify / restart commands (unauthenticated) |
| 22 | SSH (OpenSSH DroboApp) | telemetry (`/proc`) — needs the `admin` login |
| 139 / 445 | SMB / CIFS | file sharing |
| 548 | AFP (Netatalk) | file sharing |
| 4700 | localhost only | internal |

Ports 80/8080/631 are closed (Apache DroboApp not running).

---

## 4. Data source #1 — the NASD XML stream (TCP 5000)

Connect to `5000`, read **one** `<ESATMUpdate>` XML document, close. The device
re-emits the document every ~10–20 s while the socket stays open; we read one
and disconnect. No auth, no request payload needed.

- Framing: some readers see a `DRINASD`/length preamble; the robust approach
  (what we use) is to read to end-of-document and locate the `<?xml …` /
  `</ESATMUpdate>` bounds. See `drobo/client.py`.
- Security hardening in the parser: **DTDs are rejected** (blocks the "billion
  laughs" XML-bomb DoS). See `drobo/parser.py`.

### Device-level fields

| XML tag | Meaning |
|---|---|
| `mName`, `mModel`, `mSerial` | identity |
| `mVersion`, `mReleaseDate` | firmware version / date |
| `mStatus` | overall status code (see §5) |
| `mFirmwareFeatureStates` | redundancy mode: **6 = single, 7 = dual** |
| `mRelayoutCount` | >0 while a data relayout/rebuild is in progress |
| `mTotalCapacityProtected` | usable protected pool (bytes) |
| `mUsedCapacityProtected` / `mFreeCapacityProtected` | used / free of that pool |
| `mYellowThreshold` / `mRedThreshold` | fullness warning thresholds (÷100 = %) |
| `DNASDroboAppsEnabled` | DroboApps on/off |
| `mSlotsExp/nX` | per-bay sub-documents (see below) |

### Per-slot fields (`mSlotsExp/n0 … n5`)

| XML tag | Meaning / gotcha |
|---|---|
| `mSlotNumber` | 0-based bay index (use this for labels everywhere) |
| `mStatus` | per-slot status code (see §5): 3 = OK, 128 = empty, 134 = failed |
| `mErrorCount` | **cumulative soft-error tally, NOT an error code.** Worth watching even at "Healthy". |
| `mDiskState` | **0 = empty, 16 = in-use data pack, 32 = mSATA cache** |
| `mDiskType` | media type code |
| `mTemperature` | **always `0` on this firmware — no temperature at all** |
| `mMake`, `mDiskFwRev`, `mSerial` | drive make / firmware / serial |
| `mPhysicalCapacity`, `mManagedCapacity` | raw / managed bytes |
| `SSDLifeRemaining` | **always `100` — placeholder, not real wear** |
| `RotationalSpeed` | **a device code, NOT RPM** (see §5) |

> The parser also **de-duplicates** slot nodes (the stream can repeat `nX`
> entries).

---

## 5. Code → label mappings (`drobo/codes.py`)

- **Overall status:** `0x18000` / `0x8000` → OK; relayout/degraded codes map to
  warning/critical (see `OVERALL_STATUS`).
- **Slot status:** `3` = OK/Healthy, `128` = empty, `134` = failed (see `SLOT_STATUS`).
- **Redundancy (`mFirmwareFeatureStates`):** `6` = single-disk, `7` = dual-disk.
- **Disk state (`mDiskState`):** `0` = empty, `16` = in-use (data pack),
  `32` = in-use (mSATA cache accelerator).
- **Rotational speed (`RotationalSpeed`):** a proprietary code, **RPM = code ×
  200**. Known values: `1` → SSD (no rotation), `27` → 5400 RPM, `36` → 7200 RPM.
  Decoded by `codes.rpm_from_code()`.

---

## 6. Data source #2 — DIRNETTM control channel (TCP 5001)

An unauthenticated binary protocol the old Dashboard used for control actions.
We reverse-engineered only the **safe, reversible** subset:

- **Identify** (blink the lights) / **stop identify**
- **Restart** the unit

Implemented in `drobo/control.py`; exposed on `/settings` but **OFF by default**
(needs `DROBO_ENABLE_CONTROL=1` and a per-process CSRF token). Full protocol
notes: [`../RESEARCH-settings.md`](../RESEARCH-settings.md). Anything
destructive (reformat, redundancy change) is intentionally **not** implemented.

---

## 7. Data source #3 — SSH telemetry, and the SMART verdict

Port 22 runs an OpenSSH DroboApp. Login is **`admin`** (`uid=1000`,
**not root**; `admin` is not a sudoer). Password lives in `.env` as
`DROBO_PASSWORD` (never printed/committed). We SSH in on a timer and read
`/proc` — this powers the throughput and hardware panels.

Readable as `admin` (all confirmed):

| Source | Gives us |
|---|---|
| `/proc/stat` | CPU % overall + per-core (jiffy deltas), incl. **iowait** |
| `/proc/meminfo` | RAM used / cache / free, swap |
| `/proc/loadavg` | 1/5/15-min load + process counts |
| `/proc/uptime` | uptime |
| `/proc/diskstats` (`sda`) | **real volume I/O** — sectors ×512 = bytes/s |
| `/proc/net/dev` | NIC counters → network throughput |
| busybox `top` | top processes (best-effort) |

### Why SMART & drive temperature are NOT obtainable (verified 2026-07-03)

Confirmed three independent ways over SSH:

1. **Only one block device exists.** `/proc/partitions` + `/sys/block` show a
   single `sda` — the **~64 TiB *virtual* BeyondRAID volume** (`scsi 0:0:0:0:
   Direct-Access Drobo 5N`, `dri_dnas` driver). There is **no `/dev/sdb…sdf`**;
   the five physical HDDs + mSATA are never enumerated by Linux.
2. **No tools, no sensors.** `smartctl`/`smartd`/`hddtemp` are not installed
   (only `hdparm`, which can't even open `/dev/sda` as `admin`). There is **no
   `/sys/class/hwmon` and no `/sys/class/thermal`** — the board exposes zero
   temperature sensors.
3. **The firmware keeps it internal.** The `nasd` binary contains the strings
   `Slot temperature: %d` and `SMART`, so the controller *reads* SMART/temp to
   make its own health calls — but it only *publishes* the distilled per-slot
   status, and reports `mTemperature = 0` on the 5N.

**Consequence:** real per-disk SMART attributes (reallocated/pending/uncorrectable
sectors, power-on hours, true SSD wear) and temperatures are **architecturally
unavailable** — not a limitation of this app. The best health proxy we have is
**`mErrorCount` trending over time** (the `/errors` page logs each increase).
Full evidence + the (rejected) install proposals: [`../RESEARCH-extraction.md`](../RESEARCH-extraction.md).

---

## 8. BeyondRAID capacity model (`drobo/storage.py`)

BeyondRAID mixes drive sizes by partitioning the disks into **horizontal zones**
at each distinct drive size; each zone gets its own redundancy. This is why the
naive "usable = raw − the two largest drives" rule (Drobo's own calculator)
gets *usable* right but wrongly lumps **protection** together with
**reserved-for-expansion**.

### The zone rule (`protection_reserve()`)

Sort drive sizes; walk the height bands between successive sizes. A band with
`k` active disks and height `h`:

- if `k > redundancy_level` → it's protectable: **protection += redundancy_level × h**, usable += `(k − redundancy_level) × h`
- if `k ≤ redundancy_level` → it **cannot** be protected → the whole `k × h` is
  **reserved for expansion** (unallocated).

Then we anchor "usable" to the device's own reported protected total and derive:
`parity_reserve = min(zone protection, raw − protected_total)`; `unallocated =
raw − parity_reserve − protected_total` (rolls in a little management overhead).

### Worked example — this unit (8, 8, 4, 4, 3 TB, dual redundancy)

| Zone (height) | Active disks | Dual-redundancy result |
|---|---|---|
| 0–3 TB | 5 | 6 TB protection + 9 TB usable |
| 3–4 TB | 4 | 2 TB protection + 2 TB usable |
| 4–8 TB | **2** (only the two 8 TB) | **8 TB reserved/unallocated** (2 disks can't survive 2 failures) |

| Figure | Value |
|---|---|
| Raw physical (5 data bays) | **27.01 TB** |
| Usable / protected pool | **10.87 TB** (device-reported) |
| Used for **protection** | **~8 TB** (not 16!) |
| **Unallocated** / reserved for expansion | **~8.13 TB** |

Invariants the code guarantees: no negative slices; `used + free ==
protected_total`; `protection + protected_total + unallocated == raw`. The
mSATA cache (slot 5, `is_accelerator`) and empty bays are excluded from the math.

**Rules of thumb:** single redundancy loses ~1 drive to protection, dual ~2;
mismatched drives create "unallocated" that only unlocks when you replace a
**smaller** drive with a **larger** one.

---

## 9. Drive failure & rebuild — the important part

**A rebuild is done entirely by the Drobo's own firmware/controller — the
Dashboard software is NOT required.** This is why the EOL box is still fine to
run: swap a dead drive and it rebuilds itself, no company/software needed.

### Procedure
1. A drive fails → its bay LED goes **solid/blinking red**; the array keeps
   serving data (degraded but protected).
2. **Pull the dead drive, slot in a new one.** The controller auto-detects it
   and starts the relayout. No cables, no buttons, no app.
3. Lights **alternate red/green** during relayout; back to **solid green** when
   done (can take hours→days).

### LED semantics
| LED | Meaning |
|---|---|
| One bay **blinking red** | that drive failed — replace it |
| Bays **alternating red/green** | rebuild in progress — **do not pull any drive** |
| All bays **solid red** | capacity warning (>95% full), *not* a failure |
| Solid **green** | healthy |

### Do / don't
- ✅ Replace **equal-or-larger** drives; **one at a time**.
- ✅ With **dual** redundancy you survive two failures — but a rebuild is the
  most fragile moment.
- ❌ **Never** pull a second drive mid-rebuild.
- ❌ **Never reorder / reseat drives or move the pack to another Drobo** —
  BeyondRAID stores each disk's *position*; shuffling can trigger a
  re-initialise and wipe the array. Label bays if you ever remove disks.
- ⚠️ A **dead controller/chassis** or all-bays-blinking-red (metadata
  corruption) is **not** a drive swap — it needs offline BeyondRAID recovery.

The dashboard surfaces relayout state: `mRelayoutCount` +
`data_protection_in_progress` drive the "Data protection in progress — do not
remove any drives" banner.

---

## 10. Gotchas & operational notes

- **`mTemperature = 0` and `SSDLifeRemaining = 100` are firmware placeholders** —
  captured but shown as "not reported" in the UI. Don't treat them as real.
- **`mErrorCount` is a cumulative tally**, and the device never timestamps it —
  so we log each increase ourselves (SQLite) to give errors a "when".
- **`RotationalSpeed` is a code, not RPM** (× 200).
- Adding data to the device is a **write to `/mnt/DroboFS`** (ext4 on the virtual
  volume); the reported "63.9 TB" size there is the *thin-provisioned max*, not
  real free space — trust the NASD protected figures instead.
- The box typically runs **high load / high iowait** (Samba + Plex + array
  activity); that's normal for this hardware, not a fault.
- Don't hammer the aging device — the dashboard polls on a timer and caches the
  last-good snapshot.

---

## 11. Credentials & security

- Port **5000 (NASD) and 5001 (DIRNETTM) are unauthenticated** — assume anyone
  on the LAN can read status / send the (limited) control commands.
- SSH uses the **`admin`** account; the password is referenced as
  **`DROBO_PASSWORD`** in `.env` — **never print, echo into logged commands, or
  commit it.**
- Control writes (`/settings`) are gated behind `DROBO_ENABLE_CONTROL=1` + a
  per-process CSRF token, and refuse to restart mid-relayout.

---

## 12. References

- droboports — [NASD XML format](https://github.com/droboports/droboports.github.io/wiki/NASD-XML-format)
- [AndrewMobbs/drobomon](https://github.com/AndrewMobbs/drobomon) — Go REST monitor
- [cosmouser/drobo_exporter](https://github.com/cosmouser/drobo_exporter) — Go Prometheus exporter (status-code maps)
- Drobo BeyondRAID capacity behaviour — Drobo user manual + the
  [drobo-talk mixed-drive thread](https://groups.google.com/g/drobo-talk/c/du9hLFmJifM)
