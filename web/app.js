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
// Media rendering (image snapshot, HLS, MJPEG, YouTube, embeds)
// ---------------------------------------------------------------------------
function youtubeEmbed(url) {
  const u = new URL(url);
  if (u.pathname.startsWith("/embed/")) return url;
  const id = u.hostname === "youtu.be" ? u.pathname.slice(1) : u.searchParams.get("v");
  return id ? `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&mute=1` : url;
}

let activeHls = null;
let refreshTimer = null;

function renderMedia(container, preview) {
  if (activeHls) { activeHls.destroy(); activeHls = null; }
  clearInterval(refreshTimer);
  container.replaceChildren();
  if (!preview) {
    container.textContent = "Pas d'aperçu disponible";
    return;
  }
  const url = preview.url; // images come through the API snapshot proxy
  if (preview.type === "image" || preview.type === "mjpeg") {
    const img = document.createElement("img");
    img.alt = "";
    img.src = url;
    img.onerror = () => (container.textContent = "Image indisponible");
    container.append(img);
    if (preview.type === "image") {
      refreshTimer = setInterval(() => (img.src = `${url}?t=${Date.now()}`), 60_000);
    }
  } else if (preview.type === "hls") {
    const video = Object.assign(document.createElement("video"), { muted: true, autoplay: true, playsInline: true, controls: true });
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
    } else if (window.Hls?.isSupported()) {
      activeHls = new Hls();
      activeHls.loadSource(url);
      activeHls.attachMedia(video);
    }
    container.append(video);
  } else {
    const iframe = document.createElement("iframe");
    iframe.src = preview.type === "youtube" ? youtubeEmbed(url) : url;
    iframe.allow = "autoplay; fullscreen";
    iframe.loading = "lazy";
    container.append(iframe);
  }
}

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------
const map = new maplibregl.Map({
  container: "map",
  style: MAP_STYLE,
  center: [8, 46],
  zoom: 3,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl(), "top-right");
map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-right");

map.on("load", () => {
  map.addSource("webcams", {
    type: "vector",
    tiles: [new URL(`${API}/tiles/{z}/{x}/{y}.pbf`, location.href).href.replace(/%7B/g, "{").replace(/%7D/g, "}")],
    maxzoom: 14,
    attribution: "Webcams © OpenStreetMap contributors & sources",
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
    renderMedia(media, cam.preview);
    popup.setDOMContent(root);
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
  renderMedia(document.getElementById("card-media"), current.preview);
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
  if (e.target.closest("video, iframe")) return;
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
    renderMedia(document.getElementById("card-media"), current.preview);
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

function setToken(value) {
  token = value;
  try { value ? localStorage.setItem("token", value) : localStorage.removeItem("token"); } catch { /* ignore */ }
  document.getElementById("login-btn").hidden = !!value;
  document.getElementById("logout-btn").hidden = !value;
}
setToken(token);

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
