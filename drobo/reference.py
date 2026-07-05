"""A small local lookup "database" for drive errors and Drobo status codes.

This is static reference data that powers the /errors page's lookup tables. It
answers the question "what does this number mean?" for:

* the Drobo per-drive ``mErrorCount`` (which is a *count*, not an error code),
* the Drobo slot-status / disk-state / rotational-speed codes, and
* common S.M.A.R.T. attributes that indicate drive trouble.

IMPORTANT: this Drobo virtualises its disks behind BeyondRAID, so real
per-drive S.M.A.R.T. is NOT readable from it (established during earlier
probing). The S.M.A.R.T. table below is therefore an educational reference for
interpreting drive health in general — not live values from this device.
"""

from __future__ import annotations

from . import codes

# --------------------------------------------------------------------------- #
# What the Drobo error count actually is.
# --------------------------------------------------------------------------- #
ERROR_COUNT_EXPLAINER = {
    "field": "mErrorCount",
    "is_code": False,
    "summary": (
        "A running tally of I/O errors the Drobo firmware has logged against a "
        "bay — not an error code. A value of 7 means seven events were recorded "
        'over the drive\'s life, not "error #7".'
    ),
    "detail": (
        "The Drobo community reverse-engineering wiki documents this field only "
        'as "Unknown. Only available on Drobo 5N." There is no public code '
        "table because the value is a counter. A small, static count while the "
        "bay still reports Healthy (slot status 3) is usually benign — a "
        "transient read retry, a bus reset, or a hiccup during a power event. "
        "What matters is the trend: a count that keeps climbing is an early "
        "warning to back up and consider replacing that drive. That is exactly "
        "why this dashboard timestamps every increase it observes."
    ),
    "guidance": [
        "Static count, drive still green: note it, no action needed.",
        "Count rising over days/weeks: back up now; plan to replace the drive.",
        "Count rising AND slot status leaves Healthy: replace the drive.",
    ],
}


# --------------------------------------------------------------------------- #
# Common S.M.A.R.T. attributes worth knowing (reference only on this device).
# id -> (name, meaning, "higher raw is worse" flag)
# --------------------------------------------------------------------------- #
SMART_ATTRIBUTES: list[dict] = [
    {
        "id": 5,
        "name": "Reallocated Sectors Count",
        "meaning": "Sectors remapped after read/write failures. Any non-zero value means the drive has found bad sectors; a rising count is a strong failure signal.",
        "rising_is_bad": True,
    },
    {
        "id": 187,
        "name": "Reported Uncorrectable Errors",
        "meaning": "Errors that could not be recovered by ECC. Google's disk study flagged this as one of the best failure predictors.",
        "rising_is_bad": True,
    },
    {
        "id": 188,
        "name": "Command Timeout",
        "meaning": "Operations aborted due to timeout — often cabling/power, sometimes a dying drive.",
        "rising_is_bad": True,
    },
    {
        "id": 197,
        "name": "Current Pending Sector Count",
        "meaning": "Unstable sectors waiting to be remapped. Non-zero means data at risk; often clears or converts to Reallocated (5).",
        "rising_is_bad": True,
    },
    {
        "id": 198,
        "name": "Offline Uncorrectable",
        "meaning": "Sectors that failed offline read tests and can't be corrected. Non-zero is serious.",
        "rising_is_bad": True,
    },
    {
        "id": 199,
        "name": "UDMA CRC Error Count",
        "meaning": "Errors on the SATA link. Usually a bad cable or connector rather than the drive itself.",
        "rising_is_bad": True,
    },
    {
        "id": 1,
        "name": "Raw Read Error Rate",
        "meaning": "Rate of hardware read errors. Interpretation is vendor-specific (Seagate encodes it oddly); watch for change, not absolute value.",
        "rising_is_bad": True,
    },
    {
        "id": 10,
        "name": "Spin Retry Count",
        "meaning": "Retries needed to spin the platters up to speed. Non-zero suggests a tired motor/bearing.",
        "rising_is_bad": True,
    },
    {
        "id": 196,
        "name": "Reallocation Event Count",
        "meaning": "How many times sectors were remapped. Pairs with attribute 5.",
        "rising_is_bad": True,
    },
    {
        "id": 9,
        "name": "Power-On Hours",
        "meaning": "Total time powered on. Not a fault — context for the drive's age.",
        "rising_is_bad": False,
    },
    {
        "id": 12,
        "name": "Power Cycle Count",
        "meaning": "Number of power-on/off cycles. Context, not a fault.",
        "rising_is_bad": False,
    },
    {
        "id": 194,
        "name": "Temperature",
        "meaning": "Drive temperature. Sustained high temps shorten life (this Drobo firmware always reports 0, i.e. not exposed).",
        "rising_is_bad": True,
    },
    {
        "id": 231,
        "name": "SSD Life Left",
        "meaning": "Remaining SSD endurance as a percentage (100 = new). Applies to the mSATA cache.",
        "rising_is_bad": False,
    },
]


def _rpm_table() -> list[dict]:
    rows = []
    for code, label in sorted(codes.KNOWN_RPM_CODES.items()):
        rpm, _ = codes.rpm_from_code(code)
        rows.append({"code": code, "rpm": rpm, "label": label})
    return rows


def reference_tables() -> dict:
    """Return every lookup table as JSON-serialisable data for the UI/API."""
    return {
        "error_count": ERROR_COUNT_EXPLAINER,
        "slot_status": [
            {"code": k, "hex": hex(k), "label": v[0], "severity": v[1]}
            for k, v in sorted(codes.SLOT_STATUS.items())
        ],
        "disk_state": [{"code": k, "label": v} for k, v in sorted(codes.DISK_STATE.items())],
        "disk_type": [{"code": k, "label": v} for k, v in sorted(codes.DISK_TYPE.items())],
        "rotational_speed": {
            "rule": "RPM = code × 200 (code 1 = SSD). Undocumented field; derived and cross-checked against the installed drives.",
            "codes": _rpm_table(),
        },
        "smart": {
            "note": "Reference only — this Drobo hides physical disks behind BeyondRAID, so live S.M.A.R.T. is not readable from it.",
            "attributes": SMART_ATTRIBUTES,
        },
    }
