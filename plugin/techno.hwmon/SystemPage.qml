import QtQuick
import qs.Commons
import qs.Ui
import "Format.js" as Format

// Page 2 (A7): high-level view — load average, per-core usage + frequency,
// RAM / swap, disk and network throughput, backups (v4; v7: UPower check lives on the Hardware page).
Column {
  id: page

  property var snapshot: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  readonly property real columnGap: Style.space(20)
  readonly property real halfWidth: (width - columnGap) / 2
  readonly property var coreRows: Format.perCoreRows(snapshot)
  readonly property var disk: Format.get(snapshot, "system.disk")
  readonly property var net: Format.get(snapshot, "system.net")

  spacing: Style.space(10)

  // ---------- Load ----------
  PanelSectionHeader {
    text: "LOAD"
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  StatRow { width: parent.width; label: "Load avg 1 / 5 / 15 min"; value: Format.loadAvg(page.snapshot); foreground: page.foreground; fontFamily: page.fontFamily }
  StatRow { width: parent.width; label: "CPU usage (all)"; value: Format.pct(Format.get(page.snapshot, "cpu.usage_pct"), 1); foreground: page.foreground; fontFamily: page.fontFamily }

  PanelSeparator { foreground: page.foreground }

  // ---------- Per core ----------
  PanelSectionHeader {
    text: "PER CORE"
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  Text {
    visible: page.coreRows.length === 0
    textFormat: Text.PlainText
    text: Format.DASH
    color: page.foreground
    opacity: 0.6
    font.family: page.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  Repeater {
    model: page.coreRows

    Column {
      required property var modelData
      width: page.width
      spacing: Style.spacing.xs

      StatRow {
        width: parent.width
        label: modelData.label + "  " + modelData.freq
        value: modelData.usage
        foreground: page.foreground
        fontFamily: page.fontFamily
      }

      Meter {
        width: parent.width
        fraction: modelData.fraction
        foreground: page.foreground
      }
    }
  }

  PanelSeparator { foreground: page.foreground }

  // ---------- Memory ----------
  PanelSectionHeader {
    text: "MEMORY"
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  StatRow {
    width: parent.width; label: "RAM"
    value: Format.usedOfTotal(Format.get(page.snapshot, "system.mem_used_bytes"), Format.get(page.snapshot, "system.mem_total_bytes"))
    foreground: page.foreground; fontFamily: page.fontFamily
  }
  Meter {
    width: parent.width
    fraction: Format.fraction(Format.get(page.snapshot, "system.mem_used_bytes"), Format.get(page.snapshot, "system.mem_total_bytes"))
    foreground: page.foreground
  }
  StatRow {
    width: parent.width; label: "Swap"
    value: Format.usedOfTotal(Format.get(page.snapshot, "system.swap_used_bytes"), Format.get(page.snapshot, "system.swap_total_bytes"))
    foreground: page.foreground; fontFamily: page.fontFamily
  }
  Meter {
    width: parent.width
    fraction: Format.fraction(Format.get(page.snapshot, "system.swap_used_bytes"), Format.get(page.snapshot, "system.swap_total_bytes"))
    foreground: page.foreground
  }

  PanelSeparator { foreground: page.foreground }

  // ---------- Disk + network ----------
  PanelSectionHeader {
    text: "DISK" + (Format.get(page.disk, "device") ? " · " + String(Format.get(page.disk, "device")).toUpperCase() : "")
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  Grid {
    width: parent.width
    columns: 2
    columnSpacing: page.columnGap
    rowSpacing: Style.spacing.labelGap

    StatRow { width: page.halfWidth; label: "Read"; value: Format.rate(Format.get(page.disk, "read_bps")); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow { width: page.halfWidth; label: "Write"; value: Format.rate(Format.get(page.disk, "write_bps")); foreground: page.foreground; fontFamily: page.fontFamily }
  }

  PanelSectionHeader {
    // system.net is null as a whole when there is no default route (A2).
    text: page.net ? "NETWORK · " + Format.text(Format.get(page.net, "iface")).toUpperCase() : "NETWORK · NO DEFAULT ROUTE"
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  Grid {
    width: parent.width
    columns: 2
    columnSpacing: page.columnGap
    rowSpacing: Style.spacing.labelGap

    StatRow { width: page.halfWidth; label: "Rx"; value: Format.rate(Format.get(page.net, "rx_bps")); foreground: page.foreground; fontFamily: page.fontFamily }
    StatRow { width: page.halfWidth; label: "Tx"; value: Format.rate(Format.get(page.net, "tx_bps")); foreground: page.foreground; fontFamily: page.fontFamily }
  }

  PanelSeparator { foreground: page.foreground }

  // ---------- Backups (v4; v7: UPower check moved to Hardware > Battery) ----------
  PanelSectionHeader {
    text: "BACKUPS"
    foreground: page.foreground
    fontFamily: page.fontFamily
  }

  StatRow {
    width: parent.width; label: "Home snapshots"
    value: Format.homeSnapshots(page.snapshot)
    foreground: page.foreground; fontFamily: page.fontFamily
  }
  StatRow {
    width: parent.width; label: "NAS backup"
    value: Format.nasBackup(page.snapshot)
    foreground: page.foreground; fontFamily: page.fontFamily
  }
}
