// Reports: list of generated reports, download link, aur admin ke liye "run daily report now".
import { listReports, reportLink, runReportNow, hasRole } from "./api.js";
import { clear, el, errorText, fmtTime, notice, statusChip } from "./dom.js";

export async function renderReports(container) {
  const message = el("div", { "aria-live": "polite" });
  const tableBox = el("div");
  const refresh = el("button", { type: "button", class: "btn" }, "Refresh");
  const isAdmin = hasRole("admin");

  async function download(report, button) {
    clear(message);
    button.disabled = true;
    try {
      const link = await reportLink(report.id);
      window.open(link.url, "_blank", "noopener"); // 5 minute ka signed link
    } catch (err) {
      // Local setup mein S3 nahi: report server ke reports/ folder mein save hoti hai.
      const kind = err.code === "REPORT_NOT_IN_S3" ? "info" : "error";
      message.append(notice(kind, errorText(err)));
    } finally {
      button.disabled = false;
    }
  }

  function table(reports) {
    const head = el("tr", {}, ["Report", "Type", "Status", "Created", "Location", ""].map((name) => el("th", { scope: "col" }, name)));
    const rows = reports.map((report) => {
      const button = el("button", { type: "button", class: "btn btn-small" }, "Download");
      button.addEventListener("click", () => download(report, button));
      return el("tr", {},
        el("td", {}, `#${report.id}`),
        el("td", {}, report.kind),
        el("td", {}, statusChip(report.status)),
        el("td", {}, fmtTime(report.created_at)),
        el("td", {}, report.s3_key || "-"),
        el("td", {}, report.status === "done" && report.s3_key ? button : null));
    });
    return el("div", { class: "table-wrap" },
      el("table", { class: "data" },
        el("caption", { class: "sr-only" }, "Generated reports, newest first"),
        el("thead", {}, head),
        el("tbody", {}, rows)));
  }

  async function load() {
    try {
      const reports = await listReports();
      clear(tableBox);
      tableBox.append(reports.length ? table(reports) : notice("info", "No reports yet."));
    } catch (err) {
      clear(tableBox).append(notice("error", errorText(err)));
    }
  }

  container.append(el("h1", {}, "Reports"));

  if (isAdmin) {
    const runButton = el("button", { type: "button", class: "btn btn-primary" }, "Run daily report now");
    runButton.addEventListener("click", async () => {
      clear(message);
      runButton.disabled = true;
      runButton.textContent = "Running...";
      try {
        const result = await runReportNow();
        const text = result.status === "skipped" ? "Today's report already exists." : "Report created.";
        message.append(notice("success", `${text} ${result.location}`));
        await load();
      } catch (err) {
        message.append(notice("error", errorText(err)));
      } finally {
        runButton.disabled = false;
        runButton.textContent = "Run daily report now";
      }
    });
    container.append(el("div", { class: "actions" }, runButton));
  }

  refresh.addEventListener("click", load);
  container.append(el("div", { class: "toolbar" }, refresh), message, tableBox);
  await load();
}
