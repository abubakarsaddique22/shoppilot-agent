// Small helpers to build the page.
// Rule of this UI: text from the server (customer messages, subjects, notes, model summaries) is untrusted.
// It is only ever added as a text node. Nothing in this folder turns a string into markup.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "style") continue; // inline styles are blocked by the Content-Security-Policy: use CSS classes
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

// ---------------------------------------------------------------- formatting
export function fmtPkr(value) {
  const n = Number(value);
  if (value === null || value === undefined || !Number.isFinite(n)) return "-";
  return `PKR ${n.toLocaleString("en-PK", { maximumFractionDigits: 2 })}`;
}

// The API sends UTC times, sometimes without a time zone mark. Treat those as UTC.
export function parseTime(iso) {
  if (!iso) return null;
  const text = String(iso);
  const hasZone = /(Z|[+-]\d\d:?\d\d)$/.test(text);
  const date = new Date(hasZone ? text : `${text}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function fmtTime(iso) {
  const date = parseTime(iso);
  if (!date) return "-";
  return date.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function fmtRelative(iso) {
  const date = parseTime(iso);
  if (!date) return "-";
  const diff = date.getTime() - Date.now();
  const minutes = Math.round(Math.abs(diff) / 60000);
  let text;
  if (minutes < 1) text = "less than a minute";
  else if (minutes < 60) text = `${minutes} min`;
  else if (minutes < 48 * 60) text = `${Math.round(minutes / 60)} h`;
  else text = `${Math.round(minutes / 1440)} days`;
  return diff >= 0 ? `in ${text}` : `${text} ago`;
}

export function errorText(err) {
  return err && err.message ? err.message : "Something went wrong.";
}

// ---------------------------------------------------------------- small components
export const STATUS = {
  new: { label: "New", icon: "✦" },
  working: { label: "Working", icon: "⟳" },
  waiting_approval: { label: "Waiting approval", icon: "⏳" },
  done: { label: "Done", icon: "✓" },
  escalated: { label: "Escalated", icon: "⚠" },
};

// A status is never shown by colour alone: it also has an icon and a word.
export function statusChip(status) {
  const info = STATUS[status] || { label: status || "unknown", icon: "•" };
  const cls = String(status || "unknown").replace(/[^a-z_]/g, "");
  return el("span", { class: `chip s-${cls}` }, el("span", { "aria-hidden": "true" }, info.icon), " ", info.label);
}

export function tierChip(tier) {
  const cls = String(tier || "unknown").replace(/[^a-z_]/g, "");
  return el("span", { class: `chip tier-${cls}` }, `${tier || "?"} tier`);
}

export function notice(kind, text) {
  return el("p", { class: `notice ${kind}`, role: kind === "error" ? "alert" : "status" }, text);
}

let cardCounter = 0;
export function card(title, ...children) {
  const id = `card-${++cardCounter}`;
  return el("section", { class: "card", "aria-labelledby": id }, el("h3", { id }, title), ...children);
}

// A definition list from [label, value] pairs. Empty values are left out.
export function kv(rows) {
  const dl = el("dl", { class: "kv" });
  for (const [label, raw] of rows) {
    if (raw === null || raw === undefined || raw === "" || (Array.isArray(raw) && raw.length === 0)) continue;
    let value = raw;
    if (typeof raw === "boolean") value = raw ? "yes" : "no";
    else if (Array.isArray(raw) && raw.every((item) => !(item instanceof Node))) value = raw.join(", ");
    dl.append(el("dt", {}, label), el("dd", {}, value));
  }
  return dl;
}
