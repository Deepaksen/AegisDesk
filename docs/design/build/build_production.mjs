// Build docs/AegisDesk-Productionisation-and-GenX-Platform-Design.docx (docx-js).
//
//   cd docs/design/build && npm install && npm run production
//
// A target design (not yet built): productionisation, multi-cloud deployment with disaster
// recovery, enterprise integrations, and the GenX platform. Figures come from
// ../diagrams-production/*.png. Vendor facts come from public sources listed in the last
// section and must be validated in proofs of concept.
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { HeadingLevel, PageBreak, Paragraph, TableOfContents, TextRun } from "docx";
import { BLUE, CONTENT_W, FONT, PX_PER_DXA, createKit } from "./docx_kit.mjs";
import { content } from "./production_content.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const output = resolve(here, "..", "..", "AegisDesk-Productionisation-and-GenX-Platform-Design.docx");
const kit = createKit({
  diagramsDir: resolve(here, "..", "diagrams-production"),
  headerText: "AegisDesk · Productionisation, Cloud Integration and GenX Platform · v1.0",
});

// -- title page ----------------------------------------------------------------------------
kit.add(
  new Paragraph({ spacing: { before: 2000, after: 200 }, children: [new TextRun({ text: "AegisDesk", size: 64, bold: true, color: BLUE, font: FONT })] }),
  new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: "Productionisation, Cloud Integration and the GenX Platform", size: 36, color: "2F5597" })] }),
  new Paragraph({ spacing: { after: 700 }, children: [new TextRun({ text: "Design document and tutorial", size: 40, color: "404040" })] }),
  kit.p("How the AegisDesk prototype becomes a production service on Google Cloud, AWS and Azure with disaster recovery and one delivery pipeline; how it integrates with the enterprise’s agent, identity, security, observability, FinOps and governance products; and how its parts become GenX, a reusable platform for every agentic use case in the enterprise.", { run: { size: 24, color: "404040" } }),
  kit.spacer(),
  kit.table(
    ["Item", "Value"],
    [
      ["Document", "AegisDesk Productionisation, Cloud Integration and GenX Platform Design"],
      ["Version", "1.0"],
      ["Date", "30 September 2026"],
      ["Status", "Draft for review. A **target design**: nothing in it is built yet"],
      ["Builds on", "AegisDesk High-Level Design v1.0 (`docs/AegisDesk-High-Level-Design.docx`): the system as built through Milestone 11"],
      ["Scope", "Phase 1 (productionise, multi-cloud, DR, CI/CD), Phase 2 (enterprise integrations), Phase 3 (GenX platform)"],
      ["Vendor facts", "From public vendor documentation and announcements available in September 2026 (section 27). Every vendor capability must be confirmed in a proof of concept before it is relied on"],
    ],
    [0.22, 0.78],
  ),
  new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("Contents")] }),
  new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
  kit.p("If the table of contents is empty, right-click it in Word and choose Update Field.", { run: { italics: true, size: 18, color: "7F7F7F" } }),
);

content(kit, { landscapeMaxW: (16838 - 2 * 1440) * PX_PER_DXA, portraitMaxW: CONTENT_W * PX_PER_DXA });

const stats = await kit.build(output, {
  title: "AegisDesk Productionisation, Cloud Integration and GenX Platform Design",
  description: "Target design: multi-cloud production deployment with DR and CI/CD, enterprise integrations, and the GenX agentic platform",
});
console.log(`wrote ${output} (${stats.figures} figures, ${stats.tables} captioned tables, ${stats.sections} sections)`);
