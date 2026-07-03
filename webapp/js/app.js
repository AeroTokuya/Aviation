/* HeliNav — VFR ヘリコプター ナビ (PWA)
 * 無料・軽量・オフライン対応。Leaflet + OpenStreetMap。
 * 参考情報のみ。運航判断は公式情報とパイロットが行うこと。
 */
'use strict';

const DATA_FILES = {
  airports: 'data/airports.geojson',
  heliports: 'data/heliports.geojson',
  navaids: 'data/navaids.geojson',
  hazards: 'data/powerlines.geojson',
};

const state = {
  map: null,
  layers: {},          // layer name -> L.LayerGroup
  visible: { airports: true, heliports: true, navaids: true, hazards: true, route: true },
  data: {},            // layer name -> GeoJSON
  route: [],           // [{lat, lon, name}]
  routeLine: null,
  routeMarkers: [],
  routeMode: false,
  own: { marker: null, latlng: null, heading: null, follow: false },
  magvar: 8,           // 西偏差(°) 日本は約7〜9°W
  gs: 110,             // 地上速度 kt (ETA用)
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
const trueToMag = t => (t + state.magvar + 360) % 360;   // 西偏差は真方位に加算
const fmtBrg = d => String(Math.round(d)).padStart(3, '0');
const fmtNM = n => n < 10 ? n.toFixed(1) : Math.round(n).toString();

/* ---------------- 起動 ---------------- */
async function init() {
  restorePrefs();
  buildMap();
  wireUI();
  registerSW();
  await loadAllData();
  updateNetStatus();
  window.addEventListener('online', updateNetStatus);
  window.addEventListener('offline', updateNetStatus);
}

function buildMap() {
  state.map = L.map('map', { zoomControl: true, tap: true, attributionControl: true })
    .setView([35.6, 139.7], 8);

  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18, crossOrigin: true,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(state.map);

  for (const name of Object.keys(DATA_FILES)) state.layers[name] = L.layerGroup().addTo(state.map);
  state.layers.route = L.layerGroup().addTo(state.map);

  state.map.on('click', onMapClick);
  // ズーム/移動で間引きレイヤーを再描画
  let gateTimer;
  state.map.on('moveend zoomend', () => {
    clearTimeout(gateTimer);
    gateTimer = setTimeout(() => {
      for (const name of Object.keys(GATED)) if (state.visible[name]) renderLayer(name);
    }, 120);
  });
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
const GATED = { heliports: 8 };

function renderLayer(name) {
  const grp = state.layers[name];
  grp.clearLayers();
  const gj = state.data[name];
  if (!gj) return;
  if (name === 'hazards') return renderHazards(gj, grp);

  const feats = gj.features || [];
  let list = feats;
  if (name in GATED) {
    if (state.map.getZoom() < GATED[name]) { updateGateNote(name, feats.length, 0); return; }
    const b = state.map.getBounds();
    list = feats.filter(f => {
      const c = f.geometry && f.geometry.coordinates;
      return c && b.contains([c[1], c[0]]);
    });
    // 描画上限(安全弁)。病院ヘリパッドを優先表示。
    if (list.length > 400) {
      list.sort((a, z) => (a.properties.type === 'hospital' ? 0 : 1) - (z.properties.type === 'hospital' ? 0 : 1));
      list = list.slice(0, 400);
    }
    updateGateNote(name, feats.length, list.length);
  }
  for (const f of list) {
    const c = f.geometry.coordinates;
    L.marker([c[1], c[0]], { icon: facilityIcon(f), keyboard: false })
      .on('click', ev => { L.DomEvent.stop(ev); openFacility(f); })
      .addTo(grp);
  }
}

function updateGateNote(name, total, shown) {
  if (name !== 'heliports') return;
  const btn = document.querySelector('.layer-toggle[data-layer="heliports"] .lbl');
  if (!btn) return;
  btn.textContent = shown === 0 ? 'ヘリ/病院' : `ヘリ/病院 ${shown}`;
}

function renderHazards(gj, grp) {
  L.geoJSON(gj, {
    style: { color: '#ff9f1c', weight: 3, opacity: 0.9, dashArray: '1 6', lineCap: 'round' },
    pointToLayer: (f, latlng) => L.circleMarker(latlng, { radius: 4, color: '#ff9f1c', weight: 2, fillOpacity: 0.6 }),
    onEachFeature: (f, layer) => {
      const p = f.properties || {};
      const label = p.name || (p.type === 'tower' ? '送電鉄塔' : '送電線');
      layer.on('click', ev => { L.DomEvent.stop(ev); showToast('⚡ ' + label + (p.sample ? ' (サンプル)' : '')); });
    },
  }).addTo(grp);
}

const ICONS = {
  airport: '✈️', heliport: '🚁', hospital: '🏥', navaid: '📡',
};
function facilityIcon(f) {
  const t = (f.properties && f.properties.type) || 'airport';
  const glyph = ICONS[t] || '•';
  const cls = t === 'hospital' ? 'hospital' : t;
  return L.divIcon({
    className: '', iconSize: [34, 34], iconAnchor: [17, 17],
    html: `<div class="mk ${cls}">${glyph}</div>`,
  });
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
    fromOwn = `<div class="sheet-sub">現在地から: <b>${fmtNM(nm)} NM</b> / 真 ${fmtBrg(brg)}° (磁 ${fmtBrg(trueToMag(brg))}°)</div>`;
  }

  const kv = rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join('');
  document.getElementById('sheet-body').innerHTML = `
    <div class="sheet-title">${escapeHtml(title)}</div>
    <div class="sheet-sub">${escapeHtml(p.name_en || typeLabel(p.type))}</div>
    ${fromOwn}
    <dl class="kv">${kv}</dl>
    <div class="sheet-actions">
      <button class="btn-primary" id="sheet-add">＋ ルートに追加</button>
      <button class="btn-ghost" id="sheet-direct">→ ダイレクト</button>
      <button class="btn-ghost" id="sheet-close">閉じる</button>
    </div>`;
  document.getElementById('sheet').classList.remove('hidden');
  document.getElementById('sheet-add').onclick = () => { addWaypoint(lat, lon, title); closeSheet(); };
  document.getElementById('sheet-direct').onclick = () => { directTo(lat, lon, title); closeSheet(); };
  document.getElementById('sheet-close').onclick = closeSheet;
}
function typeLabel(t) {
  return { airport: '空港', heliport: 'ヘリポート', hospital: 'ドクターヘリ基地病院', navaid: '航法無線施設' }[t] || '';
}
function closeSheet() { document.getElementById('sheet').classList.add('hidden'); }

/* ---------------- ルート ---------------- */
function onMapClick(e) {
  if (!state.routeMode) return;
  addWaypoint(e.latlng.lat, e.latlng.lng, 'WP');
}
function addWaypoint(lat, lon, name) {
  state.route.push({ lat, lon, name });
  redrawRoute();
  openRoutebar();
  showToast('ウェイポイント追加: ' + name);
}
function directTo(lat, lon, name) {
  if (!state.own.latlng) { showToast('現在地が未取得です'); return; }
  state.route = [
    { lat: state.own.latlng.lat, lon: state.own.latlng.lng, name: '現在地' },
    { lat, lon, name },
  ];
  redrawRoute();
  openRoutebar();
}
function redrawRoute() {
  const grp = state.layers.route;
  grp.clearLayers();
  state.routeMarkers = [];
  const pts = state.route.map(w => [w.lat, w.lon]);
  if (pts.length >= 2) {
    L.polyline(pts, { color: '#35c2ff', weight: 4, opacity: 0.9 }).addTo(grp);
  }
  state.route.forEach((w, i) => {
    const m = L.marker([w.lat, w.lon], {
      icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 15], html: `<div class="wp-marker">${i + 1}</div>` }),
      draggable: true,
    }).addTo(grp);
    m.on('dragend', ev => { const ll = ev.target.getLatLng(); w.lat = ll.lat; w.lon = ll.lng; redrawRoute(); });
    m.on('click', ev => { L.DomEvent.stop(ev); });
    state.routeMarkers.push(m);
  });
  updateRoutebar();
}
function updateRoutebar() {
  const legsEl = document.getElementById('rb-legs');
  let total = 0;
  const rows = [];
  for (let i = 1; i < state.route.length; i++) {
    const a = state.route[i - 1], b = state.route[i];
    const nm = haversineNM(a, b), brg = bearingTrue(a, b);
    total += nm;
    const eta = state.gs > 0 ? (nm / state.gs) * 60 : 0;
    rows.push(`<div class="leg">
      <span class="num">${i}</span>
      <span class="name">${escapeHtml(a.name)} → ${escapeHtml(b.name)}</span>
      <span class="brg">磁 ${fmtBrg(trueToMag(brg))}°</span>
      <span class="nm">${fmtNM(nm)}NM · ${Math.round(eta)}分</span>
      <button class="del" data-i="${i}">×</button>
    </div>`);
  }
  legsEl.innerHTML = rows.join('') || '<div class="rb-hint">ウェイポイントが2点以上でルートを表示します。</div>';
  legsEl.querySelectorAll('.del').forEach(btn => btn.onclick = () => { state.route.splice(+btn.dataset.i, 1); redrawRoute(); });
  const etaTot = state.gs > 0 ? (total / state.gs) * 60 : 0;
  document.getElementById('rb-summary').textContent =
    state.route.length >= 2 ? `全長 ${fmtNM(total)} NM · ${state.route.length - 1}区間 · ${Math.round(etaTot)}分@${state.gs}kt` : '地図/施設をタップ';
}
function openRoutebar() { document.getElementById('routebar').classList.remove('hidden'); }
function clearRoute() { state.route = []; redrawRoute(); }

/* ---------------- 現在地 ---------------- */
function locate() {
  if (!navigator.geolocation) { showToast('位置情報が使えません'); return; }
  setGpsStatus('取得中…');
  navigator.geolocation.watchPosition(onPos, err => {
    setGpsStatus('GPS ✕', false);
    showToast('位置取得エラー: ' + err.message);
  }, { enableHighAccuracy: true, maximumAge: 2000, timeout: 15000 });
  state.own.follow = true;
}
function onPos(pos) {
  const { latitude, longitude, heading, speed } = pos.coords;
  const ll = L.latLng(latitude, longitude);
  state.own.latlng = ll;
  if (heading != null && !isNaN(heading)) state.own.heading = heading;
  drawOwnship();
  const kt = speed != null && !isNaN(speed) ? (speed * 1.94384).toFixed(0) : '--';
  setGpsStatus(`GS ${kt}kt`, true);
  if (state.own.follow) state.map.panTo(ll, { animate: true });
}
function drawOwnship() {
  if (!state.own.latlng) return;
  const hdg = state.own.heading || 0;
  const html = `<div class="ownship" style="transform:rotate(${hdg}deg)">
    <svg width="26" height="26" viewBox="0 0 26 26"><path d="M13 1 L23 24 L13 18 L3 24 Z" fill="#35c2ff" stroke="#fff" stroke-width="1.5"/></svg></div>`;
  const icon = L.divIcon({ className: '', iconSize: [26, 26], iconAnchor: [13, 13], html });
  if (!state.own.marker) state.own.marker = L.marker(state.own.latlng, { icon, interactive: false, zIndexOffset: 1000 }).addTo(state.map);
  else { state.own.marker.setLatLng(state.own.latlng); state.own.marker.setIcon(icon); }
}

/* ---------------- ハザード取得 (OSM Overpass) ---------------- */
async function fetchHazards() {
  if (!navigator.onLine) { showToast('オフラインのため取得できません'); return; }
  const b = state.map.getBounds();
  const bbox = `${b.getSouth().toFixed(4)},${b.getWest().toFixed(4)},${b.getNorth().toFixed(4)},${b.getEast().toFixed(4)}`;
  showToast('送電線を取得中…');
  const q = `[out:json][timeout:25];(way["power"="line"](${bbox});way["power"="minor_line"](${bbox}););out geom;`;
  try {
    const res = await fetch('https://overpass-api.de/api/interpreter', { method: 'POST', body: 'data=' + encodeURIComponent(q) });
    const json = await res.json();
    const feats = (json.elements || []).filter(el => el.geometry).map(el => ({
      type: 'Feature',
      properties: { type: 'powerline', name: (el.tags && (el.tags.name || el.tags.operator)) || '送電線', voltage: el.tags && el.tags.voltage },
      geometry: { type: 'LineString', coordinates: el.geometry.map(g => [g.lon, g.lat]) },
    }));
    // 既存サンプル + 取得分をマージ
    const base = (state.data.hazards && state.data.hazards.features) || [];
    const merged = { type: 'FeatureCollection', features: base.filter(f => f.properties && f.properties.sample).concat(feats) };
    state.data.hazards = merged;
    localStorage.setItem('heli.data.hazards', JSON.stringify(merged));
    renderLayer('hazards');
    showToast(`送電線 ${feats.length} 本を取得・保存しました`);
  } catch (e) {
    showToast('取得失敗: ' + e.message);
  }
}

/* ---------------- オフライン: 表示エリアのタイル保存 ---------------- */
async function saveArea() {
  const b = state.map.getBounds();
  const z0 = state.map.getZoom();
  const zooms = [z0, Math.min(18, z0 + 1), Math.min(18, z0 + 2)];
  const urls = [];
  for (const z of zooms) {
    const nw = latlng2tile(b.getNorth(), b.getWest(), z);
    const se = latlng2tile(b.getSouth(), b.getEast(), z);
    for (let x = nw.x; x <= se.x; x++)
      for (let y = nw.y; y <= se.y; y++)
        urls.push(`https://tile.openstreetmap.org/${z}/${x}/${y}.png`);
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
  if (/power|hazard|line/i.test(m) || /power|hazard/i.test(fname)) return 'hazards';
  // フォールバック: 最初の地物の type
  const t = gj.features && gj.features[0] && gj.features[0].properties && gj.features[0].properties.type;
  return ({ airport: 'airports', heliport: 'heliports', hospital: 'heliports', navaid: 'navaids', powerline: 'hazards' })[t] || null;
}

/* ---------------- UI 配線 ---------------- */
function wireUI() {
  document.querySelectorAll('.layer-toggle').forEach(btn => {
    btn.onclick = () => {
      const name = btn.dataset.layer;
      state.visible[name] = !state.visible[name];
      btn.classList.toggle('active', state.visible[name]);
      if (state.visible[name]) state.layers[name].addTo(state.map);
      else state.map.removeLayer(state.layers[name]);
    };
  });
  document.getElementById('btn-locate').onclick = () => {
    state.own.follow = true;
    if (state.own.latlng) state.map.setView(state.own.latlng, Math.max(state.map.getZoom(), 11));
    locate();
  };
  const routeBtn = document.getElementById('btn-route');
  routeBtn.onclick = () => {
    state.routeMode = !state.routeMode;
    routeBtn.classList.toggle('active', state.routeMode);
    if (state.routeMode) { openRoutebar(); showToast('ルート作成: 地図をタップで追加'); }
  };
  document.getElementById('rb-clear').onclick = clearRoute;
  document.getElementById('rb-close').onclick = () => document.getElementById('routebar').classList.add('hidden');
  document.getElementById('btn-theme').onclick = toggleTheme;
  document.getElementById('btn-menu').onclick = () => document.getElementById('menu').classList.remove('hidden');
  document.getElementById('menu-close').onclick = () => document.getElementById('menu').classList.add('hidden');
  document.getElementById('mi-save-area').onclick = () => { document.getElementById('menu').classList.add('hidden'); saveArea(); };
  document.getElementById('mi-hazards').onclick = () => { document.getElementById('menu').classList.add('hidden'); fetchHazards(); };
  document.getElementById('mi-import').onclick = () => document.getElementById('import-file').click();
  document.getElementById('import-file').onchange = e => { if (e.target.files[0]) importGeoJSON(e.target.files[0]); };
  document.getElementById('magvar').onchange = e => { state.magvar = parseFloat(e.target.value) || 0; savePrefs(); redrawRoute(); };
  document.getElementById('gs').onchange = e => { state.gs = parseFloat(e.target.value) || 0; savePrefs(); updateRoutebar(); };
  // 地図移動で follow 解除
  state.map.on('dragstart', () => { state.own.follow = false; });
  updateCacheNote();
}

function toggleTheme() {
  const night = document.body.classList.toggle('theme-night');
  document.body.classList.toggle('theme-day', !night);
  document.getElementById('btn-theme').textContent = night ? '☀️' : '🌙';
  document.querySelector('meta[name=theme-color]').setAttribute('content', night ? '#0b1622' : '#0a7ec2');
  savePrefs();
}

/* ---------------- 状態表示・ユーティリティ ---------------- */
function setGpsStatus(txt, ok) {
  const el = document.getElementById('gps-status');
  el.textContent = txt; el.className = 'stat' + (ok ? ' ok' : ok === false ? ' warn' : '');
}
function updateNetStatus() {
  const el = document.getElementById('net-status');
  if (navigator.onLine) { el.textContent = 'オンライン'; el.className = 'stat ok'; }
  else { el.textContent = 'オフライン', el.className = 'stat warn'; }
}
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
    night: document.body.classList.contains('theme-night'), magvar: state.magvar, gs: state.gs,
  }));
}
function restorePrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('heli.prefs') || '{}');
    if (p.magvar != null) state.magvar = p.magvar;
    if (p.gs != null) state.gs = p.gs;
    if (p.night) { document.body.classList.add('theme-night'); document.body.classList.remove('theme-day'); }
  } catch (_) {}
}

function registerSW() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch(e => console.warn('SW fail', e));
  }
}

document.addEventListener('DOMContentLoaded', () => {
  init();
  const p = JSON.parse(localStorage.getItem('heli.prefs') || '{}');
  document.getElementById('magvar').value = state.magvar;
  document.getElementById('gs').value = state.gs;
  if (p.night) document.getElementById('btn-theme').textContent = '☀️';
});
