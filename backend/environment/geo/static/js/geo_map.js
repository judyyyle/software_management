/* ══════════════════════════════════════════════════════════════════
   UAV 禁飞区地图 — 前端主逻辑
   ══════════════════════════════════════════════════════════════════ */

// ── 常量 ──────────────────────────────────────────────────────────
const LAT_M    = 1 / 111000;
const colorNoFly = '#ff4444';
const colorFly   = '#33bb55';
const opNoFly  = 0.70;
const opFly    = 0.35;

// ── 状态变量 ──────────────────────────────────────────────────────
let appReady    = false;
let serverState = {};
let map, drawControl, drawnItems, buildingLayer, roadLayer;
let selRect     = null;
let selBounds   = null;
let currentFmt  = 'sumo_zip_osm';
let quickMode   = false;
let statusInterval;

// ── DOM refs ──────────────────────────────────────────────────────
const elMask       = document.getElementById('loading-mask');
const elLoadBar    = document.getElementById('load-bar');
const elLoadSub    = document.getElementById('load-sub');
const elDot        = document.getElementById('status-dot');
const elStatusTxt  = document.getElementById('status-text');
const elBtnQuery   = document.getElementById('btn-query');
const elStatCard   = document.getElementById('stat-card');
const elExportCard = document.getElementById('export-card');
const elOverlay    = document.getElementById('map-overlay');
const elSelInfo    = document.getElementById('sel-info');
const elBtnClear   = document.getElementById('btn-clear-sel');
const elStatTrunc  = document.getElementById('stat-trunc');

// ══════════════════════════════════════════════════════════════════
// 初始化地图
// ══════════════════════════════════════════════════════════════════
function initMap(centerLon, centerLat) {
  map = L.map('map', {
    center: [centerLat || 31.23, centerLon || 121.47],
    zoom: 13,
    zoomControl: true,
  });

  // ── Leaflet.draw 汉化 ──────────────────────────────────────────
  L.drawLocal.draw.toolbar.buttons.rectangle        = '框选矩形区域';
  L.drawLocal.draw.toolbar.actions.title            = '取消绘制';
  L.drawLocal.draw.toolbar.actions.text             = '取消';
  L.drawLocal.draw.toolbar.finish.title             = '完成绘制';
  L.drawLocal.draw.toolbar.finish.text              = '完成';
  L.drawLocal.draw.toolbar.undo.title               = '撤销上一个点';
  L.drawLocal.draw.toolbar.undo.text                = '撤销';
  L.drawLocal.draw.handlers.rectangle.tooltip.start = '点击并拖拽以框选研究范围';
  L.drawLocal.edit.toolbar.actions.save.title       = '保存修改';
  L.drawLocal.edit.toolbar.actions.save.text        = '保存';
  L.drawLocal.edit.toolbar.actions.cancel.title     = '取消修改，放弃更改';
  L.drawLocal.edit.toolbar.actions.cancel.text      = '取消';
  L.drawLocal.edit.toolbar.actions.clearAll.title   = '清除所有图层';
  L.drawLocal.edit.toolbar.actions.clearAll.text    = '全部清除';
  L.drawLocal.edit.toolbar.buttons.edit             = '编辑选区';
  L.drawLocal.edit.toolbar.buttons.editDisabled     = '无可编辑的图层';
  L.drawLocal.edit.toolbar.buttons.remove           = '删除图层';
  L.drawLocal.edit.toolbar.buttons.removeDisabled   = '无可删除的图层';
  L.drawLocal.edit.handlers.edit.tooltip.text       = '拖动控制点以编辑要素';
  L.drawLocal.edit.handlers.edit.tooltip.subtext    = '点击"取消"放弃更改';
  L.drawLocal.edit.handlers.remove.tooltip.text     = '点击要素以删除';

  const positron = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    { attribution: '© CARTO · © OpenStreetMap 贡献者', maxZoom: 19 }
  );
  const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap 贡献者', maxZoom: 19
  });
  positron.addTo(map);
  L.control.layers({ '浅色底图 CartoDB': positron, 'OpenStreetMap': osm }, {}, { collapsed: true }).addTo(map);

  drawnItems = new L.FeatureGroup();
  map.addLayer(drawnItems);

  drawControl = new L.Control.Draw({
    draw: {
      rectangle: { shapeOptions: { color: '#f8961e', weight: 2, fillOpacity: 0.08 } },
      polygon: false, polyline: false, circle: false,
      circlemarker: false, marker: false,
    },
    edit: { featureGroup: drawnItems, remove: false },
  });
  map.addControl(drawControl);

  map.on(L.Draw.Event.CREATED, (e) => { setSelection(e.layer.getBounds()); });

  map.on('click', (e) => {
    if (!quickMode) return;
    const lat   = e.latlng.lat;
    const lon   = e.latlng.lng;
    const halfW = parseInt(document.getElementById('qw').value) / 2;
    const halfH = parseInt(document.getElementById('qh').value) / 2;
    const dLat  = halfH * LAT_M;
    const dLon  = halfW * LAT_M / Math.cos(lat * Math.PI / 180);
    setSelection(L.latLngBounds([lat - dLat, lon - dLon], [lat + dLat, lon + dLon]));
  });

  map.on('mousemove', (e) => {
    if (!quickMode) return;
    elOverlay.textContent = `点击设置中心 | ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}`;
  });
}

// ══════════════════════════════════════════════════════════════════
// 设置选区
// ══════════════════════════════════════════════════════════════════
function setSelection(lBounds) {
  if (selRect) { map.removeLayer(selRect); }

  selRect = L.rectangle(lBounds, {
    color: '#f8961e', weight: 2, fillColor: '#f8961e', fillOpacity: 0.06,
  }).addTo(map);

  const sw  = lBounds.getSouthWest();
  const ne  = lBounds.getNorthEast();
  const nw  = L.latLng(ne.lat, sw.lng);
  const se  = L.latLng(sw.lat, ne.lng);
  const ctr = lBounds.getCenter();

  selBounds = { minx: sw.lng, miny: sw.lat, maxx: ne.lng, maxy: ne.lat };

  const fmt = (lat, lng) => `${lat.toFixed(6)}°N,  ${lng.toFixed(6)}°E`;

  document.getElementById('coord-sw').textContent  = fmt(sw.lat,  sw.lng);
  document.getElementById('coord-ne').textContent  = fmt(ne.lat,  ne.lng);
  document.getElementById('coord-nw').textContent  = fmt(nw.lat,  nw.lng);
  document.getElementById('coord-se').textContent  = fmt(se.lat,  se.lng);
  document.getElementById('coord-ctr').textContent = fmt(ctr.lat, ctr.lng);

  const dLat  = ne.lat - sw.lat;
  const dLon  = ne.lng - sw.lng;
  const wM    = Math.round(dLon * 111000 * Math.cos(ctr.lat * Math.PI / 180));
  const hM    = Math.round(dLat * 111000);

  document.getElementById('sel-size').textContent =
    `${(wM/1000).toFixed(2)} km × ${(hM/1000).toFixed(2)} km`;

  elSelInfo.style.display  = 'block';
  elBtnClear.style.display = 'block';
  elBtnQuery.disabled      = false;
  elOverlay.textContent    =
    `已选区 ${(wM/1000).toFixed(1)} km × ${(hM/1000).toFixed(1)} km  |  中心 ${fmt(ctr.lat, ctr.lng)}`;

  if (quickMode) toggleQuickMode(false);
}

// 复制四角坐标到剪贴板
document.getElementById('btn-copy-coords').addEventListener('click', () => {
  if (!selBounds) return;
  const b    = selBounds;
  const cLat = (b.miny + b.maxy) / 2;
  const cLon = (b.minx + b.maxx) / 2;
  const text =
    `西南角 (SW): ${b.miny.toFixed(6)}°N, ${b.minx.toFixed(6)}°E\n` +
    `东北角 (NE): ${b.maxy.toFixed(6)}°N, ${b.maxx.toFixed(6)}°E\n` +
    `西北角 (NW): ${b.maxy.toFixed(6)}°N, ${b.minx.toFixed(6)}°E\n` +
    `东南角 (SE): ${b.miny.toFixed(6)}°N, ${b.maxx.toFixed(6)}°E\n` +
    `中心点 (CTR): ${cLat.toFixed(6)}°N, ${cLon.toFixed(6)}°E\n` +
    `---\n` +
    `minLon=${b.minx.toFixed(6)}  maxLon=${b.maxx.toFixed(6)}\n` +
    `minLat=${b.miny.toFixed(6)}  maxLat=${b.maxy.toFixed(6)}`;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('btn-copy-coords');
    btn.textContent = '✓ 已复制';
    setTimeout(() => { btn.textContent = '📋 复制四角坐标'; }, 2000);
  });
});

// ══════════════════════════════════════════════════════════════════
// UI 控件事件绑定
// ══════════════════════════════════════════════════════════════════
const slider = document.getElementById('height-slider');
const numIn  = document.getElementById('height-input');
slider.addEventListener('input', () => { numIn.value = slider.value; });
numIn.addEventListener('input', () => {
  const v = Math.max(0, parseInt(numIn.value) || 0);
  slider.value = Math.min(500, v);
});

document.getElementById('btn-draw').addEventListener('click', () => {
  toggleQuickMode(false);
  new L.Draw.Rectangle(map, drawControl.options.draw.rectangle).enable();
  elOverlay.textContent = '在地图上拖拽绘制矩形选区';
});

document.getElementById('btn-quick').addEventListener('click', () => {
  toggleQuickMode(!quickMode);
});

function toggleQuickMode(on) {
  quickMode = on;
  document.getElementById('btn-quick').classList.toggle('active', on);
  document.getElementById('quick-panel').style.display = on ? 'block' : 'none';
  if (on) {
    map.getContainer().style.cursor = 'crosshair';
    elOverlay.textContent = '点击地图设置中心点';
  } else {
    map.getContainer().style.cursor = '';
    if (!selBounds) elOverlay.textContent = '请先选取研究范围';
  }
}

document.getElementById('btn-clear-sel').addEventListener('click', () => {
  if (selRect)       { map.removeLayer(selRect);       selRect       = null; }
  if (buildingLayer) { map.removeLayer(buildingLayer); buildingLayer = null; }
  if (roadLayer)     { map.removeLayer(roadLayer);     roadLayer     = null; }
  selBounds = null;
  elSelInfo.style.display    = 'none';
  elBtnClear.style.display   = 'none';
  elBtnQuery.disabled        = true;
  elStatCard.style.display   = 'none';
  elExportCard.style.display = 'none';
  elOverlay.textContent      = '请先选取研究范围';
});

document.getElementById('btn-query').addEventListener('click', queryBuildings);

// ── 导出格式切换 ──
const fmtDescs = {
  sumo_zip_osm: '下载选区内真实 OSM 道路，包含 <b style="color:#7ec8e3">roads.net.xml</b>（真实道路）+<b style="color:#7ec8e3">no_fly_zones.add.xml</b>，解压后直接运行。需联网（Overpass API）。',
  sumo_zip:     '纯 Python 自动生成简化<b style="color:#7ec8e3">网格路网</b>+禁飞区叠加层，无需联网。',
  sumo_poly:    'SUMO polygon additional file，需配合已有 net.xml 使用。',
  geojson:      'GeoJSON 格式，包含建筑高度与禁飞标记，适用于 QGIS / 网页地图。',
  csv:          'CSV 表格，含每栋建筑质心坐标、高度、是否禁飞，便于数据分析。',
};

document.querySelectorAll('[data-fmt]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('[data-fmt]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFmt = btn.dataset.fmt;
    document.getElementById('fmt-desc').innerHTML  = fmtDescs[currentFmt] || '';
    document.getElementById('grid-panel').style.display =
      currentFmt === 'sumo_zip' ? 'block' : 'none';
  });
});

// 默认选中真实路网
(function () {
  document.getElementById('fmt-sumo-osm').classList.add('active');
  document.getElementById('fmt-desc').innerHTML = fmtDescs['sumo_zip_osm'];
})();

document.getElementById('btn-export').addEventListener('click', exportData);

// ══════════════════════════════════════════════════════════════════
// 轮询后端状态
// ══════════════════════════════════════════════════════════════════
async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    const s   = await res.json();
    serverState = s;

    elLoadBar.style.width = s.progress + '%';

    if (s.error) {
      elLoadSub.textContent = '❌ 加载失败：' + s.error;
      elDot.className = '';
      elStatusTxt.textContent = '加载失败';
      clearInterval(statusInterval);
      return;
    }

    if (s.loaded) {
      elMask.classList.add('hidden');
      elDot.className = 'ok';
      elStatusTxt.textContent = `就绪 · ${s.total.toLocaleString()} 栋建筑`;
      clearInterval(statusInterval);
      onDataReady(s);
    } else if (s.loading) {
      elDot.className = 'spin';
      elStatusTxt.textContent = `加载中 ${s.progress}%…`;
      elLoadSub.textContent   = `已处理 ${s.progress}%，请稍候`;
    }
  } catch (e) {
    elDot.className = '';
    elStatusTxt.textContent = '连接失败';
  }
}

function onDataReady(s) {
  appReady = true;

  document.getElementById('info-total').textContent = s.total.toLocaleString();
  document.getElementById('info-hcol').textContent  = s.height_column || 'N/A';
  if (s.height_stats) {
    document.getElementById('info-hmax').textContent  = s.height_stats.max;
    document.getElementById('info-hmean').textContent = s.height_stats.mean;
  }

  const sel = document.getElementById('height-col-sel');
  sel.innerHTML = '';
  (s.numeric_columns || []).forEach(c => {
    const opt    = document.createElement('option');
    opt.value    = c;
    opt.textContent = c;
    if (c === s.height_column) opt.selected = true;
    sel.appendChild(opt);
  });

  const b = s.bounds;
  const dataLatLngBounds = L.latLngBounds([b.miny, b.minx], [b.maxy, b.maxx]);
  if (!map) {
    initMap(b.center_lon, b.center_lat);
    map.fitBounds(dataLatLngBounds, { padding: [30, 30] });
  } else {
    map.fitBounds(dataLatLngBounds, { padding: [30, 30] });
  }

  L.rectangle(dataLatLngBounds, {
    color: '#7ec8e3', weight: 1, dashArray: '4,6', fill: false, interactive: false,
  }).addTo(map).bindTooltip(
    `数据集范围<br>SW: ${b.miny.toFixed(4)}°N, ${b.minx.toFixed(4)}°E<br>` +
    `NE: ${b.maxy.toFixed(4)}°N, ${b.maxx.toFixed(4)}°E`,
    { sticky: true }
  );
}

// ══════════════════════════════════════════════════════════════════
// 查询建筑
// ══════════════════════════════════════════════════════════════════
async function queryBuildings() {
  if (!selBounds) return;

  elBtnQuery.disabled     = true;
  elBtnQuery.textContent  = '⏳ 查询中…';
  elOverlay.textContent   = '正在分析建筑数据…';

  const threshold = parseInt(document.getElementById('height-input').value) || 120;
  const hCol      = document.getElementById('height-col-sel').value || null;

  try {
    const res    = await fetch('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...selBounds, threshold, height_column: hCol, max: 30000 }),
    });
    const geojson = await res.json();
    if (geojson.error) throw new Error(geojson.error);

    renderBuildings(geojson);
    updateStats(geojson.stats);
    fetchRoads();
  } catch (e) {
    elOverlay.textContent = '❌ 查询失败：' + e.message;
    console.error(e);
  } finally {
    elBtnQuery.disabled    = false;
    elBtnQuery.textContent = '🔍 分析选区建筑';
  }
}

// ══════════════════════════════════════════════════════════════════
// 黄色道路叠加层（Overpass OSM）
// ══════════════════════════════════════════════════════════════════
async function fetchRoads() {
  if (!selBounds) return;
  if (roadLayer) { map.removeLayer(roadLayer); roadLayer = null; }
  try {
    const res = await fetch('/api/roads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(selBounds),
    });
    const gj = await res.json();
    if (gj.error) { console.warn('[roads]', gj.error); return; }

    const _w = {
      motorway:5, trunk:4, primary:3.5, secondary:3,
      tertiary:2.5, residential:2, service:1.5, unclassified:2,
      motorway_link:2.5, trunk_link:2, primary_link:2,
      secondary_link:1.5, tertiary_link:1.5, living_street:1.5, road:2,
    };
    roadLayer = L.geoJSON(gj, {
      style: f => ({ color: '#e6a817', weight: _w[f.properties.highway] || 2, opacity: 0.9 }),
      onEachFeature: (f, layer) => {
        const n = f.properties.name;
        if (n) layer.bindTooltip(
          `${n} <span style="color:#888">(${f.properties.highway})</span>`,
          { sticky: true, opacity: 0.92 }
        );
      },
    }).addTo(map);
    roadLayer.bringToBack();
  } catch (e) {
    console.warn('[roads] 下载失败:', e);
  }
}

// ══════════════════════════════════════════════════════════════════
// 渲染建筑层
// ══════════════════════════════════════════════════════════════════
function renderBuildings(geojson) {
  if (buildingLayer) map.removeLayer(buildingLayer);

  buildingLayer = L.geoJSON(geojson, {
    style: (feature) => {
      const nf = feature.properties.nf;
      return {
        color:       nf ? '#cc0000' : '#008800',
        weight:      0.6,
        fillColor:   nf ? colorNoFly : colorFly,
        fillOpacity: nf ? opNoFly    : opFly,
      };
    },
    onEachFeature: (feature, layer) => {
      const p  = feature.properties;
      layer.bindPopup(
        `<div class="popup-h">${p.h} m</div>
         <span class="popup-tag ${p.nf ? 'nf' : 'ok'}">
           ${p.nf ? '🔴 禁飞区' : '🟢 可飞区'}
         </span>`,
        { maxWidth: 180 }
      );
      layer.on('mouseover', () => layer.openPopup());
    },
  }).addTo(map);

  if (selRect) map.fitBounds(selRect.getBounds(), { padding: [20, 20] });
}

// ══════════════════════════════════════════════════════════════════
// 更新统计面板
// ══════════════════════════════════════════════════════════════════
function updateStats(stats) {
  document.getElementById('stat-total').textContent = stats.total.toLocaleString();
  document.getElementById('stat-shown').textContent = stats.shown.toLocaleString();
  document.getElementById('stat-nf').textContent    = stats.no_fly.toLocaleString();
  document.getElementById('stat-fly').textContent   = stats.fly.toLocaleString();
  elStatTrunc.style.display = stats.truncated ? 'block' : 'none';

  elStatCard.style.display   = 'block';
  elExportCard.style.display = 'block';

  const pct = stats.total > 0 ? ((stats.no_fly / stats.total) * 100).toFixed(1) : 0;
  elOverlay.textContent =
    `共 ${stats.total.toLocaleString()} 栋建筑 ｜ 禁飞区 ${stats.no_fly.toLocaleString()} (${pct}%) · 可飞区 ${stats.fly.toLocaleString()}`;
}

// ══════════════════════════════════════════════════════════════════
// 导出
// ══════════════════════════════════════════════════════════════════
async function exportData() {
  if (!selBounds) return;

  const btn       = document.getElementById('btn-export');
  btn.disabled    = true;
  btn.textContent = '⏳ 生成中…';

  const threshold = parseInt(document.getElementById('height-input').value) || 120;
  const hCol      = document.getElementById('height-col-sel').value || null;

  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...selBounds,
        threshold,
        height_column: hCol,
        format:        currentFmt,
        grid_spacing:  parseFloat(document.getElementById('grid-spacing')?.value || 200),
      }),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || res.statusText);
    }

    const blob  = await res.blob();
    const cd    = res.headers.get('Content-Disposition') || '';
    const match = cd.match(/filename="?([^"]+)"?/);
    const fname = match ? match[1] : 'export_file';
    const url   = URL.createObjectURL(blob);
    const a     = document.createElement('a');
    a.href = url; a.download = fname; a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('导出失败：' + e.message);
    console.error(e);
  } finally {
    btn.disabled    = false;
    btn.textContent = '⬇ 下载导出文件';
  }
}

// ══════════════════════════════════════════════════════════════════
// 启动
// ══════════════════════════════════════════════════════════════════
initMap(121.47, 31.23);
statusInterval = setInterval(pollStatus, 1500);
pollStatus();
