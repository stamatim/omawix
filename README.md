# Omawix

Omawix is an Omarchy-native ZIM library manager and reader. It combines catalog browsing, managed downloads, local storage controls, an embedded reader, and a compact Omarchy bar panel.

## Current behavior

- Searches and filters the online ZIM catalog by language, category, and size.
- Downloads archives with progress, pause, resume, retry, and cancellation controls.
- Finds existing `.zim` archives in configured folders and displays their metadata.
- Manages the download location and additional library folders.
- Moves selected local archives, including split archive parts, to the desktop trash after confirmation.
- Reads archives inside Omawix through its localhost-only embedded reader.
- Shows the local archive count, storage use, and quick-open actions in the Omarchy bar.
- Reads the active Omarchy palette and font tokens directly.

Incremental ZIM updates are not available. When a newer catalog edition is published, it is a separate full download; remove the older archive from storage when it is no longer needed.

## Requirements

Omawix targets current Omarchy on Arch Linux and requires Python 3.11 or newer. The setup script installs the runtime packages `kiwix-tools`, `aria2`, `webkitgtk-6.0`, `python-gobject`, and `gtk4` through Omarchy.

Contributors also need Git, Bash, and a supported Node.js release. The portable checks do not require GTK, Kiwix, aria2, or Omarchy.

## Install

Clone the repository into Omarchy's plugin directory, install the desktop launcher, and enable the widget:

```bash
omarchy plugin add https://github.com/stamatim/omawix.git --enable
~/.config/omarchy/plugins/io.github.stamatim.omawix/setup
```

Omarchy deliberately does not execute setup scripts when adding third-party plugins. Review `setup` before running it.

To use this checkout directly:

```bash
./setup
omawix
```

The setup script checks and installs the official Arch/Omarchy packages `kiwix-tools`, `aria2`, `webkitgtk-6.0`, `python-gobject`, and `gtk4` through `omarchy pkg add`. It verifies the WebKit 6.0 GI namespace and Gtk 4.10 or newer before installing the launcher. The launcher is linked to this checkout, so code updates apply immediately.

The dependency stack is GTK 4 and PyGObject for the application, WebKitGTK 6.0 for the embedded reader, `kiwix-serve` from `kiwix-tools` for serving a selected archive on localhost, and `aria2c` from `aria2` for resumable downloads.

## Use

- Launch **Omawix** from the application menu.
- Browse or search the catalog, then download an edition to the configured download folder.
- Pause or resume transfers, retry failed downloads, or cancel a transfer from the downloads view.
- Open a local archive to read it in the embedded reader. Omawix also handles `.zim` files opened from the desktop or with `omawix --open <path>`.
- Use storage management to choose the download folder, add existing library folders, and move unwanted archives to the desktop trash.

Active downloads are paused and their aria2 session is saved when Omawix closes. They resume from their partial data the next time Omawix launches; closing the application does not discard downloaded progress.

Catalog searches are sent to Kiwix but are not retained in Omawix's persistent offline cache. Unfiltered and structured-filter catalog pages remain cached for offline browsing.

## Panel

- Left-click the bar's **K** to open quick access.
- The panel remains a compact view of local archives and storage use; catalog browsing and download management stay in the desktop application.
- Right-click the bar icon to rescan; middle-click opens Omawix.
- In the bar panel, press `j`/`k` to move, `Enter` to open, `r` to refresh, `o` to open Omawix, and `Escape` to close.

## Files and migration

Omawix follows the XDG base directories:

- Configuration: `$XDG_CONFIG_HOME/omawix/config.json` (default `~/.config/omawix/config.json`)
- Catalog and download database: `$XDG_DATA_HOME/omawix/state.sqlite3` (default `~/.local/share/omawix/state.sqlite3`)
- Download session state: `$XDG_STATE_HOME/omawix/aria2.session` (default `~/.local/state/omawix/aria2.session`)
- Default archive download folder: `~/Kiwix`

Existing folders can be added without moving their archives. For migration, the legacy `~/.local/share/kiwix-desktop` folder remains a default scan location and is preserved by setup and removal. `~/Documents/Kiwix` and `~/Downloads` are also scanned by default.

## Widget settings

```bash
omarchy bar set io.github.stamatim.omawix refreshIntervalSec 120
omarchy bar set io.github.stamatim.omawix maxItems 12
omarchy bar move io.github.stamatim.omawix --section right
```

## Remove

Remove the desktop launcher before removing the plugin checkout:

```bash
~/.config/omarchy/plugins/io.github.stamatim.omawix/remove-app
omarchy plugin remove io.github.stamatim.omawix
```

Removal deletes only the Omawix launcher, desktop entry, and installed icon. ZIM archives, configuration, the catalog/download database, and download session state are preserved.

## Checks

```bash
./check
```

This runs Python unittest discovery, the Node model test, Python compilation, JSON validation, and shell syntax checks. It also runs `omarchy plugin validate .` when Omarchy is installed and clearly skips that check elsewhere. GitHub Actions runs the same portable checks on pushes and pull requests.

Saving files inside an installed plugin checkout hot-reloads the Omarchy shell. Use `omarchy restart shell` if a QML change does not reload cleanly.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development and pull request workflow. Report security issues through the private process in [SECURITY.md](SECURITY.md), not a public issue.

## Release process

1. Update the version in `manifest.json` and describe user-visible changes in `CHANGELOG.md`.
2. Run `./check` on an Omarchy system so the plugin validator is included.
3. Review the release diff, then create and push a matching `vX.Y.Z` tag.
4. Publish a GitHub release from that tag using the changelog entry as the release notes.

## License

MIT. Kiwix is a separate GPL-licensed project; Omawix uses the system-provided `kiwix-serve` tool without bundling its source.
