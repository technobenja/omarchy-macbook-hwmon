import QtQuick
import qs.Commons

// Thin horizontal gauge. `fraction` is 0..1, or null when the reading is
// missing — then only the track is drawn, never a made-up fill.
Item {
  id: meter

  property var fraction: null
  property color foreground: Color.foreground
  property color fillColor: foreground

  readonly property bool hasValue: typeof fraction === "number" && isFinite(fraction)

  implicitWidth: Style.space(120)
  implicitHeight: Style.space(6)

  Rectangle {
    id: track
    anchors.fill: parent
    radius: height / 2
    color: Qt.rgba(meter.foreground.r, meter.foreground.g, meter.foreground.b, 0.12)
  }

  Rectangle {
    visible: meter.hasValue
    anchors.left: track.left
    anchors.verticalCenter: track.verticalCenter
    height: track.height
    radius: track.radius
    color: meter.fillColor
    width: meter.hasValue ? Math.max(track.height, track.width * Math.max(0, Math.min(1, meter.fraction))) : 0

    Behavior on width { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
  }
}
