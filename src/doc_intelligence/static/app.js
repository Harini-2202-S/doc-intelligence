// Doc Intelligence web client. Server-provided text is always inserted with textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const FORMAT_LABEL = { pdf: "PDF", docx: "Word", pptx: "Slides", xlsx: "Excel", csv: "CSV", md: "Markdown", txt: "Text" };
const STAGE_TEXT = {
  verify_file: "Checking the file", parse: "Reading the document",
  index: "Indexing for search", brief: "Writing the brief",
};

const state = {
  token: null, mode: "login", collections: [], collectionId: null, tab: "documents",
  docs: [], docId: null, chatId: null, docTimer: null, listTimer: null, viewer: null,
};

// ---------- API ----------
function loadToken() { try { return sessionStorage.getItem("di_token"); } catch { return null; } }
function saveToken(token) {
  state.token = token;
  try { token ? sessionStorage.setItem("di_token", token) : sessionStorage.removeItem("di_token"); } catch { /* private mode */ }
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401 && state.token) { logout(); throw new Error("Your session expired. Please sign in again."); }
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(errorText(body.detail) || `Request failed (${res.status})`);
  return body;
}

function errorText(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return detail.message || JSON.stringify(detail);
}

const current = () => state.collections.find((c) => c.id === state.collectionId);

// ---------- auth ----------
function setAuthMode(mode) {
  state.mode = mode;
  const login = mode === "login";
  $("auth-title").textContent = login ? "Sign in to Doc Intelligence" : "Create your account";
  $("auth-submit").textContent = login ? "Sign in" : "Create account";
  $("auth-switch-text").textContent = login ? "No account?" : "Already registered?";
  $("auth-switch").textContent = login ? "Create one" : "Sign in";
  $("auth-error").textContent = "";
}

async function onAuthSubmit(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const creds = { email: form.get("email"), password: form.get("password") };
  $("auth-submit").disabled = true;
  $("auth-error").textContent = "";
  try {
    if (state.mode === "register") await api("/auth/register", { method: "POST", json: creds });
    const { access_token } = await api("/auth/login", { method: "POST", json: creds });
    saveToken(access_token);
    event.target.reset();
    await showApp();
  } catch (err) {
    $("auth-error").textContent = err.message;
  } finally {
    $("auth-submit").disabled = false;
  }
}

function logout() {
  saveToken(null);
  clearTimeout(state.docTimer);
  clearTimeout(state.listTimer);
  Object.assign(state, { collectionId: null, docId: null, chatId: null });
  closeViewer();
  $("app-view").hidden = true;
  $("auth-view").hidden = false;
}

async function showApp() {
  const me = await api("/auth/me");
  $("user-email").textContent = me.email;
  $("auth-view").hidden = true;
  $("app-view").hidden = false;
  await refreshCollections();
  selectCollection(state.collections[0].id);
  refreshStatus();
}

async function refreshStatus() {
  try {
    const s = await api("/status");
    $("status-line").textContent = s.chat_model ? `AI: ${s.chat_model}` : "AI not configured (search only)";
  } catch { /* non-essential */ }
}

// ---------- collections ----------
async function refreshCollections() {
  state.collections = await api("/collections");
  const list = $("collection-list");
  list.replaceChildren();
  for (const c of state.collections) {
    const li = el("li");
    li.append(el("span", null, c.name), el("span", "count", String(c.document_count)));
    li.classList.toggle("active", c.id === state.collectionId);
    li.addEventListener("click", () => selectCollection(c.id));
    list.append(li);
  }
}

function selectCollection(id) {
  state.collectionId = id;
  state.docId = null;
  state.chatId = null;
  document.querySelectorAll("#collection-list li").forEach((li, i) =>
    li.classList.toggle("active", state.collections[i]?.id === id));
  renderCollectionHead();
  showTab(state.tab);
}

function renderCollectionHead() {
  const c = current();
  if (!c) return;
  $("collection-title").textContent = c.name;
  $("collection-meta").textContent = `${c.document_count} document${c.document_count === 1 ? "" : "s"}` +
    (c.document_count !== c.ready_count ? ` · ${c.document_count - c.ready_count} processing or failed` : "");
}

async function onNewCollection(event) {
  event.preventDefault();
  const name = new FormData(event.target).get("name").trim();
  if (!name) return;
  const c = await api("/collections", { method: "POST", json: { name } });
  event.target.reset();
  event.target.hidden = true;
  await refreshCollections();
  selectCollection(c.id);
}

async function onCollectionMenu() {
  const c = current();
  const choice = prompt(`Rename “${c.name}”, or type DELETE to delete it and all its documents:`, c.name);
  if (choice === null || choice.trim() === "" || choice.trim() === c.name) return;
  if (choice.trim() === "DELETE") {
    if (!confirm(`Delete “${c.name}” and every document in it? This cannot be undone.`)) return;
    await api(`/collections/${c.id}`, { method: "DELETE" });
    await refreshCollections();
    selectCollection(state.collections[0].id);
  } else {
    await api(`/collections/${c.id}`, { method: "PATCH", json: { name: choice.trim() } });
    await refreshCollections();
    renderCollectionHead();
  }
}

// ---------- tabs ----------
function showTab(tab) {
  state.tab = tab;
  for (const name of ["documents", "ask", "search"]) $(`tab-${name}`).hidden = name !== tab;
  document.querySelectorAll(".tab-btn[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (tab === "documents") (state.docId ? openDocument(state.docId) : showLibrary());
  if (tab === "ask") refreshChats().catch((e) => { $("ask-error").textContent = e.message; });
}

// ---------- documents ----------
function showLibrary() {
  clearTimeout(state.docTimer);
  state.docId = null;
  $("library").hidden = false;
  $("doc-view").hidden = true;
  refreshDocs();
}

async function refreshDocs() {
  clearTimeout(state.listTimer);
  const collectionId = state.collectionId;
  const docs = await api(`/documents?collection_id=${collectionId}`);
  if (collectionId !== state.collectionId) return;
  state.docs = docs;
  const rows = $("doc-rows");
  rows.replaceChildren();
  $("no-docs").hidden = docs.length > 0;
  $("doc-table").hidden = docs.length === 0;
  for (const d of docs) {
    const tr = el("tr");
    const name = el("td");
    const title = el("span", null, d.title);
    name.append(title);
    if (d.warnings.length) name.append(el("span", "warn-icon", "⚠"));
    name.append(el("span", "sub", d.original_filename));
    const fmt = el("td");
    fmt.append(el("span", `fmt ${d.file_format}`, FORMAT_LABEL[d.file_format] || d.file_format));
    const status = el("td");
    status.append(el("span", `state ${d.status}`, d.status === "processing" ? "Processing…" : d.status === "ready" ? "Ready" : "Failed"));
    if (d.error) status.append(el("span", "sub", d.error));
    tr.append(name, fmt, el("td", null, d.page_count ? String(d.page_count) : "–"), status);
    tr.addEventListener("click", () => openDocument(d.id));
    rows.append(tr);
  }
  if (docs.some((d) => d.status === "processing") && state.tab === "documents" && !state.docId) {
    state.listTimer = setTimeout(refreshDocs, 2000);
  }
  const c = current();
  if (c && (c.document_count !== docs.length || c.ready_count !== docs.filter((d) => d.status === "ready").length)) {
    await refreshCollections();
    renderCollectionHead();
  }
}

async function uploadFiles(files) {
  const queue = $("upload-queue");
  for (const file of files) {
    const item = el("div", "queue-item");
    const label = el("span", null, file.name);
    const st = el("span", "muted", "Uploading…");
    item.append(label, st);
    queue.append(item);
    const form = new FormData();
    form.append("file", file);
    form.append("collection_id", state.collectionId);
    try {
      await api("/documents", { method: "POST", body: form });
      item.remove();
    } catch (err) {
      item.classList.add("bad");
      st.textContent = err.message;
      setTimeout(() => item.remove(), 8000);
    }
    refreshDocs();
  }
}

async function openDocument(id) {
  clearTimeout(state.docTimer);
  clearTimeout(state.listTimer);
  state.docId = id;
  if (state.tab !== "documents") showTab("documents");
  $("library").hidden = true;
  $("doc-view").hidden = false;
  let doc;
  try { doc = await api(`/documents/${id}`); } catch (err) { $("brief").replaceChildren(el("p", "error", err.message)); return; }
  if (state.docId !== id) return;
  renderDocument(doc);
  const job = doc.latest_job;
  if (job && (job.status === "queued" || job.status === "running")) state.docTimer = setTimeout(() => openDocument(id), 2000);
}

function renderDocument(doc) {
  $("doc-title").textContent = doc.title;
  $("doc-meta").textContent = [FORMAT_LABEL[doc.file_format], doc.original_filename,
    doc.page_count ? `${doc.pages.length} ${unitName(doc)}` : null, doc.chunk_count ? `${doc.chunk_count} indexed passages` : null,
    doc.index_version > 1 ? `index v${doc.index_version}` : null].filter(Boolean).join(" · ");

  const warnings = $("doc-warnings");
  warnings.replaceChildren(...doc.warnings.map((w) => el("div", "banner", `⚠ Security: ${w}`)));

  const status = $("doc-status");
  const job = doc.latest_job;
  status.hidden = true;
  status.className = "status";
  if (job && (job.status === "queued" || job.status === "running")) {
    status.hidden = false;
    status.textContent = `${STAGE_TEXT[job.current_stage] || "Waiting to start"}…` + (job.error ? ` (retrying: ${job.error})` : "");
  } else if (job && job.status === "failed") {
    status.hidden = false;
    status.classList.add("failed");
    status.textContent = doc.status === "ready" ? `Searchable, but the brief failed: ${job.error}` : `Processing failed: ${job.error}`;
  }
  $("doc-ask").disabled = doc.status !== "ready";
  renderBrief(doc);
}

function unitName(doc) {
  const unit = { pptx: "slide", pdf: "page", xlsx: "sheet", csv: "row block" }[doc.file_format] || "section";
  return doc.pages.length === 1 ? unit : `${unit}s`;
}

function citeButton(cite) {
  const b = el("button", "cite", cite.page_label || `p. ${cite.page}`);
  b.type = "button";
  b.title = cite.heading ? `${cite.title}: ${cite.heading}` : cite.title;
  b.addEventListener("click", () => openViewer(cite));
  return b;
}

function withCites(text, cites) {
  const li = el("li");
  li.append(document.createTextNode(text + " "));
  const seen = new Set();
  for (const c of cites || []) {
    const key = `${c.page}:${c.start}`;
    if (!seen.has(key)) { seen.add(key); li.append(citeButton(c)); }
  }
  return li;
}

function renderBrief(doc) {
  const box = $("brief");
  box.replaceChildren();
  const b = doc.briefing;
  if (!b) {
    if (doc.status === "ready" && !(doc.latest_job && ["queued", "running"].includes(doc.latest_job.status))) {
      box.append(el("p", "muted", "No brief yet. Add an OpenAI key and click Re-process to generate one. Search and Ask already work."));
    }
    return;
  }
  const c = b.content;
  const summary = el("p", "summary");
  summary.append(document.createTextNode(c.summary + " "));
  for (const cite of c.summary_citations || []) summary.append(citeButton(cite));
  box.append(summary);
  box.append(el("p", "muted small brief-meta", [c.document_type, `brief by ${b.model}`,
    b.coverage < 1 ? `covers ${Math.round(b.coverage * 100)}% of the document (sampled)` : null].filter(Boolean).join(" · ")));

  if (c.key_points?.length) {
    box.append(el("h3", null, "Key points"));
    const ul = el("ul", "points");
    for (const p of c.key_points) ul.append(withCites(p.text, p.citations));
    box.append(ul);
  }
  if (c.action_items?.length) {
    box.append(el("h3", null, "Actions and obligations"));
    const table = el("table", "table");
    const head = el("tr");
    for (const h of ["What", "Who", "When", "Source"]) head.append(el("th", null, h));
    table.append(el("thead")).append(head);
    const body = el("tbody");
    for (const a of c.action_items) {
      const tr = el("tr");
      const src = el("td");
      for (const cite of a.citations) src.append(citeButton(cite));
      tr.append(el("td", null, a.text), el("td", null, a.owner || "–"), el("td", null, a.due || "–"), src);
      body.append(tr);
    }
    table.append(body);
    box.append(table);
  }
  if (c.facts?.length) {
    box.append(el("h3", null, "Facts extracted"));
    const grid = el("div", "facts");
    for (const f of c.facts) {
      const card = el("div", "fact");
      const value = el("div", "value", f.value + " ");
      for (const cite of f.citations) value.append(citeButton(cite));
      card.append(el("div", "type", f.type), value);
      if (f.context) card.append(el("div", "ctx", f.context));
      grid.append(card);
    }
    box.append(grid);
  }
  if (c.open_questions?.length) {
    box.append(el("h3", null, "Not covered by this document"));
    const ul = el("ul", "points questions");
    for (const q of c.open_questions) ul.append(el("li", null, q));
    box.append(ul);
  }
}

async function onReprocess() {
  try { await api(`/documents/${state.docId}/reprocess`, { method: "POST" }); } catch (err) { alert(err.message); }
  openDocument(state.docId);
}

async function onDeleteDoc() {
  const doc = state.docs.find((d) => d.id === state.docId);
  if (!confirm(`Delete “${doc ? doc.title : "this document"}”? Its file, index and brief are removed. This cannot be undone.`)) return;
  await api(`/documents/${state.docId}`, { method: "DELETE" });
  showLibrary();
}

// ---------- source viewer ----------
async function openViewer(cite, page = cite.page) {
  state.viewer = { cite, page };
  const pageData = await api(`/documents/${cite.document_id}/pages/${page}`);
  if (!state.viewer || state.viewer.cite !== cite || state.viewer.page !== page) return;
  state.viewer.count = pageData.page_count;
  $("viewer").hidden = false;
  $("viewer-doc").textContent = cite.title;
  $("viewer-page").textContent = `${pageData.label} (${pageData.number} of ${pageData.page_count})`;
  $("viewer-prev").disabled = page <= 1;
  $("viewer-next").disabled = page >= pageData.page_count;
  const box = $("viewer-text");
  box.replaceChildren();
  const text = pageData.text;
  if (page === cite.page && cite.end > cite.start) {
    const mark = el("mark", null, text.slice(cite.start, cite.end));
    box.append(document.createTextNode(text.slice(0, cite.start)), mark, document.createTextNode(text.slice(cite.end)));
    mark.scrollIntoView({ block: "center" });
  } else {
    box.textContent = text;
    box.scrollTop = 0;
  }
}

function closeViewer() { $("viewer").hidden = true; state.viewer = null; }

// ---------- ask ----------
async function refreshChats() {
  const chats = await api(`/chat/sessions?collection_id=${state.collectionId}`);
  const list = $("chat-list");
  list.replaceChildren();
  $("no-chats").hidden = chats.length > 0;
  for (const chat of chats) {
    const li = el("li");
    li.append(el("span", null, chat.title), el("span", "sub", chat.document_title ? `About: ${chat.document_title}` : "Whole collection"));
    li.classList.toggle("active", chat.id === state.chatId);
    li.addEventListener("click", () => openChat(chat.id));
    list.append(li);
  }
  if (!state.chatId) resetChatPanel();
}

function resetChatPanel() {
  $("chat-title").textContent = "Ask your documents";
  $("chat-scope").textContent = `Scope: ${current()?.name || ""}`;
  $("messages").replaceChildren($("chat-empty") || el("div"));
  $("chat-empty").hidden = false;
}

async function startChat(documentId = null) {
  const chat = await api("/chat/sessions", { method: "POST", json: { collection_id: state.collectionId, document_id: documentId } });
  state.chatId = chat.id;
  if (state.tab !== "ask") showTab("ask");
  await refreshChats();
  await openChat(chat.id);
  return chat;
}

async function openChat(id) {
  state.chatId = id;
  const chat = await api(`/chat/sessions/${id}`);
  $("chat-title").textContent = chat.title;
  $("chat-scope").textContent = chat.document_title ? `Document: ${chat.document_title}` : `Collection: ${chat.collection_name}`;
  const box = $("messages");
  const empty = $("chat-empty");
  box.replaceChildren(empty);
  empty.hidden = chat.messages.length > 0;
  for (const m of chat.messages) box.append(renderMessage(m));
  box.scrollTop = box.scrollHeight;
  document.querySelectorAll("#chat-list li").forEach((li) => li.classList.remove("active"));
}

function renderMessage(m) {
  const div = el("div", `msg ${m.role}`);
  if (m.role !== "assistant") { div.textContent = m.content; return div; }
  const byLabel = Object.fromEntries((m.citations || []).map((c) => [c.label, c]));
  for (const part of m.content.split(/(\[S\d+\])/)) {
    const match = part.match(/^\[(S\d+)\]$/);
    if (match && byLabel[match[1]]) {
      const b = el("button", "cite", match[1].slice(1));
      b.type = "button";
      b.title = `${byLabel[match[1]].title}, ${byLabel[match[1]].page_label}`;
      b.addEventListener("click", () => openViewer(byLabel[match[1]]));
      div.append(b);
    } else {
      div.append(document.createTextNode(part));
    }
  }
  if (m.grounded === false) div.append(el("div", "gap-badge", "Not answered by your documents"));
  if (m.citations?.length) {
    const sources = el("div", "sources");
    for (const c of m.citations) {
      const row = el("button", "source-row");
      row.type = "button";
      row.append(el("strong", null, c.label.slice(1)), el("span", null, c.title),
        el("span", "where", `${c.page_label}${c.heading ? " · " + c.heading : ""}`));
      row.addEventListener("click", () => openViewer(c));
      sources.append(row);
    }
    div.append(sources);
  }
  return div;
}

async function onAsk(event) {
  event.preventDefault();
  const form = event.target;
  const content = new FormData(form).get("content").trim();
  if (!content) return;
  $("ask-error").textContent = "";
  if (!state.chatId) {
    try { await startChat(); } catch (err) { $("ask-error").textContent = err.message; return; }
  }
  const box = $("messages");
  $("chat-empty").hidden = true;
  box.append(renderMessage({ role: "user", content }));
  const thinking = el("div", "msg assistant thinking", "Searching your documents…");
  box.append(thinking);
  box.scrollTop = box.scrollHeight;
  form.reset();
  $("ask-submit").disabled = true;
  try {
    const reply = await api(`/chat/sessions/${state.chatId}/messages`, { method: "POST", json: { content } });
    thinking.replaceWith(renderMessage(reply));
    box.scrollTop = box.scrollHeight;
    if (["New conversation", "Ask your documents"].includes($("chat-title").textContent)) $("chat-title").textContent = content.slice(0, 80);
    refreshChats();
  } catch (err) {
    thinking.remove();
    $("ask-error").textContent = err.message;
  } finally {
    $("ask-submit").disabled = false;
  }
}

// ---------- search ----------
async function onSearch(event) {
  event.preventDefault();
  const query = new FormData(event.target).get("query");
  const out = $("search-results");
  out.replaceChildren(el("p", "muted small", "Searching…"));
  try {
    const hits = await api("/search", { method: "POST", json: { query, collection_id: state.collectionId } });
    out.replaceChildren();
    if (!hits.length) out.append(el("p", "muted", "Nothing found in this collection."));
    for (const h of hits) {
      const div = el("div", "hit");
      const head = el("div", "hit-head");
      const title = el("strong", null, h.heading ? `${h.title} — ${h.heading}` : h.title);
      const open = citeButton(h);
      head.append(title, open);
      div.append(head, el("p", null, h.excerpt.slice(0, 320) + (h.excerpt.length > 320 ? "…" : "")));
      out.append(div);
    }
  } catch (err) {
    out.replaceChildren(el("p", "error", err.message));
  }
}

// ---------- wiring ----------
document.addEventListener("DOMContentLoaded", async () => {
  $("auth-form").addEventListener("submit", onAuthSubmit);
  $("auth-switch").addEventListener("click", () => setAuthMode(state.mode === "login" ? "register" : "login"));
  $("logout").addEventListener("click", logout);
  $("new-collection").addEventListener("click", () => {
    const f = $("collection-form");
    f.hidden = !f.hidden;
    if (!f.hidden) f.querySelector("input").focus();
  });
  $("collection-form").addEventListener("submit", (e) => onNewCollection(e).catch((err) => alert(err.message)));
  $("collection-menu").addEventListener("click", () => onCollectionMenu().catch((err) => alert(err.message)));
  document.querySelectorAll(".tab-btn[data-tab]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.tab === "documents") state.docId = null;
    showTab(b.dataset.tab);
  }));

  const drop = $("dropzone");
  $("file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("drag"); uploadFiles([...e.dataTransfer.files]); });

  $("back-to-library").addEventListener("click", showLibrary);
  $("doc-ask").addEventListener("click", () => startChat(state.docId).catch((e) => alert(e.message)));
  $("doc-reprocess").addEventListener("click", onReprocess);
  $("doc-delete").addEventListener("click", () => onDeleteDoc().catch((e) => alert(e.message)));

  $("viewer-close").addEventListener("click", closeViewer);
  $("viewer-prev").addEventListener("click", () => state.viewer && openViewer(state.viewer.cite, state.viewer.page - 1));
  $("viewer-next").addEventListener("click", () => state.viewer && openViewer(state.viewer.cite, state.viewer.page + 1));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeViewer(); });

  $("new-chat").addEventListener("click", () => { state.chatId = null; refreshChats(); resetChatPanel(); });
  $("ask-form").addEventListener("submit", onAsk);
  $("ask-form").querySelector("textarea").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ask-form").requestSubmit(); }
  });
  $("search-form").addEventListener("submit", onSearch);

  state.token = loadToken();
  if (state.token) {
    try { await showApp(); return; } catch { saveToken(null); }
  }
  logout();
});
