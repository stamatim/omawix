function formatBytes(bytes) {
  var value = Number(bytes || 0)
  if (!isFinite(value) || value <= 0) return "0 B"
  var units = ["B", "KB", "MB", "GB", "TB"]
  var index = 0
  while (value >= 1000 && index < units.length - 1) {
    value /= 1000
    index++
  }
  var decimals = value >= 100 || index === 0 ? 0 : (value >= 10 ? 1 : 2)
  return value.toFixed(decimals).replace(/\.0+$/, "") + " " + units[index]
}

function bookMeta(book) {
  if (!book) return ""
  var parts = []
  if (book.language) parts.push(String(book.language))
  if (book.edition) parts.push(String(book.edition))
  if (book.date) parts.push(String(book.date))
  parts.push(formatBytes(book.sizeBytes))
  return parts.join(" · ")
}

if (typeof module !== "undefined") {
  module.exports = { formatBytes: formatBytes, bookMeta: bookMeta }
}
