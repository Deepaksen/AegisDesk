// Shared helpers for the Word design documents (docx-js).
//
//   const kit = createKit({ diagramsDir, headerText });
//   kit.add(kit.h1("1 Introduction"), kit.p("…"), ...kit.figure("01-x", "caption"));
//   await kit.build(outputPath, { title, description });
//
// A4 portrait with 1-inch margins, Calibri 10.5 pt, numbered figure and table captions,
// a header line and "Page X of Y" footer, and a table-of-contents field that Word fills in
// when the document is opened (updateFields).
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  AlignmentType, BorderStyle, Document, Footer, Header, HeadingLevel, ImageRun, LevelFormat,
  Packer, PageNumber, PageOrientation, Paragraph, SectionType, ShadingType, Table,
  TableCell, TableRow, TextRun, WidthType,
} from "docx";

// A4, 1-inch margins (DXA: 1440 per inch).
export const PAGE = { width: 11906, height: 16838 };
export const MARGIN = 1440;
export const CONTENT_W = PAGE.width - 2 * MARGIN; // 9026
export const PX_PER_DXA = 96 / 1440;
export const FONT = "Calibri";
export const BLUE = "1F3864";

const CALLOUTS = {
  plain: { title: "In plain words", fill: "EEF4FB", bar: "2F5597" },
  why: { title: "Why it matters", fill: "FFF4E5", bar: "C27C0E" },
  plug: { title: "How it plugs in", fill: "EAF6EE", bar: "2E7D32" },
  decision: { title: "Design decision", fill: "F3EEFA", bar: "6A3FA0" },
  caution: { title: "Validate first", fill: "FDECEC", bar: "B3261E" },
  note: { title: "Note", fill: "F3F4F6", bar: "6B7280" },
};

export function createKit({ diagramsDir, headerText }) {
  const sizes = diagramsDir ? JSON.parse(readFileSync(join(diagramsDir, "sizes.json"), "utf8")) : {};

  // -- inline text -------------------------------------------------------------------------

  /** "**bold**" and "`code`" inside a string become separate runs. */
  function runs(text, base = {}) {
    return text
      .split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
      .filter(Boolean)
      .map((part) => {
        if (part.startsWith("**")) return new TextRun({ ...base, text: part.slice(2, -2), bold: true });
        if (part.startsWith("`"))
          return new TextRun({ ...base, text: part.slice(1, -1), font: "Consolas", size: (base.size ?? 21) - 2 });
        return new TextRun({ ...base, text: part });
      });
  }

  const p = (text, opts = {}) =>
    new Paragraph({ spacing: { after: 120, line: 276 }, ...opts, children: runs(text, opts.run) });
  // A section break already starts a new page, so headings opening a section don't add one.
  const h1 = (text, pageBreak = true) =>
    new Paragraph({ heading: HeadingLevel.HEADING_1, pageBreakBefore: pageBreak, children: [new TextRun(text)] });
  const h2 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_2, keepNext: true, children: [new TextRun(text)] });
  const h3 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_3, keepNext: true, children: [new TextRun(text)] });
  const bullets = (items, level = 0) =>
    items.map((t) => new Paragraph({ numbering: { reference: "bullets", level }, spacing: { after: 60 }, children: runs(t) }));
  let listNo = 0;
  /** A numbered list that starts again at 1 (a new numbering instance per list). */
  const numbered = (items) => {
    const instance = ++listNo;
    return items.map(
      (t) => new Paragraph({ numbering: { reference: "numbers", level: 0, instance }, spacing: { after: 60 }, children: runs(t) }),
    );
  };
  const spacer = () => new Paragraph({ spacing: { after: 60 }, children: [] });

  // -- tables ------------------------------------------------------------------------------

  const border = { style: BorderStyle.SINGLE, size: 4, color: "A6B4C8" };
  const borders = { top: border, bottom: border, left: border, right: border };

  function table(headers, rows, fractions, width = CONTENT_W) {
    const cols = fractions.map((f) => Math.floor(width * f));
    cols[cols.length - 1] = width - cols.slice(0, -1).reduce((a, b) => a + b, 0);
    const cell = (text, i, header, last = false) =>
      new TableCell({
        width: { size: cols[i], type: WidthType.DXA },
        borders,
        shading: header ? { fill: "DCE6F2", type: ShadingType.CLEAR, color: "auto" } : undefined,
        margins: { top: 60, bottom: 60, left: 100, right: 100 },
        // keepNext on the last row keeps the table together with its caption below.
        children: [new Paragraph({ spacing: { after: 0 }, keepNext: last || undefined, children: runs(text, { size: 18, bold: header || undefined }) })],
      });
    return new Table({
      width: { size: width, type: WidthType.DXA },
      columnWidths: cols,
      rows: [
        new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, i, true)) }),
        ...rows.map((r, n) => new TableRow({ cantSplit: true, children: r.map((c, i) => cell(c, i, false, n === rows.length - 1)) })),
      ],
    });
  }
  let tableNo = 0;
  // Captions follow their table.
  const tableCaption = (text) =>
    new Paragraph({
      spacing: { before: 120, after: 80 },
      children: [new TextRun({ text: `Table ${++tableNo}: ${text}`, italics: true, size: 18, color: "44546A" })],
    });

  /** A shaded one-cell box with a coloured bar on the left: kind is a key of CALLOUTS. */
  function callout(kind, body, title) {
    const style = CALLOUTS[kind];
    if (!style) throw new Error(`unknown callout ${kind}`);
    const paras = (Array.isArray(body) ? body : [body]).map((t, i, all) =>
      t.startsWith("• ")
        ? new Paragraph({ spacing: { after: 40 }, indent: { left: 280, hanging: 200 }, children: runs(`•  ${t.slice(2)}`, { size: 20 }) })
        : new Paragraph({ spacing: { after: i === all.length - 1 ? 0 : 80, line: 264 }, children: runs(t, { size: 20 }) }),
    );
    const none = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
    return [
      new Table({
        width: { size: CONTENT_W, type: WidthType.DXA },
        columnWidths: [CONTENT_W],
        rows: [
          new TableRow({
            cantSplit: paras.length < 8,
            children: [
              new TableCell({
                width: { size: CONTENT_W, type: WidthType.DXA },
                shading: { fill: style.fill, type: ShadingType.CLEAR, color: "auto" },
                borders: { top: none, bottom: none, right: none, left: { style: BorderStyle.SINGLE, size: 24, color: style.bar } },
                margins: { top: 100, bottom: 100, left: 180, right: 160 },
                children: [
                  new Paragraph({
                    spacing: { after: 60 },
                    keepNext: true,
                    children: [new TextRun({ text: title ?? style.title, bold: true, size: 20, color: style.bar })],
                  }),
                  ...paras,
                ],
              }),
            ],
          }),
        ],
      }),
      new Paragraph({ spacing: { after: 120 }, children: [] }),
    ];
  }

  /** A grey monospaced block; leading spaces are kept. */
  function code(text) {
    const lines = text.replace(/\n$/, "").split("\n");
    return [
      new Table({
        width: { size: CONTENT_W, type: WidthType.DXA },
        columnWidths: [CONTENT_W],
        rows: [
          new TableRow({
            cantSplit: true,
            children: [
              new TableCell({
                width: { size: CONTENT_W, type: WidthType.DXA },
                shading: { fill: "F5F6F8", type: ShadingType.CLEAR, color: "auto" },
                borders,
                margins: { top: 80, bottom: 80, left: 140, right: 140 },
                children: lines.map(
                  (line) =>
                    new Paragraph({
                      spacing: { after: 0, line: 240 },
                      children: [new TextRun({ text: line || " ", font: "Consolas", size: 16 })],
                    }),
                ),
              }),
            ],
          }),
        ],
      }),
      new Paragraph({ spacing: { after: 120 }, children: [] }),
    ];
  }

  // -- figures -----------------------------------------------------------------------------

  let figureNo = 0;
  function figure(name, caption, { maxW = CONTENT_W * PX_PER_DXA, maxH = 800 } = {}) {
    const size = sizes[name];
    if (!size) throw new Error(`no rendered diagram ${name}`);
    const scale = Math.min(maxW / size.width, maxH / size.height, 1);
    return [
      new Paragraph({
        alignment: AlignmentType.CENTER,
        keepNext: true,
        spacing: { before: 120, after: 60 },
        children: [
          new ImageRun({
            type: "png",
            data: readFileSync(join(diagramsDir, `${name}.png`)),
            transformation: { width: Math.round(size.width * scale), height: Math.round(size.height * scale) },
            altText: { title: caption, description: caption, name },
          }),
        ],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 200 },
        children: [new TextRun({ text: `Figure ${++figureNo}: ${caption}`, italics: true, size: 18, color: "44546A" })],
      }),
    ];
  }

  // -- sections (portrait flow, with landscape pages for wide figures) ----------------------

  const sections = [];
  let current = [];
  function add(...items) {
    for (const item of items.flat()) current.push(item);
  }
  function landscape(...items) {
    sections.push({ orientation: "portrait", children: current });
    sections.push({ orientation: "landscape", children: items.flat() });
    current = [];
  }

  // -- document ----------------------------------------------------------------------------

  const header = () =>
    new Header({
      children: [
        new Paragraph({
          alignment: AlignmentType.RIGHT,
          border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: "A6B4C8", space: 4 } },
          children: [new TextRun({ text: headerText, size: 16, color: "7F7F7F" })],
        }),
      ],
    });
  const footer = () =>
    new Footer({
      children: [
        new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({ children: ["Page ", PageNumber.CURRENT, " of ", PageNumber.TOTAL_PAGES], size: 16, color: "7F7F7F" }),
          ],
        }),
      ],
    });

  async function build(output, { title, description }) {
    sections.push({ orientation: "portrait", children: current });
    current = [];
    const doc = new Document({
      creator: "AegisDesk",
      title,
      description,
      features: { updateFields: true },
      styles: {
        default: { document: { run: { font: FONT, size: 21 } } },
        paragraphStyles: [
          { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
            run: { size: 34, bold: true, color: BLUE, font: FONT }, paragraph: { spacing: { before: 240, after: 160 }, outlineLevel: 0 } },
          { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
            run: { size: 27, bold: true, color: "2F5597", font: FONT }, paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 } },
          { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
            run: { size: 23, bold: true, color: "404040", font: FONT }, paragraph: { spacing: { before: 180, after: 80 }, outlineLevel: 2 } },
        ],
      },
      numbering: {
        config: [
          { reference: "bullets", levels: [
            { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 270 } } } },
            { level: 1, format: LevelFormat.BULLET, text: "◦", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 1080, hanging: 270 } } } },
          ] },
          { reference: "numbers", levels: [
            { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 300 } } } },
          ] },
        ],
      },
      sections: sections
        .filter((s) => s.children.length)
        .map((s, i) => ({
          properties: {
            type: i === 0 ? undefined : SectionType.NEXT_PAGE,
            page: {
              size: { width: PAGE.width, height: PAGE.height, orientation: s.orientation === "landscape" ? PageOrientation.LANDSCAPE : PageOrientation.PORTRAIT },
              margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN },
            },
          },
          headers: { default: header() },
          footers: { default: footer() },
          children: s.children,
        })),
    });
    writeFileSync(output, await Packer.toBuffer(doc));
    return { figures: figureNo, tables: tableNo, sections: sections.filter((s) => s.children.length).length };
  }

  return {
    runs, p, h1, h2, h3, bullets, numbered, spacer, table, tableCaption, callout, code, figure, add, landscape, build,
  };
}
