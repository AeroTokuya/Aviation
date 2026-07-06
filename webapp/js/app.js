/* HeliNav — VFR ヘリコプター ナビ (PWA)
 * 無料・軽量・オフライン対応。Leaflet + OpenStreetMap。
 * Garmin Pilot / Air Navigation Pro を参考にした飛行中利用向け UI:
 *   NAVバー(GS/TRK/ALT/BRG/DIST/ETE/XTK)・トラックアップ・自動WPシーケンス・
 *   Direct-To検索・NRST・Wake Lock。
 * 参考情報のみ。運航判断は公式情報とパイロットが行うこと。
 */
'use strict';

const DATA_FILES = {
  airports: 'data/airports.geojson',
  heliports: 'data/heliports.geojson',
  navaids: 'data/navaids.geojson',
  hazards: 'data/powerlines.geojson',
  airspace: 'data/airspace.geojson',
};

// 空域クラス。important = 既定で表示
const ASP_CLASSES = {
  CTR:  { label: '管制圏',            color: '#2f6fed', dash: null,   fill: 0.05, on: true },
  INFO: { label: '情報圏',            color: '#1fa294', dash: '6 6',  fill: 0.04, on: true },
  RSTR: { label: '飛行回避(原子力等)', color: '#e0342f', dash: '2 5',  fill: 0.12, on: true },
  TCA:  { label: '進入管制区(概略)',   color: '#8a5cf5', dash: '12 8', fill: 0.02, on: false },
};

// ベースマップ。日本の VFR 用途には地理院タイルが安定・低クラッタで最適。
const BASEMAPS = {
  gsi_pale:  { name: '淡色 (地理院)',    url: 'https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png',          attr: '地理院タイル', maxZoom: 18 },
  gsi_std:   { name: '標準 (地理院)',    url: 'https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png',           attr: '地理院タイル', maxZoom: 18 },
  osm:       { name: 'OSM',             url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',                     attr: '© OpenStreetMap contributors', maxZoom: 18 },
  gsi_photo: { name: '航空写真 (地理院)', url: 'https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.png', attr: '地理院タイル(写真)', maxZoom: 18 },
};
const BASEMAP_FALLBACK = ['gsi_pale', 'osm', 'gsi_std'];

const state = {
  map: null,
  basemap: 'gsi_pale',
  tileLayer: null,
  tileErrs: 0,
  layers: {},          // layer name -> L.LayerGroup
  visible: { airports: true, heliports: true, navaids: true, hazards: true, airspace: true, wx: true, route: true },
  asp: Object.fromEntries(Object.entries(ASP_CLASSES).map(([k, v]) => [k, v.on])),
  wx: { by: {}, at: 0 },        // METAR/TAF: icaoId -> report
  fuel: { burn: 0, onboard: 0 }, // L/h, L
  data: {},            // layer name -> GeoJSON
  route: [],           // [{lat, lon, name}]
  routeMode: false,
  magvar: 8,           // 西偏差(°) 日本は約7〜9°W
  gs: 110,             // 計画地上速度 kt (GPS 無効時の ETE 用)
  own: {
    watchId: null,
    latlng: null,       // L.LatLng
    track: null,        // 真トラック(°)。GPS course or 自己計算
    gsKt: null,         // GPS 対地速度 kt
    altFt: null,        // GPS 高度 ft
    accM: null,         // 水平精度 m
    marker: null,
    accCircle: null,
    prev: null,         // {lat, lon, t} トラック自己計算用
    lastFixT: 0,
  },
  nav: { activeIdx: null, arrived: false },  // route[activeIdx] が現在の目標WP
  view: { mode: 'north', follow: false, rot: 0, rotCont: 0 }, // rot: 画面回転角(deg, 連続値)
  sim: { active: false, timer: null, distNM: 0 },
  wake: null,
  search: [],           // Direct-To 検索インデックス
};

/* ---------------- 幾何・計算 ---------------- */
const R_NM = 3440.065;         // 地球半径 [NM]
const toRad = d => d * Math.PI / 180;
const toDeg = r => r * 180 / Math.PI;

function haversineNM(a, b) {
  const dLat = toRad(b.lat - a.lat), dLon = toRad(b.lon - a.lon);
  const la1 = toRad(a.lat), la2 = toRad(b.lat);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dLon / 2) ** 2;
  return 2 * R_NM * Math.asin(Math.min(1, Math.sqrt(h)));
}
function bearingTrue(a, b) {
  const la1 = toRad(a.lat), la2 = toRad(b.lat), dLon = toRad(b.lon - a.lon);
  const y = Math.sin(dLon) * Math.cos(la2);
  const x = Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(dLon);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}
// from→to のコースに対する pos の横方向偏位 [NM]。正 = コースの右側
function crossTrackNM(from, to, pos) {
  const d13 = haversineNM(from, pos) / R_NM;
  const th13 = toRad(bearingTrue(from, pos));
  const th12 = toRad(bearingTrue(from, to));
  return Math.asin(Math.sin(d13) * Math.sin(th13 - th12)) * R_NM;
}
// 出発点 a から真方位 brg へ distNM 進んだ地点
function destPoint(a, brgDeg, distNM) {
  const d = distNM / R_NM, th = toRad(brgDeg);
  const la1 = toRad(a.lat), lo1 = toRad(a.lon);
  const la2 = Math.asin(Math.sin(la1) * Math.cos(d) + Math.cos(la1) * Math.sin(d) * Math.cos(th));
  const lo2 = lo1 + Math.atan2(Math.sin(th) * Math.sin(d) * Math.cos(la1), Math.cos(d) - Math.sin(la1) * Math.sin(la2));
  return { lat: toDeg(la2), lon: ((toDeg(lo2) + 540) % 360) - 180 };
}
const angDiff = (a, b) => { let d = (a - b) % 360; if (d > 180) d -= 360; if (d < -180) d += 360; return d; };
const trueToMag = t => (t + state.magvar + 360) % 360;   // 西偏差は真方位に加算
const fmtBrg = d => String(Math.round(d)).padStart(3, '0');
const fmtNM = n => n < 10 ? n.toFixed(1) : Math.round(n).toString();
function fmtETE(min) {
  if (!isFinite(min) || min < 0) return '--:--';
  if (min < 60) return `${String(Math.floor(min)).padStart(2, '0')}:${String(Math.round(min % 1 * 60)).padStart(2, '0')}`;
  return `${Math.floor(min / 60)}h${String(Math.round(min % 60)).padStart(2, '0')}`;
}

/* ---------------- 起動 ---------------- */
async function init() {
  restorePrefs();
  buildMap();
  wireUI();
  registerSW();
  await loadAllData();
  buildSearchIndex();
  restoreWx();
  fetchWx();
  setInterval(fetchWx, 10 * 60 * 1000);   // METAR は 10 分ごとに自動更新
  updateNetStatus();
  window.addEventListener('online', () => { updateNetStatus(); fetchWx(); });
  window.addEventListener('offline', updateNetStatus);
  requestWakeLock();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') requestWakeLock();
  });
  startGPS();           // 起動と同時に GPS 取得開始(飛行中に操作不要)
  updateHUD();
}

function buildMap() {
  state.map = L.map('map', {
    zoomControl: false, tap: true, attributionControl: false,
    fadeAnimation: false,  // 回転時のタイルちらつき防止
  }).setView([35.6, 139.7], 8);

  setBasemap(state.basemap);

  for (const name of Object.keys(DATA_FILES)) state.layers[name] = L.layerGroup().addTo(state.map);
  state.layers.wx = L.layerGroup().addTo(state.map);
  state.layers.route = L.layerGroup().addTo(state.map);
  for (const name of Object.keys(state.layers)) if (!state.visible[name]) state.map.removeLayer(state.layers[name]);

  state.map.on('click', onMapClick);
  // ズーム/移動で間引きレイヤーを再描画
  let gateTimer;
  state.map.on('moveend zoomend', () => {
    clearTimeout(gateTimer);
    gateTimer = setTimeout(() => {
      for (const name of Object.keys(GATED)) if (state.visible[name]) renderLayer(name);
      updateZoomClass();
    }, 120);
  });
  updateZoomClass();
  // 手動パンで追従解除(ノースアップ時のみドラッグ可)
  state.map.on('dragstart', () => setFollow(false));
}

/* ---------------- ベースマップ ---------------- */
function setBasemap(key, opts = {}) {
  const bm = BASEMAPS[key] || BASEMAPS.gsi_pale;
  state.basemap = key;
  state.tileErrs = 0;
  if (state.tileLayer) state.map.removeLayer(state.tileLayer);
  state.tileLayer = L.tileLayer(bm.url, { maxZoom: bm.maxZoom, crossOrigin: true });
  // タイル取得失敗が続いたら別ソースへ自動切替(地図が真っ白になるのを防ぐ)
  state.tileLayer.on('tileerror', () => {
    state.tileErrs++;
    if (state.tileErrs === 12 && navigator.onLine) {
      const next = BASEMAP_FALLBACK.find(k => k !== state.basemap && !(opts.tried || []).includes(k));
      if (next) {
        showToast(`地図タイルの取得に失敗 → ${BASEMAPS[next].name} に切替えます`);
        setBasemap(next, { tried: [...(opts.tried || []), state.basemap], auto: true });
        updateBasemapChips();
      }
    }
  });
  state.tileLayer.addTo(state.map);
  document.getElementById('attrib').textContent = '© ' + bm.attr;
  if (!opts.auto) savePrefs();
}
function updateBasemapChips() {
  document.querySelectorAll('#bm-chips .chip').forEach(b =>
    b.classList.toggle('active', b.dataset.bm === state.basemap));
}

// ズームに応じたデクラッタ: 広域ではマーカーを縮小・識別ラベル非表示
function updateZoomClass() {
  const z = state.map.getZoom();
  document.body.classList.toggle('show-ids', z >= 9);
  document.body.classList.toggle('z-lo', z <= 10);
}

/* ---------------- データ読込・描画 ---------------- */
async function loadAllData() {
  await Promise.all(Object.keys(DATA_FILES).map(async name => {
    try {
      // ユーザー取込データがあれば優先
      const custom = localStorage.getItem('heli.data.' + name);
      const gj = custom ? JSON.parse(custom) : await (await fetch(DATA_FILES[name])).json();
      state.data[name] = gj;
      renderLayer(name);
    } catch (e) {
      console.warn('load fail', name, e);
    }
  }));
}

// 大量地物のレイヤーは、表示範囲＋最小ズームでマーカーを間引く(コックピットで見やすく・軽量に)
const GATED = { heliports: 9 };

function renderLayer(name) {
  const grp = state.layers[name];
  grp.clearLayers();
  const gj = state.data[name];
  if (!gj) return;
  if (name === 'hazards') return renderHazards(gj, grp);

  if (name === 'airspace') return renderAirspace(gj, grp);

  const feats = gj.features || [];
  let list = feats;
  if (name in GATED) {
    const z = state.map.getZoom();
    if (z < GATED[name]) return;
    const b = state.map.getBounds();
    list = feats.filter(f => {
      const c = f.geometry && f.geometry.coordinates;
      return c && b.contains([c[1], c[0]]);
    });
    // ズーム 13 未満は病院ヘリパッドのみ表示(密集地の視認性優先)
    if (name === 'heliports' && z < 13) list = list.filter(f => f.properties.type === 'hospital');
    // 描画上限(安全弁)。病院ヘリパッドを優先表示。
    if (list.length > 400) {
      list.sort((a, z2) => (a.properties.type === 'hospital' ? 0 : 1) - (z2.properties.type === 'hospital' ? 0 : 1));
      list = list.slice(0, 400);
    }
  }
  for (const f of list) {
    const c = f.geometry.coordinates;
    L.marker([c[1], c[0]], { icon: facilityIcon(f), keyboard: false })
      .on('click', ev => { L.DomEvent.stop(ev); openFacility(f); })
      .addTo(grp);
  }
}

/* ---------------- 空域 ---------------- */
function renderAirspace(gj, grp) {
  const NM2M = 1852;
  for (const f of (gj.features || [])) {
    const p = f.properties || {};
    const cfg = ASP_CLASSES[p.class];
    if (!cfg || !state.asp[p.class]) continue;
    let shape;
    if (f.geometry.type === 'Point' && p.radius_nm) {
      const [lon, lat] = f.geometry.coordinates;
      shape = L.circle([lat, lon], {
        radius: p.radius_nm * NM2M,
        color: cfg.color, weight: p.class === 'RSTR' ? 2.5 : 2, opacity: 0.85,
        dashArray: cfg.dash, fillColor: cfg.color, fillOpacity: cfg.fill,
        interactive: true, bubblingMouseEvents: false,
      });
    } else if (f.geometry.type === 'Polygon') {
      shape = L.polygon(f.geometry.coordinates[0].map(c => [c[1], c[0]]), {
        color: cfg.color, weight: 2, opacity: 0.85,
        dashArray: cfg.dash, fillColor: cfg.color, fillOpacity: cfg.fill,
        interactive: true, bubblingMouseEvents: false,
      });
    } else continue;
    shape.on('click', ev => { L.DomEvent.stop(ev); openAirspace(p); });
    shape.addTo(grp);
  }
}
function openAirspace(p) {
  const cfg = ASP_CLASSES[p.class] || {};
  const rows = [];
  if (p.apt) rows.push(['関連空港', p.apt]);
  if (p.radius_nm) rows.push(['半径(概略)', p.radius_nm + ' NM']);
  if (p.alt) rows.push(['高度', p.alt]);
  if (p.freq) rows.push(['周波数', p.freq]);
  if (p.note) rows.push(['備考', p.note]);
  const kv = rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join('');
  document.getElementById('sheet-body').innerHTML = `
    <div class="sheet-title"><span class="asp-swatch" style="--c:${cfg.color}"></span>${escapeHtml(p.name || '空域')}</div>
    <div class="sheet-sub">${escapeHtml(cfg.label || p.class || '')}</div>
    <dl class="kv">${kv}</dl>
    <div class="sheet-warn">⚠️ 表示範囲は<b>円による概略</b>です。実際の水平・垂直範囲は AIP Japan・告示で必ず照合してください。</div>
    <div class="sheet-actions"><button class="btn-ghost" id="sheet-close">閉じる</button></div>`;
  document.getElementById('sheet').classList.remove('hidden');
  document.getElementById('sheet-close').onclick = closeSheet;
}

function renderHazards(gj, grp) {
  for (const f of (gj.features || [])) {
    const p = f.properties || {};
    if (f.geometry.type === 'Point' && p.type === 'obstacle') {
      // 障害物: 三角シンボル + 高さ(ft)ラベル
      const [lon, lat] = f.geometry.coordinates;
      const ft = p.height_m ? Math.round(p.height_m * 3.28084) : null;
      const lbl = ft ? `<div class="mk-id ob-id">${ft.toLocaleString()}</div>` : '';
      const icon = L.divIcon({
        className: '', iconSize: [40, 40], iconAnchor: [20, 20],
        html: `<div class="mk-rot"><div class="mk">${SYM.obstacle(p)}</div>${lbl}</div>`,
      });
      L.marker([lat, lon], { icon, keyboard: false })
        .on('click', ev => { L.DomEvent.stop(ev); openObstacle(p, lat, lon); })
        .addTo(grp);
      continue;
    }
    L.geoJSON(f, {
      style: { color: '#ff9f1c', weight: 3, opacity: 0.9, dashArray: '1 6', lineCap: 'round' },
      pointToLayer: (f2, latlng) => L.circleMarker(latlng, { radius: 4, color: '#ff9f1c', weight: 2, fillOpacity: 0.6 }),
      onEachFeature: (f2, layer) => {
        const label = p.name || (p.type === 'tower' ? '送電鉄塔' : '送電線');
        layer.on('click', ev => { L.DomEvent.stop(ev); showToast('⚡ ' + label + (p.sample ? ' (サンプル)' : '')); });
      },
    }).addTo(grp);
  }
}
function openObstacle(p, lat, lon) {
  const rows = [];
  if (p.height_m) rows.push(['高さ', `${Math.round(p.height_m * 3.28084).toLocaleString()} ft (${p.height_m} m) AGL`]);
  if (p.kind) rows.push(['種別', p.kind]);
  rows.push(['座標', `${lat.toFixed(4)}, ${lon.toFixed(4)}`]);
  if (state.own.latlng) {
    const a = { lat: state.own.latlng.lat, lon: state.own.latlng.lng };
    const nm = haversineNM(a, { lat, lon }), brg = bearingTrue(a, { lat, lon });
    rows.push(['現在地から', `${fmtNM(nm)} NM / 磁 ${fmtBrg(trueToMag(brg))}°`]);
  }
  const kv = rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join('');
  document.getElementById('sheet-body').innerHTML = `
    <div class="sheet-title">⚠ ${escapeHtml(p.name || '障害物')}</div>
    <div class="sheet-sub">障害物${p.sample ? ' (収録サンプル)' : ' (OSM 由来)'}</div>
    <dl class="kv">${kv}</dl>
    <div class="sheet-warn">⚠️ 障害物データは<b>網羅的ではありません</b>。低高度飛行時は航空図・現地情報で必ず確認してください。</div>
    <div class="sheet-actions"><button class="btn-ghost" id="sheet-close">閉じる</button></div>`;
  document.getElementById('sheet').classList.remove('hidden');
  document.getElementById('sheet-close').onclick = closeSheet;
}

/* 航空図スタイルの SVG シンボル (VFRチャート風: 管制=青 / 非管制=マゼンタ) */
const SYM = {
  airport(p) {
    const col = p.freqs && p.freqs.TWR ? 'var(--sym-apt-twr)' : 'var(--sym-apt)';
    const big = p.class === 'large airport';
    const r = big ? 8.5 : 7;
    return `<svg viewBox="0 0 24 24" class="${big ? 'sym-lg' : ''}">
      <circle cx="12" cy="12" r="${r}" fill="var(--sym-bg)" stroke="${col}" stroke-width="1.8"/>
      <rect x="10.8" y="${12 - r + 1.6}" width="2.4" height="${(r - 1.6) * 2}" rx="1.1" fill="${col}" transform="rotate(45 12 12)"/>
    </svg>`;
  },
  heliport() {
    return `<svg viewBox="0 0 24 24" class="sym-sm">
      <circle cx="12" cy="12" r="7" fill="var(--sym-bg)" stroke="var(--sym-heli)" stroke-width="1.8"/>
      <path d="M9.4 8.5v7M14.6 8.5v7M9.4 12h5.2" stroke="var(--sym-heli)" stroke-width="1.9" stroke-linecap="round"/>
    </svg>`;
  },
  hospital() {
    return `<svg viewBox="0 0 24 24">
      <circle cx="12" cy="12" r="7.5" fill="var(--sym-bg)" stroke="var(--sym-hosp)" stroke-width="1.9"/>
      <path d="M9.2 8.4v7.2M14.8 8.4v7.2M9.2 12h5.6" stroke="var(--sym-hosp)" stroke-width="2" stroke-linecap="round"/>
    </svg>`;
  },
  obstacle(p) {
    // 航空図式の障害物シンボル (高さ 150m 以上は塔形を強調)
    const tall = (p.height_m || 0) >= 150;
    return `<svg viewBox="0 0 24 24" class="sym-sm">
      <path d="M12 3.5 L18.5 20 H5.5 Z" fill="${tall ? 'var(--hazard)' : 'var(--sym-bg)'}" fill-opacity="${tall ? .45 : 1}" stroke="var(--hazard)" stroke-width="1.8" stroke-linejoin="round"/>
      <circle cx="12" cy="3.5" r="1.7" fill="var(--hazard)"/>
    </svg>`;
  },
  navaid(p) {
    if ((p.kind || '').startsWith('NDB')) {
      return `<svg viewBox="0 0 24 24" class="sym-sm">
        <circle cx="12" cy="12" r="7" fill="none" stroke="var(--sym-apt)" stroke-width="1.7" stroke-dasharray="1.5 3" stroke-linecap="round"/>
        <circle cx="12" cy="12" r="2.1" fill="var(--sym-apt)"/>
      </svg>`;
    }
    return `<svg viewBox="0 0 24 24">
      <path d="M7.5 5.5 h9 L21 12 l-4.5 6.5 h-9 L3 12 Z" fill="var(--sym-bg)" stroke="var(--sym-nav)" stroke-width="1.8" stroke-linejoin="round"/>
      <circle cx="12" cy="12" r="1.9" fill="var(--sym-nav)"/>
    </svg>`;
  },
};
function facilityIcon(f) {
  const p = f.properties || {};
  const t = p.type || 'airport';
  const svg = (SYM[t] || SYM.airport)(p);
  // 地図回転を打ち消すラッパー(.mk-rot)にシンボル＋識別ラベルを入れる。
  // 見た目は小さく、タップ領域は 40px 確保。
  const id = (t === 'airport' || t === 'navaid') && p.ident ? `<div class="mk-id">${escapeHtml(p.ident)}</div>` : '';
  return L.divIcon({
    className: '', iconSize: [40, 40], iconAnchor: [20, 20],
    html: `<div class="mk-rot"><div class="mk">${svg}</div>${id}</div>`,
  });
}
// 検索/NRST 結果行用の小型シンボル
function listSym(type) {
  const p = type === 'airport' ? { freqs: { TWR: 1 } } : {};
  return `<span class="res-ic">${(SYM[type] || SYM.airport)(p)}</span>`;
}

/* ---------------- 気象 (METAR/TAF) ---------------- */
// aviationweather.gov のデータ API (無料・CORS 可)。日本全域の METAR+TAF を一括取得。
const WX_URL = 'https://aviationweather.gov/api/data/metar?bbox=24,122,46,148&format=json&taf=true';

async function fetchWx(manual) {
  if (!navigator.onLine) { if (manual) showToast('オフライン: 保存済みの気象を表示中'); return; }
  try {
    const res = await fetch(WX_URL);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const arr = await res.json();
    const by = {};
    for (const m of arr) if (m.icaoId) by[m.icaoId] = m;
    if (!Object.keys(by).length) throw new Error('データなし');
    state.wx.by = by;
    state.wx.at = Date.now();
    try { localStorage.setItem('heli.wx', JSON.stringify({ at: state.wx.at, by })); } catch (_) {}
    renderWx();
    if (manual) showToast(`METAR ${Object.keys(by).length} 局を更新しました`);
  } catch (e) {
    if (manual) showToast('気象取得失敗: ' + e.message);
  }
}
function restoreWx() {
  try {
    const w = JSON.parse(localStorage.getItem('heli.wx') || 'null');
    if (w && w.by) { state.wx.by = w.by; state.wx.at = w.at || 0; renderWx(); }
  } catch (_) {}
}
// フライトカテゴリ判定 (ceiling ft / 視程 SM)
function wxCategory(m) {
  let vis = m.visib;
  if (typeof vis === 'string') vis = parseFloat(vis);
  if (vis == null || isNaN(vis)) vis = 10;
  let ceil = Infinity;
  for (const c of (m.clouds || [])) {
    if (['BKN', 'OVC', 'OVX', 'VV'].includes(c.cover) && c.base != null) ceil = Math.min(ceil, c.base);
  }
  if (ceil < 500 || vis < 1) return 'LIFR';
  if (ceil < 1000 || vis < 3) return 'IFR';
  if (ceil <= 3000 || vis <= 5) return 'MVFR';
  return 'VFR';
}
const wxStale = () => state.wx.at && (Date.now() - state.wx.at) > 70 * 60 * 1000;
function renderWx() {
  const grp = state.layers.wx;
  grp.clearLayers();
  if (!state.data.airports) return;
  const stale = wxStale() ? ' stale' : '';
  for (const f of state.data.airports.features) {
    const p = f.properties || {};
    const m = state.wx.by[p.ident];
    if (!m) continue;
    const cat = wxCategory(m);
    const [lon, lat] = f.geometry.coordinates;
    const icon = L.divIcon({
      className: '', iconSize: [40, 40], iconAnchor: [20, 20],
      html: `<div class="mk-rot"><div class="wx-dot ${cat.toLowerCase()}${stale}">${cat}</div></div>`,
    });
    L.marker([lat, lon], { icon, keyboard: false, zIndexOffset: 500 })
      .on('click', ev => { L.DomEvent.stop(ev); openFacility(f); })
      .addTo(grp);
  }
}
// シート用: METAR 解読 + 生電文
function wxBlock(ident) {
  const m = state.wx.by[ident];
  if (!m) return '';
  const cat = wxCategory(m);
  const age = state.wx.at ? Math.round((Date.now() - state.wx.at) / 60000) : null;
  const rows = [];
  if (m.wdir != null && m.wspd != null) {
    const dir = m.wdir === 0 && m.wspd === 0 ? 'CALM' : (typeof m.wdir === 'number' ? fmtBrg(m.wdir) + '°' : String(m.wdir));
    rows.push(['風', `${dir} ${m.wspd}kt` + (m.wgst ? ` G${m.wgst}` : '')]);
  }
  if (m.visib != null) {
    const v = typeof m.visib === 'string' ? parseFloat(m.visib) : m.visib;
    const plus = typeof m.visib === 'string' && m.visib.includes('+');
    if (!isNaN(v)) rows.push(['視程', `${plus ? '≥' : ''}${(v * 1.609).toFixed(v * 1.609 < 5 ? 1 : 0)} km`]);
  }
  const cl = (m.clouds || []).filter(c => c.cover && c.cover !== 'CAVOK').map(c => c.cover + (c.base != null ? ' ' + c.base.toLocaleString() + 'ft' : '')).join(' / ');
  if (cl) rows.push(['雲', cl]);
  if (m.temp != null) rows.push(['気温/露点', `${m.temp}°C / ${m.dewp != null ? m.dewp + '°C' : '--'}`]);
  if (m.altim != null) rows.push(['QNH', `${Math.round(m.altim)} hPa`]);
  const kv = rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join('');
  return `
    <div class="wx-head">
      <span class="pill ${cat.toLowerCase()}">${cat}</span>
      <span class="wx-age">${age != null ? `取得 ${age} 分前` : ''}${wxStale() ? ' ⚠古い' : ''}</span>
    </div>
    <dl class="kv">${kv}</dl>
    ${m.rawOb ? `<pre class="raw">${escapeHtml(m.rawOb)}</pre>` : ''}
    ${m.rawTaf ? `<pre class="raw">${escapeHtml(m.rawTaf)}</pre>` : ''}`;
}

/* ---------------- 日の出・日の入 (NOAA 略算) ---------------- */
function sunTimes(lat, lng, date = new Date()) {
  const rad = Math.PI / 180, dayMs = 864e5, J1970 = 2440588, J2000 = 2451545;
  const toJulian = d => d.valueOf() / dayMs - 0.5 + J1970;
  const fromJulian = j => new Date((j + 0.5 - J1970) * dayMs);
  const lw = rad * -lng, phi = rad * lat;
  const d = toJulian(date) - J2000;
  const n = Math.round(d - 0.0009 - lw / (2 * Math.PI));
  const approxTransit = Ht => 0.0009 + (Ht + lw) / (2 * Math.PI) + n;
  const ds = approxTransit(0);
  const M = rad * (357.5291 + 0.98560028 * ds);
  const Lsun = M + rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M)) + rad * 102.9372 + Math.PI;
  const dec = Math.asin(Math.sin(Lsun) * Math.sin(rad * 23.4397));
  const Jnoon = J2000 + ds + 0.0053 * Math.sin(M) - 0.0069 * Math.sin(2 * Lsun);
  const cosH = (Math.sin(rad * -0.833) - Math.sin(phi) * Math.sin(dec)) / (Math.cos(phi) * Math.cos(dec));
  if (cosH < -1 || cosH > 1) return null;   // 白夜/極夜
  const w0 = Math.acos(cosH);
  const Jset = J2000 + approxTransit(w0) + 0.0053 * Math.sin(M) - 0.0069 * Math.sin(2 * Lsun);
  return { rise: fromJulian(Jnoon - (Jset - Jnoon)), set: fromJulian(Jset) };
}
const fmtHM = d => d.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });

/* ---------------- 滑走路ミニ図 ---------------- */
function rwyDiagram(p) {
  if (!p.rwy) return '';
  const m = String(p.rwy).match(/^(\d{2})/);
  if (!m) return '';
  const deg = parseInt(m[1], 10) * 10;
  return `<div class="rwy-box">
    <svg viewBox="0 0 84 84">
      <circle cx="42" cy="42" r="38" fill="none" stroke="var(--line)" stroke-width="1.5"/>
      <text x="42" y="12" text-anchor="middle" font-size="9" fill="var(--muted)" font-weight="700">N</text>
      <g transform="rotate(${deg} 42 42)">
        <rect x="37.5" y="10" width="9" height="64" rx="2" fill="var(--muted)"/>
        <path d="M42 16v52" stroke="var(--panel-solid)" stroke-width="1.6" stroke-dasharray="5 4"/>
      </g>
    </svg>
    <div class="rwy-lbl">RWY ${escapeHtml(String(p.rwy))}${p.rwy_cnt > 1 ? ` ×${p.rwy_cnt}` : ''}</div>
  </div>`;
}

/* ---------------- 施設シート ---------------- */
function openFacility(f) {
  const p = f.properties || {};
  const [lon, lat] = f.geometry.coordinates;
  const title = p.name || p.ident || '施設';
  const rows = [];
  if (p.ident) rows.push(['識別', p.ident + (p.iata ? ' / ' + p.iata : '')]);
  if (p.class) rows.push(['種別', p.class]);
  if (p.kind) rows.push(['種別', p.kind]);
  if (p.muni) rows.push(['所在', p.muni]);
  if (p.freq != null) rows.push(['周波数', String(p.freq)]);
  else if (p.type === 'navaid') rows.push(['周波数', '要 AIP 照合']);
  if (p.ch) rows.push(['CH', p.ch]);
  if (p.apt) rows.push(['関連空港', p.apt]);
  if (p.power) rows.push(['出力', p.power]);
  if (p.freqs) {
    const fl = Object.entries(p.freqs).map(([k, v]) => `${k} ${v}`).join(' / ');
    rows.push(['通信', fl]);
  }
  if (p.elev_ft != null) rows.push(['標高', p.elev_ft.toLocaleString() + ' ft']);
  if (p.rwy_ft != null) rows.push(['滑走路', `${p.rwy_ft.toLocaleString()} ft` + (p.rwy ? ` (RWY ${p.rwy})` : '') + (p.rwy_cnt > 1 ? ` ×${p.rwy_cnt}` : '')]);
  if (p.cat) rows.push(['区分', p.cat]);
  if (p.note) rows.push(['備考', p.note]);
  rows.push(['座標', `${lat.toFixed(4)}, ${lon.toFixed(4)}`]);

  let fromOwn = '';
  if (state.own.latlng) {
    const a = { lat: state.own.latlng.lat, lon: state.own.latlng.lng };
    const b = { lat, lon };
    const nm = haversineNM(a, b), brg = bearingTrue(a, b);
    fromOwn = `<div class="sheet-sub">現在地から: <b>${fmtNM(nm)} NM</b> / 磁 ${fmtBrg(trueToMag(brg))}° (真 ${fmtBrg(brg)}°)</div>`;
  }

  // 日の出/日の入 (VFR 日中制限の確認用)
  const sun = sunTimes(lat, lon);
  if (sun) rows.push(['日出/日没', `${fmtHM(sun.rise)} / ${fmtHM(sun.set)} JST`]);

  const kv = rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join('');
  const warn = p.type === 'navaid'
    ? `<div class="sheet-warn">⚠️ 周波数・位置はオープンデータ(OurAirports)由来で、日本の航法無線施設は再編により<b>古い/相違の可能性</b>があります。必ず最新の AIP Japan で照合してください。</div>`
    : '';
  const wx = p.type === 'airport' ? wxBlock(p.ident) : '';
  document.getElementById('sheet-body').innerHTML = `
    <div class="sheet-title">${escapeHtml(title)}</div>
    <div class="sheet-sub">${escapeHtml(p.name_en || typeLabel(p.type))}</div>
    ${fromOwn}
    ${wx}
    <div class="sheet-cols">
      <dl class="kv">${kv}</dl>
      ${rwyDiagram(p)}
    </div>
    ${warn}
    <div class="sheet-actions">
      <button class="btn-primary" id="sheet-direct">D→ ダイレクト</button>
      <button class="btn-ghost" id="sheet-add">＋ ルートに追加</button>
      <button class="btn-ghost" id="sheet-close">閉じる</button>
    </div>`;
  document.getElementById('sheet').classList.remove('hidden');
  document.getElementById('sheet-add').onclick = () => { addWaypoint(lat, lon, p.ident || title); closeSheet(); };
  document.getElementById('sheet-direct').onclick = () => { directTo(lat, lon, p.ident || title); closeSheet(); };
  document.getElementById('sheet-close').onclick = closeSheet;
}
function typeLabel(t) {
  return { airport: '空港', heliport: 'ヘリポート', hospital: 'ドクターヘリ基地病院', navaid: '航法無線施設' }[t] || '';
}
function closeSheet() { document.getElementById('sheet').classList.add('hidden'); }

/* ---------------- ルート・アクティブレグ ---------------- */
function onMapClick(e) {
  if (!state.routeMode) return;
  const ll = correctedLatLng(e);
  addWaypoint(ll.lat, ll.lng, 'WP' + (state.route.length + 1));
}
// トラックアップ(回転)中は Leaflet の座標変換が回転を知らないため補正する
function correctedLatLng(e) {
  const rot = state.view.rot;
  if (!rot || !e.originalEvent) return e.latlng;
  const cx = window.innerWidth / 2, cy = window.innerHeight / 2 + currentShiftPx();
  const dx = e.originalEvent.clientX - cx, dy = e.originalEvent.clientY - cy;
  const a = toRad(-rot);
  const ux = dx * Math.cos(a) - dy * Math.sin(a);
  const uy = dx * Math.sin(a) + dy * Math.cos(a);
  const size = state.map.getSize();
  return state.map.containerPointToLatLng([size.x / 2 + ux, size.y / 2 + uy]);
}
function addWaypoint(lat, lon, name) {
  state.route.push({ lat, lon, name });
  if (state.nav.activeIdx == null && state.route.length >= 2) setActiveWp(1);
  redrawRoute();
  openRoutebar();
  showToast('ウェイポイント追加: ' + name);
}
function directTo(lat, lon, name) {
  const from = state.own.latlng
    ? { lat: state.own.latlng.lat, lon: state.own.latlng.lng, name: '現在地' }
    : (() => { const c = state.map.getCenter(); return { lat: c.lat, lon: c.lng, name: '地図中心' }; })();
  state.route = [from, { lat, lon, name }];
  setActiveWp(1);
  redrawRoute();
  openRoutebar();
  showToast('D→ ' + name);
  if (state.own.latlng) setFollow(true);
}
function setActiveWp(i) {
  state.nav.activeIdx = i;
  state.nav.arrived = false;
  redrawRoute();
  updateHUD();
}
function redrawRoute() {
  const grp = state.layers.route;
  grp.clearLayers();
  const act = state.nav.activeIdx;
  // レグごとに描画: アクティブレグ=マゼンタ(太)、それ以外=シアン
  for (let i = 1; i < state.route.length; i++) {
    const a = state.route[i - 1], b = state.route[i];
    const active = i === act;
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
      color: active ? '#ff3ec8' : '#35c2ff', weight: active ? 6 : 4, opacity: active ? 1 : 0.85,
    }).addTo(grp);
  }
  state.route.forEach((w, i) => {
    const cls = i === act ? 'wp-marker active' : 'wp-marker';
    const m = L.marker([w.lat, w.lon], {
      icon: L.divIcon({ className: '', iconSize: [34, 34], iconAnchor: [17, 17], html: `<div class="mk-rot"><div class="${cls}">${i + 1}</div><div class="mk-id">${escapeHtml(w.name)}</div></div>` }),
      draggable: true,
    }).addTo(grp);
    m.on('dragend', ev => { const ll = ev.target.getLatLng(); w.lat = ll.lat; w.lon = ll.lng; redrawRoute(); updateHUD(); });
    m.on('click', ev => { L.DomEvent.stop(ev); });
  });
  updateRoutebar();
}
function updateRoutebar() {
  const legsEl = document.getElementById('rb-legs');
  let total = 0;
  const rows = [];
  const gsEff = effectiveGS();
  for (let i = 1; i < state.route.length; i++) {
    const a = state.route[i - 1], b = state.route[i];
    const nm = haversineNM(a, b), brg = bearingTrue(a, b);
    total += nm;
    const ete = gsEff > 0 ? (nm / gsEff) * 60 : Infinity;
    const active = i === state.nav.activeIdx;
    rows.push(`<div class="leg${active ? ' leg-active' : ''}" data-act="${i}">
      <span class="num">${active ? '▶' : i}</span>
      <span class="name">${escapeHtml(a.name)} → ${escapeHtml(b.name)}</span>
      <span class="brg">磁 ${fmtBrg(trueToMag(brg))}°</span>
      <span class="nm">${fmtNM(nm)}NM · ${fmtETE(ete)}</span>
      <button class="del" data-i="${i}">×</button>
    </div>`);
  }
  legsEl.innerHTML = rows.join('') || '<div class="rb-hint">ウェイポイントが2点以上でルートを表示します。</div>';
  legsEl.querySelectorAll('.del').forEach(btn => btn.onclick = ev => {
    ev.stopPropagation();
    const i = +btn.dataset.i;
    state.route.splice(i, 1);
    if (state.nav.activeIdx != null) {
      if (state.route.length < 2) state.nav.activeIdx = null;
      else if (state.nav.activeIdx >= state.route.length) state.nav.activeIdx = state.route.length - 1;
      else if (i < state.nav.activeIdx) state.nav.activeIdx--;
    }
    redrawRoute(); updateHUD();
  });
  legsEl.querySelectorAll('.leg').forEach(el => el.onclick = () => setActiveWp(+el.dataset.act));
  const etaTot = gsEff > 0 ? (total / gsEff) * 60 : Infinity;
  // 燃料計画 (Garmin Pilot 参考): 必要燃料 + 30分予備
  let fuelTxt = '';
  if (state.fuel.burn > 0 && isFinite(etaTot) && state.route.length >= 2) {
    const req = etaTot / 60 * state.fuel.burn;
    const reserve = state.fuel.burn * 0.5;
    fuelTxt = ` · 燃料 ${Math.round(req)}+予備${Math.round(reserve)}L`;
    if (state.fuel.onboard > 0) fuelTxt += req + reserve > state.fuel.onboard ? ' ⚠不足' : ` / 搭載${Math.round(state.fuel.onboard)}L`;
  }
  document.getElementById('rb-summary').textContent =
    state.route.length >= 2 ? `全長 ${fmtNM(total)} NM · ${state.route.length - 1}区間 · ${fmtETE(etaTot)} @${Math.round(gsEff)}kt${fuelTxt}` : '地図/施設をタップ';
}
function openRoutebar() { document.getElementById('routebar').classList.remove('hidden'); }
function clearRoute() { state.route = []; state.nav.activeIdx = null; redrawRoute(); updateHUD(); }

// ETE 計算に使う速度: GPS の実測 GS(20kt以上) > 計画速度
function effectiveGS() {
  return (state.own.gsKt != null && state.own.gsKt >= 20) ? state.own.gsKt : state.gs;
}

// WP 自動シーケンス: 0.3NM 以内 or 通過(コースに対し WP が後方) で次レグへ
function checkSequence() {
  const i = state.nav.activeIdx;
  if (i == null || !state.own.latlng || i >= state.route.length) return;
  const pos = { lat: state.own.latlng.lat, lon: state.own.latlng.lng };
  const wp = state.route[i];
  const dist = haversineNM(pos, wp);
  const from = state.route[i - 1];
  const course = bearingTrue(from, wp);
  const passed = dist < 3 && Math.abs(angDiff(bearingTrue(pos, wp), course)) > 110;
  if (dist < 0.3 || passed) {
    if (i + 1 < state.route.length) {
      setActiveWp(i + 1);
      showToast('▶ 次のWP: ' + state.route[i + 1].name);
    } else if (!state.nav.arrived) {
      state.nav.arrived = true;
      showToast('🏁 最終WP ' + wp.name + ' に到達');
    }
  }
}

/* ---------------- GPS・自機 ---------------- */
function startGPS() {
  if (!navigator.geolocation) { setGpsDot(false); showToast('位置情報が使えません'); return; }
  if (state.own.watchId != null) return;   // 多重 watch 防止
  state.own.watchId = navigator.geolocation.watchPosition(onPos, err => {
    setGpsDot(false);
    if (err.code === err.PERMISSION_DENIED) showToast('位置情報が許可されていません');
  }, { enableHighAccuracy: true, maximumAge: 1000, timeout: 20000 });
}
function onPos(pos) {
  if (state.sim.active) return;   // デモ飛行中は実GPSを無視
  const c = pos.coords;
  handleFix({
    lat: c.latitude, lon: c.longitude,
    gsKt: (c.speed != null && !isNaN(c.speed)) ? c.speed * 1.94384 : null,
    heading: (c.heading != null && !isNaN(c.heading)) ? c.heading : null,
    altFt: (c.altitude != null && !isNaN(c.altitude)) ? c.altitude * 3.28084 : null,
    accM: c.accuracy, t: pos.timestamp || Date.now(),
  });
}
// GPS/シミュレーション共通の測位処理
function handleFix(fix) {
  const own = state.own;
  const cur = { lat: fix.lat, lon: fix.lon, t: fix.t };
  // トラック: GPS course があり移動中ならそれを、無ければ位置差分から自己計算
  if (fix.heading != null && (fix.gsKt == null || fix.gsKt > 3)) {
    own.track = fix.heading;
  } else if (own.prev) {
    const d = haversineNM(own.prev, cur);
    if (d > 0.005) {   // 約9m 以上動いたら更新(静止時のふらつき防止)
      own.track = bearingTrue(own.prev, cur);
      if (fix.gsKt == null) {
        const dtH = (cur.t - own.prev.t) / 3600000;
        if (dtH > 0) fix.gsKt = d / dtH;
      }
    }
  }
  if (!own.prev || haversineNM(own.prev, cur) > 0.005) own.prev = cur;

  own.latlng = L.latLng(fix.lat, fix.lon);
  own.gsKt = fix.gsKt;
  own.altFt = fix.altFt;
  own.accM = fix.accM;
  own.lastFixT = fix.t;
  setGpsDot(true, fix.accM);
  drawOwnship();
  checkSequence();
  updateHUD();
  if (state.view.follow) followView();
}
function drawOwnship() {
  const own = state.own;
  if (!own.latlng) return;
  // 画面上の機首角 = トラック + 地図回転角
  const scr = ((own.track || 0) + state.view.rot) % 360;
  const html = `<div class="ownship" style="transform:rotate(${scr}deg)">
    <svg width="34" height="34" viewBox="0 0 26 26">
      <path d="M13 1.2 L22.5 24 L13 18.6 L3.5 24 Z" fill="#00c2ff" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M13 6.5 L18.6 21.2 L13 18 Z" fill="rgba(0,0,0,.25)"/>
    </svg></div>`;
  const icon = L.divIcon({ className: '', iconSize: [34, 34], iconAnchor: [17, 17], html });
  if (!own.marker) own.marker = L.marker(own.latlng, { icon, interactive: false, zIndexOffset: 1000 }).addTo(state.map);
  else { own.marker.setLatLng(own.latlng); own.marker.setIcon(icon); }
  if (own.accM != null && own.accM > 30) {
    if (!own.accCircle) own.accCircle = L.circle(own.latlng, { radius: own.accM, weight: 1, color: '#35c2ff', opacity: .5, fillOpacity: .06, interactive: false }).addTo(state.map);
    else { own.accCircle.setLatLng(own.latlng); own.accCircle.setRadius(own.accM); }
  } else if (own.accCircle) { state.map.removeLayer(own.accCircle); own.accCircle = null; }
}

/* ---------------- ビュー(追従・トラックアップ) ---------------- */
const maprotEl = () => document.getElementById('maprot');
function currentShiftPx() {
  // トラックアップ時は自機を画面下 1/3 に置き前方を広く見せる
  return state.view.mode === 'track' ? Math.round(window.innerHeight * 0.18) : 0;
}
function setFollow(on) {
  state.view.follow = on;
  document.getElementById('btn-locate').classList.toggle('active', on);
  if (on) followView();
}
function followView() {
  const own = state.own;
  if (!own.latlng) return;
  state.map.setView(own.latlng, state.map.getZoom(), { animate: false });
  if (state.view.mode === 'track' && own.track != null) applyRotation(-own.track);
}
function applyRotation(deg) {
  // 連続角で保持し 359→0 の逆回転を防ぐ
  state.view.rotCont += angDiff(deg, ((state.view.rotCont % 360) + 360) % 360);
  state.view.rot = ((state.view.rotCont % 360) + 360) % 360;
  maprotEl().style.transform = `translateY(${currentShiftPx()}px) rotate(${state.view.rotCont}deg)`;
  document.documentElement.style.setProperty('--crot', `${-state.view.rotCont}deg`);
  drawOwnship();
}
function setOrientation(mode) {
  state.view.mode = mode;
  const btn = document.getElementById('btn-orient');
  const track = mode === 'track';
  btn.textContent = track ? 'TRK↑' : 'N↑';
  btn.classList.toggle('active', track);
  document.body.classList.toggle('trackup', track);
  // トラックアップ中はジェスチャを無効化(回転座標系で誤動作するため)。ズームは大ボタンで。
  const m = state.map;
  if (track) {
    m.dragging.disable(); m.touchZoom.disable(); m.doubleClickZoom.disable(); m.scrollWheelZoom.disable();
  } else {
    m.dragging.enable(); m.touchZoom.enable(); m.doubleClickZoom.enable(); m.scrollWheelZoom.enable();
  }
  m.invalidateSize({ pan: false });
  if (track) {
    setFollow(true);
    applyRotation(state.own.track != null ? -state.own.track : 0);
  } else {
    applyRotation(0);
    maprotEl().style.transform = '';
    if (state.view.follow) followView();
  }
  savePrefs();
}

/* ---------------- NAVバー(HUD) ---------------- */
function updateHUD() {
  const own = state.own;
  const set = (id, v) => { document.getElementById(id).textContent = v; };
  set('hud-gs', own.gsKt != null ? String(Math.round(own.gsKt)) : '--');
  set('hud-trk', own.track != null ? fmtBrg(trueToMag(own.track)) : '---');
  set('hud-alt', own.altFt != null ? String(Math.round(own.altFt)) : '----');

  const i = state.nav.activeIdx;
  const hasNav = i != null && i < state.route.length;
  const wpEl = document.getElementById('hud-wpname');
  if (!hasNav) {
    set('hud-wpname', '-----'); set('hud-brg', '---'); set('hud-dist', '--');
    set('hud-ete', '--:--'); set('hud-xtk', '--');
    wpEl.classList.remove('mag');
    return;
  }
  const wp = state.route[i];
  set('hud-wpname', wp.name);
  wpEl.classList.add('mag');
  const ref = own.latlng ? { lat: own.latlng.lat, lon: own.latlng.lng } : null;
  if (!ref) { set('hud-brg', '---'); set('hud-dist', '--'); set('hud-ete', '--:--'); set('hud-xtk', '--'); return; }
  const dist = haversineNM(ref, wp);
  const brg = bearingTrue(ref, wp);
  set('hud-brg', fmtBrg(trueToMag(brg)));
  set('hud-dist', fmtNM(dist));
  const gsEff = effectiveGS();
  set('hud-ete', fmtETE(gsEff > 0 ? dist / gsEff * 60 : Infinity));
  // XTK: アクティブレグからの偏位。L/R = コースのどちら側に居るか
  const from = state.route[i - 1];
  const xtk = crossTrackNM(from, wp, ref);
  set('hud-xtk', Math.abs(xtk) < 0.05 ? '0.0' : `${Math.abs(xtk).toFixed(1)} ${xtk > 0 ? 'R' : 'L'}`);
}

/* ---------------- Direct-To 検索・NRST ---------------- */
function buildSearchIndex() {
  const idx = [];
  const push = (f, type) => {
    const p = f.properties || {};
    const [lon, lat] = f.geometry.coordinates;
    idx.push({
      lat, lon, type,
      ident: p.ident || '', iata: p.iata || '', name: p.name || '', muni: p.muni || '',
      label: p.ident ? `${p.ident} ${p.name || ''}` : (p.name || ''),
      f,
    });
  };
  for (const f of (state.data.airports?.features || [])) push(f, 'airport');
  for (const f of (state.data.navaids?.features || [])) push(f, 'navaid');
  for (const f of (state.data.heliports?.features || [])) push(f, f.properties.type === 'hospital' ? 'hospital' : 'heliport');
  state.search = idx;
}
function searchFacilities(q) {
  q = q.trim().toLowerCase();
  if (!q) return [];
  const scored = [];
  for (const it of state.search) {
    const ident = it.ident.toLowerCase(), iata = it.iata.toLowerCase();
    const name = it.name.toLowerCase(), muni = it.muni.toLowerCase();
    let s = -1;
    if (ident === q || iata === q) s = 100;
    else if (ident.startsWith(q) || iata.startsWith(q)) s = 80;
    else if (name.startsWith(q)) s = 60;
    else if (name.includes(q)) s = 40;
    else if (muni.includes(q)) s = 20;
    if (s >= 0) scored.push([s + (it.type === 'airport' ? 5 : 0), it]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  return scored.slice(0, 25).map(x => x[1]);
}
function resultRow(it, ref) {
  let sub = typeLabel(it.type) + (it.muni ? ' · ' + it.muni : '');
  let right = '';
  if (ref) {
    const nm = haversineNM(ref, it), brg = trueToMag(bearingTrue(ref, it));
    right = `<span class="res-dist">${fmtNM(nm)}<small>NM</small></span><span class="res-brg">${fmtBrg(brg)}°</span>`;
  }
  return `<div class="res-row" data-lat="${it.lat}" data-lon="${it.lon}" data-name="${escapeHtml(it.ident || it.name)}">
    ${listSym(it.type)}
    <span class="res-main"><b>${escapeHtml(it.label)}</b><small>${escapeHtml(sub)}</small></span>
    ${right}
    <span class="res-go">D→</span>
  </div>`;
}
function ownRef() {
  return state.own.latlng ? { lat: state.own.latlng.lat, lon: state.own.latlng.lng } : null;
}
function renderDirectResults() {
  const q = document.getElementById('direct-q').value;
  const list = searchFacilities(q);
  const ref = ownRef();
  const el = document.getElementById('direct-results');
  el.innerHTML = list.map(it => resultRow(it, ref)).join('') ||
    (q.trim() ? '<div class="rb-hint">該当なし</div>' : '<div class="rb-hint">識別コード(RJTT)・名称(羽田)・所在地で検索</div>');
  wireResultRows(el, () => closePanel('direct'));
}
let nrstKind = 'airport';
function renderNrst() {
  const ref = ownRef() || (() => { const c = state.map.getCenter(); return { lat: c.lat, lon: c.lng }; })();
  const pool = state.search.filter(it => nrstKind === 'airport'
    ? it.type === 'airport'
    : (it.type === 'heliport' || it.type === 'hospital'));
  const list = pool
    .map(it => [haversineNM(ref, it), it])
    .sort((a, b) => a[0] - b[0])
    .slice(0, 15)
    .map(x => x[1]);
  const el = document.getElementById('nrst-results');
  el.innerHTML = list.map(it => resultRow(it, ref)).join('') || '<div class="rb-hint">データなし</div>';
  if (!ownRef()) el.insertAdjacentHTML('afterbegin', '<div class="rb-hint">⚠ GPS 未取得のため地図中心からの距離です</div>');
  wireResultRows(el, () => closePanel('nrst'));
}
function wireResultRows(el, close) {
  el.querySelectorAll('.res-row').forEach(row => {
    row.onclick = () => {
      directTo(+row.dataset.lat, +row.dataset.lon, row.dataset.name);
      close();
    };
  });
}
function openPanel(id) {
  for (const p of ['direct', 'nrst', 'sheet', 'menu', 'layers']) if (p !== id) document.getElementById(p).classList.add('hidden');
  document.getElementById(id).classList.remove('hidden');
}
function closePanel(id) { document.getElementById(id).classList.add('hidden'); }

/* ---------------- デモ飛行(シミュレーション) ---------------- */
function toggleSim() {
  if (state.sim.active) return stopSim();
  if (state.route.length < 2) { showToast('先にルートを作成してください(2点以上)'); return; }
  state.sim.active = true;
  state.sim.distNM = 0;
  if (state.nav.activeIdx == null) setActiveWp(1);
  const start = state.route[0];
  setFollow(true);
  showToast('🛰️ デモ飛行開始 (' + Math.round(state.gs) + 'kt)');
  let pos = { lat: start.lat, lon: start.lon };
  let t = Date.now();
  state.sim.timer = setInterval(() => {
    const i = Math.min(state.nav.activeIdx ?? 1, state.route.length - 1);
    const wp = state.route[i];
    const brg = bearingTrue(pos, wp);
    const step = state.gs / 3600;   // 1秒あたりNM
    pos = destPoint(pos, brg, step);
    t += 1000;
    handleFix({ lat: pos.lat, lon: pos.lon, gsKt: state.gs, heading: brg, altFt: 1500, accM: 5, t });
    if (state.nav.arrived) stopSim();
  }, 1000);
}
function stopSim() {
  clearInterval(state.sim.timer);
  state.sim.timer = null;
  state.sim.active = false;
  showToast('デモ飛行終了');
}

/* ---------------- Wake Lock(画面スリープ防止) ---------------- */
async function requestWakeLock() {
  try {
    if (!('wakeLock' in navigator)) { setDot('dot-wake', null); return; }
    state.wake = await navigator.wakeLock.request('screen');
    setDot('dot-wake', true);
    state.wake.addEventListener('release', () => setDot('dot-wake', false));
  } catch (_) { setDot('dot-wake', false); }
}

/* ---------------- ハザード取得 (OSM Overpass) ---------------- */
async function fetchHazards() {
  if (!navigator.onLine) { showToast('オフラインのため取得できません'); return; }
  const b = state.map.getBounds();
  const bbox = `${b.getSouth().toFixed(4)},${b.getWest().toFixed(4)},${b.getNorth().toFixed(4)},${b.getEast().toFixed(4)}`;
  showToast('送電線・障害物を取得中…');
  const q = `[out:json][timeout:30];(
    way["power"="line"](${bbox});
    way["power"="minor_line"](${bbox});
    node["man_made"~"^(tower|mast|chimney|communications_tower)$"]["height"](${bbox});
    node["man_made"="wind_turbine"](${bbox});
  );out geom;`;
  try {
    const res = await fetch('https://overpass-api.de/api/interpreter', { method: 'POST', body: 'data=' + encodeURIComponent(q) });
    const json = await res.json();
    const feats = [];
    let nLine = 0, nObst = 0;
    for (const el of (json.elements || [])) {
      const tags = el.tags || {};
      if (el.type === 'way' && el.geometry) {
        nLine++;
        feats.push({
          type: 'Feature',
          properties: { type: 'powerline', name: tags.name || tags.operator || '送電線', voltage: tags.voltage },
          geometry: { type: 'LineString', coordinates: el.geometry.map(g => [g.lon, g.lat]) },
        });
      } else if (el.type === 'node' && el.lat != null) {
        // 障害物: 高さ 50m 以上 (風車はタグが無くても概ね 100m 級として収録)
        const h = parseFloat(tags.height) || (tags.man_made === 'wind_turbine' ? 100 : 0);
        if (h < 50) continue;
        nObst++;
        feats.push({
          type: 'Feature',
          properties: {
            type: 'obstacle', height_m: Math.round(h),
            name: tags.name || ({ chimney: '煙突', mast: '鉄塔・マスト', tower: '塔', communications_tower: '通信塔', wind_turbine: '風車' })[tags.man_made] || '障害物',
            kind: tags.man_made,
          },
          geometry: { type: 'Point', coordinates: [el.lon, el.lat] },
        });
      }
    }
    // 既存サンプル + 取得分をマージ
    const base = (state.data.hazards && state.data.hazards.features) || [];
    const merged = { type: 'FeatureCollection', features: base.filter(f => f.properties && f.properties.sample).concat(feats) };
    state.data.hazards = merged;
    localStorage.setItem('heli.data.hazards', JSON.stringify(merged));
    renderLayer('hazards');
    showToast(`送電線 ${nLine} 本・障害物 ${nObst} 件を取得・保存しました`);
  } catch (e) {
    showToast('取得失敗: ' + e.message);
  }
}

/* ---------------- オフライン: 表示エリアのタイル保存 ---------------- */
async function saveArea() {
  const b = state.map.getBounds();
  const z0 = state.map.getZoom();
  const zooms = [z0, Math.min(18, z0 + 1), Math.min(18, z0 + 2)];
  const tpl = BASEMAPS[state.basemap].url;
  const urls = [];
  for (const z of zooms) {
    const nw = latlng2tile(b.getNorth(), b.getWest(), z);
    const se = latlng2tile(b.getSouth(), b.getEast(), z);
    for (let x = nw.x; x <= se.x; x++)
      for (let y = nw.y; y <= se.y; y++)
        urls.push(tpl.replace('{z}', z).replace('{x}', x).replace('{y}', y));
  }
  if (urls.length > 1500) { showToast(`タイル数が多すぎます (${urls.length})。ズームインしてください`); return; }
  showToast(`タイル ${urls.length} 枚を保存中…`);
  let ok = 0;
  const cache = await caches.open('helinav-tiles');
  for (const u of urls) {
    try { const r = await fetch(u, { mode: 'cors' }); if (r.ok) { await cache.put(u, r.clone()); ok++; } } catch (_) {}
  }
  showToast(`オフライン保存完了: ${ok}/${urls.length} 枚`);
  updateCacheNote();
}
function latlng2tile(lat, lon, z) {
  const n = 2 ** z;
  const x = Math.floor((lon + 180) / 360 * n);
  const latR = toRad(lat);
  const y = Math.floor((1 - Math.log(Math.tan(latR) + 1 / Math.cos(latR)) / Math.PI) / 2 * n);
  return { x: Math.max(0, Math.min(n - 1, x)), y: Math.max(0, Math.min(n - 1, y)) };
}

/* ---------------- データ取込 ---------------- */
function importGeoJSON(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const gj = JSON.parse(reader.result);
      const name = guessDataset(gj, file.name);
      if (!name) { showToast('対象レイヤーを判定できませんでした'); return; }
      state.data[name] = gj;
      localStorage.setItem('heli.data.' + name, JSON.stringify(gj));
      renderLayer(name);
      buildSearchIndex();
      showToast(`${name} を取込みました (${(gj.features || []).length}件)`);
    } catch (e) { showToast('取込失敗: ' + e.message); }
  };
  reader.readAsText(file);
}
function guessDataset(gj, fname) {
  const m = (gj.meta && gj.meta.dataset) || '';
  if (/air/i.test(m) || /airport/i.test(fname)) return 'airports';
  if (/heli|hosp/i.test(m) || /heli|hosp/i.test(fname)) return 'heliports';
  if (/nav|vor/i.test(m) || /nav|vor/i.test(fname)) return 'navaids';
  if (/airspace|ctr|tca/i.test(m) || /airspace|ctr/i.test(fname)) return 'airspace';
  if (/power|hazard|line/i.test(m) || /power|hazard/i.test(fname)) return 'hazards';
  // フォールバック: 最初の地物の type
  const t = gj.features && gj.features[0] && gj.features[0].properties && gj.features[0].properties.type;
  return ({ airport: 'airports', heliport: 'heliports', hospital: 'heliports', navaid: 'navaids', powerline: 'hazards', airspace: 'airspace' })[t] || null;
}

/* ---------------- UI 配線 ---------------- */
function wireUI() {
  // レイヤーチップ
  document.querySelectorAll('#layer-chips .chip').forEach(btn => {
    const name = btn.dataset.layer;
    btn.classList.toggle('active', !!state.visible[name]);
    btn.onclick = () => {
      state.visible[name] = !state.visible[name];
      btn.classList.toggle('active', state.visible[name]);
      if (state.visible[name]) { state.layers[name].addTo(state.map); renderLayer(name); }
      else state.map.removeLayer(state.layers[name]);
      savePrefs();
    };
  });
  // 空域クラスチップ
  document.querySelectorAll('#asp-chips .chip').forEach(btn => {
    const k = btn.dataset.asp;
    btn.classList.toggle('active', !!state.asp[k]);
    btn.onclick = () => {
      state.asp[k] = !state.asp[k];
      btn.classList.toggle('active', state.asp[k]);
      renderLayer('airspace');
      savePrefs();
    };
  });
  document.getElementById('btn-layers').onclick = () => openPanel('layers');
  document.getElementById('layers-close').onclick = () => closePanel('layers');
  // ベースマップチップ (単一選択)
  document.querySelectorAll('#bm-chips .chip').forEach(btn => {
    btn.onclick = () => { setBasemap(btn.dataset.bm); updateBasemapChips(); };
  });
  updateBasemapChips();
  document.getElementById('zoom-in').onclick = () => state.map.zoomIn();
  document.getElementById('zoom-out').onclick = () => state.map.zoomOut();
  document.getElementById('btn-locate').onclick = () => {
    startGPS();
    if (state.own.latlng) {
      state.map.setView(state.own.latlng, Math.max(state.map.getZoom(), 11), { animate: false });
      setFollow(true);
    } else {
      showToast('GPS 取得中…');
      setFollow(true);
    }
  };
  document.getElementById('btn-orient').onclick = () =>
    setOrientation(state.view.mode === 'track' ? 'north' : 'track');
  document.getElementById('btn-direct').onclick = () => {
    openPanel('direct');
    renderDirectResults();
    document.getElementById('direct-q').focus();
  };
  document.getElementById('direct-close').onclick = () => closePanel('direct');
  document.getElementById('direct-q').oninput = renderDirectResults;
  document.getElementById('btn-nrst').onclick = () => { openPanel('nrst'); renderNrst(); };
  document.getElementById('nrst-close').onclick = () => closePanel('nrst');
  document.querySelectorAll('.seg-btn').forEach(b => b.onclick = () => {
    nrstKind = b.dataset.nrst;
    document.querySelectorAll('.seg-btn').forEach(x => x.classList.toggle('active', x === b));
    renderNrst();
  });
  const routeBtn = document.getElementById('btn-route');
  routeBtn.onclick = () => {
    state.routeMode = !state.routeMode;
    routeBtn.classList.toggle('active', state.routeMode);
    if (state.routeMode) { openRoutebar(); showToast('ルート作成: 地図をタップで追加'); }
  };
  document.getElementById('rb-clear').onclick = clearRoute;
  document.getElementById('rb-close').onclick = () => document.getElementById('routebar').classList.add('hidden');
  document.getElementById('btn-menu').onclick = () => openPanel('menu');
  document.getElementById('menu-close').onclick = () => closePanel('menu');
  document.getElementById('mi-theme').onclick = toggleTheme;
  document.getElementById('mi-wx').onclick = () => { closePanel('menu'); fetchWx(true); };
  document.getElementById('mi-save-area').onclick = () => { closePanel('menu'); saveArea(); };
  document.getElementById('mi-hazards').onclick = () => { closePanel('menu'); fetchHazards(); };
  document.getElementById('mi-import').onclick = () => document.getElementById('import-file').click();
  document.getElementById('mi-sim').onclick = () => { closePanel('menu'); toggleSim(); };
  document.getElementById('import-file').onchange = e => { if (e.target.files[0]) importGeoJSON(e.target.files[0]); };
  document.getElementById('magvar').onchange = e => { state.magvar = parseFloat(e.target.value) || 0; savePrefs(); redrawRoute(); updateHUD(); };
  document.getElementById('gs').onchange = e => { state.gs = parseFloat(e.target.value) || 0; savePrefs(); updateRoutebar(); updateHUD(); };
  document.getElementById('fuel-burn').onchange = e => { state.fuel.burn = parseFloat(e.target.value) || 0; savePrefs(); updateRoutebar(); };
  document.getElementById('fuel-onboard').onchange = e => { state.fuel.onboard = parseFloat(e.target.value) || 0; savePrefs(); updateRoutebar(); };
  updateCacheNote();
}

function toggleTheme() {
  const night = document.body.classList.toggle('theme-night');
  document.body.classList.toggle('theme-day', !night);
  document.querySelector('meta[name=theme-color]').setAttribute('content', night ? '#0b1622' : '#0a7ec2');
  savePrefs();
}

/* ---------------- 状態表示・ユーティリティ ---------------- */
function setDot(id, ok) {
  const el = document.getElementById(id);
  el.className = 'dot' + (ok == null ? '' : ok ? ' ok' : ' warn');
}
function setGpsDot(ok, accM) {
  setDot('dot-gps', ok);
  const el = document.getElementById('dot-gps');
  el.textContent = ok && accM != null ? `GPS ±${Math.round(accM)}m` : 'GPS';
}
function updateNetStatus() { setDot('dot-net', navigator.onLine); }
async function updateCacheNote() {
  try {
    const cache = await caches.open('helinav-tiles');
    const keys = await cache.keys();
    document.getElementById('cache-note').textContent = `オフライン地図タイル: ${keys.length} 枚保存済み`;
  } catch (_) {}
}
let toastTimer;
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 2600);
}
function escapeHtml(s) { return s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

function savePrefs() {
  localStorage.setItem('heli.prefs', JSON.stringify({
    night: document.body.classList.contains('theme-night'),
    magvar: state.magvar, gs: state.gs, mode: state.view.mode,
    visible: state.visible, asp: state.asp, fuel: state.fuel, basemap: state.basemap,
  }));
}
function restorePrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('heli.prefs') || '{}');
    if (p.magvar != null) state.magvar = p.magvar;
    if (p.gs != null) state.gs = p.gs;
    if (p.visible) Object.assign(state.visible, p.visible);
    if (p.asp) Object.assign(state.asp, p.asp);
    if (p.fuel) Object.assign(state.fuel, p.fuel);
    if (p.basemap && BASEMAPS[p.basemap]) state.basemap = p.basemap;
    if (p.night) { document.body.classList.add('theme-night'); document.body.classList.remove('theme-day'); }
    if (p.mode === 'track') setTimeout(() => setOrientation('track'), 0);
  } catch (_) {}
}

function registerSW() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch(e => console.warn('SW fail', e));
  }
}

document.addEventListener('DOMContentLoaded', () => {
  init();
  document.getElementById('magvar').value = state.magvar;
  document.getElementById('gs').value = state.gs;
  if (state.fuel.burn) document.getElementById('fuel-burn').value = state.fuel.burn;
  if (state.fuel.onboard) document.getElementById('fuel-onboard').value = state.fuel.onboard;
});

// デバッグ/テスト用フック
window.HN = { state, handleFix, directTo, addWaypoint, setOrientation, toggleSim, setActiveWp, fetchWx, fetchHazards, renderWx };
