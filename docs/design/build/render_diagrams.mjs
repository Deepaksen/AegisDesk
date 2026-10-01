// Render a folder of Mermaid sources (*.mmd) to PNG (2x) with Mermaid in headless Chromium.
//
//   cd docs/design/build && npm install && npm run diagrams
//   node render_diagrams.mjs ../diagrams-production     # one folder (default ../diagrams)
//
// Writes <name>.png next to each source and sizes.json (pixel sizes, used by the
// Word build to keep aspect ratios).
import { readFileSync, readdirSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const here = dirname(fileURLToPath(import.meta.url));
const diagrams = resolve(here, process.argv[2] ?? "../diagrams");
const mermaidJs = readFileSync(join(here, "node_modules/mermaid/dist/mermaid.min.js"), "utf8");
const executablePath = [
  process.env.CHROMIUM_PATH,
  "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
].find((p) => p && existsSync(p));

const theme = {
  theme: "base",
  themeVariables: {
    fontFamily: "Arial, Helvetica, sans-serif",
    fontSize: "15px",
    primaryColor: "#E8F0FB",
    primaryBorderColor: "#2F5597",
    primaryTextColor: "#1F2937",
    secondaryColor: "#FFF4E5",
    tertiaryColor: "#F3F4F6",
    lineColor: "#4B5563",
    clusterBkg: "#F8FAFC",
    clusterBorder: "#94A3B8",
    actorBkg: "#E8F0FB",
    actorBorder: "#2F5597",
    noteBkgColor: "#FFF8DB",
    noteBorderColor: "#C9A227",
  },
  flowchart: { htmlLabels: true, curve: "basis", padding: 12, nodeSpacing: 40, rankSpacing: 50 },
  sequence: { mirrorActors: false, actorMargin: 20, messageMargin: 26, wrap: true, width: 160, noteMargin: 8 },
  er: { useMaxWidth: false },
  securityLevel: "loose",
  startOnLoad: false,
};

const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ deviceScaleFactor: 2, viewport: { width: 1800, height: 1200 } });
await page.setContent(`<!doctype html><html><body style="margin:0;background:#fff">
  <div id="out" style="display:inline-block;padding:16px;background:#fff"></div></body></html>`);
await page.addScriptTag({ content: mermaidJs });
await page.evaluate((cfg) => window.mermaid.initialize(cfg), theme);

const sizes = {};
const files = readdirSync(diagrams).filter((f) => f.endsWith(".mmd")).sort();
for (const file of files) {
  const name = file.replace(/\.mmd$/, "");
  const source = readFileSync(join(diagrams, file), "utf8");
  const ok = await page.evaluate(async ({ id, src }) => {
    const out = document.getElementById("out");
    out.innerHTML = "";
    try {
      const { svg } = await window.mermaid.render(id, src);
      out.innerHTML = svg;
      const el = out.querySelector("svg");
      // Natural size from the viewBox (Mermaid otherwise scales to the container).
      const vb = el.viewBox.baseVal;
      el.removeAttribute("style");
      el.setAttribute("width", String(Math.ceil(vb.width)));
      el.setAttribute("height", String(Math.ceil(vb.height)));
      return "ok";
    } catch (e) {
      return String(e);
    }
  }, { id: `d_${name.replace(/[^a-z0-9]/gi, "_")}`, src: source });
  if (ok !== "ok") {
    console.error(`FAILED ${file}: ${ok}`);
    process.exitCode = 1;
    continue;
  }
  const box = page.locator("#out");
  const png = join(diagrams, `${name}.png`);
  await box.screenshot({ path: png });
  const bb = await box.boundingBox();
  sizes[name] = { width: Math.round(bb.width), height: Math.round(bb.height) };
  console.log(`${name}.png  ${sizes[name].width}x${sizes[name].height}`);
}
writeFileSync(join(diagrams, "sizes.json"), JSON.stringify(sizes, null, 2) + "\n");
await browser.close();
