import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "io.github.stamatim.omawix"
  ipcTarget: "io.github.stamatim.omawix"
  manageIpc: false

  property int selectedIndex: 0
  property bool cursorActive: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  function ensureCursor() {
    if (library.books.length === 0) selectedIndex = -1
    else selectedIndex = Math.max(0, Math.min(selectedIndex, library.books.length - 1))
  }

  function moveCursor(delta) {
    cursorActive = true
    if (library.books.length === 0) return
    selectedIndex = Math.max(0, Math.min(library.books.length - 1, selectedIndex + delta))
    scrollSelectedIntoView()
  }

  function activateCursor() {
    if (selectedIndex >= 0 && selectedIndex < library.books.length)
      library.openBook(library.books[selectedIndex])
  }

  function scrollSelectedIntoView() {
    if (!bookColumn || selectedIndex < 0 || selectedIndex >= bookColumn.children.length) return
    Qt.callLater(function() {
      var item = bookColumn.children[selectedIndex]
      if (!item) return
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var margin = Style.space(8)
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (point.y < panelFlick.contentY + margin)
        panelFlick.contentY = Math.max(0, point.y - margin)
      else if (point.y + item.height > panelFlick.contentY + panelFlick.height - margin)
        panelFlick.contentY = Math.min(maxY, point.y + item.height + margin - panelFlick.height)
    })
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    library.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Service {
    id: library
    settings: root.settings
    onBooksChanged: root.ensureCursor()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { library.refresh(); return "ok" }
    function openApp(): string { library.openApp(); return "ok" }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: Component {
      Item {
        implicitWidth: Style.space(17)
        implicitHeight: Style.space(17)
        Text {
          anchors.centerIn: parent
          text: "K"
          color: root.barForeground
          font.family: root.fontFamily
          font.pixelSize: Style.font.heading
          font.bold: true
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) library.refresh()
      else if (buttonCode === Qt.MiddleButton) library.openApp()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(390))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.moveCursor(dy) }
      onActivateRequested: root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "r" || text === "R") library.refresh()
        else if (text === "o" || text === "O") library.openApp()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "Omawix"
            meta: library.bookCount === 1 ? "1 local archive" : library.bookCount + " local archives"
            detail: Model.formatBytes(library.totalBytes)
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                text: "K"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
                font.bold: true
              }
            }
          }

          Text {
            visible: library.lastError !== ""
            width: parent.width
            text: library.lastError
            textFormat: Text.PlainText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            visible: library.probed && (!library.readerAvailable || !library.downloadAvailable)
            width: parent.width
            text: !library.readerAvailable && !library.downloadAvailable
              ? "Embedded reader and downloads are unavailable. Run setup to install dependencies."
              : (!library.readerAvailable
                ? "The embedded reader is unavailable. Run setup to install dependencies."
                : "Downloads are unavailable. Run setup to install dependencies.")
            textFormat: Text.PlainText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            visible: library.probed && library.books.length === 0
            width: parent.width
            text: "No local ZIM archives. Open Omawix to browse the catalog or manage storage."
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          Column {
            id: bookColumn
            visible: library.books.length > 0
            width: parent.width
            spacing: Style.space(6)

            Repeater {
              model: library.books
              BookRow {
                required property var modelData
                required property int index
                width: bookColumn.width
                book: modelData
                rowIndex: index
              }
            }
          }

          PanelSeparator {
            visible: library.probed
            foreground: root.foreground
          }

          CursorSurface {
            visible: library.probed
            width: parent.width
            implicitHeight: appRow.implicitHeight + Style.space(12)
            foreground: root.foreground

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: library.openApp()
            }

            RowLayout {
              id: appRow
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(10)
              anchors.rightMargin: Style.space(10)
              spacing: Style.space(8)

              ColumnLayout {
                Layout.fillWidth: true
                spacing: Style.space(1)
                Text {
                  Layout.fillWidth: true
                  text: "Open Omawix"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                Text {
                  Layout.fillWidth: true
                  text: "Catalog, downloads, storage, and embedded reader"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
              PanelActionButton {
                iconText: "→"
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: library.openApp()
              }
            }
          }
        }
      }
    }
  }

  component BookRow: CursorSurface {
    id: row
    property var book: null
    property int rowIndex: 0
    hasCursor: root.cursorActive && root.selectedIndex === rowIndex
    foreground: root.foreground
    implicitHeight: rowContent.implicitHeight + Style.space(14)

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: { root.cursorActive = true; root.selectedIndex = row.rowIndex }
      onClicked: library.openBook(row.book)
    }

    RowLayout {
      id: rowContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      Text {
        text: "K"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.heading
        font.bold: true
      }
      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(1)
        Text {
          Layout.fillWidth: true
          text: row.book ? String(row.book.title || row.book.filename || "Untitled") : "Untitled"
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
        }
        Text {
          Layout.fillWidth: true
          text: Model.bookMeta(row.book)
          textFormat: Text.PlainText
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }
}
