"use strict";

const CONFIG = window.DROBO_CONFIG || { host: "", pollInterval: 15, controlEnabled: false, csrfToken: "" };

const $ = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function relTime(epochSeconds) {
  if (!epochSeconds) return "never";
  const secs = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  if (secs < 5) return "just now";
  if (secs < 60) return secs + "s ago";
  const mins = Math.round(secs / 60);
  if (mins < 60) return mins + "m ago";
  return Math.round(mins / 60) + "h ago";
}

function row(k, v, mono) {
  return `<div class="k">${escapeHtml(k)}</div>` +
    `<div class="v${mono ? " mono" : ""}">${escapeHtml(v == null || v === "" ? "\u2014" : v)}</div>`;
}

async function loadCurrent() {
  const banner = $("banner");
  let snap;
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    snap = await res.json();
  } catch (err) {
    banner.className = "banner critical";
    banner.textContent = "Dashboard server unreachable: " + err;
    return;
  }
  const st = snap.status;
  if (!st) {
    banner.className = "banner critical";
    banner.textContent = "Cannot reach the Drobo at " + escapeHtml(snap.host) + ":" + escapeHtml(String(snap.port)) +
      (snap.last_error ? " — " + escapeHtml(snap.last_error) : "");
    $("current-settings").innerHTML = '<div class="muted">No data.</div>';
    return;
  }
  if (snap.stale) {
    banner.className = "banner warning";
    banner.textContent = "Showing last known-good data — the Drobo has not responded recently.";
  } else {
    banner.className = "banner hidden";
  }

  const html = [
    row("Name", st.name),
    row("Model", st.model),
    row("Serial", st.device_serial, true),
    row("Health", st.status_label),
    row("Redundancy", st.redundancy_label),
    row("Firmware", (st.firmware_version || "") + (st.firmware_release ? "  (" + st.firmware_release + ")" : "")),
    row("Capacity", (st.used_human || "?") + " used of " + (st.total_human || "?") + " · " + (st.used_pct != null ? st.used_pct.toFixed(1) + "%" : "?")),
    row("Thresholds", "yellow " + st.yellow_threshold_pct + "% · red " + st.red_threshold_pct + "%"),
    row("DroboApps", st.droboapps_enabled ? "enabled" : "disabled"),
  ].join("");
  $("current-settings").innerHTML = html;
  $("freshness").innerHTML = "updated " + relTime(snap.last_success);
}

async function postControl(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Drobo-Token": CONFIG.csrfToken || "" },
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  return { ok: res.ok, status: res.status, data };
}

function showResult(el, ok, msg) {
  el.className = "result " + (ok ? "ok" : "err");
  el.textContent = msg;
}

function wireControls() {
  const idBtn = $("btn-identify");
  const stopBtn = $("btn-stop-identify");
  const idResult = $("identify-result");

  idBtn.addEventListener("click", async () => {
    idBtn.disabled = true;
    const seconds = Number($("identify-seconds").value) || 900;
    try {
      const r = await postControl("/settings/identify", { seconds });
      showResult(idResult, r.ok, r.ok
        ? "Blinking the Drobo's LEDs for " + seconds + "s — check the device."
        : "Failed: " + (r.data.error || r.status));
    } catch (e) {
      showResult(idResult, false, "Request failed: " + e);
    } finally {
      idBtn.disabled = false;
    }
  });

  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    try {
      const r = await postControl("/settings/stop-identify", {});
      showResult(idResult, r.ok, r.ok ? "Stopped blinking." : "Failed: " + (r.data.error || r.status));
    } catch (e) {
      showResult(idResult, false, "Request failed: " + e);
    } finally {
      stopBtn.disabled = false;
    }
  });

  const confirmInput = $("restart-confirm");
  const restartBtn = $("btn-restart");
  const restartResult = $("restart-result");

  confirmInput.addEventListener("input", () => {
    restartBtn.disabled = confirmInput.value !== "RESTART";
  });

  restartBtn.addEventListener("click", async () => {
    if (confirmInput.value !== "RESTART") return;
    restartBtn.disabled = true;
    showResult(restartResult, true, "Sending restart…");
    try {
      const r = await postControl("/settings/restart", { confirm: "RESTART" });
      showResult(restartResult, r.ok, r.ok
        ? "Restart command sent. The Drobo will be unavailable for a few minutes."
        : "Failed: " + (r.data.error || r.status));
    } catch (e) {
      showResult(restartResult, false, "Request failed: " + e);
    } finally {
      confirmInput.value = "";
      restartBtn.disabled = true;
    }
  });
}

loadCurrent();
if (CONFIG.controlEnabled) wireControls();
setInterval(loadCurrent, Math.max(5, Number(CONFIG.pollInterval) || 15) * 1000);
