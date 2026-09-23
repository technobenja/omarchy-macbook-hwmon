.pragma library

// The ONE threshold table for the hwmon widget (spec A8 as modified by v3 M7,
// plus A13 / A15 and the A9 staleness bound). Nothing else in the plugin
// compares a reading against a number; it asks this file. Change a limit here
// and every surface follows.
//
// TABLE entries: path into latest.json, and the value at which that reading
// becomes "warn" / "critical" (null = that level is not defined for it).
// Rules that combine several readings (A13 guard, A15 throttle, M7 cooling
// saturated) are the functions below the table.

var STALE_AFTER_S = 5          // A9: older than this (or missing) => stale

var TABLE = {
  cpu_package: { path: ["cpu", "package_c"],   warn: 80,   critical: 95 },   // °C
  battery_temp:{ path: ["battery", "temp_c"],   warn: 45,   critical: null }  // °C
  // v3 M7: the v2 "fan >= 5000 RPM" warn is REMOVED — with mbpfan the fan
  // reaches ~5,300 RPM during video playback. See "cooling saturated" below.
}

// A13 — "sleep inhibited on low battery" indicator (critical). An indicator of
// a state, not a diagnosis of anything (v3 amendment S2).
var GUARD_BATTERY_PCT = 10     // battery.pct <= this ...
var GUARD_STATUS = "Discharging" // ... AND battery.status == this ...
                               // ... AND power_guard.sleep_blocked === true

// v3 M7 as amended by S5 — "cooling saturated" (warn): fan.rpm >=
// SATURATION_FRACTION × fan.max_rpm AND cpu.package_c >= SATURATION_PACKAGE_C,
// held continuously for >= SATURATION_SUSTAIN_S by snapshot ts. Never fires on
// a single sample.
var SATURATION_FRACTION = 0.95
var SATURATION_PACKAGE_C = 80
var SATURATION_SUSTAIN_S = 60

var RANK = { normal: 0, warn: 1, critical: 2 }

function isNum(v) {
  return typeof v === "number" && isFinite(v)
}

// "normal" | "warn" | "critical" for one metric value. A null / non-numeric
// reading is "normal": the absence is shown as "–", not as an alarm.
function level(metric, value) {
  var t = TABLE[metric]
  if (!t || !isNum(value)) return "normal"
  if (isNum(t.critical) && value >= t.critical) return "critical"
  if (isNum(t.warn) && value >= t.warn) return "warn"
  return "normal"
}

function read(snapshot, path) {
  var cur = snapshot
  for (var i = 0; i < path.length; i++) {
    if (cur === null || typeof cur !== "object") return null
    cur = cur[path[i]]
  }
  return cur === undefined ? null : cur
}

function levelIn(snapshot, metric) {
  var t = TABLE[metric]
  return t ? level(metric, read(snapshot, t.path)) : "normal"
}

// ---------------------------------------------------------------- A13

function guardCritical(snapshot) {
  var pct = read(snapshot, ["battery", "pct"])
  return isNum(pct) && pct <= GUARD_BATTERY_PCT
      && read(snapshot, ["battery", "status"]) === GUARD_STATUS
      && read(snapshot, ["power_guard", "sleep_blocked"]) === true
}

function guardLevel(snapshot) {
  return guardCritical(snapshot) ? "critical" : "normal"
}

// ---------------------------------------------------------------- A15

function throttleLevel(snapshot) {
  return read(snapshot, ["cpu", "throttle", "recent"]) === true ? "warn" : "normal"
}

// ---------------------------------------------------------------- M7 / S5

// The instantaneous condition, one snapshot. Any null reading => false.
function saturatedNow(snapshot) {
  var rpm = read(snapshot, ["fan", "rpm"])
  var max = read(snapshot, ["fan", "max_rpm"])
  var pkg = read(snapshot, ["cpu", "package_c"])
  if (!isNum(rpm) || !isNum(max) || max <= 0 || !isNum(pkg)) return false
  return rpm >= SATURATION_FRACTION * max && pkg >= SATURATION_PACKAGE_C
}

// Sustain tracker, kept by the widget: { since: ts|null, lastTs: ts|null }.
// `since` is the snapshot ts at which the unbroken run of saturated samples
// began. The run breaks (since -> null) when the condition fails, the data is
// not live (stale / missing / invalid), the snapshot has no ts, the ts goes
// backwards, or two consecutive observed samples are more than STALE_AFTER_S
// apart (an unobserved gap cannot count as "sustained").
function newTracker() {
  return { since: null, lastTs: null }
}

function trackSaturation(prev, snapshot, live) {
  var ts = read(snapshot, ["ts"])
  if (!isNum(ts)) return newTracker()
  if (live !== true || !saturatedNow(snapshot)) return { since: null, lastTs: ts }
  var p = prev || newTracker()
  if (isNum(p.since) && isNum(p.lastTs) && ts >= p.lastTs && ts - p.lastTs <= STALE_AFTER_S)
    return { since: p.since, lastTs: ts }
  return { since: ts, lastTs: ts }
}

// Seconds the current run has lasted (null when there is no run). Read from
// the tracker alone: trackSaturation() already judged the snapshot it was fed,
// and the widget assigns snapshot and tracker as two separate properties, so
// cross-checking them here would flicker for the instant between the two
// assignments (seen in the Quickshell harness). The widget treats stale data
// as "normal" itself and resets the tracker.
function saturationHeldS(tracker) {
  if (!tracker || !isNum(tracker.since) || !isNum(tracker.lastTs) || tracker.lastTs < tracker.since) return null
  return tracker.lastTs - tracker.since
}

// `snapshot` is accepted for call-site symmetry and ignored (see above).
function saturationLevel(tracker, snapshot) {
  var held = saturationHeldS(tracker)
  return held !== null && held >= SATURATION_SUSTAIN_S ? "warn" : "normal"
}

// ---------------------------------------------------------------- worst

function worse(a, b) {
  return RANK[b] > RANK[a] ? b : a
}

// Worst level across every rule — drives the bar label colour. `tracker` is
// the widget's saturation tracker (null => "cooling saturated" cannot fire).
function worstLevel(snapshot, tracker) {
  var worst = "normal"
  for (var metric in TABLE) worst = worse(worst, levelIn(snapshot, metric))
  worst = worse(worst, guardLevel(snapshot))
  worst = worse(worst, throttleLevel(snapshot))
  worst = worse(worst, saturationLevel(tracker || null, snapshot))
  return worst
}
