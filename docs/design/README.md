# Design document sources

Two Word documents in `docs/` are generated from this folder:

| Document | What it describes | Diagrams | Builder |
|---|---|---|---|
| `AegisDesk-High-Level-Design.docx` | The system **as built** through Milestone 11 | `diagrams/` (20) | `build/build_hld.mjs` |
| `AegisDesk-Productionisation-and-GenX-Platform-Design.docx` | The **target design** for the next stage: multi-cloud deployment with disaster recovery and one CI/CD pipeline, the enterprise integrations, and the GenX platform (a tutorial as well as a design; nothing in it is built yet) | `diagrams-production/` (36) | `build/build_production.mjs` with its text in `build/production/*.mjs` |

* `*.mmd` are the Mermaid sources of every figure (GitHub renders them too); `*.png` are the rendered figures at 2× scale, and `sizes.json` their pixel sizes.
* `build/render_diagrams.mjs` renders one folder of Mermaid sources with headless Chromium.
* `build/docx_kit.mjs` holds the shared page layout and helpers (headings, tables, figures, callout boxes, code blocks).

```bash
cd docs/design/build
npm install            # docx, mermaid, playwright-core (no browser download)
npm run all            # all diagrams, then both .docx files
npm run hld            # or one document with its diagrams
npm run production
# CHROMIUM_PATH=/path/to/chrome if Playwright's Chromium is not at /opt/pw-browsers
```

Node is used only for this documentation tooling; the application is Python.
