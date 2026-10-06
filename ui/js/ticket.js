// Ticket detail: customer message, live agent timeline (node by node), evidence panel, aur approval.
// Live run: POST /run ka SSE stream fetch + ReadableStream se parha jata hai (api.js streamRun).
import { getTicket, hasRole, listApprovals, streamRun } from "./api.js";
import { card, clear, el, errorText, fmtPkr, fmtTime, kv, notice, statusChip, tierChip } from "./dom.js";
import { approvalCard, valueText } from "./approvals.js";

// Node events ke "detail" mein se jo cheezein evidence panel mein jati hain.
const EVIDENCE_KEYS = ["order", "facts", "proposal", "ruling", "approval", "result", "outgoing", "draft"];
const CASE_KEYS = ["intent", "order_ref", "route", "route_reason", "outcome", "verified", "escalated", "errors", "actions_taken"];

const EVIDENCE_SECTIONS = [
  ["case", "Case"],
  ["order", "Order"],
  ["facts", "Facts the agent used (ids)"],
  ["proposal", "Agent proposal"],
  ["ruling", "Policy ruling"],
  ["interrupt", "Waiting for a human"],
  ["payload", "Approval request"],
  ["approval", "Human decision"],
  ["result", "Result"],
  ["outgoing", "Customer reply"],
  ["draft", "Draft"],
];

function plainBlock(value) {
  if (Array.isArray(value) || typeof value !== "object") return el("p", {}, valueText(value));
  return kv(Object.entries(value).map(([key, item]) => [key, valueText(item)]));
}

export async function renderTicket(container, ticketId, autorun) {
  const canRun = hasRole("support", "manager", "owner", "admin");
  const canSeeApprovals = hasRole("manager", "owner", "admin");
  const evidence = { case: {} };
  let controller = null;
  let running = false;
  let closed = false;
  let lastStatus = null;

  const headerBox = el("div");
  const messagesBox = el("div");
  const evidenceBox = el("div");
  const actionsBox = el("div");
  const approvalBox = el("div");
  const historyBox = el("div");
  const runMessage = el("div", { "aria-live": "polite" });
  const timeline = el("ol", { class: "timeline", "aria-live": "polite", "aria-label": "Agent timeline" });
  const runButton = el("button", { type: "button", class: "btn btn-primary" }, "Run agent");
  const timelineHint = el("p", { class: "muted" }, "Press Run agent to watch it work, node by node.");
  const timelineCard = el("div", {},
    card("Live agent timeline",
      el("div", { class: "actions" }, runButton),
      runMessage,
      timelineHint,
      timeline));

  // ---------------------------------------------------------------- render helpers
  function renderHeader(ticket, graph) {
    lastStatus = ticket.status;
    clear(headerBox).append(
      card(
        `Ticket ${ticket.id}`,
        kv([
          ["Status", statusChip(ticket.status)],
          ["Customer", ticket.customer_email],
          ["Subject", ticket.subject],
          ["Channel", ticket.channel],
          ["Intent", ticket.intent],
          ["Order", ticket.order_ref],
          ["Policy tier", graph && graph.tier && graph.tier !== "none" ? tierChip(graph.tier) : null],
          ["Cost", `${fmtPkr(ticket.cost_pkr)} (${ticket.tokens} tokens)`],
          ["Created", fmtTime(ticket.created_at)],
        ]),
        graph && graph.escalated ? notice("warn", "The agent escalated this ticket to a human.") : null,
        graph && graph.errors && graph.errors.length ? notice("error", `Agent errors: ${graph.errors.join(", ")}`) : null,
      ),
    );
  }

  function renderMessages(messages) {
    const items = messages.map((m) =>
      el("article", { class: `message ${m.direction}` },
        el("header", { class: "message-head" },
          el("strong", {}, m.direction === "inbound" ? "Customer" : "Agent reply"),
          el("span", { class: "muted" }, fmtTime(m.created_at))),
        el("p", { class: "message-body" }, m.body))); // textContent: customer text is untrusted
    clear(messagesBox).append(
      card("Conversation", el("p", { class: "muted small" }, "Customer text is untrusted and shown as plain text."),
        items.length ? items : el("p", { class: "muted" }, "No messages.")));
  }

  function renderActions(actions) {
    const items = actions.map((a) =>
      el("div", { class: "action-item" },
        el("details", {},
          el("summary", {}, el("strong", {}, a.tool), " ", el("span", { class: "muted" }, fmtTime(a.created_at))),
          el("pre", {}, JSON.stringify(a.result_json ?? {}, null, 2)))));
    clear(actionsBox).append(
      card("Actions the agent took", items.length ? items : el("p", { class: "muted" }, "No actions yet.")));
  }

  function renderHistory(history) {
    clear(historyBox);
    if (!history || history.length === 0) return;
    const lines = history.map((h) =>
      el("li", {}, `step ${h.step} (${h.source}): next ${h.next_nodes.length ? h.next_nodes.join(", ") : "end"}`));
    historyBox.append(
      card("Saved checkpoints", el("details", {}, el("summary", {}, `${history.length} checkpoints`), el("ul", {}, lines))));
  }

  function renderEvidence() {
    clear(evidenceBox);
    const blocks = [];
    for (const [key, label] of EVIDENCE_SECTIONS) {
      const value = evidence[key];
      const empty = value === undefined || value === null || (Array.isArray(value) && value.length === 0) ||
        (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0);
      if (empty) continue;
      blocks.push(el("div", { class: "evidence-block" }, el("h4", {}, label), plainBlock(value)));
    }
    evidenceBox.append(
      card("Evidence",
        el("p", { class: "muted small" }, "The facts and rules behind the agent's decision."),
        blocks.length ? blocks : el("p", { class: "muted" }, "Nothing yet. Run the agent to see the facts it used.")));
  }

  // ---------------------------------------------------------------- data loading
  async function loadDetail() {
    const detail = await getTicket(ticketId);
    if (closed) return;
    renderHeader(detail.ticket, detail.graph);
    renderMessages(detail.messages);
    renderActions(detail.actions);
    renderHistory(detail.history);
    if (detail.graph && detail.graph.pending_interrupt) evidence.interrupt = detail.graph.pending_interrupt;
    else delete evidence.interrupt;
    renderEvidence();
  }

  async function loadApproval() {
    if (!canSeeApprovals) {
      clear(approvalBox);
      if (lastStatus === "waiting_approval") {
        approvalBox.append(notice("warn", "This ticket is waiting for a manager or owner to approve it."));
      }
      return;
    }
    const groups = await Promise.all(["pending", "approved", "rejected", "expired"].map((s) => listApprovals(s)));
    if (closed) return;
    const mine = groups.flat().filter((a) => a.ticket_id === ticketId);
    clear(approvalBox);
    if (mine.length === 0) {
      if (lastStatus === "waiting_approval") {
        approvalBox.append(notice("warn", "Waiting for an approval that your role cannot see (owner tier?)."));
      }
      return;
    }
    const pending = mine.find((a) => a.status === "pending");
    if (pending) evidence.payload = pending.payload_json;
    renderEvidence();
    for (const approval of mine) approvalBox.append(approvalCard(approval, afterDecision));
  }

  // ---------------------------------------------------------------- timeline
  function addItem({ title, parent, ms, text, detail, cls }) {
    timelineHint.hidden = true;
    const detailRows = detail ? Object.entries(detail).map(([key, value]) => [key, valueText(value)]) : [];
    const li = el("li", { class: `tl-item ${cls || ""}` },
      el("div", { class: "tl-body" },
        el("div", { class: "tl-head" },
          el("strong", {}, title),
          parent ? el("span", { class: "chip" }, `in ${parent}`) : null,
          ms !== undefined ? el("span", { class: "muted small" }, `${ms} ms`) : null),
        text ? el("p", { class: "tl-text" }, text) : null,
        detailRows.length ? el("details", {}, el("summary", {}, "Details"), kv(detailRows)) : null));
    timeline.append(li);
    li.scrollIntoView({ block: "nearest" });
  }

  function collectEvidence(detail) {
    for (const key of EVIDENCE_KEYS) {
      if (detail[key] !== undefined) evidence[key] = detail[key];
    }
    for (const key of CASE_KEYS) {
      if (detail[key] !== undefined) evidence.case[key] = detail[key];
    }
    renderEvidence();
  }

  function handleEvent(name, data) {
    if (closed) return;
    if (name === "start") {
      addItem({ title: "Run started", text: `Mode: ${data.mode}`, cls: "tl-start" });
    } else if (name === "node") {
      const detail = data.detail || {};
      collectEvidence(detail);
      addItem({ title: data.node, parent: data.parent, ms: data.ms, text: data.summary, detail });
    } else if (name === "interrupt") {
      evidence.interrupt = data;
      renderEvidence();
      addItem({
        title: "Waiting for approval",
        text: "The agent stopped. A human must approve or reject before it can continue.",
        detail: data,
        cls: "tl-wait",
      });
      loadApproval().catch((err) => runMessage.append(notice("error", errorText(err))));
    } else if (name === "end") {
      const paused = data.status === "waiting_approval";
      addItem({
        title: paused ? "Run paused: waiting for approval" : "Run finished",
        text: `Ticket status: ${data.ticket_status}${data.ms ? ` | ${data.ms} ms` : ""}`,
        cls: paused ? "tl-wait" : "tl-end",
      });
      loadDetail().catch((err) => runMessage.append(notice("error", errorText(err))));
    } else if (name === "error") {
      addItem({ title: "Run failed", text: data.message, cls: "tl-error" });
      runMessage.append(notice("error", `${data.message} You can press Run agent to continue.`));
    }
  }

  async function startRun() {
    if (running || closed || !canRun) return;
    running = true;
    runButton.disabled = true;
    runButton.textContent = "Running...";
    clear(runMessage);
    controller = new AbortController();
    try {
      await streamRun(ticketId, handleEvent, controller.signal);
    } catch (err) {
      if (!closed) runMessage.append(notice("error", errorText(err)));
    } finally {
      running = false;
      if (!closed) {
        runButton.disabled = false;
        runButton.textContent = "Run agent";
      }
    }
  }

  // Approval ka faisla ho gaya.
  async function afterDecision(result) {
    if (closed) return;
    if (result.graph_status === "resumed") {
      addItem({ title: "Decision applied", text: "The agent resumed and finished the ticket.", cls: "tl-end" });
      try {
        await loadDetail();
      } catch (err) {
        runMessage.append(notice("error", errorText(err)));
      }
    } else if (canRun) {
      addItem({ title: "Decision saved", text: "Continuing the run now.", cls: "tl-start" });
      await startRun(); // resume na ho saka to yahan se checkpoint se aage chalta hai
    }
  }

  // ---------------------------------------------------------------- page
  runButton.addEventListener("click", startRun);
  if (!canRun) runButton.hidden = true;

  container.append(
    el("a", { class: "back-link", href: "#/inbox" }, "\u2190 Back to inbox"),
    el("h1", {}, `Ticket ${ticketId}`),
    el("div", { class: "two-col" },
      el("div", {}, headerBox, messagesBox, evidenceBox, actionsBox),
      el("div", {}, approvalBox, timelineCard, historyBox)),
  );
  renderEvidence();

  try {
    await loadDetail();
    await loadApproval();
  } catch (err) {
    clear(headerBox).append(notice("error", errorText(err)));
  }

  if (autorun && canRun) {
    // "?run=1" hata do, taake page refresh par agent dobara na chale.
    window.history.replaceState(null, "", `#/ticket/${encodeURIComponent(ticketId)}`);
    startRun();
  }

  return () => {
    closed = true;
    if (controller) controller.abort(); // stream band; server apna run poora kar leta hai
  };
}
