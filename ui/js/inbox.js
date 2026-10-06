// Ticket inbox + customer-email simulator.
import { createTicket, hasRole, listTickets, simulate } from "./api.js";
import { STATUS, card, clear, el, errorText, fmtPkr, fmtRelative, notice, statusChip } from "./dom.js";

const REFRESH_MS = 15000;

const PRESETS = [
  { id: "late_order", label: "Late order", hint: "Customer writes in Roman Urdu: order is late, wants a refund." },
  { id: "damaged_item", label: "Damaged item", hint: "Item arrived cracked. Refund request that needs a human approval." },
  { id: "injection_attempt", label: "Prompt-injection attempt", hint: "Hostile text tries to force a PKR 50000 refund. The agent must refuse." },
];

function openTicket(ticketId) {
  window.location.hash = `#/ticket/${encodeURIComponent(ticketId)}?run=1`; // ticket page khud agent chala deta hai
}

// ---------------------------------------------------------------- simulator
function simulatorCard() {
  const message = el("div", { "aria-live": "polite" });
  const buttons = [];

  function setBusy(busy) {
    for (const button of buttons) button.disabled = busy;
  }

  async function startPreset(preset) {
    clear(message);
    setBusy(true);
    try {
      const created = await simulate(preset.id);
      openTicket(created.ticket_id);
    } catch (err) {
      clear(message).append(notice("error", errorText(err)));
      setBusy(false);
    }
  }

  const presetList = el("div", { class: "preset-list" });
  for (const preset of PRESETS) {
    const button = el("button", { type: "button", class: "btn btn-primary", onclick: () => startPreset(preset) }, preset.label);
    buttons.push(button);
    presetList.append(el("div", { class: "preset" }, button, el("span", { class: "muted small" }, preset.hint)));
  }

  // Apni marzi ka email
  const email = el("input", { id: "sim-email", type: "email", required: true, autocomplete: "off" });
  const subject = el("input", { id: "sim-subject", type: "text", maxlength: 200 });
  const body = el("textarea", { id: "sim-body", maxlength: 5000, required: true, rows: 4 });
  const submit = el("button", { type: "submit", class: "btn" }, "Create ticket and run agent");
  buttons.push(submit);
  const form = el("form", { novalidate: true },
    el("div", { class: "field" },
      el("label", { for: email.id }, "Customer email"),
      email,
      el("span", { class: "hint" }, "Use the email of a seeded MockShop order, or the agent will not find the order.")),
    el("div", { class: "field" }, el("label", { for: subject.id }, "Subject (optional)"), subject),
    el("div", { class: "field" }, el("label", { for: body.id }, "Message"), body),
    submit,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clear(message);
    if (!email.value.trim() || !body.value.trim()) {
      message.append(notice("error", "Enter the customer email and a message."));
      return;
    }
    setBusy(true);
    try {
      const created = await createTicket(email.value.trim(), subject.value.trim(), body.value.trim());
      openTicket(created.ticket_id);
    } catch (err) {
      message.append(notice("error", errorText(err)));
      setBusy(false);
    }
  });

  return card(
    "Customer-email simulator",
    el("p", { class: "muted" }, "Create a test ticket with one click. The agent starts working as soon as the ticket opens."),
    presetList,
    message,
    el("details", {}, el("summary", {}, "Write your own customer email"), form),
  );
}

// ---------------------------------------------------------------- inbox table
function ticketTable(tickets) {
  const head = el("tr", {},
    ["Ticket", "Customer", "Subject", "Status", "Intent", "Cost", "Updated"].map((name) => el("th", { scope: "col" }, name)));
  const rows = tickets.map((t) =>
    el("tr", {},
      el("td", {}, el("a", { href: `#/ticket/${encodeURIComponent(t.id)}` }, t.id)),
      el("td", {}, t.customer_email),
      el("td", { class: "truncate" }, t.subject || "(no subject)"),
      el("td", {}, statusChip(t.status)),
      el("td", {}, t.intent || "-"),
      el("td", {}, fmtPkr(t.cost_pkr)),
      el("td", {}, fmtRelative(t.updated_at))));
  return el("div", { class: "table-wrap" },
    el("table", { class: "data" },
      el("caption", { class: "sr-only" }, "Tickets, newest first"),
      el("thead", {}, head),
      el("tbody", {}, rows)));
}

export async function renderInbox(container) {
  let status = "";
  let closed = false;

  const select = el("select", { id: "ticket-status" },
    el("option", { value: "" }, "All"),
    Object.entries(STATUS).map(([value, info]) => el("option", { value }, info.label)));
  const refresh = el("button", { type: "button", class: "btn" }, "Refresh");
  const tableBox = el("div", { "aria-live": "polite" });

  async function load() {
    try {
      const tickets = await listTickets({ status, limit: 100 });
      if (closed) return;
      clear(tableBox);
      if (tickets.length === 0) {
        tableBox.append(notice("info", status ? "No tickets with this status." : "No tickets yet. Create a test ticket above."));
      } else {
        tableBox.append(ticketTable(tickets));
      }
    } catch (err) {
      if (!closed) clear(tableBox).append(notice("error", errorText(err)));
    }
  }

  select.addEventListener("change", () => {
    status = select.value;
    load();
  });
  refresh.addEventListener("click", load);

  container.append(el("h1", {}, "Ticket inbox"));
  if (hasRole("support", "manager", "owner", "admin")) container.append(simulatorCard());
  container.append(
    el("div", { class: "toolbar" },
      el("div", { class: "field" }, el("label", { for: "ticket-status" }, "Status"), select),
      refresh),
    tableBox,
  );

  await load();
  const timer = setInterval(load, REFRESH_MS);
  return () => {
    closed = true;
    clearInterval(timer);
  };
}
