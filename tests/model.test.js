const assert = require("node:assert/strict")
const Model = require("../Model.js")

assert.equal(Model.formatBytes(0), "0 B")
assert.equal(Model.formatBytes(1_500_000_000), "1.50 GB")
assert.equal(
  Model.bookMeta({ language: "English", edition: "Full", date: "2025-01", sizeBytes: 2_000_000 }),
  "English · Full · 2025-01 · 2 MB"
)
