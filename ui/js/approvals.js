// Approval queue + ek approval ka card (Approve / Reject, note lazmi).
// approvalCard() ticket page mein bhi use hota hai.
import { decideApproval, getSession, listApprovals } from "./api.js";
import { card, clear, el, errorText, fmtPkr, fmtRelative, fmtTime, kv, notice, statusChip, tierChip } from "./dom.js";

// Kaun kis tier ka faisla kar sakta hai (server yehi dobara check karta hai). Admin sirf parh sakta hai.
const APPROVER_ROLES = { manager: ["manager", "owner"], owner: ["owner"] };
const HIDDEN_PAYLOAD_KEYS = ["key"]; // andar ka idempotency key, insaan ke kaam ka nahi
let fieldCounter = 0;

// Kisi bhi server value ko seedha plain text mein badalta hai (textContent ke liye).
export function valueText(value) {
  if (value === null || value === undefined) return "-";
  if (Array.isArray(value)) return value.map(valueText).join(", ") || "-";
  if (typeof value === "object") {
    return Object.entries(value)
      .map(([key, item]) => `${key}: ${valueText(item)}`)
      .join("; ");
  }
  return String(value);
}

function canDecide(approval) {
  const session = getSession();
  return approval.status === "pending" && Boolean(session) && (APPROVER_ROLES[approval.tier] || []).includes(session.role);
}

function decisionForm(approval, requested, onDecided) {
  const id = ++fieldCounter;
  const note = el("textarea", { id: `note-${id}`, maxlength: 500, rows: 3, required: true });
  const amount = el("input", {
    id: `amount-${id}`,
    type: "number",
    min: 1,
    max: requested || null,
    step: 1,
    inputmode: "numeric",
  });
  const message = el("div");
  const approve = el("button", { type: "button", class: "btn btn-approve" }, "Approve");
  const reject = el("button", { type: "button", class: "btn btn-reject" }, "Reject");

  const fields = [
    el("div", { class: "field" },
      el("label", { for: note.id }, "Note (required)"),
      note,
      el("span", { class: "hint" }, "Why are you approving or rejecting? This is saved in the audit log.")),
  ];
  if (requested) {
    fields.push(
      el("div", { class: "field" },
        el("label", { for: amount.id }, `Approve a lower amount (optional, up to ${requested})`),
        amount,
        el("span", { class: "hint" }, "Leave empty to approve the full requested amount.")),
    );
  }
  const form = el("form", { class: "decision-form", novalidate: true }, ...fields, message, el("div", { class: "actions" }, approve, reject));

  function showError(text, field) {
    clear(message).append(notice("error", text));
    if (field) field.focus();
  }

  async function submit(status) {
    clear(message);
    const text = note.value.trim();
    if (!text) {
      showError("A note is required before you can approve or reject.", note);
      return;
    }
    let lower = null;
    if (status === "approved" && amount.value !== "") {
      lower = Number(amount.value);
      if (!Number.isInteger(lower) || lower < 1 || (requested && lower > requested)) {
        showError(`The amount must be a whole number between 1 and ${requested}.`, amount);
        return;
      }
      if (requested && lower === requested) lower = null; // poori raqam: alag se bhejne ki zaroorat nahi
    }

    approve.disabled = true;
    reject.disabled = true;
    const idleLabels = [approve.textContent, reject.textContent];
    (status === "approved" ? approve : reject).textContent = "Saving...";
    try {
      const result = await decideApproval(approval.id, status, text, lower);
      const done = status === "approved" ? "Approved." : "Rejected.";
      const next =
        result.graph_status === "resumed"
          ? " The agent resumed and finished the ticket."
          : " The decision is saved. Open the ticket and run the agent to continue.";
      form.replaceChildren(notice("success", done + next));
      if (onDecided) onDecided(result);
    } catch (err) {
      showError(errorText(err));
      approve.disabled = false;
      reject.disabled = false;
      approve.textContent = idleLabels[0];
      reject.textContent = idleLabels[1];
    }
  }

  approve.addEventListener("click", () => submit("approved"));
  reject.addEventListener("click", () => submit("rejected"));
  form.addEventListener("submit", (event) => event.preventDefault());
  return form;
}

export function approvalCard(approval, onDecided) {
  const payload = approval.payload_json || {};
  const requested = Number(payload.amount_pkr);
  const requestedAmount = Number.isFinite(requested) && requested > 0 ? requested : null;
  const evidenceRows = Object.entries(payload)
    .filter(([key]) => key !== "amount_pkr" && !HIDDEN_PAYLOAD_KEYS.includes(key))
    .map(([key, value]) => [key, valueText(value)]);

  const node = card(
    `Approval #${approval.id}: ${approval.action}`,
    el("p", {}, statusChip(approval.status), " ", tierChip(approval.tier)),
    kv([
      ["Ticket", el("a", { href: `#/ticket/${encodeURIComponent(approval.ticket_id)}` }, approval.ticket_id)],
      ["Amount", requestedAmount ? fmtPkr(requestedAmount) : null],
      ["Requested", `${fmtTime(approval.requested_at)} (${fmtRelative(approval.requested_at)})`],
      [approval.status === "pending" ? "Expires" : "Expired / expires", fmtRelative(approval.expires_at)],
      ["Decided by", approval.decided_by],
      ["Decided at", approval.decided_at ? fmtTime(approval.decided_at) : null],
      ["Note", approval.note],
    ]),
    evidenceRows.length ? el("h4", {}, "Evidence") : null,
    evidenceRows.length ? kv(evidenceRows) : null,
  );
  node.classList.add("approval", `is-${approval.status}`);

  if (canDecide(approval)) {
    node.append(decisionForm(approval, requestedAmount, onDecided));
  } else if (approval.status === "pending") {
    const session = getSession();
    const why =
      session && session.role === "admin"
        ? "Read only: the admin role cannot decide approvals."
        : `Only ${approval.tier === "owner" ? "an owner" : "a manager or owner"} can decide this ${approval.tier}-tier approval.`;
    node.append(notice("info", why));
  }
  return node;
}

// ---------------------------------------------------------------- page
const FILTERS = [
  ["pending", "Pending"],
  ["approved", "Approved"],
  ["rejected", "Rejected"],
  ["expired", "Expired"],
];

export async function renderApprovals(container) {
  let status = "pending";
  const list = el("div", { "aria-live": "polite" });
  const select = el("select", { id: "approval-status" }, FILTERS.map(([value, label]) => el("option", { value }, label)));
  const refresh = el("button", { type: "button", class: "btn" }, "Refresh");

  async function load() {
    clear(list).append(el("p", { class: "muted" }, "Loading..."));
    try {
      const approvals = await listApprovals(status);
      clear(list);
      if (approvals.length === 0) {
        list.append(notice("info", status === "pending" ? "Nothing is waiting for approval." : `No ${status} approvals.`));
        return;
      }
      for (const approval of approvals) list.append(approvalCard(approval));
    } catch (err) {
      clear(list).append(notice("error", errorText(err)));
    }
  }

  select.addEventListener("change", () => {
    status = select.value;
    load();
  });
  refresh.addEventListener("click", load);

  container.append(
    el("h1", {}, "Approval queue"),
    el("p", { class: "muted" }, "Refunds above the auto limit wait here. A note is required for every decision."),
    el("div", { class: "toolbar" },
      el("div", { class: "field" }, el("label", { for: "approval-status" }, "Show"), select),
      refresh),
    list,
  );
  await load();
}
