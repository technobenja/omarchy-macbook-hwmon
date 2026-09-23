import QtQuick
import qs.Commons

// One "label ........ value" line. Values are plain text; a null reading
// arrives here already rendered as "–" by Format.js.
Item {
  id: row

  property string label: ""
  property string value: ""
  property color foreground: Color.foreground
  property color valueColor: foreground
  property string fontFamily: Style.font.family
  property real fontSize: Style.font.bodySmall

  implicitWidth: labelText.implicitWidth + valueText.implicitWidth + Style.space(8)
  implicitHeight: Math.max(labelText.implicitHeight, valueText.implicitHeight)

  Text {
    id: labelText
    textFormat: Text.PlainText
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    width: Math.max(0, row.width - valueText.implicitWidth - Style.space(8))
    text: row.label
    color: row.foreground
    opacity: 0.6
    elide: Text.ElideRight
    font.family: row.fontFamily
    font.pixelSize: row.fontSize
  }

  Text {
    id: valueText
    textFormat: Text.PlainText
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    text: row.value
    color: row.valueColor
    font.family: row.fontFamily
    font.pixelSize: row.fontSize
  }
}
