# Design document sources

`docs/AegisDesk-High-Level-Design.docx` is generated from this folder:

* `diagrams/*.mmd`: Mermaid sources of every figure (GitHub renders them too); `*.png` are the rendered figures at 2× scale, and `sizes.json` their pixel sizes.
* `build/render_diagrams.mjs`: renders the Mermaid sources with headless Chromium.
* `build/build_hld.mjs`: writes the Word document with `docx` (all text lives here).

```bash
cd docs/design/build
npm install            # docx, mermaid, playwright-core (no browser download)
npm run all            # diagrams, then the .docx
# CHROMIUM_PATH=/path/to/chrome if Playwright's Chromium is not at /opt/pw-browsers
```

Node is used only for this documentation tooling; the application is Python.
