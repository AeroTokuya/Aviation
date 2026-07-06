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

// アイコンの基準半径 px。cameraIcon 内の縮尺計算の分母も兼ねる
const BASE_ICON_RADIUS = 34;

/** ズームに応じたアイコン半径 (広域表示では小さくして重なりを軽減) */
function iconRadius() {
  const z = map.getZoom();
  if (!Number.isFinite(z) || z >= 10) return BASE_ICON_RADIUS;
  if (z >= 8) return 26;
  return 19;
}

/** 撮影方角を示す扇形 (FOV) + カメラ本体の SVG アイコンを作る */
function cameraIcon(heading, fov, color, r) {
  const cx = r, cy = r;
  const s = r / BASE_ICON_RADIUS; // 基準サイズに対する縮尺
  const a0 = ((heading - fov / 2 - 90) * Math.PI) / 180;
  const a1 = ((heading + fov / 2 - 90) * Math.PI) / 180;
  const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
  const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
  const large = fov > 180 ? 1 : 0;
  const svg = `
    <svg width="${r * 2}" height="${r * 2}" viewBox="0 0 ${r * 2} ${r * 2}">
      <path d="M${cx},${cy} L${x0},${y0} A${r},${r} 0 ${large} 1 ${x1},${y1} Z"
            fill="${color}" fill-opacity="0.30" stroke="${color}" stroke-width="1.5"/>
      <circle cx="${cx}" cy="${cy}" r="${11 * s}" fill="${color}" stroke="#fff" stroke-width="${2.5 * s}"/>
      <g transform="translate(${cx},${cy}) scale(${s}) translate(-6,-4.5)">
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

let camerasCache = [];

async function loadCameras() {
  const res = await fetch("/api/cameras");
  const cams = await res.json();
  camerasCache = cams;
  renderMarkers(cams);
}

function renderMarkers(cams) {
  const bounds = [];
  const r = iconRadius();
  lastIconRadius = r;
  for (const cam of cams) {
    let color;
    if (cam.summary) {
      color = CATEGORY_COLORS[cam.summary.flight_category] || CATEGORY_COLORS.UNKNOWN;
    } else {
      color = cam.status === "error" ? CATEGORY_COLORS.UNKNOWN : VIEW_COLOR;
    }
    const icon = cameraIcon(cam.heading_deg, cam.fov_deg, color, r);
    const tooltip = cam.summary
      ? `${cam.name}<br>視程 ${fmtVisibility(cam.summary)} / シーリング ${fmtCeiling(cam.summary)}`
      : `${cam.name}<br>${STATUS_LABELS[cam.status] || ""}`;
    // 推定ありのカメラは常に最前面 (推定なしアイコンに埋もれてクリック不能になるのを防ぐ)
    const zOff = cam.summary ? 10000 : 0;
    if (markers[cam.id]) {
      markers[cam.id].setIcon(icon);
      markers[cam.id].setTooltipContent(tooltip);
      markers[cam.id].setZIndexOffset(zOff);
    } else {
      const m = L.marker([cam.lat, cam.lon], { icon, title: cam.name, zIndexOffset: zOff });
      m.on("click", () => openPanel(cam.id));
      m.bindTooltip(tooltip, { direction: "top", offset: [0, -14] });
      m.addTo(map);
      markers[cam.id] = m;
    }
    bounds.push([cam.lat, cam.lon]);
  }
  if (bounds.length && !map._loadedOnce) {
    // 初期表示は推定できているカメラ群に寄せる (なければ全カメラ)
    const estBounds = cams.filter((c) => c.summary).map((c) => [c.lat, c.lon]);
    map.fitBounds(estBounds.length ? estBounds : bounds, { padding: [70, 70], maxZoom: 11 });
    map._loadedOnce = true;
  }
}

let lastIconRadius = null;
map.on("zoomend", () => {
  // 半径バケットが変わらないズームでは全マーカーの再生成をしない
  if (iconRadius() === lastIconRadius) return;
  renderMarkers(camerasCache);
});

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
  const isImage = cam.image_capable;

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

  // METAR (最寄り観測局が設定されているカメラのみ)
  show("metar-section", !!data.metar);
  if (data.metar) {
    document.getElementById("metar-raw").textContent = data.metar.raw;
    document.getElementById("metar-stale").textContent = data.metar.stale
      ? "(取得失敗のため前回値)"
      : "";
  }

  // 視程トレンド (推定ありのカメラのみ)
  show("trend-section", false);
  if (est && est.visibility_km != null) loadTrend(camId);

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

  // 画像 (フレーム取得可能なカメラ)
  show("img-current-block", isImage && status !== "error");
  show("img-reference-block", isImage && data.has_reference);
  if (isImage) {
    const ts = Date.now();
    const cur = document.getElementById("img-current");
    cur.onerror = () => show("img-current-block", false);
    cur.src = `/api/cameras/${camId}/image/current?t=${ts}`;
    if (data.has_reference) {
      document.getElementById("img-reference").src = `/api/cameras/${camId}/image/reference?t=${ts}`;
    }
    document.getElementById("img-time").textContent = new Date().toLocaleTimeString("ja-JP");
  }

  // ターゲット設定エディタ (基準画像があるカメラのみ)
  if (!editor || editor.camId !== camId) {
    document.getElementById("editor").classList.add("hidden");
    editor = null;
  }
  const editBtn = document.getElementById("open-editor");
  show("open-editor", isImage && data.has_reference);
  editBtn.onclick = () => toggleEditor(data);

  // 基準画像の取得ボタン (取得可能なリモートカメラのみ)
  const capBtn = document.getElementById("capture-reference");
  show("capture-reference", isImage && cam.source_type !== "local");
  capBtn.onclick = async () => {
    const msg = data.has_reference
      ? "既存の晴天時基準画像を現在の画像で上書きします。今は快晴で遠方まで見えていますか?"
      : "現在の画像を晴天時の基準画像として保存します。今は快晴で遠方まで見えていますか?";
    if (!confirm(msg)) return;
    const r = await fetch(`/api/cameras/${camId}/capture_reference`, { method: "POST" });
    alert(r.ok ? "保存しました。" : "保存に失敗しました: " + (await r.text()));
    openPanel(camId);
  };
  let hint = "";
  if (data.last_error) {
    hint = "画像取得エラー: " + data.last_error;
  } else if (status === "needs_reference") {
    hint =
      "晴天時の基準画像が未取得です。快晴の日に下のボタンで取得すると画像比較ができるようになります。距離・標高つきターゲットを config/cameras.json に登録すると視程・シーリング推定が有効になります。";
  } else if (status === "needs_targets") {
    hint =
      "基準画像はあります。config/cameras.json にターゲット (距離・標高・bbox) を登録すると視程・シーリング推定が有効になります。";
  }
  document.getElementById("setup-hint").textContent = hint;

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

/* ---------- 視程トレンド (スパークライン) ---------- */

async function loadTrend(camId) {
  try {
    const res = await fetch(`/api/cameras/${camId}/history?hours=12`);
    if (!res.ok) return;
    const points = (await res.json()).points.filter((p) => p.visibility_km != null);
    if (selectedCameraId !== camId) return;
    if (points.length < 2) {
      show("trend-section", false);
      return;
    }
    show("trend-section", true);
    drawTrend(points);
    setTrendArrow(points);
  } catch {
    show("trend-section", false);
  }
}

function drawTrend(points) {
  const canvas = document.getElementById("trend-canvas");
  const W = (canvas.width = canvas.clientWidth || 360);
  const H = canvas.height;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);

  const t0 = points[0].ts;
  const t1 = points[points.length - 1].ts;
  const span = Math.max(t1 - t0, 1);
  const maxV = Math.max(...points.map((p) => p.visibility_km), 10);
  const pad = 6;
  const x = (ts) => pad + ((ts - t0) / span) * (W - 2 * pad);
  const y = (v) => H - pad - (v / maxV) * (H - 2 * pad);

  // 目盛り (5km / 8km: フライトカテゴリ境界)
  for (const [v, color] of [[5, "#d62828"], [8, "#1f6fd6"]]) {
    if (v < maxV) {
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.35;
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(pad, y(v));
      ctx.lineTo(W - pad, y(v));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
    }
  }

  ctx.strokeStyle = "#14213d";
  ctx.lineWidth = 1.8;
  ctx.beginPath();
  points.forEach((p, i) => {
    const px = x(p.ts), py = y(p.visibility_km);
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  });
  ctx.stroke();
  const last = points[points.length - 1];
  ctx.fillStyle = "#14213d";
  ctx.beginPath();
  ctx.arc(x(last.ts), y(last.visibility_km), 3, 0, Math.PI * 2);
  ctx.fill();
  ctx.font = "10px sans-serif";
  ctx.fillStyle = "#556";
  ctx.fillText(`${maxV.toFixed(0)}km`, 2, 10);
}

function setTrendArrow(points) {
  const arrow = document.getElementById("trend-arrow");
  const last = points[points.length - 1];
  // 約1時間前の値と比較
  const targetTs = last.ts - 3600;
  let past = points[0];
  for (const p of points) {
    if (Math.abs(p.ts - targetTs) < Math.abs(past.ts - targetTs)) past = p;
  }
  if (last.ts - past.ts < 900) {
    arrow.textContent = "";
    return;
  }
  const ratio = last.visibility_km / Math.max(past.visibility_km, 0.1);
  if (ratio > 1.15) {
    arrow.textContent = "↑ 改善傾向";
    arrow.className = "up";
  } else if (ratio < 0.85) {
    arrow.textContent = "↓ 悪化傾向";
    arrow.className = "down";
  } else {
    arrow.textContent = "→ 横ばい";
    arrow.className = "flat";
  }
}

/* ---------- ターゲット設定エディタ ---------- */

let editor = null; // {camId, camLat, camLon, targets, skyBbox, pending}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371.0;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function editorScale() {
  const img = document.getElementById("editor-img");
  return { sx: img.clientWidth / img.naturalWidth, sy: img.clientHeight / img.naturalHeight };
}

function editorRedraw() {
  if (!editor) return;
  const img = document.getElementById("editor-img");
  const canvas = document.getElementById("editor-canvas");
  if (!img.naturalWidth) return;
  canvas.width = img.clientWidth;
  canvas.height = img.clientHeight;
  const { sx, sy } = editorScale();
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.font = "11px sans-serif";

  const drawBox = (bbox, stroke, fill, label) => {
    const [x, y, w, h] = bbox;
    ctx.fillStyle = fill;
    ctx.fillRect(x * sx, y * sy, w * sx, h * sy);
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.6;
    ctx.strokeRect(x * sx, y * sy, w * sx, h * sy);
    if (label) {
      ctx.fillStyle = stroke;
      ctx.fillText(label, x * sx + 2, Math.max(y * sy - 3, 10));
    }
  };

  if (editor.skyBbox) drawBox(editor.skyBbox, "#0aa0c8", "rgba(10,160,200,.12)", "空領域");
  for (const t of editor.targets) {
    drawBox(t.bbox, "#1f6fd6", "rgba(31,111,214,.12)", `${t.name} (${t.distance_km}km)`);
  }
  if (editor.pending) {
    ctx.setLineDash([5, 4]);
    drawBox(editor.pending, "#e07b00", "rgba(224,123,0,.15)", "新規");
    ctx.setLineDash([]);
  }
}

function editorRenderList() {
  const ul = document.getElementById("editor-targets");
  ul.innerHTML = "";
  editor.targets.forEach((t, i) => {
    const li = document.createElement("li");
    const elev = t.elevation_ft_msl != null ? ` / ${t.elevation_ft_msl}ft` : "";
    const span = document.createElement("span");
    span.textContent = `${t.name} — ${t.distance_km}km${elev}`;
    const del = document.createElement("button");
    del.textContent = "削除";
    del.onclick = () => {
      editor.targets.splice(i, 1);
      editorRenderList();
      editorRedraw();
    };
    li.appendChild(span);
    li.appendChild(del);
    ul.appendChild(li);
  });
}

function toggleEditor(data) {
  const div = document.getElementById("editor");
  if (!div.classList.contains("hidden")) {
    div.classList.add("hidden");
    editor = null;
    return;
  }
  editor = {
    camId: data.camera.id,
    camLat: data.camera.lat,
    camLon: data.camera.lon,
    targets: (data.setup.targets || []).map((t) => ({ ...t })),
    skyBbox: data.setup.sky_bbox ? [...data.setup.sky_bbox] : null,
    pending: null,
  };
  div.classList.remove("hidden");
  show("editor-form", false);
  const img = document.getElementById("editor-img");
  img.onload = () => {
    editorRedraw();
    editorRenderList();
  };
  img.src = `/api/cameras/${editor.camId}/image/reference?t=${Date.now()}`;
}

function initEditorEvents() {
  const canvas = document.getElementById("editor-canvas");
  let dragStart = null;

  const toNatural = (ev) => {
    const rect = canvas.getBoundingClientRect();
    const { sx, sy } = editorScale();
    return [
      Math.max(0, Math.round((ev.clientX - rect.left) / sx)),
      Math.max(0, Math.round((ev.clientY - rect.top) / sy)),
    ];
  };

  canvas.addEventListener("pointerdown", (ev) => {
    if (!editor) return;
    canvas.setPointerCapture(ev.pointerId);
    dragStart = toNatural(ev);
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (!editor || !dragStart) return;
    const [x, y] = toNatural(ev);
    editor.pending = [
      Math.min(dragStart[0], x),
      Math.min(dragStart[1], y),
      Math.abs(x - dragStart[0]),
      Math.abs(y - dragStart[1]),
    ];
    editorRedraw();
  });
  canvas.addEventListener("pointerup", () => {
    if (!editor || !dragStart) return;
    dragStart = null;
    if (editor.pending && editor.pending[2] >= 6 && editor.pending[3] >= 6) {
      show("editor-form", true);
    } else {
      editor.pending = null;
      editorRedraw();
    }
  });
  window.addEventListener("resize", editorRedraw);

  document.getElementById("ef-calc").addEventListener("click", () => {
    const lat = parseFloat(document.getElementById("ef-lat").value);
    const lon = parseFloat(document.getElementById("ef-lon").value);
    if (!editor || isNaN(lat) || isNaN(lon)) return;
    const d = haversineKm(editor.camLat, editor.camLon, lat, lon);
    document.getElementById("ef-dist").value = d.toFixed(1);
  });

  document.getElementById("ef-add").addEventListener("click", () => {
    if (!editor || !editor.pending) return;
    const name = document.getElementById("ef-name").value.trim();
    const dist = parseFloat(document.getElementById("ef-dist").value);
    const elevRaw = document.getElementById("ef-elev").value;
    if (!name || isNaN(dist) || dist <= 0) {
      alert("名前と距離 (km) を入力してください。");
      return;
    }
    const t = { name, distance_km: dist, bbox: editor.pending };
    if (elevRaw !== "") t.elevation_ft_msl = parseFloat(elevRaw);
    editor.targets.push(t);
    editor.pending = null;
    ["ef-name", "ef-dist", "ef-elev", "ef-lat", "ef-lon"].forEach(
      (id) => (document.getElementById(id).value = "")
    );
    show("editor-form", false);
    editorRenderList();
    editorRedraw();
  });

  document.getElementById("ef-sky").addEventListener("click", () => {
    if (!editor || !editor.pending) return;
    editor.skyBbox = editor.pending;
    editor.pending = null;
    show("editor-form", false);
    editorRedraw();
  });

  document.getElementById("ef-cancel").addEventListener("click", () => {
    if (!editor) return;
    editor.pending = null;
    show("editor-form", false);
    editorRedraw();
  });

  document.getElementById("ef-save").addEventListener("click", async () => {
    if (!editor) return;
    const camId = editor.camId;
    const res = await fetch(`/api/cameras/${camId}/setup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ targets: editor.targets, sky_bbox: editor.skyBbox }),
    });
    if (res.ok) {
      alert("保存しました。推定を更新します。");
      editor = null;
      document.getElementById("editor").classList.add("hidden");
      openPanel(camId);
      loadCameras();
    } else {
      alert("保存に失敗しました: " + (await res.text()));
    }
  });
}

initEditorEvents();

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
