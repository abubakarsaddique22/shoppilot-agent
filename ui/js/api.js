// Saari server calls yahan se guzarti hain. Baqi files kabhi fetch() seedha nahi karti.
// Token sirf sessionStorage mein rehta hai (tab band, token gaya). Token 30 minute mein expire hota hai.

const API_BASE = "/api"; // Caddy "/api" hata kar request api:8000 ko bhejta hai
const SESSION_KEY = "shoppilot_session";

// ---------------------------------------------------------------- session
export function getSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const session = JSON.parse(raw);
    if (!session.token || Date.now() >= session.expiresAt) {
      sessionStorage.removeItem(SESSION_KEY);
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function saveSession(tokenOut) {
  const session = {
    token: tokenOut.access_token,
    email: tokenOut.email,
    role: tokenOut.role,
    expiresAt: Date.now() + tokenOut.expires_in * 1000,
  };
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  return session;
}

export function clearSession() {
  sessionStorage.removeItem(SESSION_KEY);
}

export function hasRole(...roles) {
  const session = getSession();
  return Boolean(session) && roles.includes(session.role);
}

function goToLogin() {
  window.location.href = "/login.html";
}

// ---------------------------------------------------------------- errors
// Server ka error shape: {"error": {"code", "message", "request_id", "details"}}
function makeError(status, data, fallback) {
  const info = data && data.error ? data.error : {};
  let message = info.message || fallback || "Something went wrong.";
  if (info.code === "VALIDATION_ERROR" && info.details && Array.isArray(info.details.errors)) {
    message = info.details.errors.map((e) => `${e.loc.slice(1).join(".")}: ${e.msg}`).join("; ");
  }
  const err = new Error(message);
  err.status = status;
  err.code = info.code || "UNKNOWN";
  err.requestId = info.request_id || null;
  return err;
}

// ---------------------------------------------------------------- core request
function toQuery(query) {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

async function request(path, { method = "GET", body, query, auth = true } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const session = getSession();
    if (!session) {
      goToLogin();
      throw makeError(401, null, "Please log in.");
    }
    headers.Authorization = `Bearer ${session.token}`;
  }

  let response;
  try {
    response = await fetch(API_BASE + path + toQuery(query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw makeError(0, null, "Cannot reach the server. Is it running?");
  }

  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && auth) {
      clearSession();
      goToLogin();
    }
    throw makeError(response.status, data);
  }
  return data;
}

// ---------------------------------------------------------------- endpoints
export const login = (email, password) =>
  request("/v1/auth/login", { method: "POST", body: { email, password }, auth: false });

export const listTickets = (query) => request("/v1/tickets", { query });
export const getTicket = (id) => request(`/v1/tickets/${encodeURIComponent(id)}`);
export const createTicket = (customer_email, subject, body) =>
  request("/v1/tickets", { method: "POST", body: { customer_email, subject, body } });

export const simulate = (preset) => request("/v1/simulator", { method: "POST", body: { preset } });

export const listApprovals = (status = "pending") => request("/v1/approvals", { query: { status } });
export const decideApproval = (id, status, note, amount_pkr) =>
  request(`/v1/approvals/${id}/decision`, {
    method: "POST",
    body: { status, note, ...(amount_pkr ? { amount_pkr } : {}) },
  });

export const listReports = () => request("/v1/reports");
export const reportLink = (id) => request(`/v1/reports/${id}/link`);
export const runReportNow = () => request("/v1/admin/reports/run", { method: "POST" });

// ---------------------------------------------------------------- live run (SSE)
// EventSource Authorization header nahi bhej sakta, is liye fetch + ReadableStream.
// onEvent(name, data) har event par chalta hai: start, node, interrupt, end, error.
function parseBlock(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // heartbeat comment
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}

export async function streamRun(ticketId, onEvent, signal) {
  const session = getSession();
  if (!session) {
    goToLogin();
    throw makeError(401, null, "Please log in.");
  }

  let response;
  try {
    response = await fetch(`${API_BASE}/v1/tickets/${encodeURIComponent(ticketId)}/run`, {
      method: "POST",
      headers: { Authorization: `Bearer ${session.token}`, Accept: "text/event-stream" },
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") return;
    throw makeError(0, null, "Cannot reach the server. Is it running?");
  }

  if (!response.ok) {
    const data = await response.json().catch(() => null);
    if (response.status === 401) {
      clearSession();
      goToLogin();
    }
    throw makeError(response.status, data);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const parsed = parseBlock(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (parsed) onEvent(parsed.event, parsed.data);
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") throw err;
  } finally {
    reader.cancel().catch(() => {});
  }
}
