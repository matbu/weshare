const API = window.API_BASE || "/api";
let token = null;
try { token = localStorage.getItem("token"); } catch { /* storage disabled */ }

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const resp = await fetch(API + path, { ...options, headers });
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
  if (!resp.ok) throw Object.assign(new Error(data?.detail?.message || data?.detail || `Erreur ${resp.status}`), { status: resp.status });
  return data;
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 4000);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function date(d) {
  return d ? new Date(d).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "–";
}

function button(label, onClick, cls = "") {
  const b = Object.assign(document.createElement("button"), { type: "button", textContent: label, className: cls });
  b.onclick = async () => {
    b.disabled = true;
    try { await onClick(); } catch (err) { toast(err.message); } finally { b.disabled = false; }
  };
  return b;
}

// --- Sources ---------------------------------------------------------------
async function loadSources() {
  const rows = await api("/admin/sources");
  const table = document.getElementById("sources");
  table.innerHTML = "<tr><th>Source</th><th>Webcams</th><th>Dernière collecte</th><th>État</th><th></th></tr>";
  for (const s of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><strong>${esc(s.label)}</strong><br><span class="muted">${esc(s.name)}</span></td>
      <td>${Number(s.webcams).toLocaleString("fr-FR")}</td><td>${date(s.last_run)}</td>
      <td>${s.enabled ? '<span class="on">Active</span>' : '<span class="off">Désactivée</span>'}</td><td></td>`;
    tr.lastElementChild.append(button(s.enabled ? "Désactiver" : "Activer", async () => {
      if (s.enabled && !confirm(`Désactiver « ${s.label} » ? Ses webcams seront masquées.`)) return;
      const res = await api(`/admin/sources/${encodeURIComponent(s.name)}`, { method: "PUT", body: { enabled: !s.enabled } });
      toast(`${s.label} ${res.enabled ? "activée" : "désactivée"} (${res.webcams_affected.toLocaleString("fr-FR")} webcams recalculées)`);
      loadSources();
    }, s.enabled ? "danger" : "primary"));
    table.append(tr);
  }
}

// --- Removal requests ------------------------------------------------------
async function loadRemovals() {
  const rows = await api("/admin/removal-requests");
  document.getElementById("removals-count").textContent = rows.length ? `(${rows.length})` : "";
  const box = document.getElementById("removals");
  box.replaceChildren();
  if (!rows.length) box.innerHTML = '<p class="muted">Aucune demande en attente.</p>';
  for (const r of rows) {
    const card = document.createElement("article");
    card.className = "admin-card";
    card.innerHTML = `
      <p><strong>${r.scope === "site" ? "Tout un site" : "Une webcam"}</strong> · ${date(r.created_at)}</p>
      ${r.webcam_id ? `<p>Webcam #${r.webcam_id} ${esc(r.webcam_name || "")} (masquée en attendant)</p>` : ""}
      ${r.url ? `<p><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url)}</a></p>` : ""}
      <p>De : ${esc(r.requester || "")} &lt;<a href="mailto:${esc(r.email)}">${esc(r.email)}</a>&gt;</p>
      ${r.message ? `<blockquote>${esc(r.message)}</blockquote>` : ""}`;
    const actions = document.createElement("div");
    actions.className = "actions";
    const process = (action, label) => button(label, async () => {
      const res = await api(`/admin/removal-requests/${r.id}/process`, { method: "POST", body: { action } });
      toast(res.blocked_hosts ? `Bloqué : ${res.blocked_hosts.join(", ")} (${res.webcams_rejected} webcams retirées)` : "Traité");
      refresh();
    }, action === "dismiss" ? "" : "danger");
    if (r.webcam_id) actions.append(process("remove", "Retirer la webcam"));
    actions.append(process("block_site", "Bloquer tout le site"), process("dismiss", r.webcam_id ? "Refuser (rétablir)" : "Refuser"));
    card.append(actions);
    box.append(card);
  }
}

// --- Pending webcams -------------------------------------------------------
async function loadPending() {
  const rows = await api("/admin/pending");
  document.getElementById("pending-count").textContent = rows.length ? `(${rows.length})` : "";
  const box = document.getElementById("pending");
  box.replaceChildren();
  if (!rows.length) box.innerHTML = '<p class="muted">Rien en attente.</p>';
  for (const w of rows) {
    const card = document.createElement("article");
    card.className = "admin-card";
    const media = w.preview?.type === "image" ? `<img src="${esc(w.preview.url)}" alt="" loading="lazy">` : "";
    card.innerHTML = `${media}<p><strong>#${w.id} ${esc(w.name || "sans nom")}</strong></p>
      <p class="muted">${esc([w.city, w.country_code].filter(Boolean).join(", "))} · ${w.latitude.toFixed(4)}, ${w.longitude.toFixed(4)}</p>
      ${w.page_url ? `<p><a href="${esc(w.page_url)}" target="_blank" rel="noopener">${esc(w.page_url)}</a></p>` : ""}`;
    const actions = document.createElement("div");
    actions.className = "actions";
    for (const [action, label, cls] of [["approve", "Publier", "primary"], ["reject", "Refuser", "danger"]]) {
      actions.append(button(label, async () => {
        await api(`/admin/webcams/${w.id}/moderate`, { method: "POST", body: { action } });
        refresh();
      }, cls));
    }
    card.append(actions);
    box.append(card);
  }
}

// --- Blocked hosts ---------------------------------------------------------
async function loadBlocked() {
  const rows = await api("/admin/blocked-hosts");
  const table = document.getElementById("blocked");
  table.innerHTML = rows.length ? "<tr><th>Site</th><th>Raison</th><th>Depuis</th><th></th></tr>" : '<tr><td class="muted">Aucun site bloqué.</td></tr>';
  for (const h of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(h.host)}</td><td>${esc(h.reason || "")}</td><td>${date(h.created_at)}</td><td></td>`;
    tr.lastElementChild.append(button("Débloquer", async () => {
      await api(`/admin/blocked-hosts/${encodeURIComponent(h.host)}`, { method: "DELETE" });
      toast(`${h.host} débloqué (ses webcams restent retirées)`);
      loadBlocked();
    }));
    table.append(tr);
  }
}

document.getElementById("block-form").onsubmit = async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const params = new URLSearchParams({ url: form.get("url") });
  if (form.get("reason")) params.set("reason", form.get("reason"));
  try {
    const res = await api(`/admin/blocked-hosts?${params}`, { method: "POST" });
    toast(`${res.host} bloqué (${res.webcams_rejected} webcams retirées)`);
    e.target.reset();
    refresh();
  } catch (err) { toast(err.message); }
};

function refresh() {
  return Promise.all([loadSources(), loadRemovals(), loadPending(), loadBlocked()]);
}

(async () => {
  try {
    if (!token) throw new Error();
    const me = await api("/auth/me");
    if (!["moderator", "admin"].includes(me.role)) throw new Error();
    document.getElementById("who").textContent = me.email;
    document.getElementById("panels").hidden = false;
    await refresh();
  } catch {
    document.getElementById("denied").hidden = false;
  }
})();
