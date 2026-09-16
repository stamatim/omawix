import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  property var settings: ({})
  property bool refreshing: false
  property bool probed: false
  property bool readerAvailable: false
  property bool downloadAvailable: false
  property bool appInstalled: false
  property int bookCount: 0
  property double totalBytes: 0
  property var books: []
  property var libraryDirs: []
  property string lastError: ""

  readonly property string pluginPath: Quickshell.env("HOME") + "/.config/omarchy/plugins/io.github.stamatim.omawix"
  readonly property string statusPath: pluginPath + "/lib/status.py"
  readonly property string appPath: pluginPath + "/app/omawix"
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 60, 10, 3600)
  readonly property int maxItems: intSetting("maxItems", 8, 3, 20)
  readonly property int MAX_OUTPUT_BYTES: 1024 * 1024

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var value = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(value)) value = fallback
    return Math.max(min, Math.min(max, value))
  }

  function refresh() {
    if (statusProcess.running) return
    refreshing = true
    statusProcess.command = ["python3", statusPath, String(maxItems)]
    statusProcess.running = true
  }

  function applyStatus(raw) {
    try {
      var status = JSON.parse(String(raw || ""))
      if (!status || status.ok !== true) throw new Error(status && status.error ? status.error : "Invalid status")
      readerAvailable = status.readerAvailable === true
      downloadAvailable = status.downloadAvailable === true
      appInstalled = status.appInstalled === true
      bookCount = Math.max(0, Number(status.bookCount || 0))
      totalBytes = Math.max(0, Number(status.totalBytes || 0))
      libraryDirs = Array.isArray(status.libraryDirs) ? status.libraryDirs : []
      var sourceBooks = Array.isArray(status.books) ? status.books : []
      books = sourceBooks.slice(0, maxItems)
      lastError = Array.isArray(status.errors) && status.errors.length > 0
        ? String(status.errors[0]) : ""
      probed = true
    } catch (error) {
      lastError = String(error.message || error || "Could not read the Omawix library").slice(0, 180)
    }
  }

  function openApp() {
    if (appInstalled) Quickshell.execDetached(["omawix"])
    else Quickshell.execDetached(["python3", appPath])
  }

  function openBook(book) {
    if (!book || !book.path) return
    if (appInstalled) Quickshell.execDetached(["omawix", "--open", String(book.path)])
    else Quickshell.execDetached(["python3", appPath, "--open", String(book.path)])
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusOutput; waitForEnd: true }
    stderr: StdioCollector { id: statusError; waitForEnd: true }
    onExited: function(exitCode) {
      root.refreshing = false
      var output = String(statusOutput.text || "")
      var error = String(statusError.text || "")
      if (output.length > root.MAX_OUTPUT_BYTES) {
        root.lastError = "Omawix returned too much library data"
      } else if (exitCode === 0 || output !== "") {
        root.applyStatus(output)
      } else {
        root.lastError = String(error || "Omawix scan failed").replace(/\s+/g, " ").trim().slice(0, 180)
      }
    }
  }
}
