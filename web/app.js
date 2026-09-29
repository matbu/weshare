const API = window.API_BASE || "/api";
const MAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
let token = null;
try { token = localStorage.getItem("token"); } catch { /* storage disabled */ }

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const resp = await fetch(API + path, { ...options, headers });
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
  if (!resp.ok) {
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join(", ")
      : detail?.message || `Erreur ${resp.status}`;
    throw Object.assign(new Error(message), { status: resp.status, data });
  }
  return data;
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 3500);
}

function place(cam) {
  return [cam.city, cam.region, cam.country_code].filter(Boolean).join(", ");
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------------------
// Media rendering: near-live snapshots, HLS, MJPEG, YouTube and provider players
// ---------------------------------------------------------------------------
const SNAPSHOT_REFRESH_MS = 3_000; // answered by the API cache; sources are polled adaptively (3-60 s)

function youtubeEmbed(url) {
  const u = new URL(url);
  if (u.pathname.startsWith("/embed/")) return url;
  const id = u.hostname === "youtu.be" ? u.pathname.slice(1)
    : u.pathname.startsWith("/live/") ? u.pathname.split("/")[2]
    : u.searchParams.get("v");
  return id ? `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&mute=1` : url;
}

// An http:// page cannot be framed from an https:// site (mixed content).
function embeddable(url) {
  return location.protocol !== "https:" || url.startsWith("https:");
}

function ago(date) {
  const s = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (s < 90) return "à l'instant";
  if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.round(s / 3600)} h`;
  return `il y a ${Math.round(s / 86400)} j`;
}

let stopMedia = () => {};

function renderMedia(container, cam) {
  stopMedia();
  stopMedia = () => {};
  container.replaceChildren();
  const stage = document.createElement("div");
  stage.className = "stage";
  const bar = document.createElement("div");
  bar.className = "media-bar";
  container.append(stage, bar);
  if (!cam) return;

  const preview = cam.preview;
  const embed = cam.embed_url && cam.embed_url !== preview?.url ? cam.embed_url : null;
  const site = cam.page_url || (preview?.type === "iframe" ? preview.url : null) || cam.embed_url;

  function button(label, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.onclick = (e) => { e.stopPropagation(); onClick(); };
    bar.append(b);
    return b;
  }
  function badge(text, live = false) {
    const el = document.createElement("span");
    el.className = live ? "badge live" : "badge";
    el.textContent = text;
    bar.prepend(el);
    return el;
  }

  function show(kind, url) {
    stopMedia();
    stage.replaceChildren();
    bar.querySelectorAll(".badge").forEach((b) => b.remove());
    if (kind === "image") (preview?.proxied ? showSnapshot(url) : showDirectImage(url));
    else if (kind === "mjpeg") { stage.append(Object.assign(new Image(), { src: url, alt: "" })); badge("● Direct", true); }
    else if (kind === "hls") showHls(url);
    else if (kind === "dash") showDash(url);
    else if (kind === "mp4") showVideo(url);
    else if (kind === "youtube" || kind === "iframe") showFrame(kind === "youtube" ? youtubeEmbed(url) : url, kind);
    else stage.textContent = "Pas d'aperçu disponible";
  }

  function showSnapshot(url) {
    const img = new Image();
    img.alt = "";
    stage.append(img);
    const age = badge("…");
    let etag = null, updated = null, objectUrl = null, alive = true;

    async function refresh() {
      if (document.hidden) return;
      try {
        const resp = await fetch(url, { cache: "no-cache" });
        if (!resp.ok) throw new Error();
        const tag = resp.headers.get("ETag");
        updated = new Date(resp.headers.get("X-Image-Updated") || Date.now());
        if (tag && tag === etag) return; // unchanged picture: nothing to swap
        etag = tag;
        const next = URL.createObjectURL(await resp.blob());
        const loader = new Image();
        loader.onload = () => {        // swap only once decoded: no flicker
          if (!alive) return URL.revokeObjectURL(next);
          img.src = next;
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          objectUrl = next;
        };
        loader.src = next;
      } catch {
        if (!objectUrl) stage.textContent = "Image indisponible";
      } finally {
        if (updated) age.textContent = `Mis à jour ${ago(updated)}`;
      }
    }
    refresh();
    const timer = setInterval(refresh, SNAPSHOT_REFRESH_MS);
    const onVisible = () => !document.hidden && refresh();
    document.addEventListener("visibilitychange", onVisible);
    stopMedia = () => {
      alive = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }

  // Commercial provider image, loaded by the browser from the provider itself (never proxied).
  function showDirectImage(url) {
    const img = Object.assign(new Image(), { alt: "", src: url });
    img.onerror = () => (stage.textContent = "Image indisponible");
    stage.append(img);
    const timer = setInterval(() => {
      if (!document.hidden) img.src = url + (url.includes("?") ? "&" : "?") + "_=" + Math.floor(Date.now() / 60_000);
    }, 60_000);
    stopMedia = () => clearInterval(timer);
  }

  function videoElement(loop = false) {
    const video = Object.assign(document.createElement("video"), { muted: true, autoplay: true, playsInline: true, controls: true, loop });
    video.onerror = () => (stage.textContent = "Vidéo indisponible");
    stage.append(video);
    return video;
  }
  // live === false: a recording (timelapse, clip), not a live stream
  function streamBadge() {
    if (preview?.live === false) badge("Enregistrement");
    else badge("● Direct", true);
  }

  function showHls(url) {
    const video = videoElement();
    let hls = null;
    if (video.canPlayType("application/vnd.apple.mpegurl")) video.src = url;
    else if (window.Hls?.isSupported()) { hls = new Hls(); hls.loadSource(url); hls.attachMedia(video); }
    streamBadge();
    stopMedia = () => hls?.destroy();
  }

  function showDash(url) {
    const video = videoElement();
    if (!window.dashjs) { stage.textContent = "Lecteur DASH indisponible"; return; }
    const player = dashjs.MediaPlayer().create();
    player.initialize(video, url, true);
    streamBadge();
    stopMedia = () => player.reset();
  }

  function showVideo(url) {
    videoElement(true).src = url;
    badge("Enregistrement");
  }

  function showFrame(url, kind) {
    if (!embeddable(url)) {
      stage.textContent = "Ce lecteur ne peut pas s'afficher ici (site non sécurisé).";
      return;
    }
    if (consent.get() !== "granted" && !consent.onceFor.has(url)) {
      // Third-party players may set cookies: ask first (GDPR / ePrivacy).
      const box = document.createElement("div");
      box.className = "consent-gate";
      box.innerHTML = `<p>Ce lecteur est fourni par <strong>${escapeHtml(hostOf(url))}</strong>, qui peut déposer des cookies.</p>`;
      const once = Object.assign(document.createElement("button"), { type: "button", textContent: "Afficher" });
      const always = Object.assign(document.createElement("button"), { type: "button", textContent: "Toujours autoriser", className: "primary" });
      once.onclick = (e) => { e.stopPropagation(); consent.onceFor.add(url); showFrame(url, kind); };
      always.onclick = (e) => { e.stopPropagation(); consent.set("granted"); showFrame(url, kind); };
      box.append(once, always);
      stage.replaceChildren(box);
      return;
    }
    stage.replaceChildren();
    const iframe = document.createElement("iframe");
    iframe.src = url;
    iframe.allow = "autoplay; fullscreen";
    iframe.referrerPolicy = "no-referrer-when-downgrade";
    stage.append(iframe);
    if (kind === "youtube") badge("● Direct", true);
  }

  // The provider's interactive player (360° panorama, timelapse, live) is shown by default
  // when there is one; the viewer's choice (interactive / image) is remembered.
  const canEmbed = embed && embeddable(embed);
  const showEmbed = () => show(embed.includes("youtu") ? "youtube" : "iframe", embed);
  const showPreview = () => show(preview?.type, preview?.url);
  let interactive = canEmbed && (!preview || (getViewPref() !== "image" && consent.get() !== "denied"));
  interactive ? showEmbed() : showPreview();

  if (canEmbed && preview) {
    const toggle = button(interactive ? "Image" : "Vue interactive", () => {
      interactive = !interactive;
      setViewPref(interactive ? "interactive" : "image");
      interactive ? showEmbed() : showPreview();
      toggle.textContent = interactive ? "Image" : "Vue interactive";
    });
  }
  if (site) {
    // Credit the owner and send them the visitor: "Source : provider ↗"
    const a = document.createElement("a");
    a.href = site;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = `Source : ${hostOf(site)} ↗`;
    a.title = "Voir la webcam sur le site de son propriétaire";
    a.onclick = (e) => e.stopPropagation();
    bar.append(a);
  }
  button("Signaler", () => openRemoval(cam)).title = "Signaler un problème ou demander le retrait";
  if (isModerator()) {
    button("Retirer", async () => {
      if (!confirm(`Retirer « ${cam.name || "cette webcam"} » du site ?`)) return;
      try {
        await api(`/admin/webcams/${cam.id}/moderate`, { method: "POST", body: { action: "reject" } });
        toast("Webcam retirée");
      } catch (err) { toast(err.message); }
    }).className = "danger";
  }
  // Windy terms (art. 8): visible acknowledgment next to every Windy webcam.
  if ([preview?.url, cam.embed_url].some((u) => u && u.includes("windy.com"))) {
    const credit = document.createElement("a");
    credit.className = "credit";
    credit.href = "https://www.windy.com/webcams/add";
    credit.target = "_blank";
    credit.rel = "noopener";
    credit.textContent = "Webcams provided by windy.com - add new webcam";
    credit.onclick = (e) => e.stopPropagation();
    container.append(credit);
  }
  if (!preview && !canEmbed) {
    stage.textContent = site ? "Aperçu indisponible : voir le site de la webcam" : "Pas d'aperçu disponible";
  }
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; }
}

// Consent for third-party players (YouTube, Windy, providers), remembered for 6 months.
const consent = {
  onceFor: new Set(),
  get() {
    try {
      const saved = JSON.parse(localStorage.getItem("consent") || "null");
      return saved && Date.now() - saved.at < 182 * 86400_000 ? saved.value : null;
    } catch { return null; }
  },
  set(value) {
    try { localStorage.setItem("consent", JSON.stringify({ value, at: Date.now() })); } catch { /* ignore */ }
    document.getElementById("consent-banner").hidden = true;
    this.onChange?.();
  },
};

function getViewPref() {
  try { return localStorage.getItem("mediaView"); } catch { return null; }
}

function setViewPref(value) {
  try { localStorage.setItem("mediaView", value); } catch { /* storage disabled */ }
}

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------
const map = new maplibregl.Map({
  container: "map",
  style: MAP_STYLE,
  center: [8, 46],
  zoom: 3,
  attributionControl: { compact: false }, // Windy terms: acknowledgment visible at all times
});
map.addControl(new maplibregl.NavigationControl(), "top-right");
map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-right");

map.on("load", () => {
  map.addSource("webcams", {
    type: "vector",
    tiles: [new URL(`${API}/tiles/{z}/{x}/{y}.pbf`, location.href).href.replace(/%7B/g, "{").replace(/%7D/g, "}")],
    maxzoom: 14,
    // Windy Webcams API terms, art. 8.2: this exact acknowledgment must stay visible.
    attribution: 'Webcams provided by <a href="https://www.windy.com/" target="_blank" rel="noopener">windy.com</a> - <a href="https://www.windy.com/webcams/add" target="_blank" rel="noopener">add new webcam</a> · Webcams © contributeurs OpenStreetMap et sites des fournisseurs',
  });
  map.addLayer({
    id: "clusters",
    type: "circle",
    source: "webcams",
    "source-layer": "webcams",
    filter: [">", ["get", "count"], 1],
    paint: {
      "circle-color": ["case", [">", ["get", "live"], 0], "#1e9e5a", "#8a919c"],
      "circle-opacity": 0.85,
      "circle-radius": ["interpolate", ["linear"], ["get", "count"], 2, 12, 100, 18, 1000, 26, 10000, 34],
      "circle-stroke-width": 2,
      "circle-stroke-color": "#ffffff",
    },
  });
  map.addLayer({
    id: "cluster-count",
    type: "symbol",
    source: "webcams",
    "source-layer": "webcams",
    filter: [">", ["get", "count"], 1],
    layout: {
      "text-field": ["case", [">=", ["get", "count"], 1000],
        ["concat", ["to-string", ["round", ["/", ["get", "count"], 1000]]], "k"],
        ["to-string", ["get", "count"]]],
      "text-font": ["Noto Sans Bold"],
      "text-size": 12,
      "text-allow-overlap": true,
    },
    paint: { "text-color": "#ffffff" },
  });
  map.addLayer({
    id: "points",
    type: "circle",
    source: "webcams",
    "source-layer": "webcams",
    filter: ["==", ["get", "count"], 1],
    paint: {
      "circle-color": ["case", [">", ["get", "live"], 0], "#1e9e5a", "#8a919c"],
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 4, 14, 8],
      "circle-stroke-width": 1.5,
      "circle-stroke-color": "#ffffff",
    },
  });

  for (const layer of ["clusters", "points"]) {
    map.on("mouseenter", layer, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", layer, () => (map.getCanvas().style.cursor = ""));
  }
});

map.on("click", "clusters", (e) => {
  map.easeTo({ center: e.features[0].geometry.coordinates, zoom: map.getZoom() + 2.5 });
});

map.on("click", "points", async (e) => {
  if (picking) return;
  const feature = e.features[0];
  const popup = new maplibregl.Popup({ maxWidth: "320px" })
    .setLngLat(feature.geometry.coordinates)
    .setHTML(`<div class="popup-body"><h4>${escapeHtml(feature.properties.name || "Webcam")}</h4><p>Chargement…</p></div>`)
    .addTo(map);
  try {
    const cam = await api(`/webcams/${feature.properties.id}`);
    const root = document.createElement("div");
    const media = document.createElement("div");
    media.className = "media";
    const links = cam.sources
      .flatMap((s) => [s.webpage_url && `<a href="${escapeHtml(s.webpage_url)}" target="_blank" rel="noopener">Site</a>`,
                       s.source_url && `<a href="${escapeHtml(s.source_url)}" target="_blank" rel="noopener">${escapeHtml(s.source)}</a>`])
      .filter(Boolean).join("");
    root.innerHTML = `<div class="popup-body"><h4>${escapeHtml(cam.name || "Webcam")}</h4><p>${escapeHtml(place(cam))}</p>${links}</div>`;
    root.prepend(media);
    renderMedia(media, cam);
    popup.setDOMContent(root);
    popup.on("close", () => stopMedia());
  } catch (err) {
    popup.setHTML(`<div class="popup-body"><p>${escapeHtml(err.message)}</p></div>`);
  }
});

async function loadStats() {
  try {
    const s = await api("/stats");
    document.getElementById("stats").textContent =
      `${s.webcams.toLocaleString()} webcams · ${s.live.toLocaleString()} en direct · ${s.countries} pays`;
  } catch { /* stats are optional */ }
}
loadStats();

// ---------------------------------------------------------------------------
// Discover (swipe)
// ---------------------------------------------------------------------------
const queue = [];
let current = null;
const card = document.getElementById("card");

async function fillQueue() {
  if (queue.length > 3) return;
  try {
    queue.push(...(await api("/webcams/random?count=15")));
  } catch (err) {
    toast(err.message);
  }
}

async function nextCam() {
  if (!queue.length) await fillQueue();
  current = queue.shift();
  fillQueue();
  if (!current) {
    document.getElementById("card-title").textContent = "Aucune webcam en direct pour l'instant";
    document.getElementById("card-place").textContent = "Le collecteur remplit la base, revenez bientôt.";
    renderMedia(document.getElementById("card-media"), null);
    return;
  }
  document.getElementById("card-title").textContent = current.name || "Webcam";
  document.getElementById("card-place").textContent = place(current);
  renderMedia(document.getElementById("card-media"), current);
}

function swipeAway(direction) {
  card.style.transform = `translateX(${direction * 120}%) rotate(${direction * 12}deg)`;
  card.style.opacity = "0";
  setTimeout(() => {
    card.classList.add("dragging");
    card.style.transform = "";
    card.style.opacity = "";
    nextCam();
    requestAnimationFrame(() => card.classList.remove("dragging"));
  }, 250);
}

let dragStart = null;
card.addEventListener("pointerdown", (e) => {
  if (e.target.closest("video, iframe, button, a")) return;
  dragStart = e.clientX;
  card.classList.add("dragging");
  card.setPointerCapture(e.pointerId);
});
card.addEventListener("pointermove", (e) => {
  if (dragStart === null) return;
  const dx = e.clientX - dragStart;
  card.style.transform = `translateX(${dx}px) rotate(${dx / 25}deg)`;
});
card.addEventListener("pointerup", (e) => {
  if (dragStart === null) return;
  const dx = e.clientX - dragStart;
  dragStart = null;
  card.classList.remove("dragging");
  if (Math.abs(dx) > 100) swipeAway(Math.sign(dx));
  else card.style.transform = "";
});

document.getElementById("next-btn").onclick = () => swipeAway(-1);
document.getElementById("show-on-map-btn").onclick = () => {
  if (!current) return;
  showView("map");
  map.flyTo({ center: [current.longitude, current.latitude], zoom: 13 });
};
document.addEventListener("keydown", (e) => {
  if (!document.getElementById("view-discover").classList.contains("active")) return;
  if (document.querySelector("dialog[open]")) return;
  if (e.key === "ArrowRight") swipeAway(1);
  if (e.key === "ArrowLeft") swipeAway(-1);
});

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------
let discoverStarted = false;

function showView(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "map") {
    map.resize();
    renderMedia(document.getElementById("card-media"), null);
  } else if (!discoverStarted) {
    discoverStarted = true;
    nextCam();
  } else if (current) {
    renderMedia(document.getElementById("card-media"), current);
  }
}
document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => showView(b.dataset.view)));

// ---------------------------------------------------------------------------
// Account
// ---------------------------------------------------------------------------
const authDialog = document.getElementById("auth-dialog");
const addDialog = document.getElementById("add-dialog");
let registering = false;

document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));

let currentUser = null;

function isModerator() {
  return ["moderator", "admin"].includes(currentUser?.role);
}

function setUser(user) {
  currentUser = user;
  document.getElementById("admin-link").hidden = !isModerator();
}

function setToken(value) {
  token = value;
  try { value ? localStorage.setItem("token", value) : localStorage.removeItem("token"); } catch { /* ignore */ }
  document.getElementById("login-btn").hidden = !!value;
  document.getElementById("logout-btn").hidden = !value;
  if (!value) setUser(null);
}
setToken(token);
if (token) api("/auth/me").then(setUser).catch((err) => err.status === 401 && setToken(null));

document.getElementById("login-btn").onclick = () => authDialog.showModal();
document.getElementById("logout-btn").onclick = () => { setToken(null); toast("Déconnecté"); };

document.getElementById("auth-switch").onclick = () => {
  registering = !registering;
  document.getElementById("auth-title").textContent = registering ? "Créer un compte" : "Connexion";
  document.getElementById("auth-switch").textContent = registering ? "J'ai déjà un compte" : "Créer un compte";
  document.getElementById("display-name-field").hidden = !registering;
};

document.getElementById("auth-form").onsubmit = async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const body = { email: form.get("email"), password: form.get("password") };
  if (registering && form.get("display_name")) body.display_name = form.get("display_name");
  try {
    const res = await api(registering ? "/auth/register" : "/auth/login", { method: "POST", body });
    setToken(res.access_token);
    setUser(res.user);
    authDialog.close();
    toast(`Bienvenue ${res.user.display_name || res.user.email}`);
    if (pendingAdd) { pendingAdd = false; openAdd(); }
  } catch (err) {
    document.getElementById("auth-error").textContent = err.message;
  }
};

// ---------------------------------------------------------------------------
// Add a webcam
// ---------------------------------------------------------------------------
const addForm = document.getElementById("add-form");
let pendingAdd = false;
let picking = false;

function openAdd() {
  if (!token) {
    pendingAdd = true;
    authDialog.showModal();
    return;
  }
  const center = map.getCenter();
  if (!addForm.latitude.value) addForm.latitude.value = center.lat.toFixed(5);
  if (!addForm.longitude.value) addForm.longitude.value = center.lng.toFixed(5);
  addDialog.showModal();
}
document.getElementById("add-btn").onclick = openAdd;

document.getElementById("pick-btn").onclick = () => {
  addDialog.close();
  showView("map");
  picking = true;
  map.getCanvas().style.cursor = "crosshair";
  toast("Cliquez sur l'emplacement de la webcam");
};

map.on("click", (e) => {
  if (!picking) return;
  picking = false;
  map.getCanvas().style.cursor = "";
  addForm.latitude.value = e.lngLat.lat.toFixed(5);
  addForm.longitude.value = e.lngLat.lng.toFixed(5);
  addDialog.showModal();
});

addForm.onsubmit = async (e) => {
  e.preventDefault();
  const form = new FormData(addForm);
  const body = {
    name: form.get("name"),
    latitude: Number(form.get("latitude")),
    longitude: Number(form.get("longitude")),
    media_url: form.get("media_url") || null,
    page_url: form.get("page_url") || null,
    description: form.get("description") || null,
  };
  try {
    const cam = await api("/webcams", { method: "POST", body });
    addDialog.close();
    addForm.reset();
    toast(cam.status === "approved" ? "Webcam ajoutée !" : "Merci ! Votre webcam sera visible après modération.");
  } catch (err) {
    document.getElementById("add-error").textContent =
      err.status === 409 ? "Cette webcam existe déjà." : err.status === 401 ? "Reconnectez-vous." : err.message;
    if (err.status === 401) setToken(null);
  }
};

// ---------------------------------------------------------------------------
// Removal requests ("Retirer ma webcam")
// ---------------------------------------------------------------------------
const removalDialog = document.getElementById("removal-dialog");
const removalForm = document.getElementById("removal-form");

function openRemoval(cam = null) {
  removalForm.reset();
  document.getElementById("removal-error").textContent = "";
  removalForm.webcam_id.value = cam?.id ?? "";
  removalForm.url.value = cam ? (cam.page_url || cam.embed_url || "") : "";
  document.getElementById("removal-target").textContent = cam
    ? `Webcam : ${cam.name || "sans nom"}${place(cam) ? " (" + place(cam) + ")" : ""}`
    : "";
  removalForm.scope.value = cam ? "webcam" : "site";
  removalDialog.showModal();
}
document.querySelectorAll("[data-open-removal]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openRemoval(); }));

removalForm.onsubmit = async (e) => {
  e.preventDefault();
  const form = new FormData(removalForm);
  const body = {
    scope: form.get("scope"),
    webcam_id: form.get("webcam_id") ? Number(form.get("webcam_id")) : null,
    url: form.get("url") || null,
    email: form.get("email"),
    requester: form.get("requester") || null,
    message: form.get("message") || null,
    website: form.get("website") || null,
  };
  try {
    const res = await api("/removal-requests", { method: "POST", body });
    removalDialog.close();
    toast(res.hidden
      ? "Demande reçue : la webcam est masquée en attendant notre vérification."
      : "Demande reçue : nous la traitons au plus vite.");
  } catch (err) {
    document.getElementById("removal-error").textContent = err.message;
  }
};

// ---------------------------------------------------------------------------
// Third-party content consent banner
// ---------------------------------------------------------------------------
const banner = document.getElementById("consent-banner");
banner.hidden = consent.get() !== null;
document.getElementById("consent-accept").onclick = () => consent.set("granted");
document.getElementById("consent-refuse").onclick = () => consent.set("denied");
document.querySelectorAll("[data-open-consent]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); banner.hidden = false; }));
consent.onChange = () => {
  // Re-render what is on screen with the new choice.
  if (document.getElementById("view-discover").classList.contains("active") && current) {
    renderMedia(document.getElementById("card-media"), current);
  }
};
