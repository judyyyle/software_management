<template>
  <div class="unified-map-wrap">
    <!-- Leaflet 地图容器 -->
    <div ref="mapEl" class="unified-map"></div>

    <!-- 加载中遮罩 -->
    <div v-if="layerLoading" class="umap-loading">
      <div class="umap-spinner"></div>
      <span>{{ layerLoadingText }}</span>
    </div>

    <!-- 图例 -->
    <div class="umap-legend">
      <div class="umap-legend__item">
        <span class="umap-legend__pin" style="background:#1e40af">🏭</span>仓库
      </div>
      <div class="umap-legend__item">
        <span class="umap-legend__pin" style="background:#d97706">⚡</span>充换电站
      </div>
      <div class="umap-legend__item">
        <span class="umap-legend__box" style="background:#ff4444;opacity:.65"></span>禁飞区建筑
      </div>
      <div class="umap-legend__item">
        <span class="umap-legend__box" style="background:#33bb55;opacity:.4"></span>可飞区建筑
      </div>
      <div class="umap-legend__item">
        <span class="umap-legend__line" style="background:#e6a817"></span>道路网络
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { useSceneStore, type SceneContext } from '@/stores/scene'
import { useEntityStore } from '@/stores/entity'

declare const L: typeof import('leaflet')

// ── 类型 ─────────────────────────────────────────────────────────

interface SelBounds { minx: number; miny: number; maxx: number; maxy: number }

// ── Store & refs ──────────────────────────────────────────────────

const sceneStore  = useSceneStore()
const entityStore = useEntityStore()

const mapEl         = ref<HTMLDivElement | null>(null)
const layerLoading  = ref(false)
const layerLoadingText = ref('正在加载地图数据…')

let map: ReturnType<typeof L.map> | null = null
let buildingLayer:  L.GeoJSON      | null = null
let roadLayer:      L.GeoJSON      | null = null
let depotGroup:     L.LayerGroup   | null = null
let stationGroup:   L.LayerGroup   | null = null

// ── 道路权重（与 GeoTool 保持一致）────────────────────────────────

const ROAD_WIDTHS: Record<string, number> = {
  motorway: 5, trunk: 4, primary: 3.5, secondary: 3,
  tertiary: 2.5, residential: 2, service: 1.5, unclassified: 2,
  motorway_link: 2.5, trunk_link: 2, primary_link: 2,
  secondary_link: 1.5, tertiary_link: 1.5, living_street: 1.5, road: 2,
}

// ── 地图初始化 ────────────────────────────────────────────────────

function initMap() {
  if (!mapEl.value) return

  // L.canvas() 渲染器：强制，不可更改（支持 3000+ 多边形高频刷新）
  const renderer = L.canvas({ padding: 0.5 })

  map = L.map(mapEl.value, {
    center:   [31.23, 121.47],
    zoom:     13,
    renderer,
    zoomControl: true,
  })

  L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    { attribution: '© CARTO · © OpenStreetMap 贡献者', maxZoom: 19 }
  ).addTo(map)
}

// ── 数据加载 ───────────────────────────────────────────────────────

async function loadLayers(scene: SceneContext) {
  if (!map) return

  layerLoading.value     = true
  layerLoadingText.value = '正在加载建筑与路网数据…'

  const bounds: SelBounds = {
    minx: scene.sel_bounds.minx,
    miny: scene.sel_bounds.miny,
    maxx: scene.sel_bounds.maxx,
    maxy: scene.sel_bounds.maxy,
  }

  // 并行获取建筑和路网数据（各自独立 catch，互不影响）
  let buildGeoJSON: unknown = null
  let roadGeoJSON:  unknown = null

  await Promise.all([
    fetch('/api/geo/query', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        ...bounds,
        threshold:     scene.threshold,
        height_column: scene.height_column ?? null,
        max:           30000,
      }),
    }).then(r => r.json()).then(d => { if (!d.error) buildGeoJSON = d }).catch(() => {}),

    fetch('/api/geo/roads', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(bounds),
    }).then(r => r.json()).then(d => { if (!d.error) roadGeoJSON = d }).catch(() => {}),
  ])

  // 建筑图层
  if (buildingLayer) { map.removeLayer(buildingLayer); buildingLayer = null }
  if (buildGeoJSON) {
    buildingLayer = L.geoJSON(buildGeoJSON as GeoJSON.FeatureCollection, {
      style: (feature: GeoJSON.Feature | undefined) => {
        const nf = feature?.properties?.nf
        return {
          color:       nf ? '#cc0000' : '#008800',
          weight:      0.6,
          fillColor:   nf ? '#ff4444' : '#33bb55',
          fillOpacity: nf ? 0.65 : 0.35,
        }
      },
      onEachFeature: (feature: GeoJSON.Feature, layer: L.Layer) => {
        const p = feature.properties
        if (!p) return
        layer.bindPopup(
          `<div style="font-size:1rem;font-weight:700">${p.h} m</div>
           <span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;
                        background:${p.nf ? '#ae2012' : '#2d6a4f'};color:#fff">
             ${p.nf ? '🔴 禁飞区' : '🟢 可飞区'}
           </span>`,
          { maxWidth: 180 }
        )
      },
    }).addTo(map)
  }

  // 道路图层
  if (roadLayer) { map.removeLayer(roadLayer); roadLayer = null }
  if (roadGeoJSON) {
    roadLayer = L.geoJSON(roadGeoJSON as GeoJSON.FeatureCollection, {
      style: (f: GeoJSON.Feature | undefined) => ({
        color:   '#e6a817',
        weight:  ROAD_WIDTHS[f?.properties?.highway] ?? 2,
        opacity: 0.9,
      }),
      onEachFeature: (f: GeoJSON.Feature, layer: L.Layer) => {
        const n = f.properties?.name
        if (n) layer.bindTooltip(
          `${n} <span style="color:#888">(${f.properties?.highway})</span>`,
          { sticky: true, opacity: 0.92 }
        )
      },
    }).addTo(map)
    roadLayer.bringToBack()
  }

  // 缩放到路网范围
  const rb = scene.road_network?.bounds
  if (rb) {
    map.fitBounds([[rb.min_lat, rb.min_lng], [rb.max_lat, rb.max_lng]], { padding: [20, 20] })
  } else {
    map.fitBounds([
      [bounds.miny, bounds.minx],
      [bounds.maxy, bounds.maxx],
    ], { padding: [20, 20] })
  }

  // 场景加载完成后立即绘制设施标注（确保显示在建筑/道路图层之上）
  drawFacilities()

  layerLoading.value = false
}

// ── 设施标注（仓库 + 充换电站）────────────────────────────────────

function _depotIcon(name: string) {
  return L.divIcon({
    className: '',
    html: `<div class="fac-pin fac-pin--depot" title="${name}">🏭</div>`,
    iconSize:   [34, 34],
    iconAnchor: [17, 17],
    popupAnchor: [0, -18],
  })
}

function _stationIcon(name: string) {
  return L.divIcon({
    className: '',
    html: `<div class="fac-pin fac-pin--station" title="${name}">⚡</div>`,
    iconSize:   [28, 28],
    iconAnchor: [14, 14],
    popupAnchor: [0, -15],
  })
}

function drawFacilities() {
  if (!map) return

  // 确保 LayerGroup 存在（创建一次后复用）
  if (!depotGroup)   { depotGroup   = L.layerGroup().addTo(map) }
  if (!stationGroup) { stationGroup = L.layerGroup().addTo(map) }

  depotGroup.clearLayers()
  stationGroup.clearLayers()

  for (const depot of entityStore.depots) {
    // 跳过坐标未就绪（未选地图时默认 0,0）
    if (depot.lng === 0 && depot.lat === 0) continue
    L.marker([depot.lat, depot.lng], { icon: _depotIcon(depot.name) })
      .bindPopup(
        `<div class="fac-popup">
          <div class="fac-popup__title">🏭 ${depot.name}</div>
          <div class="fac-popup__row"><span>ID</span><span>${depot.depot_id}</span></div>
          <div class="fac-popup__row"><span>容量</span><span>${depot.capacity} 件</span></div>
          <div class="fac-popup__row"><span>充换电位</span><span>${depot.parking_slots} 个</span></div>
          <div class="fac-popup__row"><span>坐标</span><span>${depot.lng.toFixed(5)}, ${depot.lat.toFixed(5)}</span></div>
        </div>`,
        { maxWidth: 220, className: 'fac-popup-wrap' }
      )
      .addTo(depotGroup)
  }

  for (const sta of entityStore.stations) {
    if (sta.lng === 0 && sta.lat === 0) continue
    L.marker([sta.lat, sta.lng], { icon: _stationIcon(sta.name) })
      .bindPopup(
        `<div class="fac-popup">
          <div class="fac-popup__title">⚡ ${sta.name}</div>
          <div class="fac-popup__row"><span>ID</span><span>${sta.station_id}</span></div>
          <div class="fac-popup__row"><span>并发槽位</span><span>${sta.parking_slots} 个</span></div>
          <div class="fac-popup__row"><span>换电耗时</span><span>${sta.swap_time} s</span></div>
          <div class="fac-popup__row"><span>坐标</span><span>${sta.lng.toFixed(5)}, ${sta.lat.toFixed(5)}</span></div>
        </div>`,
        { maxWidth: 220, className: 'fac-popup-wrap' }
      )
      .addTo(stationGroup)
  }
}

// ── 生命周期 ──────────────────────────────────────────────────────

onMounted(() => {
  initMap()
  if (sceneStore.context) {
    loadLayers(sceneStore.context)
  } else {
    // 无场景时也尝试绘制设施（坐标为 0 的会被跳过）
    drawFacilities()
  }
})

onBeforeUnmount(() => {
  depotGroup?.remove()
  stationGroup?.remove()
  map?.remove()
  map = null
})

// ── 监听 sceneStore 变更（跨路由导航时自动刷新地图层）───────────────

watch(() => sceneStore.context, (ctx) => {
  if (ctx && map) loadLayers(ctx)
})

// ── 监听实体变更（redistributeByBounds 或初始化后重绘设施标注）───────

watch(
  () => [entityStore.depots, entityStore.stations],
  () => { if (map) drawFacilities() },
  { deep: true }
)

// ── P2 接口（供 DispatchCenter 父组件调用）────────────────────────────

function setFacilities(_geojson: unknown) { drawFacilities() }
function updateTruck(_id: string, _lng: number, _lat: number) { /* P2: 卡车位置更新 */ }
function updateDrone(_id: string, _lng: number, _lat: number, _alt: number) { /* P2: 无人机位置更新 */ }
function addOrder(_id: string, _lng: number, _lat: number) { /* P2: 订单标记 */ }
function clearDynamic() {
  depotGroup?.clearLayers()
  stationGroup?.clearLayers()
}

defineExpose({ setFacilities, updateTruck, updateDrone, addOrder, clearDynamic })
</script>

<style scoped>
.unified-map-wrap {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 400px;
  border-radius: inherit;
  overflow: hidden;
}

.unified-map {
  width: 100%;
  height: 100%;
}

/* ── 加载遮罩 ── */
.umap-loading {
  position: absolute;
  inset: 0;
  background: rgba(240, 244, 248, 0.92);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  z-index: 9999;
  font-size: 0.9rem;
  color: #333;
}

.umap-spinner {
  width: 36px;
  height: 36px;
  border: 3px solid #b0c4de;
  border-top-color: #1565c0;
  border-radius: 50%;
  animation: umap-spin 0.9s linear infinite;
}

@keyframes umap-spin { to { transform: rotate(360deg); } }

/* ── 图例 ── */
.umap-legend {
  position: absolute;
  bottom: 28px;
  right: 10px;
  background: rgba(255, 255, 255, 0.95);
  border: 1px solid #b0c4de;
  border-radius: 8px;
  padding: 10px 14px;
  z-index: 800;
  font-size: 0.78rem;
  color: #333;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.umap-legend__item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.umap-legend__box {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid #ccc;
  flex-shrink: 0;
}

.umap-legend__pin {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  font-size: 0.7rem;
  flex-shrink: 0;
  border: 1.5px solid rgba(255, 255, 255, 0.9);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2);
}

.umap-legend__line {
  display: inline-block;
  width: 20px;
  height: 3px;
  border-radius: 2px;
  flex-shrink: 0;
}

/* ── 设施标注 Pin（DivIcon，使用 :deep 穿透 Leaflet DOM）── */
:deep(.fac-pin) {
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
  border: 2.5px solid rgba(255, 255, 255, 0.95);
  cursor: pointer;
  transition: transform 0.15s;
  user-select: none;
}

:deep(.fac-pin:hover) { transform: scale(1.18); }

:deep(.fac-pin--depot) {
  width: 34px;
  height: 34px;
  font-size: 1.15rem;
  background: #1e40af;
}

:deep(.fac-pin--station) {
  width: 28px;
  height: 28px;
  font-size: 0.95rem;
  background: #d97706;
}

/* ── 设施弹窗内容 ── */
:deep(.fac-popup-wrap .leaflet-popup-content-wrapper) {
  border-radius: 10px;
  padding: 0;
  overflow: hidden;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18);
}

:deep(.fac-popup-wrap .leaflet-popup-content) {
  margin: 0;
}

:deep(.fac-popup) {
  min-width: 180px;
  font-size: 0.82rem;
  color: #1a1a2e;
}

:deep(.fac-popup__title) {
  padding: 8px 12px;
  font-weight: 700;
  font-size: 0.9rem;
  background: #f0f4ff;
  border-bottom: 1px solid #dde6f5;
}

:deep(.fac-popup__row) {
  display: flex;
  justify-content: space-between;
  padding: 4px 12px;
  border-bottom: 1px solid #f0f0f0;
  gap: 12px;
}

:deep(.fac-popup__row:last-child) { border-bottom: none; }
:deep(.fac-popup__row span:first-child) { color: #666; }
:deep(.fac-popup__row span:last-child) { font-weight: 600; color: #1a1a2e; }
</style>
