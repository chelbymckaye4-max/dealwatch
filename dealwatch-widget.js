// Dealwatch widget for the free "Scriptable" app (iPhone Home Screen + Lock Screen)
// Setup: paste your Dealwatch address below, save, then add a Scriptable widget
// to your Home Screen and choose this script.

const BASE = "https://YOUR-USERNAME.github.io/YOUR-REPO";   // <-- your Dealwatch address, no trailing slash

const C = {
  top: new Color("#2b2a28"),
  bottom: new Color("#1a1a19"),
  hot: new Color("#e8836b"),
  mint: new Color("#8fd0b0"),
  text: new Color("#ffffff"),
  muted: new Color("#b5b3ae"),
};

async function loadDeals() {
  try {
    const req = new Request(BASE + "/top.json?t=" + Date.now());
    const json = await req.loadJSON();
    return json.deals || [];
  } catch (e) {
    return null;
  }
}

function ago(ts) {
  const m = Math.max(1, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 60 ? m + "m" : Math.round(m / 60) + "h";
}

function money(n) {
  return n ? "$" + (n >= 100 ? Math.round(n).toLocaleString() : n.toFixed(2)) : "";
}

async function attachImages(deals) {
  await Promise.all(deals.map(async (d) => {
    if (!d.image) return;
    try { d._img = await new Request(d.image).loadImage(); } catch (e) {}
  }));
}

function dealLine(d) {
  const bits = [];
  if (d._img && d.pct) bits.push(d.pct + "% off");
  if (d.price) bits.push(money(d.price));
  if (d.verified) bits.push("verified");
  if (d.code) bits.push("code " + d.code);
  bits.push(ago(d.ts));
  return bits.join("  \u00b7  ");
}

function addRow(parent, d, big) {
  const row = parent.addStack();
  row.centerAlignContent();
  row.spacing = 10;
  if (d.url) row.url = d.url;

  if (d._img) {
    const im = row.addImage(d._img);
    im.imageSize = new Size(big ? 48 : 40, big ? 48 : 40);
    im.cornerRadius = 10;
    im.applyFillingContentMode();
  } else {
    const badge = row.addStack();
    badge.size = new Size(big ? 58 : 50, big ? 40 : 34);
    badge.cornerRadius = 10;
    badge.backgroundColor = d.urgent ? C.hot : new Color("#ffffff", 0.14);
    badge.centerAlignContent();
    const pct = badge.addText(d.pct ? d.pct + "%" : "NEW");
    pct.font = Font.heavySystemFont(big ? 20 : 16);
    pct.textColor = C.text;
    pct.minimumScaleFactor = 0.6;
  }

  const col = row.addStack();
  col.layoutVertically();
  col.spacing = 2;
  const title = col.addText(d.title);
  title.font = Font.semiboldSystemFont(big ? 14 : 12);
  title.textColor = C.text;
  title.lineLimit = 2;
  const sub = col.addText(dealLine(d));
  sub.font = Font.mediumSystemFont(11);
  sub.textColor = d.verified ? C.mint : C.muted;
  sub.lineLimit = 1;
}

function countFor(family) {
  return family === "small" ? 1 : family === "large" || family === "extraLarge" ? 6 : 3;
}

function buildLockScreen(deals) {
  const w = new ListWidget();
  const d = deals && deals[0];
  const t = w.addText(d ? (d.pct ? d.pct + "% off" : "Hot deal") : "Dealwatch");
  t.font = Font.boldSystemFont(14);
  if (d) {
    const s = w.addText(d.title);
    s.font = Font.systemFont(11);
    s.lineLimit = 2;
    if (d.url) w.url = d.url;
  }
  return w;
}

function buildHomeWidget(deals, family) {
  const w = new ListWidget();
  const g = new LinearGradient();
  g.colors = [C.top, C.bottom];
  g.locations = [0, 1];
  w.backgroundGradient = g;
  w.setPadding(12, 14, 12, 14);

  const header = w.addStack();
  header.centerAlignContent();
  const name = header.addText("DEALWATCH");
  name.font = Font.heavySystemFont(11);
  name.textColor = C.hot;
  header.addSpacer();
  const hot = header.addText("hottest now");
  hot.font = Font.mediumSystemFont(11);
  hot.textColor = C.muted;
  w.addSpacer(8);

  if (deals === null) {
    const t = w.addText("Can't reach Dealwatch. Check the address in this script.");
    t.font = Font.systemFont(12);
    t.textColor = C.text;
    return w;
  }
  if (!deals.length) {
    const t = w.addText("No hot deals yet. Check back soon.");
    t.font = Font.systemFont(13);
    t.textColor = C.text;
    return w;
  }

  const count = countFor(family);
  const shown = deals.slice(0, count);
  if (family === "small") {
    const d = shown[0];
    if (d._img) {
      const im = w.addImage(d._img);
      im.imageSize = new Size(46, 46);
      im.cornerRadius = 10;
      im.applyFillingContentMode();
      w.addSpacer(4);
    }
    const pct = w.addText(d.pct ? d.pct + "%" : "NEW");
    pct.font = Font.heavySystemFont(d._img ? 26 : 38);
    pct.textColor = d.urgent ? C.hot : C.mint;
    const title = w.addText(d.title);
    title.font = Font.semiboldSystemFont(12);
    title.textColor = C.text;
    title.lineLimit = 3;
    w.addSpacer();
    const sub = w.addText(dealLine(d));
    sub.font = Font.mediumSystemFont(10);
    sub.textColor = C.muted;
    sub.lineLimit = 1;
    if (d.url) w.url = d.url;
  } else {
    shown.forEach((d, i) => {
      addRow(w, d, count <= 3);
      if (i < shown.length - 1) w.addSpacer(count <= 3 ? 8 : 6);
    });
  }
  w.addSpacer();
  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
  return w;
}

const deals = await loadDeals();
const family = config.widgetFamily || "medium";
if (deals && family.indexOf("accessory") !== 0) await attachImages(deals.slice(0, countFor(family)));
const widget = family.indexOf("accessory") === 0 ? buildLockScreen(deals) : buildHomeWidget(deals, family);

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await widget.presentMedium();
}
Script.complete();
