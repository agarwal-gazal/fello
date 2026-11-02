// Rebuild: node deck/build.js (needs pptxgenjs and the pptx skill scripts; planted.json is parsed from scoring/PLANTED_CASES.txt)
// Builds gtm-health.pptx. All figures are copied from model/BACKTEST.txt, resolve/EVALUATION.txt, scoring/PLANTED_CASES.txt.
const pptxgen = require("pptxgenjs");
const fs = require("fs");
const SKILL = "/root/.claude/skills/synced/76639506-e08c-45ba-8848-ef1387f55148_ecb70f58-a8eb-4b93-943e-5867eed5f53e/pptx";
const { applyTheme } = require(SKILL + "/scripts/apply_theme.js");
const planted = JSON.parse(fs.readFileSync(process.env.PLANTED || __dirname + "/planted.json", "utf8"));

const THEME = {
  name: "CSM Priority", headFontFace: "Cambria", bodyFontFace: "Calibri",
  colors: { dk1: "1B2A30", lt1: "FFFFFF", dk2: "0B3C49", lt2: "EAF2F3", accent1: "0B3C49", accent2: "F26B3A",
            accent3: "2A9D8F", accent4: "8AA3AB", accent5: "E9B44C", accent6: "6B7F86", hlink: "0B3C49", folHlink: "6B7F86" },
};
const HEX = { teal: "0B3C49", orange: "F26B3A", green: "2A9D8F", grey: "8AA3AB", amber: "E9B44C", slate: "6B7F86", ink: "1B2A30", pale: "EAF2F3" };

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9"; // 10 x 5.625
pres.title = "Weekly CSM priority list"; pres.author = "Gazal Agarwal";
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
const C = pres.SchemeColor;

pres.defineSlideMaster({
  title: "DARK", background: { color: C.text2 },
  objects: [],
  slideNumber: { x: 9.2, y: 5.25, w: 0.5, h: 0.25, fontSize: 10, color: C.accent4, align: "right" },
});
pres.defineSlideMaster({
  title: "CONTENT", background: { color: C.background1 },
  objects: [{ placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9, h: 0.8, fontSize: 30, bold: true, color: C.text2, valign: "middle", align: "left", margin: 0 }, text: "" } }],
  slideNumber: { x: 9.2, y: 5.25, w: 0.5, h: 0.25, fontSize: 10, color: C.accent6, align: "right" },
});

const shadow = () => ({ type: "outer", color: "000000", blur: 6, offset: 2, angle: 90, opacity: 0.12 });
const chartBase = () => ({
  catAxisLabelColor: HEX.slate, valAxisLabelColor: HEX.slate, catAxisLabelFontFace: "+mn-lt", valAxisLabelFontFace: "+mn-lt",
  catAxisLabelFontSize: 12, valAxisLabelFontSize: 12, valGridLine: { color: "D5E0E3", size: 0.5 }, catGridLine: { style: "none" },
  legendFontFace: "+mn-lt", legendFontSize: 12, legendColor: HEX.ink, titleFontFace: "+mn-lt", titleFontSize: 14, titleColor: HEX.ink,
  dataLabelFontFace: "+mn-lt", dataLabelFontSize: 12, dataLabelColor: HEX.ink,
});

// ---------- 1. Title
{
  const s = pres.addSlide({ masterName: "DARK" });
  s.addText("Who should each CSM call this week, and why", { x: 0.6, y: 1.2, w: 5.4, h: 1.9, fontFace: "Cambria", fontSize: 34, bold: true, color: "FFFFFF", margin: 0, valign: "top", isTextBox: true, objectName: "Title" });
  s.addText("A ranked, explained worklist from five messy systems, instead of a colour dashboard", { x: 0.6, y: 3.25, w: 5.2, h: 0.8, fontSize: 16, color: "CADCE0", margin: 0, valign: "top", isTextBox: true, objectName: "Subtitle" });
  s.addText("Gazal Agarwal  ·  Staff Engineer, GTM AI Team take-home", { x: 0.6, y: 4.7, w: 5.5, h: 0.35, fontSize: 12, color: "8AA3AB", margin: 0, isTextBox: true, objectName: "Byline" });
  // motif: the ranked list itself
  const rows = [["Lagoon Dynamics", "red", 0.82], ["Keystone Point", "red", 0.80], ["Willow Stack", "red", 0.78], ["Osprey Retail", "yellow", 0.26], ["Cedar Row", "yellow", 0.21], ["Harbor Lane", "green", 0.07]];
  const col = { red: HEX.orange, yellow: HEX.amber, green: HEX.green };
  s.addShape(pres.ShapeType.roundRect, { x: 6.2, y: 0.9, w: 3.3, h: 3.75, fill: { color: "114E5E" }, rectRadius: 0.12, line: { color: "114E5E" }, objectName: "List card" });
  s.addText("My week", { x: 6.45, y: 1.05, w: 2.5, h: 0.35, fontSize: 14, bold: true, color: "FFFFFF", margin: 0, isTextBox: true, objectName: "List heading" });
  rows.forEach((r, i) => {
    const y = 1.55 + i * 0.5;
    s.addShape(pres.ShapeType.ellipse, { x: 6.45, y: y + 0.09, w: 0.2, h: 0.2, fill: { color: col[r[1]] }, line: { color: col[r[1]] }, objectName: "Band dot " + (i + 1) });
    s.addText(r[0], { x: 6.8, y, w: 1.6, h: 0.38, fontSize: 12, color: "FFFFFF", margin: 0, valign: "middle", isTextBox: true, objectName: "Row name " + (i + 1) });
    s.addShape(pres.ShapeType.rect, { x: 8.4, y: y + 0.12, w: 0.9 * r[2] / 0.82, h: 0.14, fill: { color: col[r[1]] }, line: { color: col[r[1]] }, objectName: "Risk bar " + (i + 1) });
  });
  s.addNotes("One line: the request was a red/yellow/green dashboard plus automated emails. I read the real need as a short ranked list per CSM with the reasons attached. The list on the right is illustrative, not real account output.");
}

// ---------- 2. Reframe
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("The need is a ranked list, not a colour", { placeholder: "title" });
  s.addText([
    { text: "A CSM with about 120 accounts cannot tell which five to call this week, or why.", options: { breakLine: true, bold: true, color: C.text2 } },
    { text: "Two renewals were lost with the warning signs already in Zendesk and product data. Nobody had joined them to the account or put them in front of a person in time.", options: { breakLine: true } },
    { text: " ", options: { breakLine: true, fontSize: 8 } },
    { text: "So I build a weekly pipeline that ends in a ranked worklist with reasons. Colours stay, only as a summary. Customer emails are drafted for approval, never sent by the system.", options: {} },
  ], { x: 0.5, y: 1.3, w: 9, h: 1.7, fontSize: 16, color: C.text1, valign: "top", margin: 0, paraSpaceAfter: 6, isTextBox: true, objectName: "Argument" });
  const stats = [
    ["49%", "of each CSM's top 10 really churned or downgraded", "vs 18% by chance, 34% for a simple rule"],
    ["0", "wrong links out of about 61,000 records", "5,127 CRM records resolve to exactly 5,000 accounts"],
    ["9 / 7", "weeks out the two lost accounts turn red", "lost_B at 9, lost_A at 7; both decoys never go red"],
  ];
  stats.forEach((st, i) => {
    const x = 0.5 + i * 3.05;
    s.addShape(pres.ShapeType.roundRect, { x, y: 3.2, w: 2.9, h: 1.85, fill: { color: C.background2 }, line: { color: C.background2 }, rectRadius: 0.1, shadow: shadow(), objectName: "Stat card " + (i + 1) });
    s.addText(st[0], { x: x + 0.2, y: 3.28, w: 2.5, h: 0.7, fontFace: "Cambria", fontSize: 40, bold: true, color: i === 2 ? C.accent2 : C.text2, margin: 0, isTextBox: true, objectName: "Stat value " + (i + 1) });
    s.addText(st[1], { x: x + 0.2, y: 3.98, w: 2.5, h: 0.55, fontSize: 14, bold: true, color: C.text1, margin: 0, valign: "top", isTextBox: true, objectName: "Stat label " + (i + 1) });
    s.addText(st[2], { x: x + 0.2, y: 4.5, w: 2.5, h: 0.45, fontSize: 12, color: C.accent6, margin: 0, valign: "top", isTextBox: true, objectName: "Stat note " + (i + 1) });
  });
  s.addNotes("Numbers: model/BACKTEST.txt (top-10 precision 48.9% vs 18.4% chance, 34.3% rule), resolve/EVALUATION.txt, scoring/PLANTED_CASES.txt. All on synthetic data that I generated, with an answer key I wrote. Say that.");
}

// ---------- 3. Architecture
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Raw exports in, reviewed drafts out", { placeholder: "title" });
  const row1 = [["raw", "verbatim JSON, idempotent, keeps arrival time"], ["stg", "deduplicated, test and internal rows flagged"], ["er", "one account id, method and confidence per link"], ["core", "facts keyed by account, labels from Stripe"], ["features", "only what was known that morning"]];
  const row2 = [["model", "logistic regression, walk-forward backtest"], ["score", "risk, reasons, alerts, full inputs stored"], ["agent", "Monday run, changes, drafts pending only"], ["ui", "My week, account, approval queue"], ["isolation", "tenant_id in every key, row-level security"]];
  const box = (r, list, y, accentIdx) => list.forEach((b, i) => {
    const x = 0.5 + i * 1.82;
    const dark = i !== accentIdx;
    s.addShape(pres.ShapeType.roundRect, { x, y, w: 1.62, h: 1.55, fill: { color: dark ? C.accent1 : C.accent2 }, line: { color: dark ? C.accent1 : C.accent2 }, rectRadius: 0.08, shadow: shadow(), objectName: "Box " + b[0] });
    s.addText(b[0], { x: x + 0.1, y: y + 0.1, w: 1.42, h: 0.4, fontFace: "Cambria", fontSize: 18, bold: true, color: "FFFFFF", margin: 0, isTextBox: true, objectName: "Box title " + b[0] });
    s.addText(b[1], { x: x + 0.1, y: y + 0.55, w: 1.42, h: 0.9, fontSize: 12, color: "FFFFFF", margin: 0, valign: "top", isTextBox: true, objectName: "Box text " + b[0] });
    if (i < list.length - 1) s.addText("›", { x: x + 1.62, y: y + 0.5, w: 0.2, h: 0.5, fontSize: 20, bold: true, color: C.accent4, align: "center", margin: 0, isTextBox: true, objectName: "Arrow " + b[0] });
  });
  box(1, row1, 1.3, 2); box(2, row2, 3.1, 1);
  s.addText("The answer key (ground_truth) is read only by evaluation. The pipeline never sees it.", { x: 0.5, y: 4.85, w: 8.2, h: 0.3, fontSize: 12, color: C.accent6, margin: 0, isTextBox: true, objectName: "Footnote" });
  s.addNotes("Orange boxes are where the hard judgment sits: entity resolution (er) and the scoring store. VERIFY before sending: the agent, ui and isolation (row-level security) boxes describe the agreed contract; check they are built and running.");
}

// ---------- 4. Data and entity resolution
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Joining the systems: zero wrong links", { placeholder: "title" });
  const hdr = (t, a = "left") => ({ text: t, options: { bold: true, color: "FFFFFF", fill: { color: C.accent1 }, fontSize: 14, align: a } });
  const cell = (t, o = {}) => ({ text: t, options: Object.assign({ fontSize: 14, color: C.text1 }, o) });
  const rows = [[hdr("Source records"), hdr("Linked", "right"), hdr("Wrong", "right")],
    ["Stripe customers", "100.0%", "0"], ["Zendesk organisations", "100.0%", "0"], ["Product workspaces", "100.0%", "0"],
    ["Gmail messages", "99.1%", "0"], ["Zendesk tickets", "95.6%", "0"]].map((r, i) => i === 0 ? r : r.map((t, j) => cell(t, { align: j ? "right" : "left", fill: { color: i % 2 ? C.background2 : C.background1 } })));
  s.addTable(rows, { x: 0.5, y: 1.3, w: 4.9, colW: [2.7, 1.1, 1.1], rowH: 0.46, border: { type: "none" }, margin: [0, 0.1, 0, 0.1], objectName: "Linking table" });
  s.addText("Scored against an answer key the pipeline never reads. 0 of 2,333 test-mode and internal records were linked.", { x: 0.5, y: 4.15, w: 4.9, h: 0.7, fontSize: 12, color: C.accent6, margin: 0, valign: "top", isTextBox: true, objectName: "Table note" });
  const facts = [
    ["38%", "of tickets carry no organisation, and exact names link only 61% of Stripe customers"],
    ["5,127 → 5,000", "CRM records become exactly 5,000 accounts; 29 look-alike name pairs, none merged"],
    ["4%", "of tickets left unlinked on purpose: personal email addresses with no known person"],
  ];
  facts.forEach((f, i) => {
    const y = 1.3 + i * 1.2;
    s.addShape(pres.ShapeType.roundRect, { x: 5.8, y, w: 3.7, h: 1.06, fill: { color: C.background2 }, line: { color: C.background2 }, rectRadius: 0.08, objectName: "Fact card " + (i + 1) });
    s.addText(f[0], { x: 5.95, y: y + 0.05, w: 3.4, h: 0.4, fontFace: "Cambria", fontSize: 20, bold: true, color: i === 2 ? C.accent2 : C.text2, margin: 0, isTextBox: true, objectName: "Fact value " + (i + 1) });
    s.addText(f[1], { x: 5.95, y: y + 0.45, w: 3.4, h: 0.58, fontSize: 12, color: C.text1, margin: 0, valign: "top", isTextBox: true, objectName: "Fact text " + (i + 1) });
  });
  s.addNotes("Wrong is worse than missing: a bad link puts one account's tickets on another account's features; an unlinked ticket only lowers coverage. Personal addresses are capped at 0.75 confidence so company evidence overrules them. Caveat: rules tuned on seed 7, re-checked on unseen seed 11 with the same result. Synthetic key, written by me.");
}

// ---------- 5. Model and backtest
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Modest AUC, honest calibration", { placeholder: "title" });
  s.addChart(pres.charts.BAR, [
    { name: "Model", labels: ["4", "8", "12", "16", "24"], values: [0.710, 0.689, 0.664, 0.642, 0.597] },
    { name: "Tickets-only rule", labels: ["4", "8", "12", "16", "24"], values: [0.600, 0.587, 0.561, 0.542, 0.529] },
  ], Object.assign(chartBase(), { x: 0.4, y: 1.2, w: 4.6, h: 3.1, barDir: "col", barGrouping: "clustered", chartColors: [HEX.teal, HEX.grey],
    showTitle: true, title: "AUC by weeks before renewal", showLegend: true, legendPos: "b", showValue: true, dataLabelFormatCode: "0.00", dataLabelPosition: "outEnd",
    valAxisMinVal: 0.5, valAxisMaxVal: 0.8, valAxisMajorUnit: 0.1, valAxisLabelFormatCode: "0.0", showCatAxisTitle: true, catAxisTitle: "Weeks before renewal", catAxisTitleColor: HEX.slate, catAxisTitleFontSize: 12, catAxisTitleFontFace: "+mn-lt", barGapWidthPct: 60 }));
  s.addChart(pres.charts.LINE, [
    { name: "Predicted", labels: ["0–0.1", "0.1–0.2", "0.2–0.3", "0.3–0.4", "0.4–0.5", "0.5+"], values: [0.068, 0.146, 0.237, 0.346, 0.446, 0.630] },
    { name: "Actual", labels: ["0–0.1", "0.1–0.2", "0.2–0.3", "0.3–0.4", "0.4–0.5", "0.5+"], values: [0.082, 0.142, 0.226, 0.361, 0.462, 0.607] },
  ], Object.assign(chartBase(), { x: 5.1, y: 1.2, w: 4.5, h: 3.1, chartColors: [HEX.grey, HEX.orange], lineSize: 3, lineDataSymbolSize: 7,
    showTitle: true, title: "Calibration at 8 weeks out", showLegend: true, legendPos: "b", valAxisMinVal: 0, valAxisMaxVal: 0.7, valAxisMajorUnit: 0.1, valAxisLabelFormatCode: "0.0",
    showCatAxisTitle: true, catAxisTitle: "Predicted risk bucket", catAxisTitleColor: HEX.slate, catAxisTitleFontSize: 12, catAxisTitleFontFace: "+mn-lt" }));
  s.addText([
    { text: "Pooled AUC 0.647 ", options: { bold: true, color: C.text2 } }, { text: "over three walk-forward folds. ", options: {} },
    { text: "Calibration error 0.016. ", options: { bold: true, color: C.text2 } },
    { text: "In each CSM's top 10 (8 weeks out): 49% real, vs 34% for the rule and 18% by chance. The data's ceiling is 0.81.", options: {} },
  ], { x: 0.5, y: 4.45, w: 9, h: 0.75, fontSize: 14, color: C.text1, margin: 0, valign: "top", isTextBox: true, objectName: "Takeaway" });
  s.addNotes("Train on renewals completed before each 6-month fold, test on the next six months; never a random split. Folds: 0.612, 0.652, 0.665. Boosting was within noise (0.647 vs 0.646 in the last two folds) so the explainable logistic model wins. The y-axis starts at 0.5 because that is chance for AUC. Weakest segment SMB 0.651; first fold weak with one year of history.");
}

// ---------- 6. Lost renewals
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("The two lost renewals, scored every Monday", { placeholder: "title" });
  const weeks = []; for (let w = 25; w >= 2; w--) weeks.push(w);
  const ser = (key) => weeks.map(w => (planted[key][String(w)] ? planted[key][String(w)][0] : null));
  const labels = weeks.map(String);
  s.addChart(pres.charts.LINE, [
    { name: "lost_B", labels, values: ser("lost_B") }, { name: "lost_A", labels, values: ser("lost_A") },
    { name: "decoy, declining then saved", labels, values: ser("decoy_declining_renewed") },
    { name: "decoy, quiet", labels, values: ser("decoy_quiet_renewed") },
    { name: "red threshold (0.30)", labels, values: weeks.map(() => 0.30) },
  ], Object.assign(chartBase(), { x: 0.4, y: 1.15, w: 5.9, h: 3.9, chartColors: [HEX.teal, HEX.orange, HEX.grey, "B7C6CA", HEX.amber], lineSize: 2.5, lineDataSymbol: "none",
    lineDash: ["solid", "solid", "solid", "solid", "dash"], showLegend: true, legendPos: "b", valAxisMinVal: 0, valAxisMaxVal: 0.7, valAxisMajorUnit: 0.1, valAxisLabelFormatCode: "0.0",
    showValAxisTitle: true, valAxisTitle: "Priority (risk, lifted by alerts)", valAxisTitleColor: HEX.slate, valAxisTitleFontSize: 12, valAxisTitleFontFace: "+mn-lt",
    showCatAxisTitle: true, catAxisTitle: "Weeks before renewal", catAxisTitleColor: HEX.slate, catAxisTitleFontSize: 12, catAxisTitleFontFace: "+mn-lt", catAxisLabelFontSize: 10 }));
  const cards = [
    ["lost_B", "Top 10 from 11 weeks out, red from 9.", "Champion left, emails bounced, workspace name unrelated to the CRM name."],
    ["lost_A", "Top 10 and red from 7 weeks. Target was 8.", "Its cancel ticket was filed 8 weeks and 1 day out. The first Monday that could act on it was 7 weeks out."],
  ];
  cards.forEach((c, i) => {
    const y = 1.15 + i * 2.0;
    s.addShape(pres.ShapeType.roundRect, { x: 6.5, y, w: 3.1, h: 1.85, fill: { color: C.background2 }, line: { color: C.background2 }, rectRadius: 0.08, objectName: "Case card " + c[0] });
    s.addText(c[0], { x: 6.65, y: y + 0.08, w: 2.8, h: 0.35, fontFace: "Cambria", fontSize: 18, bold: true, color: i ? C.accent2 : C.text2, margin: 0, isTextBox: true, objectName: "Case name " + c[0] });
    s.addText(c[1], { x: 6.65, y: y + 0.45, w: 2.8, h: 0.55, fontSize: 14, bold: true, color: C.text1, margin: 0, valign: "top", isTextBox: true, objectName: "Case result " + c[0] });
    s.addText(c[2], { x: 6.65, y: y + 1.0, w: 2.8, h: 0.8, fontSize: 12, color: C.text1, margin: 0, valign: "top", isTextBox: true, objectName: "Case why " + c[0] });
  });
  s.addNotes("Model trained only on renewals finished before 2026-04-13, before either loss. Priority = model risk, lifted to the floor of any event alert (a cancellation ticket sets 0.40, from a measured 41.6% churn or downgrade rate, n=154). The model alone gave lost_A only 0.19 that Monday; I did not retune it to that account. Earlier decline was invisible: no inbound email for 255 days, 3 of its 14 tickets came from personal addresses and stayed unlinked. Both decoys never go red.");
}

// ---------- 7. 10x and skipped
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("What breaks at 10x, and what I left out", { placeholder: "title" });
  const left = [
    ["1", "Per-account feature loop", "Builds events-by-snapshots matrices; the largest accounts run out of memory first."],
    ["2", "Scoring loads every fact table", "Fine at 125k product events; needs SQL-side aggregation."],
    ["3", "Full rebuild every run", "About 80 s today, about 13 min if linear. Needs incremental builds."],
  ];
  s.addText("Breaks first (estimates; only 1x was measured)", { x: 0.5, y: 1.2, w: 4.6, h: 0.35, fontSize: 16, bold: true, color: C.text2, margin: 0, isTextBox: true, objectName: "Left heading" });
  left.forEach((l, i) => {
    const y = 1.65 + i * 1.18;
    s.addShape(pres.ShapeType.ellipse, { x: 0.5, y: y + 0.05, w: 0.42, h: 0.42, fill: { color: C.accent2 }, line: { color: C.accent2 }, objectName: "Number " + l[0] });
    s.addText(l[0], { x: 0.5, y: y + 0.05, w: 0.42, h: 0.42, fontSize: 14, bold: true, color: "FFFFFF", align: "center", valign: "middle", margin: 0, isTextBox: true, objectName: "Number text " + l[0] });
    s.addText(l[1], { x: 1.05, y, w: 4.0, h: 0.32, fontSize: 14, bold: true, color: C.text1, margin: 0, isTextBox: true, objectName: "Item title " + l[0] });
    s.addText(l[2], { x: 1.05, y: y + 0.32, w: 4.0, h: 0.75, fontSize: 14, color: C.text1, margin: 0, valign: "top", isTextBox: true, objectName: "Item text " + l[0] });
  });
  s.addShape(pres.ShapeType.roundRect, { x: 5.5, y: 1.2, w: 4.0, h: 3.85, fill: { color: C.background2 }, line: { color: C.background2 }, rectRadius: 0.1, objectName: "Skipped card" });
  s.addText("Skipped on purpose", { x: 5.75, y: 1.35, w: 3.5, h: 0.35, fontSize: 16, bold: true, color: C.text2, margin: 0, isTextBox: true, objectName: "Right heading" });
  s.addText([
    { text: "Per-tenant models and thresholds", options: { bullet: true, breakLine: true } },
    { text: "Uncertainty intervals", options: { bullet: true, breakLine: true } },
    { text: "Feedback from approved and rejected drafts", options: { bullet: true, breakLine: true } },
    { text: "Drift and calibration monitoring", options: { bullet: true, breakLine: true } },
    { text: "Involuntary churn, win-backs, GDPR deletions", options: { bullet: true, breakLine: true } },
    { text: "Red and yellow cut-offs are my capacity guess; the VP should set them", options: { bullet: true } },
  ], { x: 5.75, y: 1.8, w: 3.5, h: 3.1, fontSize: 14, color: C.text1, margin: 0, valign: "top", paraSpaceAfter: 8, isTextBox: true, objectName: "Skipped list" });
  s.addNotes("Also on the 10x list: single scheduler process with no queue, so agent runs must be idempotent; one global model needs per-tenant recalibration at a hundred tenants; the canonical account id is the earliest-created Salesforce record, which could change if an earlier duplicate arrives. VERIFY the agent and row-level-security statements against the code before sending.");
}

(async () => {
  await pres.writeFile({ fileName: "gtm-health.pptx" });
  await applyTheme("gtm-health.pptx", THEME);
  console.log("written");
})();
