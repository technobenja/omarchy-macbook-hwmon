// Node tests for the pure JS half of techno.hwmon (Format.js, Thresholds.js)
// against the schema-4 contract, tests/fixtures/latest.example.json.
//
//   node plugin/js_tests.mjs            # exit 0 = all pass
//
// The QML ".pragma library" line is stripped and each file is evaluated in its
// own vm context, the same isolation QML gives a library script.
import fs from "node:fs"
import path from "node:path"
import vm from "node:vm"
import { fileURLToPath } from "node:url"

const here = path.dirname(fileURLToPath(import.meta.url))
const pluginDir = path.join(here, "techno.hwmon")
const fixturePath = path.join(here, "..", "tests", "fixtures", "latest.example.json")

function load(name) {
  const src = fs.readFileSync(path.join(pluginDir, name), "utf8").replace(/^\s*\.pragma library\s*$/m, "")
  const ctx = vm.createContext({})
  vm.runInContext(src, ctx, { filename: name })
  return ctx
}

const F = load("Format.js")
const T = load("Thresholds.js")
const FIXTURE_TEXT = fs.readFileSync(fixturePath, "utf8")
const fixture = () => JSON.parse(FIXTURE_TEXT)

let passed = 0
const failures = []
function check(name, cond, detail) {
  if (cond) passed++
  else failures.push(name + (detail === undefined ? "" : "  -> " + JSON.stringify(detail)))
}
function eq(name, got, want) { check(name, got === want, { got, want }) }

// A snapshot with overrides applied at dotted paths.
function snap(overrides, base) {
  const s = base || fixture()
  for (const [k, v] of Object.entries(overrides || {})) {
    const parts = k.split(".")
    let cur = s
    for (let i = 0; i < parts.length - 1; i++) {
      if (cur[parts[i]] === null || typeof cur[parts[i]] !== "object") cur[parts[i]] = {}
      cur = cur[parts[i]]
    }
    if (v === undefined) delete cur[parts[parts.length - 1]]
    else cur[parts[parts.length - 1]] = v
  }
  return s
}

// Everything a render touches, for the "no TypeError" sweeps.
function renderAll(s, tracker) {
  const st = F.status(s, s === null ? "missing" : "loaded", 1790179200500, T.STALE_AFTER_S, "")
  return [
    F.compactLabel(s, false), F.compactLabel(s, true), F.barLabel(st, s, false), F.stateJson(st, s),
    F.fanSpeed(s), F.fanControl(F.get(s, "fan.control")), JSON.stringify(F.guardBanner(s)),
    F.blockerLines(s).join("|"), F.throttleRecent(s), F.homeSnapshots(s), F.upowerCheck(s), F.nasBackup(s), T.recoveryLevel(s),
    F.intText(F.get(s, "cpu.throttle.core_count")), F.intText(F.get(s, "cpu.throttle.package_count")),
    F.bool(F.get(s, "power_guard.sleep_blocked"), "yes", "no"),
    JSON.stringify(F.tempRows(F.get(s, "cpu.cores_c"))), JSON.stringify(F.tempRows(F.get(s, "temps"))),
    JSON.stringify(F.perCoreRows(s)), F.invalidCountText(s), F.loadAvg(s), String(F.fanFraction(s)),
    T.worstLevel(s, tracker), T.guardLevel(s), T.throttleLevel(s), T.saturationLevel(tracker, s),
    T.levelIn(s, "cpu_package"), T.levelIn(s, "battery_temp"),
    JSON.stringify(T.trackSaturation(tracker, s, true)), JSON.stringify(T.trackSaturation(tracker, s, false))
  ]
}

// Feed a sequence of 1 Hz samples through the tracker, as the widget does.
function runSustain(overrides, seconds, startTs) {
  let tr = T.newTracker()
  let s = null
  for (let i = 0; i <= seconds; i++) {
    s = snap(Object.assign({ ts: startTs + i }, overrides))
    tr = T.trackSaturation(tr, s, true)
  }
  return { tr, s, level: T.worstLevel(s, tr), sat: T.saturationLevel(tr, s) }
}

const HOT = { "fan.max_rpm": 6199, "cpu.package_c": 84, "cpu.throttle.recent": false,
              "battery.pct": 81, "power_guard.sleep_blocked": false, "power_guard.blockers": [] }

// ------------------------------------------------ contract / A9 schema rule
{
  const r = F.parse(FIXTURE_TEXT)
  eq("fixture parses as loaded", r.state, "loaded")
  eq("fixture schema is 4", r.snapshot.schema, 4)
  const st = F.status(r.snapshot, r.state, (r.snapshot.ts + 0.4) * 1000, T.STALE_AFTER_S, r.error)
  eq("fixture is live", st.kind, "ok")
  eq("fixture label", F.barLabel(st, r.snapshot, false), "59° 1.3k")

  for (const bad of [1, 2, 3, "4", null, undefined]) {
    const o = fixture(); if (bad === undefined) delete o.schema; else o.schema = bad
    const p = F.parse(JSON.stringify(o))
    eq("schema " + String(bad) + " -> invalid", p.state, "invalid")
    const s2 = F.status(p.snapshot, p.state, Date.now(), T.STALE_AFTER_S, p.error)
    eq("schema " + String(bad) + " -> not live label", F.barLabel(s2, p.snapshot, false), "hwmon —")
    check("schema " + String(bad) + " -> stateJson stale", JSON.parse(F.stateJson(s2, p.snapshot)).stale === true)
  }
}

// ------------------------------------------------ fixture renders (v3 surfaces)
{
  const s = fixture()
  eq("A14 control mbpfan", F.fanControl(s.fan.control), "mbpfan")
  eq("A14 control smc", F.fanControl("smc"), "SMC auto")
  eq("A14 control manual", F.fanControl("manual"), "manual")
  eq("A14 control null", F.fanControl(null), "–")
  eq("A14 speed + target", F.fanSpeed(s), "1292 rpm · target 2272 rpm")
  eq("A14 target null", F.fanSpeed(snap({ "fan.target_rpm": null })), "1292 rpm · target –")
  const b = F.guardBanner(s)
  eq("A13 banner visible on fixture", b.visible, true)
  eq("A13 banner title is a fact", b.title, "Sleep is blocked by:")
  eq("A13 banner line", b.lines.join("|"), "omarchy-update — Omarchy update in progress")
  check("A13 banner makes no causal claim", !/caus|because|prevent|will|led to|kill/i.test(b.title + b.lines.join(" ")))
  eq("A13 banner hidden when not blocked", F.guardBanner(snap({ "power_guard.sleep_blocked": false, "power_guard.blockers": [] })).visible, false)
  eq("A13 banner hidden when null", F.guardBanner(snap({ "power_guard.sleep_blocked": null })).visible, false)
  eq("A13 blocked but empty list", F.guardBanner(snap({ "power_guard.blockers": [] })).title, "Sleep is blocked (no inhibitor listed).")
  eq("A13 null element / fields", F.blockerLines(snap({ "power_guard.blockers": [null, { who: null, why: "x" }, {}] })).join("|"), "– — –|– — x|– — –")
  eq("A15 throttle recent false", F.throttleRecent(s), "no")
  eq("A15 throttle recent null (first sample)", F.throttleRecent(snap({ "cpu.throttle.recent": null })), "–")
  eq("A15 core count", F.intText(s.cpu.throttle.core_count), "3")
  // Fixture: pct 81, Discharging, sleep_blocked, recent false, 59 °C -> normal.
  eq("fixture worst level normal", T.worstLevel(s, T.newTracker()), "normal")
}

// ------------------------------------------------ acceptance 14
{
  // M7: 5300 RPM @ 72 °C is NOT warn, even held 120 s.
  const a = runSustain(Object.assign({}, HOT, { "fan.rpm": 5300, "cpu.package_c": 72 }), 120, 1790179200)
  eq("acc14 5300rpm@72C sustained 120s -> normal", a.level, "normal")
  // The v2 rule would have warned on 5300 alone; prove it is gone.
  eq("acc14 fan_rpm table entry removed", T.TABLE.fan_rpm, undefined)

  for (const rpm of [5950, 6199]) {
    const w = runSustain(Object.assign({}, HOT, { "fan.rpm": rpm }), 60, 1790179200)
    // 84 °C also trips the separate package >= 80 warn; isolate the sustain rule.
    eq(`acc14 ${rpm}/6199@84C sustained 60s -> saturation warn`, w.sat, "warn")
    eq(`acc14 ${rpm}/6199@84C sustained 60s -> label warn`, w.level, "warn")
    const n = runSustain(Object.assign({}, HOT, { "fan.rpm": rpm }), 30, 1790179200)
    eq(`acc14 ${rpm}/6199@84C only 30s -> saturation NOT warn`, n.sat, "normal")
    const one = T.trackSaturation(T.newTracker(), snap(Object.assign({ ts: 5 }, HOT, { "fan.rpm": rpm })), true)
    eq(`acc14 ${rpm} single sample -> NOT warn`, T.saturationLevel(one, snap(Object.assign({ ts: 5 }, HOT, { "fan.rpm": rpm }))), "normal")
    const edge = runSustain(Object.assign({}, HOT, { "fan.rpm": rpm }), 59, 1790179200)
    eq(`acc14 ${rpm} 59s -> NOT warn`, edge.sat, "normal")
  }
  // The label's own "warn" at 84 °C comes from cpu_package too; with the
  // package rule neutralised the sustain rule is what makes it warn:
  eq("package 84 alone is warn (unchanged A8)", T.levelIn(snap({ "cpu.package_c": 84 }), "cpu_package"), "warn")

  const crit = snap({ "battery.pct": 8, "battery.status": "Discharging", "power_guard.sleep_blocked": true })
  eq("acc14 pct8 Discharging sleep_blocked -> critical", T.worstLevel(crit, T.newTracker()), "critical")
  eq("acc14 pct8 Charging sleep_blocked -> not critical", T.worstLevel(snap({ "battery.pct": 8, "battery.status": "Charging", "power_guard.sleep_blocked": true }), T.newTracker()), "normal")
  eq("acc14 pct8 Discharging sleep_blocked false -> not critical", T.worstLevel(snap({ "battery.pct": 8, "battery.status": "Discharging", "power_guard.sleep_blocked": false, "power_guard.blockers": [] }), T.newTracker()), "normal")
  eq("A13 boundary pct 10 -> critical", T.guardLevel(snap({ "battery.pct": 10 })), "critical")
  eq("A13 pct 11 -> normal", T.guardLevel(snap({ "battery.pct": 11 })), "normal")
  eq("A13 sleep_blocked null -> normal", T.guardLevel(snap({ "battery.pct": 8, "power_guard.sleep_blocked": null })), "normal")
  eq("A13 sleep_blocked truthy non-bool -> normal", T.guardLevel(snap({ "battery.pct": 8, "power_guard.sleep_blocked": 1 })), "normal")
  eq("A13 pct null -> normal", T.guardLevel(snap({ "battery.pct": null })), "normal")
}

// ------------------------------------------------ A15 throttle warn
{
  eq("A15 recent true -> warn", T.worstLevel(snap({ "cpu.throttle.recent": true }), null), "warn")
  eq("A15 recent false -> normal", T.worstLevel(snap({ "cpu.throttle.recent": false }), null), "normal")
  eq("A15 recent null -> normal", T.worstLevel(snap({ "cpu.throttle.recent": null }), null), "normal")
  eq("worst = critical over warn", T.worstLevel(snap({ "cpu.throttle.recent": true, "battery.pct": 5 }), null), "critical")
  eq("package 95 still critical", T.worstLevel(snap({ "cpu.package_c": 95 }), null), "critical")
  eq("battery temp 45 still warn", T.worstLevel(snap({ "battery.temp_c": 45 }), null), "warn")
}

// ------------------------------------------------ sustain resets
{
  const sat = Object.assign({}, HOT, { "fan.rpm": 6000 })
  // Condition breaks at t=40 for one sample -> run restarts; 60 s after t=0 is not enough.
  let tr = T.newTracker(), s
  for (let i = 0; i <= 101; i++) {
    s = snap(Object.assign({ ts: 1000 + i }, sat, i === 40 ? { "fan.rpm": 5000 } : {}))
    tr = T.trackSaturation(tr, s, true)
    if (i === 70) eq("break at 40 -> not warn at 70", T.saturationLevel(tr, s), "normal")
  }
  eq("break at 40 -> warn at 101 (61 s after restart)", T.saturationLevel(tr, s), "warn")

  // Stale (live=false) in the middle resets.
  tr = T.newTracker()
  for (let i = 0; i <= 70; i++) {
    s = snap(Object.assign({ ts: 2000 + i }, sat))
    tr = T.trackSaturation(tr, s, i !== 30)
  }
  eq("not-live sample at 30 resets the run", T.saturationLevel(tr, s), "normal")

  // Observation gap > STALE_AFTER_S resets even if both ends are saturated.
  tr = T.newTracker()
  s = snap(Object.assign({ ts: 3000 }, sat)); tr = T.trackSaturation(tr, s, true)
  s = snap(Object.assign({ ts: 3070 }, sat)); tr = T.trackSaturation(tr, s, true)
  eq("two samples 70 s apart -> not warn", T.saturationLevel(tr, s), "normal")

  // ts going backwards resets.
  tr = runSustain(sat, 65, 4000).tr
  s = snap(Object.assign({ ts: 3990 }, sat)); tr = T.trackSaturation(tr, s, true)
  eq("ts backwards resets", T.saturationLevel(tr, s), "normal")

  // Re-reading the same snapshot keeps the run (same ts).
  const r = runSustain(sat, 60, 5000)
  const again = T.trackSaturation(r.tr, r.s, true)
  eq("re-ingest same ts keeps warn", T.saturationLevel(again, r.s), "warn")
  // The level is read from the tracker alone (no flicker between the widget's
  // two assignments); a sample that breaks the run clears it in the tracker.
  eq("tracker alone carries the level", T.saturationLevel(r.tr, null), "warn")
  eq("breaking sample clears the run", T.saturationLevel(T.trackSaturation(r.tr, snap(Object.assign({ ts: 5061 }, sat, { "fan.rpm": 1000 })), true), null), "normal")
  // Null readings: rpm / max / package null -> never saturated.
  for (const k of ["fan.rpm", "fan.max_rpm", "cpu.package_c", "fan"]) {
    const rr = runSustain(Object.assign({}, sat, { [k]: null }), 70, 6000)
    eq(`null ${k} -> saturation never fires`, rr.sat, "normal")
  }
  // Boundary: exactly 0.95 × 6199 = 5889.05; 5889 below, 5890 at/above.
  eq("5889 < 0.95×max", runSustain(Object.assign({}, sat, { "fan.rpm": 5889 }), 70, 7000).sat, "normal")
  eq("5890 >= 0.95×max", runSustain(Object.assign({}, sat, { "fan.rpm": 5890 }), 70, 7000).sat, "warn")
  eq("package 79.9 -> no saturation", runSustain(Object.assign({}, sat, { "cpu.package_c": 79.9 }), 70, 7000).sat, "normal")
}

// ------------------------------------------------ null / missing sweeps
{
  const ok = (name, fn) => { try { fn(); passed++ } catch (e) { failures.push(name + "  -> " + e) } }
  ok("render null snapshot", () => renderAll(null, null))
  ok("render {} snapshot", () => renderAll({}, T.newTracker()))
  ok("render with junk tracker", () => renderAll(fixture(), { since: "x", lastTs: {} }))

  // Leaf-null and missing-key for every leaf; branch-null for every branch.
  const leaves = [], branches = []
  ;(function walk(o, p) {
    for (const [k, v] of Object.entries(o)) {
      const q = p ? p + "." + k : k
      if (v !== null && typeof v === "object" && !Array.isArray(v)) { branches.push(q); walk(v, q) }
      else leaves.push(q)
    }
  })(fixture(), "")
  check("sweep found v3 leaves", leaves.includes("power_guard.sleep_blocked") && leaves.includes("fan.target_rpm")
        && leaves.includes("fan.control") && leaves.includes("cpu.throttle.recent") && leaves.includes("power_guard.blockers"), leaves)
  check("sweep found v3 branches", branches.includes("power_guard") && branches.includes("cpu.throttle"), branches)
  for (const k of leaves.concat(branches)) {
    for (const v of [null, undefined, "junk", 7, [], {}]) {
      ok(`${k}=${String(v === undefined ? "<missing>" : JSON.stringify(v))}`, () => {
        const s = snap({ [k]: v })
        const tr = runSustain({}, 3, 1790179200).tr
        renderAll(s, tr); renderAll(s, T.trackSaturation(tr, s, true))
      })
    }
  }
  ok("blockers array of junk", () => renderAll(snap({ "power_guard.blockers": [1, "a", null, [], { who: 5 }] }), null))
  eq("sleep_blocked null -> row –", F.bool(F.get(snap({ "power_guard.sleep_blocked": null }), "power_guard.sleep_blocked"), "yes", "no"), "–")
  eq("throttle branch null -> counts –", F.intText(F.get(snap({ "cpu.throttle": null }), "cpu.throttle.core_count")), "–")
}

// ------------------------------------------------ v4 recovery
{
  eq("fixture recovery -> normal", T.recoveryLevel(fixture()), "normal")
  eq("fixture home -> age", F.homeSnapshots(fixture()), "30 min 0 s ago")
  eq("fixture upower -> agrees", F.upowerCheck(fixture()), "agrees · UPower 47.3 % / battery 49.0 %")
  // positive controls: each signal alone raises the bar to warn
  eq("home stale -> warn", T.recoveryLevel(snap({ "recovery.home_snapshot_state": "stale", "recovery.home_snapshot_age_s": 9000 })), "warn")
  eq("home stale text", F.homeSnapshots(snap({ "recovery.home_snapshot_state": "stale", "recovery.home_snapshot_age_s": 9000 })), "STALE · 2 h 30 min ago")
  eq("upower divergent -> warn", T.recoveryLevel(snap({ "recovery.upower.state": "divergent", "recovery.upower.upower_pct": 3.27, "recovery.upower.sysfs_pct": 31.34 })), "warn")
  eq("upower divergent text", F.upowerCheck(snap({ "recovery.upower.state": "divergent", "recovery.upower.upower_pct": 3.27, "recovery.upower.sysfs_pct": 31.34 })), "DISAGREES · UPower 3.3 % / battery 31.3 %")
  eq("divergent reaches worstLevel", T.worstLevel(snap({ "recovery.upower.state": "divergent" }), null) !== "normal", true)
  // three states: could-not-check and not-set-up are shown, never raised, never "fresh"
  for (const st of ["unknown", "not_configured"]) eq("home " + st + " -> normal", T.recoveryLevel(snap({ "recovery.home_snapshot_state": st })), "normal")
  eq("home unknown text", F.homeSnapshots(snap({ "recovery.home_snapshot_state": "unknown", "recovery.home_snapshot_age_s": null })), "could not check")
  eq("home not_configured text", F.homeSnapshots(snap({ "recovery.home_snapshot_state": "not_configured", "recovery.home_snapshot_age_s": null })), "not set up")
  eq("upower unknown text", F.upowerCheck(snap({ "recovery.upower.state": "unknown" })), "could not check")
  eq("recovery null -> dashes", F.homeSnapshots(snap({ "recovery": null })) + "|" + F.upowerCheck(snap({ "recovery": null })), "–|–")
  eq("recovery null -> normal", T.recoveryLevel(snap({ "recovery": null })), "normal")

  // v5: home_snapshot_state "empty" -- set up, no snapshot taken yet.
  eq("home empty -> normal", T.recoveryLevel(snap({ "recovery.home_snapshot_state": "empty", "recovery.home_snapshot_age_s": null })), "normal")
  eq("home empty text", F.homeSnapshots(snap({ "recovery.home_snapshot_state": "empty", "recovery.home_snapshot_age_s": null })), "configured, none yet")
}

// ------------------------------------------------ v5 nas backup (R-N7/A-S5)
{
  eq("fixture nas backup -> normal", T.recoveryLevel(fixture()), "normal")
  eq("fixture nas backup -> fresh text", F.nasBackup(fixture()), "2 h 0 min ago")

  // positive controls: each nas_backup signal alone raises the bar to warn
  eq("nas backup stale -> warn", T.recoveryLevel(snap({ "recovery.nas_backup.state": "stale", "recovery.nas_backup.age_s": 4 * 86400, "recovery.nas_backup.reason": null })), "warn")
  eq("nas backup stale text", F.nasBackup(snap({ "recovery.nas_backup.state": "stale", "recovery.nas_backup.age_s": 4 * 86400, "recovery.nas_backup.reason": null })), "STALE · 4 d 0 h ago")
  eq("nas backup failed -> warn", T.recoveryLevel(snap({ "recovery.nas_backup.state": "failed", "recovery.nas_backup.reason": "no-fresh-source" })), "warn")
  eq("nas backup failed text with reason", F.nasBackup(snap({ "recovery.nas_backup.state": "failed", "recovery.nas_backup.reason": "no-fresh-source" })), "FAILED · no-fresh-source")
  eq("nas backup failed text no reason", F.nasBackup(snap({ "recovery.nas_backup.state": "failed", "recovery.nas_backup.reason": null })), "FAILED")
  eq("nas backup failed reaches worstLevel", T.worstLevel(snap({ "recovery.nas_backup.state": "failed" }), null) !== "normal", true)

  // could-not-check / not-set-up are shown, never raised, never "fresh"
  for (const st of ["unknown", "not_configured"]) eq("nas backup " + st + " -> normal", T.recoveryLevel(snap({ "recovery.nas_backup.state": st })), "normal")
  eq("nas backup unknown text", F.nasBackup(snap({ "recovery.nas_backup.state": "unknown", "recovery.nas_backup.age_s": null })), "couldn't check (not a backup failure)")
  eq("nas backup not_configured text", F.nasBackup(snap({ "recovery.nas_backup.state": "not_configured", "recovery.nas_backup.age_s": null })), "not set up")
  eq("nas backup null -> dash", F.nasBackup(snap({ "recovery": null })), "–")
  eq("nas backup null -> normal", T.recoveryLevel(snap({ "recovery": null })), "normal")

  // skipped is a WRITER-side result never reaching this formatter as "failed"
  // (compute_nas_backup_state's job); the widget only ever sees state/age_s/reason.
  eq("nas backup fresh (post-skip, age still low) -> normal text", F.nasBackup(snap({ "recovery.nas_backup.state": "fresh", "recovery.nas_backup.age_s": 3600 })), "1 h 0 min ago")

  // "never" (v2 contract): no ok EVER recorded, and no failed attempt behind
  // it either (e.g. every run so far skipped on-battery) -- a real risk,
  // warned like stale/failed, but its own text: never "STALE · –".
  eq("nas backup never -> warn", T.recoveryLevel(snap({ "recovery.nas_backup.state": "never", "recovery.nas_backup.age_s": null, "recovery.nas_backup.reason": null })), "warn")
  eq("nas backup never text", F.nasBackup(snap({ "recovery.nas_backup.state": "never", "recovery.nas_backup.age_s": null, "recovery.nas_backup.reason": null })), "configured, never completed")
  check("nas backup never text is not a bare stale dash", F.nasBackup(snap({ "recovery.nas_backup.state": "never" })).indexOf("STALE") === -1)
  eq("nas backup never reaches worstLevel", T.worstLevel(snap({ "recovery.nas_backup.state": "never" }), null) !== "normal", true)
}

console.log(`${passed} passed, ${failures.length} failed`)
for (const f of failures) console.log("FAIL " + f)
process.exit(failures.length === 0 ? 0 : 1)
