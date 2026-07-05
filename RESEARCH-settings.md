# Drobo 5N — Settings/Control Feasibility Research

**Status:** research only. No changes were made to the Drobo (`192.168.1.144`) or
any other device. This document is a feasibility assessment for adding a *third*
page (a `/settings` control page) to the existing read-only dashboard. **Nothing
here is implemented; do not implement without explicit approval.**

---

## 1. Executive summary — the headline verdict

**Yes, a settings page is technically feasible — but only for a small, carefully
chosen subset of settings, and the "big" settings (network, redundancy,
password, reset) should be deliberately excluded as unsafe or unverified.**

The key finding: the old Drobo Dashboard did **not** use SCSI or any USB/iSCSI
path to a network Drobo. It talked to the on-device `nasd` daemon over a
**second TCP channel on port 5001** (the same daemon that streams the read-only
status XML we already parse on port 5000). That channel — framed with the ASCII
tag `DIRNETTM` — carries short, numeric-opcode commands and is **completely
unauthenticated**. It was reverse-engineered publicly by ISE Labs in 2018–2020
(published as `nasty.py`; CVE-2018-14701, CVE-2018-14709, CVE-2019-6801). So the
exact mechanism the Dashboard used to *push changes* is documented and
reproducible in pure Python (no VM, no vendor software).

The catch: only a **handful of opcodes are publicly documented** (blink LEDs,
restart, set-admin, install-app, get-network, get-temp, and a probable
set-time). The opcodes for the settings the user most wants — rename, DHCP/static
IP, drive spin-down, dual-redundancy toggle, NTP servers, email alerts — are
**not** published anywhere I could find. Recovering them would require capturing
traffic from the real Dashboard talking to a Drobo (the Dashboard is defunct, so
this is hard) or fuzzing opcode numbers against the live device (risky, and
outside the read-only mandate).

Therefore:

- **Safe & feasible now:** an "Identify / blink lights" action (opcode `26`) and
  a "Restart" action (opcode `21`), plus a **read-only settings viewer** built
  from the port-5000 stream we already parse (name, model, firmware, redundancy
  mode, capacity thresholds, email-config-enabled, DroboApps-enabled) optionally
  enriched by read-only "get" opcodes (`30` network, `61` temp/uptime).
- **Exclude for safety:** admin password / reset, network (IP/DHCP) changes,
  redundancy toggle, factory reset, firmware. These are either data-destroying,
  can strand the device on the network, kick off a multi-hour relayout, or rely
  on undocumented opcodes.

A second, more powerful route exists — enabling on-device SSH (Dropbear/OpenSSH
DroboApp) and editing config on the embedded Linux — but the device's real
settings are owned by `nasd` (stored in nvram / persistent `/var`), so hand-
editing files is fragile and can be silently overwritten. That route also
requires an on-device install/change, which is out of scope for this research
task and should be a separately approved project.

---

## 2. The control / management protocol

### 2.1 There are (at least) three `nasd` ports, not one

`nasd` (the Drobo NAS daemon) listens on three ports. Our current dashboard only
uses the first:

| Port | Proto | Role |
|------|-------|------|
| 5000 | TCP | **Status stream** (read-only). On connect it emits `DRINASD` + one `<ESATMUpdate>` XML doc, re-sent every 10–20 s. This is what our app reads today. |
| 5001 | TCP | **Command channel** (read *and* write). Framed with `DIRNETTM`; carries numeric-opcode commands. This is how the Dashboard changed settings. |
| 5002 | UDP | **Discovery/broadcast.** How the Dashboard "discovered" Drobos on the LAN. |

Source: droboports wiki, *Port assignments* — <https://github.com/droboports/droboports.github.io/wiki/Port-assignments> (lists `5000 tcp nasd`, `5001 tcp nasd`, `5002 udp nasd`).

> Note: this research did **not** probe port 5001 or 5002 on the live device (the
> task allows only a read-only look at 5000). Presence of 5001 is inferred from
> the wiki + the ISE PoC, which are consistent across DroboFS/5N/5N2. It should
> be verified with a single read-only connect *before* any implementation.

### 2.2 The port-5000 stream is read-only

The status port is a one-way firehose: connect → receive XML → (optionally) keep
receiving refreshed XML. It does not parse anything you send it. Both
independent open-source readers treat it as receive-only, and our own
`drobo/client.py` does too:

- AndrewMobbs/drobomon — <https://github.com/AndrewMobbs/drobomon> (read-all, find `<?xml`).
- cosmouser/drobo_exporter — <https://github.com/cosmouser/drobo_exporter> (scan to `</ESATMUpdate>`).
- droboports *NASD XML format* — <https://github.com/droboports/droboports.github.io/wiki/NASD-XML-format>.

**Conclusion: writing settings via port 5000 is not possible. Changes go through
port 5001.**

### 2.3 The port-5001 command protocol (the important part)

Reverse-engineered by ISE Labs (Rick Ramgattie, Ian Sindermann), published as
`nasty.py` on Exploit-DB (EDB-ID 48214) and written up in "Drobo5N2: An Analysis
of a NAS Device."

- PoC source: <https://www.exploit-db.com/raw/48214>
- Write-up: <https://blog.securityevaluators.com/4f1d885df7fc> ("Drobo5N2: An Analysis of a NAS Device", ISE)
- CVEs: CVE-2018-14701, CVE-2018-14709 (missing authentication / command handling on `nasd`), CVE-2019-6801 (root shell via `popit`).

**Framing.** Every message is a 16-byte preamble + body:

```
44 52 49 4e 45 54 54 4d  |  XX 01 00 00  |  <4-byte big-endian body length>
"D  I  R  N  E  T  T  M"  |  direction/type|  size of the message that follows
```

- Handshake preamble uses `07` in the 9th byte; normal command preamble uses `0a`.
- **Handshake body:** the device serial (16-byte NUL-padded) + 4 NUL bytes + the
  serial again (16-byte NUL-padded) + 184 NUL bytes.
- **Command body:** an ASCII string ` <opcode> <args...> <serial> ` + a NUL
  terminator. The body length is sent in the preamble; large responses are
  chunked and must be reassembled using the size field.

**Flow (exactly what the Dashboard did):**

1. Connect to **5000**, read the XML, extract `mSerial` (e.g. `drb125101a00578`).
   *We already have this value from our parser.*
2. Connect to **5001** (the stat connection must stay open for the cmd port to work).
3. Send the handshake (with the serial). Read + discard the blank reply.
4. Loop: send `preamble + " <opcode> <args> <serial> \0"`, read `preamble + result`.

**The "authentication" is just knowing the serial number — which the status port
hands out for free.** This is the vulnerability (CVE-2018-14709): anyone on the
LAN can already issue these commands unauthenticated. Building a controlled UI
that only surfaces *safe* actions is arguably *safer* than the status quo,
provided the dashboard itself is access-controlled.

**Documented opcodes (from `nasty.py`'s `PAYLOADS`):**

| Opcode | Payload shape | Effect | R/W |
|--------|---------------|--------|-----|
| `21` | ` 21 {serial} ` | **Restart** the device | write |
| `26` | ` 26 <seconds> {serial} ` | **"Party mode"** — blink all LEDs for N seconds (`26 900` = 15 min on; `26 0` = off). This is the **LED identify / dim-adjacent** control. | write |
| `30` | ` 30 {serial} Network ` / ` 30 DRINasAdminConfig DRINasDroboAppsConfig {serial} ` | **Get** network config / admin+apps config | read |
| `31` | ` 31 <user> <pass> <appsEnabled> 0 11 1 {serial} ` | **Set admin** username + password + DroboApps-enabled flag | write |
| `61` | ` 61 {serial} ` | **Get** system info (temperature, uptime) | read |
| `78` | ` 78 <name> Install <url> {serial} ` | **Install a DroboApp** from a URL | write |
| `82` | ` 82 <n1> <n2> {serial} ` | Labelled "test"; args look like a Unix timestamp (`1521161215` ≈ 2018-03-16) → **probably set-time**. **Unverified.** | write? |

**What is NOT documented anywhere I found:** opcodes for rename, DHCP/static IP +
netmask + gateway, drive spin-down delay, dual-redundancy toggle, NTP server
list, and SMTP/email-alert config. The `getnet` opcode (`30 ... Network`) proves
a network *setter* almost certainly exists, but its opcode/argument format is not
public.

### 2.4 `drobo-utils` / `drobom` — the direct-attached cousin (does NOT apply to the 5N)

`drobo-utils` (petersilva/drobo-utils, formerly drobo-utils.sourceforge.net) *can*
change essentially every setting the user listed — name, IP/netmask, static-vs-
DHCP, spin-down, dual redundancy, thresholds, time, shutdown/standby — **but only
over the SCSI command set against a `/dev/sdX` block device.** It works for
USB / FireWire / eSATA / iSCSI Drobos (Gen1/2, S, Pro, Elite). It does **not**
work on a network-only Drobo (FS / 5N), which never presents a block device to a
host and speaks no SCSI to the LAN.

- README compatibility matrix (`README.rst`): the DroboFS row is `TCP/IP: data`
  only — i.e. data I/O works but **"Drobo-utils cannot access it for
  configuration."** <https://github.com/petersilva/drobo-utils/blob/main/README.rst>
- `drobom` CLI verbs: `set name`, `set IPAddress`, `set NetMask`,
  `set UseStaticIPAddress`, `set SpinDownDelayMinutes`, `set DualDiskRedundancy`,
  `set time`, `shutdown` (DRI calls it "standby").
  <https://github.com/petersilva/drobo-utils/blob/main/drobom>
- `Drobo.py` implements these as a **SCSI mode page** write (opcode `0x7a`,
  subpage `0x31`) with a flags bitfield —
  `DualDiskRedundancy = 0x0001`, `SpinDownDelay = 0x0002`,
  `UseStaticIPAddress = 0x0008` — plus `SpinDownDelayMinutes`, `IPAddress`,
  `NetMask`. <https://github.com/petersilva/drobo-utils/blob/main/Drobo.py>

**What transfers to the 5N:** only the *conceptual model* — the set of settings
Drobo exposes, their names, and their semantics (e.g. spin-down is a minutes
value; redundancy/spin-down/static-IP are flag bits). **What does not transfer:**
the transport. You cannot reuse a single line of `drobo-utils`' SCSI code against
`192.168.1.144`. The 5N equivalent of those SCSI mode-page writes is *some*
`nasd` opcode on port 5001 — and those opcodes are undocumented (see 2.3).

### 2.5 5N-specific network work in the community

- droboports wiki is the primary 5N/FS reverse-engineering hub; it documents the
  read stream fully but **not** the write opcodes. NASD XML format:
  <https://github.com/droboports/droboports.github.io/wiki/NASD-XML-format>
- ISE's `nasty.py` is the only public 5N *write* client I found.
- Forum/Reddit threads focus on the on-device Linux (DroboApps, SSH), not the
  network command opcodes.

---

## 3. The DroboApps / on-device Linux vector

This unit reports `DNASDroboAppsEnabled = 1`, so the DroboApps subsystem is on.
DroboApps run on the device's embedded ARM Linux (`ArmMarvell`).

### 3.1 What root on the device gives you

Installing an SSH DroboApp (Dropbear or OpenSSH) yields a **root shell** on the
Drobo. Root's default password is famously `root` (should be changed
immediately). Refs:

- droboports/dropbear — <https://github.com/droboports/dropbear> (`master` = 5N build).
- droboports/openssh (issue #1 shows `service.sh` internals) —
  <https://github.com/droboports/openssh/issues/1>
- Annvix "Setting up a Drobo 5N" (root pw `root`, admin user is `Admin`, sshd
  config under `/mnt/DroboFS/Shares/DroboApps/openssh/etc/sshd_config`) —
  <https://annvix.com/blog/setting-up-a-drobo-5n>

DroboApps live under the writable share
`/mnt/DroboFS/Shares/DroboApps/<app>/` and are controlled by a `service.sh`
following the DroboApp framework (`. /etc/service.subr`):
<https://github.com/droboports/droboports.github.io/wiki/Service.sh-template>

### 3.2 Where the *device* settings actually live (and why editing them is fragile)

A drobospace forum thread (archived) captured the 5N's real `/etc/init.d/rcS`,
which is the Rosetta stone for where settings are applied:
<https://web.archive.org/web/20140701075701/http://www.drobospace.com/forums/showthread.php?tid=141627&page=3>

```
/etc/init.d/net_config          # DHCP / static IP setup
/etc/init.d/ntp_config          # NTP / time setup
/bin/set_droboshare_name.sh     # device/share name  -> /var/samba/netbios.conf
/usr/bin/nasd &> /var/log/nasd.log &   # THE daemon that owns real config + serves 5000/5001
```

Critical nuance: **`nasd` is the source of truth.** It reads the device's
persisted configuration (nvram / persistent `/var`, which `enable_var` mounts)
and *applies* it via those init scripts at boot; it also re-applies/serves config
at runtime. So:

- Hand-editing `net_config`, `ntp_config`, `netbios.conf`, etc. can work for a
  boot, but `nasd` may **overwrite** it, or the UI/`DNASConfigVersion` state will
  drift from what's actually running.
- The *correct* way to change these — even from on-device — is to drive `nasd`
  (i.e. the same opcodes as §2.3), not to poke files behind its back.
- Things that genuinely live as editable Linux state (SSH port, users in
  `/etc/passwd` + `/etc/.passwd`, root home) *can* be edited safely, but those
  are DroboApp/OS concerns, not the Drobo "settings" the user asked about.

**Bottom line on this vector:** SSH gives you root, but it does **not** give you a
clean, supported way to flip the Drobo's own settings. It's best reserved for
things the network protocol can't do (real SMART temps, CPU, uptime, custom
services) — which is already noted as a future step in the project README. It is
**not** the right foundation for a settings page, and enabling it is an on-device
change that must be a separately approved project.

---

## 4. Per-setting feasibility matrix

Feasibility legend:
**(a)** documented/reverse-engineered network command (port 5001);
**(b)** only via on-device DroboApps/SSH config edits;
**(c)** risky / likely-to-brick / unsupported;
**(d)** unknown — needs experimentation (out of scope for read-only research).

| Setting (old Dashboard) | Network opcode? | Feasibility | Risk | Notes & sources |
|---|---|---|---|---|
| **Blink/identify LEDs ("party mode")** | `26 <sec>` / `26 0` | **(a)** | **Low** — cosmetic, auto-reverts | The one clearly-safe write. `nasty.py`. Not the same as the old "dim lights" (see below). |
| **Dim lights / LED schedule** | none known | **(d)** | Low–med | Dashboard had a persistent "dim" + schedule; only the transient blink (`26`) is public. Persistent-dim opcode unknown. |
| **Restart device** | `21` | **(a)** | **Med** — downtime; interrupts SMB/Plex/*arr | `nasty.py`. Confirm no relayout is running first. |
| **Standby / shutdown** | none confirmed | **(d)** | Med–high | `drobo-utils` "shutdown"/"standby" is **SCSI-only**. No confirmed 5N *power-off* opcode (only restart `21`). Recovering from a network shutdown needs physical access. |
| **Time / NTP sync** | `82` (probable set-time) | **(d)** | Low | `82`'s args resemble a Unix timestamp but are unverified. NTP-server list opcode unknown; on device it's `/etc/init.d/ntp_config`. |
| **Device / share name** | none known | **(d)** | Low–med | On device: `set_droboshare_name.sh` → `/var/samba/netbios.conf`, but `nasd`-owned. Renaming can disrupt SMB discovery / saved mounts. |
| **Network config (DHCP↔static, IP, netmask, gateway)** | `getnet`=`30 … Network` exists; **setter opcode unknown** | **(d)** → **(c)** | **High** — can strand the device off-LAN | Setter almost certainly exists (getter does) but format unpublished. On device: `/etc/init.d/net_config`. **Denylist: network change.** |
| **Drive spin-down / disk standby** | none known | **(d)** | Med | `drobo-utils` sets it via SCSI mode-page `0x31` (`SpinDownDelay=0x0002`, `SpinDownDelayMinutes`) — **SCSI, not network.** 5N opcode unknown. |
| **Dual-disk redundancy toggle** | none known | **(d)** → **(c)** | **High** — triggers hours-to-days relayout; reduces usable capacity | SCSI flag `DualDiskRedundancy=0x0001` in `drobo-utils`; no known 5N opcode. Do not expose. |
| **Email alert (SMTP) config** | none known | **(d)** | Med (holds SMTP creds) | Read stream exposes only `DNASEmailConfigEnabled` (0/1). Setter opcode unknown; involves credentials. |
| **DroboApps enable/disable** | bundled in `31` | **(a)** but coupled | Med | Opcode `31` sets apps-enabled **together with admin user+password** — can't cleanly toggle apps without also rewriting creds. Risky to expose. |
| **Admin password / credentials** | `31` (setadmin) | **(a)** | **Critical** — lockout / security | `nasty.py` `setadmin` resets user+pass. **Denylist: credentials.** Never in the UI. |
| **Install DroboApp** | `78 <name> Install <url>` | **(a)** | **High** — arbitrary code on device; historic RCE (CVE-2019-6801) | Powerful but dangerous; also an on-device change. Exclude from a settings page. |
| **Factory reset / "reset"** | none confirmed | **(c)** | **Critical — data loss** | No safe network reset opcode found; **denylist: destroying data.** Never automate. |
| **Firmware update** | (Dashboard/`drobom fwupgrade`) | **(c)** | **High — brick risk** | Out of scope; leave to any surviving official path. |
| **Capacity thresholds (yellow/red)** | none known | **(d)** | Low | Read stream exposes `mYellowThreshold`/`mRedThreshold`; setter opcode unknown. Low value. |
| **Read current settings (name, model, fw, redundancy, thresholds, email/apps flags)** | port 5000 stream (already parsed) + `30`/`61` getters | **(a)** | **None** (read-only) | Foundation of a *viewer*; we already parse most of this. |

---

## 5. Risk notes

- **Unauthenticated by design.** Port 5001 needs only the serial (free from
  5000). Our UI must not become the *only* guard — put auth in front of the
  dashboard, and keep the write path opt-in (feature flag, default off).
- **Network changes can strand the device.** A wrong static IP / netmask on
  `192.168.1.144` means no more LAN access and a physical recovery. This alone is
  reason to exclude network config, and it is on the homelab **always-pause
  denylist** (network/firewall/router changes).
- **Redundancy toggle = long relayout.** Switching single↔dual rewrites the whole
  BeyondRAID layout (hours to days) and changes usable capacity; during relayout
  drives must not be removed. High blast radius.
- **Password/reset = lockout or data loss.** `setadmin` (opcode `31`) and any
  "reset" are credential/destructive actions → denylist. Exclude entirely.
- **Restart/standby = downtime** for everything mounting the Drobo (Plex, *arr,
  qBittorrent, SMB). A network *shutdown* with no confirmed wake opcode could
  require walking to the device.
- **Undocumented opcodes must not be fuzzed** against the live unit to "discover"
  them — that violates the read-only mandate and risks unknown side effects.
  Recovering the rename/network/spin-down opcodes should come from a *packet
  capture of the real Dashboard* (if one can still be run), not trial-and-error
  on the production NAS.
- **Firmware compatibility unverified.** ISE confirmed ≤ 4.1.1; this unit is
  4.3.1. The protocol is very likely unchanged, but the write path should be
  proven with the two safe opcodes before trusting it broadly.

---

## 6. Recommendation & proposed `/settings` design (spec only — do not build yet)

### 6.1 Verdict

Build a **read-first settings page** with a **very small, safe write surface**,
using a **native port-5001 `DIRNETTM` client** (not SSH, not `drobo-utils`).
Reserve the on-device SSH route for a later, separately-approved project if we
ever need SMART temps/CPU or a setting the protocol can't reach. Do **not**
attempt network/redundancy/password/reset via the web page.

### 6.2 New code layout (nothing here overwrites existing files)

> Two other agents are editing `index.html`, `app.js`, `style.css`, `app.py`,
> `stats.*`, `drobo/rawdump.py`. The design below adds **new** files only and
> should be re-checked against those files at implementation time.

- `drobo/control.py` *(new)* — the write/command client. Implements
  `handshake(serial)`, `send_command(sock, serial, opcode, *args)`, and typed
  helpers `identify(seconds)`, `stop_identify()`, `restart()`, plus read helpers
  `get_network()` (opcode 30) and `get_sysinfo()` (opcode 61). Pure stdlib
  sockets + `struct`, mirroring `client.py`'s framing style. Guarded by an env
  flag so it's inert unless explicitly enabled.
- `templates/settings.html` *(new)* — the page.
- Routes added to `app.py` *(coordinate with the other agent editing it)*:
  `GET /settings`, `POST /settings/identify`, `POST /settings/restart`. All
  writes POST-only + CSRF token.

### 6.3 Page contents

**Section A — Current settings (read-only, always shown).** Rendered from the
data we already parse on port 5000, plus optional live getters:
name, model, serial, firmware + release date, redundancy mode, used/free vs
yellow/red thresholds, `DroboApps enabled`, `Email alerts configured`. Optional
"refresh network info" button calling `get_network()` (opcode 30, read-only) and
"system info" via `get_sysinfo()` (opcode 61) for uptime.

**Section B — Safe actions.**
- **Identify (blink lights):** duration select (30 s / 5 min / 15 min) → POST
  `/settings/identify` → backend sends ` 26 <seconds> {serial} `. A **Stop
  blinking** button sends ` 26 0 {serial} `. Low risk, auto-reverts, great first
  feature and a good end-to-end test of the write path.

**Section C — Danger zone (explicit guardrails).**
- **Restart device:** hidden behind a disclosure; requires the user to **type
  `RESTART`** into a confirm box; shows a warning listing dependent services
  (Plex/*arr/SMB) and a check that no data-protection/relayout is in progress
  (from `mRelayoutCount`/status). POST `/settings/restart` → ` 21 {serial} `.

### 6.4 Guardrails (all required)

1. **Feature flag** `DROBO_ENABLE_CONTROL` (default `0`). With it off, the page is
   a pure viewer and no command socket is ever opened.
2. **POST-only writes + CSRF token**; no state-changing GETs.
3. **Typed confirmation** for anything with downtime (Restart). No confirmation
   needed for Identify (harmless).
4. **No fields for excluded settings** (see 6.5) — they aren't in the template at
   all, so they can't be sent.
5. **Pre-flight safety check** before Restart: refuse if `mRelayoutCount > 0` or
   status indicates data protection in progress.
6. **Audit log**: every command (opcode, args-with-serial-redacted, timestamp,
   result) written to a log.
7. **Access control + rate limiting** on the dashboard itself; bind to LAN only.
8. **Timeouts / single-shot sockets** mirroring `client.py`; never leave the cmd
   socket open.
9. **Verify-after**: re-read port 5000 after an action and show the resulting
   state.

### 6.5 Deliberately excluded from the UI (and why)

- **Admin password / credentials, factory reset** — destructive / lockout;
  homelab denylist.
- **Network config (DHCP/static/IP/netmask/gateway)** — can strand the device;
  network denylist; setter opcode unknown anyway.
- **Redundancy toggle** — triggers long relayout, capacity change; opcode unknown.
- **Firmware update** — brick risk.
- **Install DroboApp (opcode 78)** — arbitrary on-device code; historic RCE.
- **DroboApps enable/disable via opcode 31** — coupled to credential rewrite;
  unsafe to expose.
- **Rename, spin-down, NTP servers, email/SMTP, thresholds, persistent LED dim/
  schedule, standby/power-off** — **excluded until their opcodes are recovered
  from a real Dashboard capture** (not by fuzzing the live unit). They can be
  promoted from "(d) unknown" to "(a)" later, one at a time, each behind its own
  confirmation.

### 6.6 Pre-implementation checklist (when approved)

1. One read-only connect to port 5000 to confirm serial (we already do this).
2. One read-only connect to port 5001 to confirm the command channel exists and
   the handshake succeeds on firmware 4.3.1 (this is the only new probe needed;
   it sends no state-changing opcode).
3. Prove the path end-to-end with **Identify** (safest, self-reverting) before
   wiring Restart.

---

## 7. Sources

- droboports — *Port assignments*: <https://github.com/droboports/droboports.github.io/wiki/Port-assignments>
- droboports — *NASD XML format*: <https://github.com/droboports/droboports.github.io/wiki/NASD-XML-format>
- droboports — *Service.sh template*: <https://github.com/droboports/droboports.github.io/wiki/Service.sh-template>
- ISE Labs — `nasty.py` PoC (EDB-ID 48214): <https://www.exploit-db.com/raw/48214>
- ISE Labs — *Drobo5N2: An Analysis of a NAS Device*: <https://blog.securityevaluators.com/4f1d885df7fc>
- CVE-2018-14701, CVE-2018-14709 (nasd missing auth / command handling), CVE-2019-6801 (root shell)
- petersilva/drobo-utils — README (compat matrix): <https://github.com/petersilva/drobo-utils/blob/main/README.rst>
- petersilva/drobo-utils — `drobom` CLI: <https://github.com/petersilva/drobo-utils/blob/main/drobom>
- petersilva/drobo-utils — `Drobo.py` (SCSI mode-page `0x31` flags): <https://github.com/petersilva/drobo-utils/blob/main/Drobo.py>
- `drobom` man page: <https://drobo-utils.sourceforge.net/drobom.html>
- AndrewMobbs/drobomon: <https://github.com/AndrewMobbs/drobomon>
- cosmouser/drobo_exporter: <https://github.com/cosmouser/drobo_exporter>
- droboports/dropbear: <https://github.com/droboports/dropbear> · droboports/openssh #1: <https://github.com/droboports/openssh/issues/1>
- Annvix — *Setting up a Drobo 5N*: <https://annvix.com/blog/setting-up-a-drobo-5n>
- drobospace forum (archived) — 5N `/etc/init.d/rcS` + dropbear autostart: <https://web.archive.org/web/20140701075701/http://www.drobospace.com/forums/showthread.php?tid=141627&page=3>
