# Changelog

## 0.1.0 – 2026-10-09

First release. Tested with ChatGPT extension 1.26.901.11451, Arc 1.167.1 and ChatGPT desktop app 26.1002.

- `build` downloads the ChatGPT extension from the Chrome Web Store, checks its publisher signature, applies the patches, and writes a build that Arc can load.
- Patches: `skip-tab-groups` (required) and `arc-display-name` (optional).
- `fetch` writes the unpatched extension, for updating a patch after an upstream change.
- `doctor` checks Arc, the native connector, the build, Developer mode, and the latest store version.
- `uninstall` deletes the build.
