// App shell: login check, navigation, aur hash routes (#/inbox, #/ticket/T-1001, #/approvals, #/reports).
// Har page ka render function container leta hai aur (chaho to) ek cleanup function wapas karta hai
// (timer band karne ya stream abort karne ke liye).
import { clearSession, getSession, hasRole } from "./api.js";
import { el, errorText, notice } from "./dom.js";
import { renderApprovals } from "./approvals.js";
import { renderInbox } from "./inbox.js";
import { renderReports } from "./reports.js";
import { renderTicket } from "./ticket.js";

const MANAGER_ROLES = ["manager", "owner", "admin"];
const main = document.getElementById("view");

if (!getSession()) {
  window.location.replace("/login.html");
}

const session = getSession();
if (session) {
  document.getElementById("user-info").textContent = `${session.email} (${session.role})`;
}
for (const link of document.querySelectorAll("nav a[data-route]")) {
  const managerOnly = link.dataset.route === "approvals" || link.dataset.route === "reports";
  link.hidden = managerOnly && !hasRole(...MANAGER_ROLES);
}

document.getElementById("logout").addEventListener("click", () => {
  clearSession();
  window.location.replace("/login.html");
});

// Token expire ho jaye to khud login par bhej do.
setInterval(() => {
  if (!getSession()) window.location.replace("/login.html");
}, 30000);

function parseRoute() {
  const [path, queryText = ""] = window.location.hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  return {
    name: parts[0] || "inbox",
    id: parts[1] ? decodeURIComponent(parts[1]) : null,
    query: new URLSearchParams(queryText),
  };
}

let cleanup = null;
let routeCounter = 0;

async function route() {
  if (!getSession()) return;
  if (cleanup) {
    cleanup();
    cleanup = null;
  }
  const mine = ++routeCounter;
  const { name, id, query } = parseRoute();
  // Har route ka apna container: purani (der se aane wali) render naye page mein na ghusay.
  const box = el("div");
  main.replaceChildren(box);

  let page = "inbox";
  let finish = null;
  try {
    if (name === "ticket" && id) {
      page = "ticket";
      document.title = `Ticket ${id} - ShopPilot`;
      finish = await renderTicket(box, id, query.get("run") === "1");
    } else if (name === "approvals" && hasRole(...MANAGER_ROLES)) {
      page = "approvals";
      document.title = "Approvals - ShopPilot";
      finish = await renderApprovals(box);
    } else if (name === "reports" && hasRole(...MANAGER_ROLES)) {
      page = "reports";
      document.title = "Reports - ShopPilot";
      finish = await renderReports(box);
    } else {
      document.title = "Inbox - ShopPilot";
      finish = await renderInbox(box);
    }
  } catch (err) {
    box.append(notice("error", errorText(err)));
  }

  // Agar is dauran user dusre page par chala gaya, to is page ka cleanup foran chalao.
  if (mine !== routeCounter) {
    if (finish) finish();
    return;
  }
  cleanup = finish;

  for (const link of document.querySelectorAll("nav a[data-route]")) {
    if (link.dataset.route === (page === "ticket" ? "inbox" : page)) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  main.focus({ preventScroll: true }); // keyboard / screen reader user naye page par rahe
}

window.addEventListener("hashchange", route);
route();
