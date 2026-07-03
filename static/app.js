/* HeliWX フロントエンド: 地図 + カメラアイコン + 推定パネル */

const CATEGORY_COLORS = {
  VFR: "#1e9e4f",
  MVFR: "#1f6fd6",
  IFR: "#d62828",
  LIFR: "#b0179c",
  UNKNOWN: "#888888",
};

// 推定なし (映像視聴のみ / 設定未完了) のアイコン色
const VIEW_COLOR = "#546686";

const STATUS_LABELS = {
  view: "ライブ映像 (推定なし)",
  page: "提供元ページで映像確認",
  needs_reference: "基準画像 未取得",
  needs_targets: "画像比較のみ (ターゲット未設定)",
  error: "画像取得エラー",
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
    let color;
    if (cam.summary) {
      color = CATEGORY_COLORS[cam.summary.flight_category] || CATEGORY_COLORS.UNKNOWN;
    } else {
      color = cam.status === "error" ? CATEGORY_COLORS.UNKNOWN : VIEW_COLOR;
    }
    const icon = cameraIcon(cam.heading_deg, cam.fov_deg, color);
    const tooltip = cam.summary
      ? `${cam.name}<br>視程 ${fmtVisibility(cam.summary)} / シーリング ${fmtCeiling(cam.summary)}`
      : `${cam.name}<br>${STATUS_LABELS[cam.status] || ""}`;
    if (markers[cam.id]) {
      markers[cam.id].setIcon(icon);
      markers[cam.id].setTooltipContent(tooltip);
    } else {
      const m = L.marker([cam.lat, cam.lon], { icon, title: cam.name });
      m.on("click", () => openPanel(cam.id));
      m.bindTooltip(tooltip, { direction: "top", offset: [0, -14] });
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

function show(id, visible) {
  document.getElementById(id).classList.toggle("hidden", !visible);
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
  const cam = data.camera;
  const est = data.estimate;
  const status = data.status;
  const isImage = cam.source_type === "url" || cam.source_type === "local";

  document.getElementById("cam-name").textContent = cam.name;
  document.getElementById("cam-desc").textContent =
    `${cam.description} (方位 ${Math.round(cam.heading_deg)}°, 画角 ${Math.round(cam.fov_deg)}°)`;

  // 提供元の表記とリンク
  const attr = document.getElementById("attribution");
  attr.innerHTML = "";
  if (cam.attribution || cam.page_url) {
    const label = document.createTextNode("映像提供: ");
    attr.appendChild(label);
    if (cam.page_url) {
      const a = document.createElement("a");
      a.href = cam.page_url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = cam.attribution || cam.page_url;
      attr.appendChild(a);
    } else {
      attr.appendChild(document.createTextNode(cam.attribution));
    }
  }

  // ステータスバッジ
  const badge = document.getElementById("category-badge");
  if (est) {
    badge.textContent = est.flight_category;
    badge.className = "badge " + est.flight_category.toLowerCase();
  } else {
    badge.textContent = STATUS_LABELS[status] || status;
    badge.className = "badge neutral";
  }

  // 数値メトリクス (推定ありのときのみ)
  show("metrics", !!est);
  if (est) {
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
  }

  // YouTube ライブ埋め込み
  const video = document.getElementById("video-embed");
  video.innerHTML = "";
  if (cam.source_type === "youtube") {
    const src = cam.youtube_channel_id
      ? `https://www.youtube.com/embed/live_stream?channel=${cam.youtube_channel_id}&autoplay=1&mute=1`
      : `https://www.youtube.com/embed/${cam.youtube_video_id}?autoplay=1&mute=1`;
    const iframe = document.createElement("iframe");
    iframe.src = src;
    iframe.allow = "autoplay; encrypted-media; picture-in-picture";
    iframe.allowFullscreen = true;
    video.appendChild(iframe);
  }
  show("video-embed", cam.source_type === "youtube");

  // 提供元ページ型: リンクボタンのみ
  const pageBtn = document.getElementById("open-page");
  if (cam.source_type === "page") {
    pageBtn.onclick = () => window.open(cam.page_url, "_blank", "noopener");
  }
  show("open-page", cam.source_type === "page");

  // 画像 (url / local カメラ)
  show("img-current-block", isImage && status !== "error");
  show("img-reference-block", isImage && data.has_reference);
  if (isImage) {
    const ts = Date.now();
    document.getElementById("img-current").src = `/api/cameras/${camId}/image/current?t=${ts}`;
    if (data.has_reference) {
      document.getElementById("img-reference").src = `/api/cameras/${camId}/image/reference?t=${ts}`;
    }
    document.getElementById("img-time").textContent = new Date().toLocaleTimeString("ja-JP");
  }

  // 基準画像の取得ボタン (url カメラのみ)
  const capBtn = document.getElementById("capture-reference");
  show("capture-reference", cam.source_type === "url");
  capBtn.onclick = async () => {
    const msg = data.has_reference
      ? "既存の晴天時基準画像を現在の画像で上書きします。今は快晴で遠方まで見えていますか?"
      : "現在の画像を晴天時の基準画像として保存します。今は快晴で遠方まで見えていますか?";
    if (!confirm(msg)) return;
    const r = await fetch(`/api/cameras/${camId}/capture_reference`, { method: "POST" });
    alert(r.ok ? "保存しました。" : "保存に失敗しました: " + (await r.text()));
    openPanel(camId);
  };
  if (status === "needs_reference") {
    document.getElementById("setup-hint").textContent =
      "晴天時の基準画像が未取得です。快晴の日に下のボタンで取得すると画像比較ができるようになります。距離・標高つきターゲットを config/cameras.json に登録すると視程・シーリング推定が有効になります。";
  } else if (status === "needs_targets") {
    document.getElementById("setup-hint").textContent =
      "基準画像はあります。config/cameras.json にターゲット (距離・標高・bbox) を登録すると視程・シーリング推定が有効になります。";
  } else {
    document.getElementById("setup-hint").textContent = "";
  }

  // ターゲット表・注記 (推定ありのときのみ)
  show("targets-section", !!est);
  if (est) {
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
