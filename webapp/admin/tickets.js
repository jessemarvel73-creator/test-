"use strict";

async function api(url, opts = {}) {
  const r = await fetch(url, {
    credentials: "same-origin",
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(opts.headers || {})
    }
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) {
    throw new Error(d.error || `Request failed (${r.status})`);
  }
  return d;
}

const $ = id => document.getElementById(id);
const esc = v => String(v ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

function fmt(v) {
  if (!v) return "-";
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

function status(v) {
  return ({
    open: "Open",
    in_progress: "In Progress",
    resolved: "Resolved",
    closed: "Closed"
  })[String(v || "").toLowerCase()] || String(v || "");
}

const state = {
  page: 1,
  limit: 15,
  status: "all",
  search: "",
  total: 0,
  pages: 1,
  selectedId: null,
  searchTimer: null
};

function buildQuery() {
  const q = new URLSearchParams();
  q.set("page", String(state.page));
  q.set("limit", String(state.limit));
  if (state.status !== "all") q.set("status", state.status);
  if (state.search) q.set("search", state.search);
  return q.toString();
}

function renderToolbar() {
  const head = document.querySelector(".panel-head");
  if (!head || $("ticketToolbar")) return;
  const toolbar = document.createElement("div");
  toolbar.id = "ticketToolbar";
  toolbar.className = "ticket-toolbar";
  toolbar.innerHTML = `
    <input id="ticketSearch" class="ticket-search" type="search" maxlength="80" placeholder="Search ID, subject, customer, username...">
    <select id="ticketStatusFilter" class="ticket-status-filter">
      <option value="all">All statuses</option>
      <option value="open">Open</option>
      <option value="in_progress">In Progress</option>
      <option value="resolved">Resolved</option>
      <option value="closed">Closed</option>
    </select>
  `;
  head.insertBefore(toolbar, head.firstChild);

  $("ticketSearch").addEventListener("input", e => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => {
      state.search = e.target.value.trim();
      state.page = 1;
      load();
    }, 250);
  });
  $("ticketStatusFilter").addEventListener("change", e => {
    state.status = e.target.value;
    state.page = 1;
    load();
  });
}

function renderPagination() {
  const old = $("ticketPagination");
  if (old) old.remove();
  const wrap = document.createElement("div");
  wrap.id = "ticketPagination";
  wrap.className = "ticket-pagination";
  wrap.innerHTML = `
    <button class="btn" id="ticketPrev" ${state.page <= 1 ? "disabled" : ""}>◀ Previous</button>
    <span>Page <b>${state.page}</b> of <b>${state.pages}</b> · ${state.total} total</span>
    <button class="btn" id="ticketNext" ${state.page >= state.pages ? "disabled" : ""}>Next ▶</button>
  `;
  $("ticketList").appendChild(wrap);
  $("ticketPrev").onclick = () => { if (state.page > 1) { state.page--; load(); } };
  $("ticketNext").onclick = () => { if (state.page < state.pages) { state.page++; load(); } };
}

async function load() {
  renderToolbar();
  try {
    const d = await api(`/api/admin/tickets?${buildQuery()}`);
    const tickets = Array.isArray(d.tickets) ? d.tickets : [];
    const p = d.pagination || {};
    state.total = Number(p.total || 0);
    state.pages = Math.max(1, Number(p.pages || 1));
    state.page = Math.min(Math.max(1, Number(p.page || 1)), state.pages);

    $("ticketCount").textContent = `${state.total} ticket${state.total === 1 ? "" : "s"}`;
    const box = $("ticketList");
    if (!tickets.length) {
      box.innerHTML = `<div class="notice">No tickets match the current search/filter.</div>`;
      return;
    }

    box.innerHTML = tickets.map(t => {
      const customer = t.first_name || (t.username ? "@" + t.username : "") || String(t.telegram_id || "Customer");
      return `
        <button class="ticket-card ${state.selectedId === Number(t.id) ? "active" : ""}" onclick="openTicket(${Number(t.id)})" type="button">
          <div class="ticket-row">
            <span class="ticket-subject">#${Number(t.id)} · ${esc(t.subject || "Untitled ticket")}</span>
            <span class="badge ${esc(t.status || "")}">${esc(status(t.status))}</span>
          </div>
          <div class="ticket-meta">${esc(customer)} · ${esc(fmt(t.updated_at))}</div>
          <div class="ticket-tags">
            <span class="tag">${esc(t.category || "general")}</span>
            <span class="tag priority-${esc(t.priority || "normal")}">${esc(t.priority || "normal")}</span>
            ${t.order_id ? `<span class="tag">Order #${esc(t.order_id)}</span>` : ""}
            <span class="tag">${Number(t.message_count || 0)} messages</span>
          </div>
          <div class="ticket-preview">${esc(t.first_message || "No message preview")}</div>
        </button>`;
    }).join("");
    renderPagination();
  } catch (e) {
    $("ticketList").innerHTML = `<div class="notice">${esc(e.message)}</div>`;
    $("ticketCount").textContent = "0 tickets";
  }
}

async function openTicket(id) {
  state.selectedId = Number(id);
  try {
    const d = await api(`/api/admin/tickets/${Number(id)}`);
    const t = d.ticket;
    const messages = Array.isArray(t.messages) ? t.messages : [];
    const customer = t.first_name || (t.username ? "@" + t.username : "") || String(t.telegram_id || "-");
    $("ticketDetail").className = "";
    $("ticketDetail").innerHTML = `
      <div class="detail-head">
        <div>
          <div class="muted">TICKET #${Number(t.id)}</div>
          <h2 class="detail-title">${esc(t.subject || "Untitled ticket")}</h2>
          <div class="customer">Customer: ${esc(customer)}</div>
          <div class="ticket-detail-tags">
            <span class="tag">${esc(t.category || "general")}</span>
            <span class="tag priority-${esc(t.priority || "normal")}">${esc(t.priority || "normal")} priority</span>
            ${t.order_id ? `<span class="tag">Order #${esc(t.order_id)}</span>` : ""}
          </div>
        </div>
        <select class="status-select" id="status" onchange="setStatus(${Number(t.id)}, this.value)">
          ${[["open","Open"],["in_progress","In Progress"],["resolved","Resolved"],["closed","Closed"]].map(([v,l]) => `<option value="${v}" ${t.status === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
      </div>
      <div class="messages">
        ${messages.length ? messages.map(m => `
          <div class="message ${m.telegram_id ? "customer" : "support"}">
            <div class="message-author">${m.telegram_id ? "Customer" : "YOUR SHOP Support"}</div>
            <div class="message-body">${esc(m.message || "")}</div>
            <div class="message-time">${esc(fmt(m.created_at))}</div>
          </div>`).join("") : `<div class="muted">No messages.</div>`}
      </div>
      <div class="reply-box">
        <textarea id="reply" placeholder="Reply to customer..."></textarea>
        <div class="reply-actions">
          <button class="btn" type="button" onclick="deleteTicket(${Number(t.id)})">🗑 Delete Ticket</button>
          <button class="btn primary" type="button" onclick="replyTicket(${Number(t.id)})">Send Reply</button>
        </div>
      </div>`;
    await load();
  } catch (e) {
    $("ticketDetail").className = "detail-empty";
    $("ticketDetail").innerHTML = `<div class="notice">${esc(e.message)}</div>`;
  }
}

async function replyTicket(id) {
  const el = $("reply");
  const m = el ? el.value.trim() : "";
  if (!m) return;
  try {
    await api(`/api/admin/tickets/${Number(id)}/message`, { method: "POST", body: JSON.stringify({message:m}) });
    await openTicket(id);
  } catch (e) { alert(e.message); }
}

async function setStatus(id, statusValue) {
  try {
    await api(`/api/admin/tickets/${Number(id)}/status`, { method: "POST", body: JSON.stringify({status:statusValue}) });
    await openTicket(id);
  } catch (e) { alert(e.message); }
}

async function deleteTicket(id) {
  if (!window.confirm(`Delete ticket #${Number(id)} and all of its messages? This cannot be undone.`)) return;
  try {
    await api(`/api/admin/tickets/${Number(id)}`, { method: "DELETE" });
    state.selectedId = null;
    $("ticketDetail").className = "detail-empty";
    $("ticketDetail").innerHTML = `<div><div class="empty-icon">🎫</div><strong>Ticket deleted</strong><p class="muted">Select another ticket from the list.</p></div>`;
    await load();
  } catch (e) { alert(e.message); }
}

$("backAdmin").onclick = () => { window.location.href = "/admin"; };
$("refresh").onclick = load;
load();
