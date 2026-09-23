import QtQuick
import qs.Commons
import qs.Ui
import "Format.js" as Format
import "Thresholds.js" as Thresholds

// Page 1 (default, A7): low-level readings — battery, AC, fan, CPU
// temperatures, every valid SMC sensor, and the invalid-sensor count.
Column {
  id: page

  property var snapshot: null
  property color foreground: Color.foreground
  property color warnColor: Color.accent
  property color urgentColor: Color.urgent
  property string fontFamily: Style.font.family

  readonly property real columnGap: Style.space(20)
  readonly property real halfWidth: (width - columnGap) / 2
  readonly property real smcGap: Style.space(14)
  readonly property real thirdWidth: (width - smcGap * 2) / 3
  readonly property var coreRows: Format.tempRows(Format.get(snapshot, "cpu.cores_c"))
  readonly property var smcRows: Format.tempRows(Format.get(snapshot, "temps"))
  readonly property var fanFraction: Format.fanFraction(snapshot)

  function levelColor(level) {
    if (level === "critical") return urgentColor
    if (level === "warn") return warnColor
    return foreground
  }

  function metricColor(metric) {
    return levelColor(Thresholds.levelIn(snapshot, metric))
  }

  spacing: Style.space(10)

  // ---------- Battery + AC ----------
  PanelSectionHeader {
    text: "BATTERY"
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  Grid {
    width: parent.width
    columns: 2
    columnSpacing: page.columnGap
    rowSpacing: Style.spacing.labelGap

    StatRow { width: page.halfWidth; label: "Charge"; value: Format.pct(Format.get(page.snapshot, "battery.pct")); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow { width: page.halfWidth; label: "Status"; value: Format.text(Format.get(page.snapshot, "battery.status")); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow { width: page.halfWidth; label: "Power"; value: Format.watts(Format.get(page.snapshot, "battery.power_w")); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow { width: page.halfWidth; label: "AC"; value: Format.bool(Format.get(page.snapshot, "ac.online"), "Online", "Offline"); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow { width: page.halfWidth; label: "Voltage"; value: Format.volts(Format.get(page.snapshot, "battery.voltage_v")); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow { width: page.halfWidth; label: "Current"; value: Format.amps(Format.get(page.snapshot, "battery.current_a")); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow {
      width: page.halfWidth; label: "Temp"
      value: Format.temp(Format.get(page.snapshot, "battery.temp_c"))
      foreground: page.foreground; valueColor: page.metricColor("battery_temp"); fontFamily: page.fontFamily
    }
    StatRow { width: page.halfWidth; label: "Cycles"; value: Format.intText(Format.get(page.snapshot, "battery.cycles")); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow { width: page.halfWidth; label: "Health"; value: Format.health(Format.get(page.snapshot, "battery.health_pct")); foreground: page.foreground; fontFamily: page.fontFamily }
  }

  // Full width: "5.44 Ah / 6.71 Ah" does not fit beside a label in a half column.
  StatRow {
    width: parent.width; label: "Charge now / full"
    value: Format.ah(Format.get(page.snapshot, "battery.charge_now_ah")) + " / " + Format.ah(Format.get(page.snapshot, "battery.charge_full_ah"))
    foreground: page.foreground; fontFamily: page.fontFamily
  }
  StatRow { width: parent.width; label: "Design capacity"; value: Format.ah(Format.get(page.snapshot, "battery.charge_design_ah")); foreground: page.foreground; fontFamily: page.fontFamily }

  PanelSeparator { foreground: page.foreground }

  // ---------- Fan ----------
  PanelSectionHeader {
    text: "FAN" + (Format.get(page.snapshot, "fan.label") ? " · " + String(Format.get(page.snapshot, "fan.label")).toUpperCase() : "")
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  StatRow {
    width: parent.width
    label: "Speed" + (Format.get(page.snapshot, "fan.manual") === true ? " (manual)" : "")
    value: Format.rpm(Format.get(page.snapshot, "fan.rpm"))
    foreground: page.foreground
    valueColor: page.metricColor("fan_rpm")
    fontFamily: page.fontFamily
  }

  Meter {
    width: parent.width
    fraction: page.fanFraction
    foreground: page.foreground
    fillColor: page.metricColor("fan_rpm")
  }

  Item {
    width: parent.width
    implicitHeight: fanMin.implicitHeight

    Text {
      id: fanMin
      textFormat: Text.PlainText
      anchors.left: parent.left
      text: "min " + Format.rpm(Format.get(page.snapshot, "fan.min_rpm"))
      color: page.foreground
      opacity: 0.6
      font.family: page.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      textFormat: Text.PlainText
      anchors.right: parent.right
      text: "max " + Format.rpm(Format.get(page.snapshot, "fan.max_rpm"))
      color: page.foreground
      opacity: 0.6
      font.family: page.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  PanelSeparator { foreground: page.foreground }

  // ---------- CPU ----------
  PanelSectionHeader {
    text: "CPU"
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  Grid {
    width: parent.width
    columns: 2
    columnSpacing: page.columnGap
    rowSpacing: Style.spacing.labelGap

    StatRow {
      width: page.halfWidth; label: "Package"
      value: Format.temp(Format.get(page.snapshot, "cpu.package_c"))
      foreground: page.foreground; valueColor: page.metricColor("cpu_package"); fontFamily: page.fontFamily
    }

    Repeater {
      model: page.coreRows
      StatRow {
        required property var modelData
        width: page.halfWidth
        label: modelData.label
        value: modelData.text
        foreground: page.foreground
        fontFamily: page.fontFamily
      }
    }
  }

  PanelSeparator { foreground: page.foreground }

  // ---------- SMC sensors ----------
  PanelSectionHeader {
    text: "SMC SENSORS (" + page.smcRows.length + " VALID)"
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  Grid {
    width: parent.width
    columns: 3
    columnSpacing: page.smcGap
    rowSpacing: Style.spacing.labelGap

    Repeater {
      model: page.smcRows
      StatRow {
        required property var modelData
        width: page.thirdWidth
        label: modelData.label
        value: modelData.text
        foreground: page.foreground
        fontFamily: page.fontFamily
      }
    }
  }

  Text {
    textFormat: Text.PlainText
    width: parent.width
    text: "Invalid sensors: " + Format.invalidCountText(page.snapshot)
    color: page.foreground
    opacity: 0.6
    wrapMode: Text.WordWrap
    font.family: page.fontFamily
    font.pixelSize: Style.font.caption
  }
}
