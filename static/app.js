/* HeliWX フロントエンド: 地図 + カメラアイコン + 推定パネル */

const CATEGORY_COLORS = {
  VFR: "#1e9e4f",
  MVFR: "#1f6fd6",
  IFR: "#d62828",
  LIFR: "#b0179c",
  UNKNOWN: "#888888",
};

const AUTO_REFRESH_MS = 60_000;

const map = L.map("map");
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

let selectedCameraId = null;
let markers = {};
let refreshTimer = null;

/** 撮影方角を示す扇形 (FOV) + カメラ本体の SVG アイコンを作る */
function cameraIcon(heading, fov, color) {
  const r = 34; // 扇形の半径 px
  const cx = r, cy = r;
  const a0 = ((heading - fov / 2 - 90) * Math.PI) / 180;
  const a1 = ((heading + fov / 2 - 90) * Math.PI) / 180;
  const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
  const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
  const large = fov > 180 ? 1 : 0;
  const svg = `
    <svg width="${r * 2}" height="${r * 2}" viewBox="0 0 ${r * 2} ${r * 2}">
      <path d="M${cx},${cy} L${x0},${y0} A${r},${r} 0 ${large} 1 ${x1},${y1} Z"
            fill="${color}" fill-opacity="0.30" stroke="${color}" stroke-width="1.5"/>
      <circle cx="${cx}" cy="${cy}" r="11" fill="${color}" stroke="#fff" stroke-width="2.5"/>
      <g transform="translate(${cx - 6},${cy - 4.5})">
        <rect x="0" y="1" width="8.5" height="7" rx="1.2" fill="#fff"/>
        <path d="M8.5 3.2 L12 1.2 V7.8 L8.5 5.8 Z" fill="#fff"/>
      </g>
    </svg>`;
  return L.divIcon({
    className: "cam-icon",
    html: svg,
    iconSize: [r * 2, r * 2],
    iconAnchor: [r, r],
  });
}

function fmtVisibility(s) {
  if (s == null || s.visibility_km == null) return "—";
  const v = s.visibility_km;
  const txt = v >= 10 ? `${Math.round(v)} km` : v >= 1 ? `${v.toFixed(1)} km` : `${Math.round(v * 1000)} m`;
  return (s.visibility_is_lower_bound ? "≥ " : "約 ") + txt;
}

function fmtCeiling(s) {
  if (s == null) return "—";
  if (s.ceiling_is_unlimited) return "雲遮蔽なし";
  if (s.ceiling_ft_agl == null) return "不明";
  return `約 ${Math.round(s.ceiling_ft_agl).toLocaleString()} ft`;
}

async function loadCameras() {
  const res = await fetch("/api/cameras");
  const cams = await res.json();
  const bounds = [];
  for (const cam of cams) {
    const cat = cam.summary ? cam.summary.flight_category : "UNKNOWN";
    const color = CATEGORY_COLORS[cat] || CATEGORY_COLORS.UNKNOWN;
    const icon = cameraIcon(cam.heading_deg, cam.fov_deg, color);
    if (markers[cam.id]) {
      markers[cam.id].setIcon(icon);
    } else {
      const m = L.marker([cam.lat, cam.lon], { icon, title: cam.name });
      m.on("click", () => openPanel(cam.id));
      m.bindTooltip(
        `${cam.name}<br>視程 ${fmtVisibility(cam.summary)} / シーリング ${fmtCeiling(cam.summary)}`,
        { direction: "top", offset: [0, -14] }
      );
      m.addTo(map);
      markers[cam.id] = m;
    }
    bounds.push([cam.lat, cam.lon]);
  }
  if (bounds.length && !map._loadedOnce) {
    map.fitBounds(bounds, { padding: [70, 70] });
    map._loadedOnce = true;
  }
}

async function openPanel(camId) {
  selectedCameraId = camId;
  const panel = document.getElementById("panel");
  panel.classList.remove("hidden");
  document.getElementById("cam-name").textContent = "読み込み中…";

  const res = await fetch(`/api/cameras/${camId}/estimate`);
  if (!res.ok) {
    document.getElementById("cam-name").textContent = "取得エラー";
    return;
  }
  const data = await res.json();
  if (selectedCameraId !== camId) return; // 別カメラに切替済み
  const est = data.estimate;

  document.getElementById("cam-name").textContent = data.camera.name;
  document.getElementById("cam-desc").textContent =
    `${data.camera.description} (方位 ${Math.round(data.camera.heading_deg)}°, 画角 ${Math.round(data.camera.fov_deg)}°, 標高 ${Math.round(data.camera.elevation_ft)} ft)`;

  const badge = document.getElementById("category-badge");
  badge.textContent = est.flight_category;
  badge.className = "badge " + est.flight_category.toLowerCase();

  document.getElementById("m-vis").textContent = fmtVisibility({
    visibility_km: est.visibility_km,
    visibility_is_lower_bound: est.visibility_is_lower_bound,
  });
  document.getElementById("m-ceil").textContent = fmtCeiling({
    ceiling_is_unlimited: est.ceiling_is_unlimited,
    ceiling_ft_agl: est.ceiling_ft_agl,
  });
  document.getElementById("m-cover").textContent = est.cloud_cover_label
    ? `${est.cloud_cover_label} (${est.cloud_cover_oktas}/8)`
    : "—";

  const ts = Date.now();
  document.getElementById("img-current").src = `/api/cameras/${camId}/image/current?t=${ts}`;
  document.getElementById("img-reference").src = `/api/cameras/${camId}/image/reference?t=${ts}`;
  document.getElementById("img-time").textContent = new Date().toLocaleTimeString("ja-JP");

  const tbody = document.querySelector("#targets-table tbody");
  tbody.innerHTML = "";
  for (const t of est.targets) {
    const tr = document.createElement("tr");
    const elev = t.elevation_ft_msl != null ? `${Math.round(t.elevation_ft_msl).toLocaleString()} ft` : "—";
    const estV = t.visibility_estimate_km != null ? `${t.visibility_estimate_km} km` : "—";
    tr.innerHTML =
      `<td>${t.name}</td><td>${t.distance_km} km</td><td>${elev}</td>` +
      `<td class="${t.visible ? "vis-ok" : "vis-ng"}">${t.visible ? "○" : "×"}</td><td>${estV}</td>`;
    tbody.appendChild(tr);
  }

  const notes = document.getElementById("notes");
  notes.innerHTML = "";
  for (const n of est.notes) {
    const li = document.createElement("li");
    li.textContent = n;
    notes.appendChild(li);
  }
}

document.getElementById("panel-close").addEventListener("click", () => {
  document.getElementById("panel").classList.add("hidden");
  selectedCameraId = null;
});

document.getElementById("refresh").addEventListener("click", () => {
  if (selectedCameraId) openPanel(selectedCameraId);
  loadCameras();
});

function scheduleAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    loadCameras();
    if (selectedCameraId) openPanel(selectedCameraId);
  }, AUTO_REFRESH_MS);
}

loadCameras();
scheduleAutoRefresh();
