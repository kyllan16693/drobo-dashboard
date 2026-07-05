"use strict";

// Details page: fetches the full NASD document from /api/raw and renders every
// field the Drobo sends, decoding numeric codes (via maps injected from
// drobo.codes) while always showing the raw value too. This endpoint opens a
// live socket to the device per request, so we refresh no faster than every
// 15s and also offer a manual "Refresh" button.

const CONFIG = window.DROBO_CONFIG || { host: "", pollInterval: 15 };
const CODE_MAPS = window.DROBO_CODE_MAPS || {};
const REFRESH_MS = Math.max(15, Number(CONFIG.pollInterval) || 15) * 1000;

const SEVERITIES = ["ok", "warning", "critical", "info", "empty", "unknown"];
const $ = (id) => document.getElementById(id);

// --- escaping / formatting helpers -----------------------------------------

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function sevClass(sev) {
  return SEVERITIES.includes(sev) ? sev : "unknown";
}

// Decode the RotationalSpeed device code: RPM = code * 200 (code 1 = SSD).
// Undocumented field; derived + cross-checked against the installed drives.
function rpmLabel(code) {
  const n = Number(code);
  if (!Number.isFinite(n) || n <= 0) return "Unknown";
  if (n === 1) return "SSD (no rotation)";
  return (n * 200) + " RPM";
}

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// Decimal units (base 1000) to match how the Drobo Dashboard reports capacity.
function humanBytes(v) {
  const num = toNum(v);
  if (num == null) return "-";
  let value = num;
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  for (let i = 0; i < units.length; i++) {
    if (Math.abs(value) < 1000 || i === units.length - 1) {
      return units[i] === "B"
        ? Math.round(value) + " B"
        : value.toFixed(2) + " " + units[i];
    }
    value /= 1000;
  }
  return "-";
}

// Threshold format is XXYY -> XX.YY%.
function pctThreshold(v) {
  const n = toNum(v);
  return n == null ? "-" : (n / 100).toFixed(2) + "%";
}

function relTime(epochSeconds) {
  if (!epochSeconds) return "";
  const secs = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  if (secs < 5) return "just now";
  if (secs < 60) return secs + "s ago";
  const mins = Math.round(secs / 60);
  if (mins < 60) return mins + "m ago";
  return Math.round(mins / 60) + "h ago";
}

// --- code decoding (maps injected from drobo.codes) -------------------------

function decodeOverall(code) {
  const hit = (CODE_MAPS.overall_status || {})[String(code)];
  return hit ? { label: hit[0], sev: hit[1] } : { label: "Unknown status (code " + code + ")", sev: "unknown" };
}
function decodeSlot(code) {
  const hit = (CODE_MAPS.slot_status || {})[String(code)];
  return hit ? { label: hit[0], sev: hit[1] } : { label: "Unknown (code " + code + ")", sev: "unknown" };
}
function decodeRedundancy(code) {
  return (CODE_MAPS.redundancy || {})[String(code)] || "Unknown (code " + code + ")";
}
function decodeDiskType(code) {
  return (CODE_MAPS.disk_type || {})[String(code)] || "Type " + code;
}

// Local decodes for coded fields not in drobo.codes. Always shown alongside the
// raw value. Disk-state meanings come from drobo/parser.py's comments.
const DISK_STATE = { "16": "Data disk", "32": "Accelerator (mSATA cache)" };

// --- value cell builders (all return escaped, safe HTML) --------------------

function txt(v) {
  const empty = v == null || v === "";
  return `<span${empty ? ' class="muted"' : ""}>${escapeHtml(empty ? "—" : v)}</span>`;
}
function mono(v) {
  const empty = v == null || v === "";
  return `<span class="mono${empty ? " muted" : ""}">${escapeHtml(empty ? "—" : v)}</span>`;
}
function bytesCell(v) {
  return `${escapeHtml(humanBytes(v))} <span class="muted mono">${escapeHtml(v)} B</span>`;
}
function flagCell(v) {
  const s = String(v == null ? "" : v);
  return `${escapeHtml(s || "—")} <span class="muted">(${s === "1" ? "enabled" : "disabled"})</span>`;
}
function errCell(v) {
  const n = toNum(v);
  return `<span class="${n && n > 0 ? "err-warn" : "err-zero"}">${escapeHtml(v)}</span>`;
}
function rawTag(v) {
  return `<span class="muted mono">(raw ${escapeHtml(v)})</span>`;
}

// --- generic table builders -------------------------------------------------

function kvTable(rows) {
  const body = rows
    .map((r) => `<tr><th>${escapeHtml(r.label)}</th><td>${r.html}</td></tr>`)
    .join("");
  return `<table class="kv"><tbody>${body}</tbody></table>`;
}

function fieldTable(rows) {
  const body = rows
    .map(
      (r) =>
        `<tr><th>${escapeHtml(r.label)}<span class="tag">${escapeHtml(r.key)}</span></th><td>${r.html}</td></tr>`
    )
    .join("");
  return `<table class="kv fields"><tbody>${body}</tbody></table>`;
}

function panel(title, sub, bodyHtml) {
  const subHtml = sub ? ` <span class="muted">${escapeHtml(sub)}</span>` : "";
  return `<section class="panel"><h2>${escapeHtml(title)}${subHtml}</h2>${bodyHtml}</section>`;
}

// --- field metadata for the "everything" table ------------------------------

const FIELD_LABELS = {
  mESAUpdateSignature: "ESA update signature",
  mESAUpdateVersion: "ESA update version",
  mESAUpdateSize: "ESA update size (bytes)",
  mESAID: "ESA ID (serial)",
  mSerial: "Serial number",
  mName: "Name",
  mVersion: "Firmware version",
  mReleaseDate: "Firmware release date",
  mArch: "Architecture",
  mFirmwareFeatures: "Firmware features (bitfield)",
  extFtr: "Extended features",
  mFirmwareTestFeatures: "Firmware test features",
  mFirmwareTestState: "Firmware test state",
  mFirmwareTestValue: "Firmware test value",
  mStatus: "Overall status",
  mRelayoutCount: "Relayout blocks remaining",
  mDoubleDegradedCnt: "Double-degraded count",
  mLatestUELGenNumber: "Latest UEL generation number",
  mUseUnprotectedCapacity: "Use unprotected capacity",
  mRealTimeIntegrityChecking: "Real-time integrity checking",
  mStoredFirmwareTestState: "Stored firmware test state",
  mStoredFirmwareTestValue: "Stored firmware test value",
  mDiskPackID: "Disk pack ID",
  mDroboName: "Drobo name",
  mConnectionType: "Connection type",
  mSlotCountExp: "Slot count",
  mFirmwareFeatureStates: "Redundancy / feature state",
  mLUNCount: "LUN count",
  mMaxLUNs: "Max LUNs",
  mSledName: "Sled name",
  mSledVersion: "Sled version",
  mSledStatus: "Sled status",
  mSledSerial: "Sled serial",
  mDiskPackStatus: "Disk pack status",
  LoggedinUsername: "Logged-in username",
  mStatusEx: "Status (extended)",
  mDeviceType: "Device type",
  mModel: "Model",
  DNASStatus: "DNAS status",
  DNASConfigVersion: "DNAS config version",
  DNASDroboAppsShared: "DroboApps shared",
  DNASDiskPackId: "DNAS disk pack ID",
  DNASFeatureTable: "DNAS feature table",
  DNASEmailConfigEnabled: "Email alerts enabled",
};

// Capacity + threshold fields are highlighted in the Capacity panel, so we skip
// them in the generic device table to avoid duplication.
const BYTE_FIELDS = new Set([
  "mTotalCapacityProtected", "mUsedCapacityProtected", "mFreeCapacityProtected",
  "mTotalCapacityUnprotected", "mUsedCapacityOS", "mTotalCapacityPT", "mUsedCapacityPT",
]);
const THRESHOLD_FIELDS = new Set(["mYellowThreshold", "mRedThreshold"]);
const FLAG_FIELDS = new Set([
  "mUseUnprotectedCapacity", "mRealTimeIntegrityChecking",
  "DNASDroboAppsShared", "DNASEmailConfigEnabled",
]);
const CONTAINER_FIELDS = new Set(["mSlotsExp", "mLUNUpdates", "DroboApps"]);

function deviceValueHtml(key, val) {
  if (key === "mStatus") {
    const d = decodeOverall(val);
    return `<span class="led sev-${sevClass(d.sev)}"><span class="dot"></span>${escapeHtml(d.label)}</span> ${rawTag(val)}`;
  }
  if (key === "mFirmwareFeatureStates") {
    return `${escapeHtml(decodeRedundancy(val))} ${rawTag(val)}`;
  }
  if (FLAG_FIELDS.has(key)) return flagCell(val);
  return txt(val);
}

// --- section renderers ------------------------------------------------------

function capacityPanel(raw) {
  const total = toNum(raw.mTotalCapacityProtected) || 0;
  const used = toNum(raw.mUsedCapacityProtected) || 0;
  const usedPct = total > 0 ? (used / total) * 100 : 0;
  const yellow = (toNum(raw.mYellowThreshold) || 8500) / 100;
  const red = (toNum(raw.mRedThreshold) || 9500) / 100;

  let capColor = "var(--ok)";
  if (usedPct >= red) capColor = "var(--critical)";
  else if (usedPct >= yellow) capColor = "var(--warning)";

  const bar =
    `<div class="capacity">` +
    `<div class="cap-bar">` +
    `<div class="cap-fill" style="width:${Math.min(100, usedPct)}%;background:${capColor}"></div>` +
    `<div class="cap-marker yellow" style="left:${yellow}%"></div>` +
    `<div class="cap-marker red" style="left:${red}%"></div>` +
    `</div>` +
    `<div class="cap-pct muted">${usedPct.toFixed(2)}% used · yellow at ${pctThreshold(raw.mYellowThreshold)} · red at ${pctThreshold(raw.mRedThreshold)}</div>` +
    `</div>`;

  const rows = [
    { label: "Total protected", html: bytesCell(raw.mTotalCapacityProtected) },
    { label: "Used protected", html: bytesCell(raw.mUsedCapacityProtected) },
    { label: "Free protected", html: bytesCell(raw.mFreeCapacityProtected) },
    { label: "Used %", html: escapeHtml(usedPct.toFixed(2)) + "%" },
    { label: "Total unprotected", html: bytesCell(raw.mTotalCapacityUnprotected) },
    { label: "Used (OS view)", html: bytesCell(raw.mUsedCapacityOS) },
    { label: "Total (PT)", html: bytesCell(raw.mTotalCapacityPT) },
    { label: "Used (PT)", html: bytesCell(raw.mUsedCapacityPT) },
    { label: "Yellow threshold", html: `${pctThreshold(raw.mYellowThreshold)} ${rawTag(raw.mYellowThreshold)}` },
    { label: "Red threshold", html: `${pctThreshold(raw.mRedThreshold)} ${rawTag(raw.mRedThreshold)}` },
  ];
  return panel("Capacity", "", bar + kvTable(rows));
}

function driveCard(slot) {
  if (!slot || typeof slot !== "object") return "";
  const st = decodeSlot(slot.mStatus);
  const diskType = decodeDiskType(slot.mDiskType);
  const stateLabel = DISK_STATE[String(slot.mDiskState)] || "State " + slot.mDiskState;
  const isAccel = String(slot.mDiskState) === "32";

  const temp =
    String(slot.mTemperature) === "0"
      ? `<span class="muted">not reported</span> ${rawTag(0)}`
      : escapeHtml(slot.mTemperature) + " °C";

  const rows = [
    { label: "Slot number", html: txt(slot.mSlotNumber) },
    { label: "Status", html: `<span class="led sev-${sevClass(st.sev)}"><span class="dot"></span>${escapeHtml(st.label)}</span> ${rawTag(slot.mStatus)}` },
    { label: "Error count", html: errCell(slot.mErrorCount) },
    { label: "Disk state", html: `${escapeHtml(stateLabel)} ${rawTag(slot.mDiskState)}` },
    { label: "Disk type", html: `${escapeHtml(diskType)} ${rawTag(slot.mDiskType)}` },
    { label: "Temperature", html: temp },
    { label: "Make", html: mono(slot.mMake) },
    { label: "Drive firmware", html: mono(slot.mDiskFwRev) },
    { label: "Serial", html: mono(slot.mSerial) },
    { label: "Physical capacity", html: bytesCell(slot.mPhysicalCapacity) },
    { label: "Managed capacity", html: bytesCell(slot.mManagedCapacity) },
    { label: "SSD life remaining", html: `${escapeHtml(slot.SSDLifeRemaining)}% <span class="muted">(firmware always reports 100)</span>` },
    { label: "Rotational speed", html: `${escapeHtml(rpmLabel(slot.RotationalSpeed))} <span class="muted">(code ${escapeHtml(slot.RotationalSpeed)})</span>` },
  ];

  return (
    `<div class="drive-card">` +
    `<div class="drive-head">` +
    `<span class="led sev-${sevClass(st.sev)}"><span class="dot"></span></span>` +
    `<b>Slot ${escapeHtml(slot.mSlotNumber)}</b>` +
    `<span class="badge ${diskType === "SSD" ? "ssd" : "hdd"}">${escapeHtml(diskType)}</span>` +
    (isAccel ? `<span class="muted">mSATA cache</span>` : "") +
    `</div>` +
    kvTable(rows) +
    `</div>`
  );
}

function drivesPanel(raw) {
  const slots = Array.isArray(raw.mSlotsExp) ? raw.mSlotsExp : [];
  const cards = slots.map(driveCard).join("");
  // The Drobo 5N's 6th slot is an mSATA cache accelerator (disk type SSD=4,
  // disk state 32), not a data bay — count it separately so the header isn't
  // misleading.
  const accel = slots.filter(
    (s) => String(s.mDiskType) === "4" && String(s.mDiskState) === "32"
  ).length;
  const dataBays = slots.length - accel;
  const sub = accel > 0
    ? `(${dataBays} data bays + ${accel} mSATA cache)`
    : `(${slots.length} slots reported)`;
  return panel("Drives", sub, `<div class="drive-grid">${cards || '<p class="muted">No slot data.</p>'}</div>`);
}

const LUN_LABELS = {
  mLUN: "LUN", mUniqueLUNID: "Unique LUN ID", mTargetName: "Target name",
  mLUNName: "LUN name", mMaximumLUNSize: "Maximum LUN size", mInitiatorNames: "Initiator names",
  ExtraInitatorInfo: "Extra initiator info", mUsedCapacityOS: "Used capacity (OS)",
  mFlags: "Flags", mPartitionCount: "Partition count", mPartitionType: "Partition type",
  mPartitionFormat: "Partition format", mShareState: "Share state",
  mNextAvailableID: "Next available ID", mInitiatorCount: "Initiator count",
  mLoggedInState: "Logged-in state",
};
const LUN_BYTE_FIELDS = new Set(["mMaximumLUNSize", "mUsedCapacityOS"]);

function lunCard(lun, idx) {
  if (!lun || typeof lun !== "object") return "";
  const rows = Object.keys(lun).map((k) => ({
    label: LUN_LABELS[k] || k,
    html: LUN_BYTE_FIELDS.has(k) ? bytesCell(lun[k]) : txt(lun[k]),
  }));
  const id = lun.mLUN != null && lun.mLUN !== "" ? lun.mLUN : idx;
  return `<div class="drive-card"><div class="drive-head"><b>LUN ${escapeHtml(id)}</b></div>${kvTable(rows)}</div>`;
}

function lunsPanel(raw) {
  const luns = Array.isArray(raw.mLUNUpdates) ? raw.mLUNUpdates : [];
  const cnt = raw.mLUNCount != null ? raw.mLUNCount : luns.length;
  const max = raw.mMaxLUNs != null ? raw.mMaxLUNs : "?";
  const cards = luns.map(lunCard).join("");
  return panel("LUNs", `(${cnt} in use · ${max} max)`, `<div class="drive-grid">${cards || '<p class="muted">No LUN data.</p>'}</div>`);
}

function devicePanel(raw) {
  const rows = [];
  Object.keys(raw).forEach((key) => {
    const val = raw[key];
    if (key === "mSlotsExp" || key === "mLUNUpdates") return; // own sections
    if (key === "DroboApps") {
      if (val && typeof val === "object") {
        rows.push({ label: "DroboApps enabled", key: "DroboApps/DNASDroboAppsEnabled", html: flagCell(val.DNASDroboAppsEnabled) });
        Object.keys(val).forEach((sk) => {
          if (sk === "DNASDroboAppsEnabled") return;
          rows.push({ label: FIELD_LABELS[sk] || sk, key: "DroboApps/" + sk, html: txt(val[sk]) });
        });
      }
      return;
    }
    if (val && typeof val === "object") {
      rows.push({ label: FIELD_LABELS[key] || key, key, html: `<span class="mono">${escapeHtml(JSON.stringify(val))}</span>` });
      return;
    }
    if (BYTE_FIELDS.has(key) || THRESHOLD_FIELDS.has(key)) return; // shown in Capacity
    rows.push({ label: FIELD_LABELS[key] || key, key, html: deviceValueHtml(key, val) });
  });
  return panel("Device, firmware & features", "", fieldTable(rows));
}

function rawPanel(raw) {
  const json = JSON.stringify(raw, null, 2);
  return (
    `<section class="panel"><details>` +
    `<summary>Raw JSON dump <span class="muted">— everything the device sent</span></summary>` +
    `<pre class="rawjson">${escapeHtml(json)}</pre>` +
    `</details></section>`
  );
}

// --- top-level render + polling ---------------------------------------------

function render(payload) {
  const banner = $("banner");

  if (!payload || payload.error) {
    banner.className = "banner critical";
    const host = (payload && payload.host) || CONFIG.host;
    const port = (payload && payload.port) || 5000;
    banner.textContent =
      "Cannot read the Drobo at " + host + ":" + port +
      (payload && payload.error ? " — " + payload.error : "");
    return;
  }

  banner.className = "banner hidden";
  const raw = payload.raw || {};

  if (raw.mName) $("device-name").textContent = raw.mName + " — Details";
  if (raw.mModel) $("device-model").textContent = raw.mModel;
  $("fetched").textContent = payload.fetched_at ? "fetched " + relTime(payload.fetched_at) : "";

  $("content").innerHTML = [
    devicePanel(raw),
    capacityPanel(raw),
    drivesPanel(raw),
    lunsPanel(raw),
    rawPanel(raw),
  ].join("");
}

let busy = false;
async function tick() {
  if (busy) return;
  busy = true;
  const btn = $("refresh");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/raw", { cache: "no-store" });
    render(await res.json());
  } catch (err) {
    const banner = $("banner");
    banner.className = "banner critical";
    banner.textContent = "Dashboard server unreachable: " + err;
  } finally {
    busy = false;
    if (btn) btn.disabled = false;
  }
}

const refreshBtn = $("refresh");
if (refreshBtn) refreshBtn.addEventListener("click", tick);
tick();
setInterval(tick, REFRESH_MS);
