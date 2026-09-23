.pragma library

// The ONE threshold table for the hwmon widget (spec A8, plus the A9 staleness
// bound). Nothing else in the plugin compares a reading against a number; it
// asks level() here. Change a limit here and every surface follows.
//
// Each entry: path into latest.json, and the value at which that reading
// becomes "warn" / "critical" (null = that level is not defined for it).

var STALE_AFTER_S = 5          // A9: older than this (or missing) => stale

var TABLE = {
  cpu_package: { path: ["cpu", "package_c"],   warn: 80,   critical: 95 },   // °C
  fan_rpm:     { path: ["fan", "rpm"],          warn: 5000, critical: null }, // RPM
  battery_temp:{ path: ["battery", "temp_c"],   warn: 45,   critical: null }  // °C
}

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

// Worst level across every metric in TABLE — drives the bar label colour.
function worstLevel(snapshot) {
  var worst = "normal"
  for (var metric in TABLE) {
    var l = levelIn(snapshot, metric)
    if (RANK[l] > RANK[worst]) worst = l
  }
  return worst
}
