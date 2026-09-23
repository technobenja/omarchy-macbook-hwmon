import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Format.js" as Format
import "Thresholds.js" as Thresholds

// techno.hwmon — bar label "<cpu package °> <fan krpm>" plus a two-page popup
// (Hardware, System). Reads the snapshot hwmon.service writes to
// $XDG_RUNTIME_DIR/hwmon/latest.json; contract = tests/fixtures/latest.example.json.
//
// Spec: specs/spec.md A6 (widget), A7 (popup), A8 (Thresholds.js), A9 (staleness).
Panel {
  id: root
  moduleName: "techno.hwmon"
  ipcTarget: "techno.hwmon"
  // This widget owns the single IpcHandler its target permits (it adds
  // state()), so the base class's handler is switched off — same as
  // omarchy.power / omarchy.agents.
  manageIpc: false

  // ---------------------------------------------------------------- theme
  // No hard-coded colours (A6): everything is the bar's or the theme's.
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgentColor: bar ? bar.urgent : Color.urgent
  readonly property color warnColor: Color.accent
  readonly property color mutedColor: Color.muted
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool vertical: bar ? bar.vertical === true : false

  function levelColor(level) {
    if (level === "muted") return mutedColor
    if (level === "critical") return urgentColor
    if (level === "warn") return warnColor
    return foreground
  }

  // ---------------------------------------------------------------- data
  // $XDG_RUNTIME_DIR, else /run/user/<uid> (uid from `id -u`, run only when
  // the variable is absent).
  readonly property string envRuntimeDir: String(Quickshell.env("XDG_RUNTIME_DIR") || "")
  property string uid: ""
  readonly property string runtimeDir: envRuntimeDir !== "" ? envRuntimeDir : (uid !== "" ? "/run/user/" + uid : "")
  readonly property string snapshotPath: runtimeDir !== "" ? runtimeDir + "/hwmon/latest.json" : ""

  property var snapshot: null
  property string loadState: "pending"   // pending | missing | invalid | loaded
  property string loadError: ""
  property double nowMs: Date.now()

  readonly property var status: Format.status(snapshot, loadState, nowMs, Thresholds.STALE_AFTER_S, loadError)
  readonly property bool stale: status.kind !== "ok"
  readonly property string label: Format.barLabel(status, snapshot, vertical)
  // v3 M7/S5: "cooling saturated" must hold for 60 s of snapshot time, so the
  // run is tracked here, sample by sample ({since, lastTs}, Thresholds.js).
  property var saturation: Thresholds.newTracker()
  readonly property string saturationLevel: stale ? "normal" : Thresholds.saturationLevel(saturation, snapshot)
  readonly property string level: stale ? "muted" : Thresholds.worstLevel(snapshot, saturation)

  function ingest(text) {
    var r = Format.parse(text)
    var now = Date.now()
    var live = Format.status(r.snapshot, r.state, now, Thresholds.STALE_AFTER_S, r.error).kind === "ok"
    root.saturation = Thresholds.trackSaturation(root.saturation, r.snapshot, live)
    root.snapshot = r.snapshot
    root.loadState = r.state
    root.loadError = r.error
    root.nowMs = now
  }

  function resetSaturation() {
    if (root.saturation.since !== null || root.saturation.lastTs !== null)
      root.saturation = Thresholds.newTracker()
  }

  function markMissing() {
    root.resetSaturation()
    root.snapshot = null
    root.loadState = "missing"
    root.loadError = ""
    root.nowMs = Date.now()
  }

  // A6 IPC: {label, stale, age_s} as JSON text (IPC returns are strings).
  function stateJson() {
    root.nowMs = Date.now()
    return Format.stateJson(root.status, root.snapshot)
  }

  FileView {
    id: snapshotFile
    path: root.snapshotPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.ingest(text())
    onLoadFailed: function(error) { root.markMissing() }
  }

  Process {
    id: uidProc
    command: ["id", "-u"]
    running: root.envRuntimeDir === ""
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var v = String(text || "").trim()
        if (/^\d+$/.test(v)) root.uid = v
      }
    }
  }

  // A9: a dead collector produces no file events, so staleness is re-judged
  // on a clock of its own. While not live it also re-reads the file, which
  // re-arms the watch if the runtime dir was removed and recreated (systemd
  // RuntimeDirectory= cleans it on stop).
  Timer {
    interval: 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: {
      root.nowMs = Date.now()
      // Stale data breaks a "cooling saturated" run (S5): it restarts from
      // the next live saturated sample.
      if (root.stale) root.resetSaturation()
      if (root.stale && root.snapshotPath !== "") snapshotFile.reload()
    }
  }

  IpcHandler {
    target: root.ipcTarget

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function state(): string { return root.stateJson() }
  }

  // ---------------------------------------------------------------- pages
  readonly property var pages: ["Hardware", "System"]
  property int pageIndex: 0
  property bool cursorActive: false

  function selectPage(index) {
    var n = pages.length
    pageIndex = ((index % n) + n) % n
    if (panelFlick) panelFlick.contentY = 0
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

  onOpenedChanged: if (opened) {
    cursorActive = false
    pageIndex = 0
    nowMs = Date.now()
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  // Shown on the bar as the open-panel mark's width (like omarchy.clock).
  readonly property real openPanelIndicatorWidth: button.labelWidth

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---------------------------------------------------------------- bar
  // WidgetButton (BarIconButton's base) because this is a text label, not a
  // glyph in a fixed icon slot — the same choice omarchy.clock makes.
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.label
    foreground: root.levelColor(root.level)
    useActiveColor: false
    fontSize: root.vertical ? Style.font.caption : Style.font.body
    horizontalMargin: 7.5
    tooltipText: "hwmon · " + root.status.headline
    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    // No fixed cap (power/network/bluetooth pattern): the panel grows to its
    // content and stops at the screen's available height; beyond that the
    // Flickable below scrolls, so every row stays reachable.
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (dx !== 0) {
          root.cursorActive = true
          root.selectPage(root.pageIndex + dx)
        }
        if (dy !== 0)
          panelFlick.contentY = root.clamp(panelFlick.contentY + dy * Style.space(56), 0,
                                           Math.max(0, panelFlick.contentHeight - panelFlick.height))
      }
      onActivateRequested: root.selectPage(root.pageIndex + 1)
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        // Shown whenever there is more below, so clipped content is visible as such.
        ScrollBar.vertical: ScrollBar { policy: panelFlick.interactive ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          // ---------- Header: title · snapshot age · current label ----------
          Item {
            width: parent.width
            implicitHeight: Math.max(headerLabels.implicitHeight, headerValue.implicitHeight)

            Column {
              id: headerLabels
              anchors.left: parent.left
              anchors.right: headerValue.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: "Hardware monitor"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
              }

              // A7/A9: snapshot age always; stale / missing is shown, never hidden.
              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: root.status.headline.toUpperCase()
                color: root.stale ? root.urgentColor : Qt.darker(root.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
                elide: Text.ElideRight
              }
            }

            Text {
              id: headerValue
              textFormat: Text.PlainText
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: Format.barLabel(root.status, root.snapshot, false)
              color: root.levelColor(root.level)
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              font.bold: true
            }
          }

          Text {
            visible: root.stale
            textFormat: Text.PlainText
            width: parent.width
            wrapMode: Text.WordWrap
            text: root.status.detail + (root.snapshot ? " Values below are the last snapshot, not current." : "")
            color: root.foreground
            opacity: 0.6
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // ---------- Page switch (agents pattern) ----------
          Row {
            id: pageSwitch
            width: parent.width
            spacing: Style.spacing.md

            readonly property real cellWidth: (width - spacing * (root.pages.length - 1)) / root.pages.length

            Repeater {
              model: root.pages

              Button {
                required property var modelData
                required property int index

                width: pageSwitch.cellWidth
                text: modelData
                selected: index === root.pageIndex
                hasCursor: root.cursorActive && index === root.pageIndex
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                verticalPadding: Style.spacing.controlPaddingY
                onClicked: {
                  root.cursorActive = true
                  root.selectPage(index)
                }
                onHovered: function(isHovered) { if (isHovered) root.cursorActive = true }
              }
            }
          }

          // Last-known values are dimmed while stale so an old number never
          // reads as current (A9).
          HardwarePage {
            visible: root.pageIndex === 0
            width: parent.width
            opacity: root.stale ? 0.45 : 1
            snapshot: root.snapshot
            saturationLevel: root.saturationLevel
            foreground: root.foreground
            warnColor: root.warnColor
            urgentColor: root.urgentColor
            fontFamily: root.fontFamily
          }

          SystemPage {
            visible: root.pageIndex === 1
            width: parent.width
            opacity: root.stale ? 0.45 : 1
            snapshot: root.snapshot
            foreground: root.foreground
            fontFamily: root.fontFamily
          }
        }
      }
    }
  }
}
