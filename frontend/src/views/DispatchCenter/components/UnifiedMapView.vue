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

declare const L: typeof import('leaflet')

// ── 类型 ─────────────────────────────────────────────────────────

interface SelBounds { minx: number; miny: number; maxx: number; maxy: number }

// ── Store & refs ──────────────────────────────────────────────────

const sceneStore = useSceneStore()

const mapEl         = ref<HTMLDivElement | null>(null)
const layerLoading  = ref(false)
const layerLoadingText = ref('正在加载地图数据…')

let map: ReturnType<typeof L.map> | null = null
let buildingLayer: L.GeoJSON | null      = null
let roadLayer: L.GeoJSON | null          = null

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

  layerLoading.value = false
}

// ── 生命周期 ──────────────────────────────────────────────────────

onMounted(() => {
  initMap()
  if (sceneStore.context) {
    loadLayers(sceneStore.context)
  }
})

onBeforeUnmount(() => {
  map?.remove()
  map = null
})

// ── 监听 sceneStore 变更（跨路由导航时自动刷新）────────────────────

watch(() => sceneStore.context, (ctx) => {
  if (ctx && map) loadLayers(ctx)
})

// ── P2 接口预留（供 DispatchCenter 父组件调用，仿真实体覆盖层）──────

function setFacilities(_geojson: unknown) { /* P2: 设施层 */ }
function updateTruck(_id: string, _lng: number, _lat: number) { /* P2: 卡车位置更新 */ }
function updateDrone(_id: string, _lng: number, _lat: number, _alt: number) { /* P2: 无人机位置更新 */ }
function addOrder(_id: string, _lng: number, _lat: number) { /* P2: 订单标记 */ }
function clearDynamic() { /* P2: 清除所有动态实体覆盖层 */ }

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

.umap-legend__line {
  display: inline-block;
  width: 20px;
  height: 3px;
  border-radius: 2px;
  flex-shrink: 0;
}
</style>
