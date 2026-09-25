.pragma library

// Pure formatting for the hwmon widget: snapshot (latest.json, contract =
// tests/fixtures/latest.example.json) -> strings, rows and fractions.
// No QML types here, so the logic can be exercised outside the shell.
//
// Every leaf of the snapshot may be null (spec A2); every function here
// accepts a null snapshot, a missing branch, or a null leaf and returns a
// display value ("–") rather than throwing.

var DASH = "–"                  // "–" : a null reading, in its slot
var STALE_LABEL = "hwmon —"     // "hwmon —" : missing or stale snapshot (A9)
var SCHEMA = 4                  // v5 (recovery.nas_backup): any other schema is not live (A9)

function get(obj, path) {
  var parts = String(path).split(".")
  var cur = obj
  for (var i = 0; i < parts.length; i++) {
    if (cur === null || cur === undefined || typeof cur !== "object") return null
    cur = cur[parts[i]]
  }
  return cur === undefined ? null : cur
}

function isNum(v) {
  return typeof v === "number" && isFinite(v)
}

function num(v) {
  return isNum(v) ? v : null
}

function fixed(v, digits, unit) {
  if (!isNum(v)) return DASH
  return v.toFixed(digits) + (unit ? " " + unit : "")
}

// ---------------------------------------------------------------- bar label

// A6: "<package_c rounded>° <rpm/1000, 1 decimal>k", e.g. "59° 1.3k".
// A null reading keeps its unit and shows "–" in the number's place.
function compactLabel(snapshot, vertical) {
  var pkg = get(snapshot, "cpu.package_c")
  var rpm = get(snapshot, "fan.rpm")
  var t = (isNum(pkg) ? String(Math.round(pkg)) : DASH) + "°"
  var f = (isNum(rpm) ? (rpm / 1000).toFixed(1) : DASH) + "k"
  return vertical ? t + "\n" + f : t + " " + f
}

// ---------------------------------------------------------------- staleness

// loadState: "pending" (no read attempted yet), "missing" (file absent /
// unreadable), "invalid" (read, but not a schema-4 snapshot), "loaded".
// Returns { kind: "ok"|"missing"|"invalid"|"stale", age_s, headline, detail }.
// age_s is null whenever there is no usable timestamp.
function status(snapshot, loadState, nowMs, staleAfterS, loadError) {
  if (loadState === "pending")
    return { kind: "missing", age_s: null, headline: "Waiting for data", detail: "No read of latest.json yet." }
  if (loadState === "missing" || (loadState === "loaded" && snapshot === null))
    return { kind: "missing", age_s: null, headline: "No data: file missing",
             detail: "latest.json not found. Is hwmon.service running?" }
  if (loadState === "invalid")
    return { kind: "invalid", age_s: null, headline: "No data: unreadable snapshot",
             detail: String(loadError || "latest.json is not a schema-" + SCHEMA + " snapshot.") }

  var ts = get(snapshot, "ts")
  if (!isNum(ts))
    return { kind: "invalid", age_s: null, headline: "No data: no timestamp", detail: "latest.json has no numeric ts." }

  var age = nowMs / 1000 - ts
  var ageRounded = Math.round(age * 10) / 10
  if (age > staleAfterS)
    return { kind: "stale", age_s: ageRounded, headline: "Stale by " + wholeSeconds(age) + " s",
             detail: "The collector has not written for " + ageText(age) + "." }
  if (age < -staleAfterS)
    return { kind: "stale", age_s: ageRounded, headline: "Clock skew: snapshot " + wholeSeconds(-age) + " s in the future",
             detail: "latest.json ts is ahead of this clock." }
  return { kind: "ok", age_s: ageRounded, headline: "Updated " + ageText(Math.max(0, age)) + " ago", detail: "" }
}

function wholeSeconds(s) {
  return String(Math.floor(Math.max(0, s)))
}

function ageText(s) {
  if (!isNum(s)) return DASH
  s = Math.max(0, s)
  if (s < 60) return Math.floor(s) + " s"
  if (s < 3600) return Math.floor(s / 60) + " min " + Math.floor(s % 60) + " s"
  if (s < 86400) return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min"
  return Math.floor(s / 86400) + " d " + Math.floor((s % 86400) / 3600) + " h"
}

function barLabel(st, snapshot, vertical) {
  return st.kind === "ok" ? compactLabel(snapshot, vertical) : STALE_LABEL
}

// A6 IPC payload: what the bar is showing, whether it is stale, how old.
function stateJson(st, snapshot) {
  return JSON.stringify({
    label: barLabel(st, snapshot, false),
    stale: st.kind !== "ok",
    age_s: st.age_s
  })
}

// Parse latest.json text. Returns { snapshot, state, error }.
function parse(text) {
  var raw = String(text || "")
  if (raw.replace(/\s+/g, "") === "") return { snapshot: null, state: "invalid", error: "latest.json is empty." }
  var parsed
  try {
    parsed = JSON.parse(raw)
  } catch (e) {
    return { snapshot: null, state: "invalid", error: "latest.json is not valid JSON." }
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))
    return { snapshot: null, state: "invalid", error: "latest.json is not a JSON object." }
  if (parsed.schema !== SCHEMA)
    return { snapshot: null, state: "invalid", error: "Unsupported schema " + String(parsed.schema) + " (expected " + SCHEMA + ")." }
  return { snapshot: parsed, state: "loaded", error: "" }
}

// ---------------------------------------------------------------- values

function temp(v) { return fixed(v, 1, "°C") }
function pct(v, digits) { return fixed(v, digits === undefined ? 0 : digits, "%") }
function volts(v) { return fixed(v, 2, "V") }
function amps(v) { return fixed(v, 2, "A") }
function ah(v) { return fixed(v, 2, "Ah") }
function rpm(v) { return isNum(v) ? Math.round(v) + " rpm" : DASH }
function intText(v) { return isNum(v) ? String(Math.round(v)) : DASH }

// A4: signed — negative discharging, positive charging. Shown with its sign.
function watts(v) {
  if (!isNum(v)) return DASH
  var s = Math.abs(v).toFixed(1) + " W"
  if (v > 0) return "+" + s
  if (v < 0) return "−" + s
  return s
}

// A2: health may exceed 100 — shown raw, never clamped.
function health(v) { return fixed(v, 1, "%") }

function bool(v, yes, no) {
  if (v === true) return yes
  if (v === false) return no
  return DASH
}

function text(v) {
  return (v === null || v === undefined || String(v) === "") ? DASH : String(v)
}

function freq(mhz) {
  if (!isNum(mhz)) return DASH
  return mhz >= 1000 ? (mhz / 1000).toFixed(2) + " GHz" : Math.round(mhz) + " MHz"
}

function bytes(v) {
  if (!isNum(v)) return DASH
  var units = ["B", "KiB", "MiB", "GiB", "TiB"]
  var i = 0
  var n = Math.abs(v)
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return (v < 0 ? "-" : "") + (i === 0 ? String(Math.round(n)) : n.toFixed(1)) + " " + units[i]
}

function rate(v) {
  return isNum(v) ? bytes(v) + "/s" : DASH
}

function usedOfTotal(used, total) {
  if (!isNum(used) && !isNum(total)) return DASH
  return bytes(used) + " / " + bytes(total)
}

function fraction(used, total) {
  if (!isNum(used) || !isNum(total) || total <= 0) return null
  return Math.max(0, Math.min(1, used / total))
}

// Fan position between its min and max (A7 "rpm vs min/max as a bar").
function fanFraction(snapshot) {
  var r = get(snapshot, "fan.rpm")
  var lo = get(snapshot, "fan.min_rpm")
  var hi = get(snapshot, "fan.max_rpm")
  if (!isNum(r) || !isNum(lo) || !isNum(hi) || hi <= lo) return null
  return Math.max(0, Math.min(1, (r - lo) / (hi - lo)))
}

function loadAvg(snapshot) {
  var l = get(snapshot, "cpu.load")
  if (!Array.isArray(l)) return [DASH, DASH, DASH].join("  ")
  var out = []
  for (var i = 0; i < 3; i++) out.push(isNum(l[i]) ? l[i].toFixed(2) : DASH)
  return out.join("  ")
}

// ---------------------------------------------------------------- rows

function sortedKeys(obj) {
  if (obj === null || typeof obj !== "object" || Array.isArray(obj)) return []
  return Object.keys(obj).sort()
}

// Label -> °C objects (cpu.cores_c, temps) as display rows, sorted by label.
function tempRows(obj) {
  var keys = sortedKeys(obj)
  var out = []
  for (var i = 0; i < keys.length; i++)
    out.push({ label: keys[i], value: num(obj[keys[i]]), text: temp(obj[keys[i]]) })
  return out
}

function perCoreRows(snapshot) {
  var list = get(snapshot, "cpu.per_core")
  if (!Array.isArray(list)) return []
  var out = []
  for (var i = 0; i < list.length; i++) {
    var c = list[i]
    var u = get(c, "usage_pct")
    out.push({
      label: "CPU " + i,
      usage: pct(u, 1),
      freq: freq(get(c, "freq_mhz")),
      fraction: isNum(u) ? Math.max(0, Math.min(1, u / 100)) : null
    })
  }
  return out
}

function invalidSensors(snapshot) {
  var list = get(snapshot, "sensors_invalid")
  return Array.isArray(list) ? list : null
}

function invalidCountText(snapshot) {
  var list = invalidSensors(snapshot)
  if (list === null) return DASH
  return list.length === 0 ? "0" : list.length + " (" + list.join(", ") + ")"
}

// ---------------------------------------------------------------- v3

// A14: fan.control -> "SMC auto" | "mbpfan" | "manual"; null -> "–". An
// unknown string is shown as-is rather than hidden.
function fanControl(v) {
  if (v === "smc") return "SMC auto"
  if (v === "mbpfan") return "mbpfan"
  if (v === "manual") return "manual"
  return text(v)
}

// A14: actual speed with the target beside it, e.g. "1292 rpm · target 2272 rpm".
function fanSpeed(snapshot) {
  return rpm(get(snapshot, "fan.rpm")) + " · target " + rpm(get(snapshot, "fan.target_rpm"))
}

// A13: each blocker as "who — why". blockers is [] when nothing blocks (S7);
// a null / non-array list, a null element or a null field still renders.
function blockerLines(snapshot) {
  var list = get(snapshot, "power_guard.blockers")
  if (!Array.isArray(list)) return []
  var out = []
  for (var i = 0; i < list.length; i++)
    out.push(text(get(list[i], "who")) + " — " + text(get(list[i], "why")))
  return out
}

// A13 popup banner. Worded as a fact about the current state (v3 amendment
// S2): it names what is inhibiting sleep and never says that caused anything.
// Returns { visible, title, lines }.
function guardBanner(snapshot) {
  if (get(snapshot, "power_guard.sleep_blocked") !== true)
    return { visible: false, title: "", lines: [] }
  var lines = blockerLines(snapshot)
  return {
    visible: true,
    title: lines.length > 0 ? "Sleep is blocked by:" : "Sleep is blocked (no inhibitor listed).",
    lines: lines
  }
}

// A15: "yes" / "no" / "–" (recent is null on the collector's first sample, S7).
function throttleRecent(snapshot) {
  return bool(get(snapshot, "cpu.throttle.recent"), "yes", "no")
}

// ---------------------------------------------------------------- v4 recovery
// recovery.home_snapshot_state: not_configured | unknown | empty | fresh | stale.
// "unknown" means hwmon could not ask snapper; it is never shown as fresh.
// "empty" means the config exists and snapper ran, but no snapshot has been
// taken yet (v5: distinct from "unknown" -- a said-clean answer, not a
// could-not-answer one).
function homeSnapshots(snapshot) {
  var state = get(snapshot, "recovery.home_snapshot_state")
  var age = get(snapshot, "recovery.home_snapshot_age_s")
  if (state === "fresh" || state === "stale")
    return (state === "stale" ? "STALE · " : "") + ageText(age) + " ago"
  if (state === "empty") return "set up, none yet"
  if (state === "not_configured") return "not set up"
  if (state === "unknown") return "could not check"
  return DASH
}

// recovery.upower: {state: ok | divergent | unknown, upower_pct, sysfs_pct}.
function upowerCheck(snapshot) {
  var state = get(snapshot, "recovery.upower.state")
  var pair = pct(get(snapshot, "recovery.upower.upower_pct"), 1) + " vs " + pct(get(snapshot, "recovery.upower.sysfs_pct"), 1)
  if (state === "ok") return "agrees · " + pair
  if (state === "divergent") return "DISAGREES · " + pair
  if (state === "unknown") return "could not check"
  return DASH
}

// ---------------------------------------------------------------- v5 nas backup

// recovery.nas_backup: {state: not_configured | unknown | failed | never |
// fresh | stale, age_s, reason}. R-N7/A-S5: hwmon reads only this small
// status file the backup job writes -- never the repo, the mount or the
// network. "skipped" alone (the backup job's own result) never reaches
// this formatter as "failed" -- "failed" reflects the most recent
// NON-skipped attempt (v2 contract), carried forward across skips, so a
// failure doesn't vanish on the next skipped tick. "never" (a real risk:
// no ok has EVER been recorded) is shown as its own text, never as
// "STALE · –" -- a bare dash there would read as a data problem, not a
// backup problem.
function nasBackup(snapshot) {
  var state = get(snapshot, "recovery.nas_backup.state")
  var age = get(snapshot, "recovery.nas_backup.age_s")
  var reason = get(snapshot, "recovery.nas_backup.reason")
  if (state === "fresh" || state === "stale")
    return (state === "stale" ? "STALE · " : "") + ageText(age) + " ago"
  if (state === "failed") return "FAILED" + (reason ? " · " + text(reason) : "")
  if (state === "never") return "not backed up yet"
  if (state === "not_configured") return "not set up"
  if (state === "unknown") return "could not check"
  return DASH
}
