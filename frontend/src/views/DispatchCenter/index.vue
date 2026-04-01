<template>
  <PageShell
    icon="🗺️"
    title="实时指挥中心"
    desc="仿真控制 · 多维 GIS 可视化 · 实时调度干预 · KPI 统计"
    :badge="systemStore.running ? 'LIVE' : 'READY'"
    :badge-type="systemStore.running ? 'live' : 'info'"
  >
    <div class="dispatch-body">
      <!-- ── 顶部仿真控制栏（迁自 SimulationBox solver tab）── -->
      <div class="dispatch-ctrl">
        <!-- 实时状态 -->
        <div class="sc-status" :class="systemStore.running ? 'sc-status--on' : 'sc-status--off'">
          <span class="sc-dot" :class="systemStore.running ? 'sc-dot--live' : 'sc-dot--idle'"></span>
          <span>{{ systemStore.running ? '仿真运行中' : (initDone ? '已初始化，等待启动' : '未初始化') }}</span>
          <span class="sc-sep">|</span>
          <span>仿真时钟：<strong>{{ systemStore.simTime.toFixed(2) }} s</strong></span>
          <span class="sc-sep">|</span>
          <span>倍率：× {{ systemStore.speedRatio }}</span>
        </div>

        <!-- 控制操作行 -->
        <div class="sc-ctrl-row">
          <!-- 实体摘要 -->
          <div class="sc-entity-row">
            <div class="sc-ent" :class="{ 'sc-ent--empty': !entityStore.depots.length }">
              🏭 仓库 <strong>{{ entityStore.depots.length }}</strong>
            </div>
            <div class="sc-ent" :class="{ 'sc-ent--empty': !entityStore.stations.length }">
              ⚡ 充换电站 <strong>{{ entityStore.stations.length }}</strong>
            </div>
            <div class="sc-ent" :class="{ 'sc-ent--empty': !entityStore.trucks.length }">
              🚛 卡车 <strong>{{ entityStore.trucks.length }}</strong>
            </div>
            <div class="sc-ent" :class="{ 'sc-ent--empty': !entityStore.drones.length }">
              🚁 无人机 <strong>{{ entityStore.drones.length }}</strong>
            </div>
          </div>

          <!-- 操作按钮 + 速率选择 -->
          <div class="sc-actions-area">
            <!-- bbox 提示 -->
            <div class="sc-bbox-hint">
              <span v-if="sceneStore.context">
                📐 场景 bbox：{{ sceneStore.context.road_network.bounds.min_lng.toFixed(4) }},
                {{ sceneStore.context.road_network.bounds.min_lat.toFixed(4) }}
                → {{ sceneStore.context.road_network.bounds.max_lng.toFixed(4) }},
                {{ sceneStore.context.road_network.bounds.max_lat.toFixed(4) }}
              </span>
              <span v-else class="sc-bbox-hint--warn">⚠️ 未加载仿真场景，将使用默认 bbox（上海）</span>
            </div>

            <div class="sc-action-row">
              <button class="sc-btn sc-btn--init" :disabled="initLoading" @click="doInit">
                {{ initLoading ? '⏳ 初始化中...' : '🚀 初始化并发送到后端' }}
              </button>
              <button class="sc-btn sc-btn--start"
                :disabled="!initDone || systemStore.running"
                @click="systemStore.start()">▶ 启动</button>
              <button class="sc-btn sc-btn--pause"
                :disabled="!systemStore.running"
                @click="systemStore.pause()">⏸ 暂停</button>
              <button class="sc-btn sc-btn--reset" @click="doReset">🔄 重置</button>
            </div>

            <div class="sc-speed-row">
              <span class="sc-speed-label">仿真倍率</span>
              <div class="sc-speed-btns">
                <button v-for="s in [0.5, 1, 2, 5, 10]" :key="s"
                  class="sc-spd" :class="{ 'sc-spd--active': systemStore.speedRatio === s }"
                  @click="doSetSpeed(s)">× {{ s }}</button>
              </div>
            </div>
          </div>
        </div>

        <!-- 后端响应日志 -->
        <div class="sc-log">
          <div class="sc-log__head">
            📋 后端响应日志
            <button class="sc-log__clear" @click="ctrlLogs = []">清空</button>
          </div>
          <div v-if="!ctrlLogs.length" class="sc-log__empty">
            点击「初始化」后，后端响应与控制结果将显示在这里
          </div>
          <div v-for="(l, i) in ctrlLogs" :key="i"
            class="sc-log__row" :class="`sc-log__row--${l.type}`">
            <span class="sc-log__ts">{{ l.ts }}</span>
            <span class="sc-log__msg">{{ l.msg }}</span>
          </div>
        </div>
      </div>

      <!-- ── 三栏主体 ── -->
      <div class="dispatch-main">
        <!-- 左侧面板 -->
        <aside class="dispatch-aside">
          <!-- KPI 卡片组（迁自 Dashboard） -->
          <div class="kpi-grid">
            <div v-for="k in kpiList" :key="k.label" class="kpi-card">
              <div class="kpi-card__icon">{{ k.icon }}</div>
              <div class="kpi-card__value">{{ k.value }}</div>
              <div class="kpi-card__label">{{ k.label }}</div>
              <div class="kpi-card__trend" :class="k.up ? 'trend--up' : 'trend--down'">
                {{ k.up ? '↑' : '↓' }} {{ k.change }}
              </div>
            </div>
          </div>

          <!-- 动态订单队列（实况统计） -->
          <SectionCard title="动态订单队列" icon="📋">
            <template v-if="orderStore.stats">
              <div class="order-stats-grid">
                <div class="order-stat">
                  <span class="order-stat__num">{{ orderStore.stats.orders_pending }}</span>
                  <span class="order-stat__lbl">待分配</span>
                </div>
                <div class="order-stat">
                  <span class="order-stat__num order-stat__num--active">{{ orderStore.stats.orders_assigned }}</span>
                  <span class="order-stat__lbl">配送中</span>
                </div>
                <div class="order-stat">
                  <span class="order-stat__num order-stat__num--success">{{ orderStore.stats.orders_completed }}</span>
                  <span class="order-stat__lbl">已完成</span>
                </div>
                <div class="order-stat">
                  <span class="order-stat__num order-stat__num--danger">{{ orderStore.stats.orders_timeout }}</span>
                  <span class="order-stat__lbl">超时</span>
                </div>
              </div>
            </template>
            <div v-else class="empty-list">
              <span>⏳ 等待仿真数据接入</span>
            </div>
          </SectionCard>

          <SectionCard title="告警中心" icon="🔔">
            <div class="empty-list">
              <div class="alert-badge alert-badge--ok">✅ 无活跃告警</div>
            </div>
          </SectionCard>

          <SectionCard title="快速决策" icon="⚡">
            <div class="empty-list">
              <span>模式 E 触发时弹出算法推荐路径</span>
            </div>
          </SectionCard>
        </aside>

        <!-- 中央地图区 -->
        <div class="dispatch-map">
          <UnifiedMapView v-if="sceneStore.context" ref="mapRef" />
          <div v-else class="map-placeholder">
            <div class="map-placeholder__icon">🌐</div>
            <p class="map-placeholder__title">尚未加载仿真场景</p>
            <p class="map-placeholder__desc">
              请前往「环境构建」完成建筑分析后<br />
              点击「<strong>导出到指挥中心地图</strong>」以建立仿真底图。
            </p>
            <router-link to="/simulation" class="go-btn">前往环境构建 →</router-link>
          </div>
        </div>

        <!-- 右侧详情面板 -->
        <aside class="dispatch-detail">
          <SectionCard title="实体详情" icon="🔍">
            <p class="detail-hint">点击地图上的实体查看详情</p>
            <div class="detail-tabs">
              <button class="detail-tab detail-tab--active">卡车</button>
              <button class="detail-tab">无人机</button>
              <button class="detail-tab">电站</button>
            </div>
            <div class="empty-list">
              <span>未选中任何实体</span>
            </div>
          </SectionCard>

          <!-- 履约模式构成（迁自 Dashboard） -->
          <div class="chart-card">
            <div class="chart-card__header">
              <span class="chart-card__title">履约模式构成</span>
              <span class="chart-card__sub">模式 A–E 触发频次</span>
            </div>
            <div class="chart-card__body">
              <div class="mode-bars">
                <div v-for="m in modes" :key="m.label" class="mode-bar">
                  <span class="mode-bar__label">{{ m.label }}</span>
                  <div class="mode-bar__track">
                    <div class="mode-bar__fill" :style="{ width: m.pct + '%', background: m.color }" />
                  </div>
                  <span class="mode-bar__pct">{{ m.pct }}%</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 能量与中继效率（迁自 Dashboard） -->
          <div class="chart-card">
            <div class="chart-card__header">
              <span class="chart-card__title">能量与中继效率</span>
              <span class="chart-card__sub">充换电站拥堵热力图</span>
            </div>
            <div class="chart-card__body chart-card__body--placeholder">
              <div class="placeholder-visual">
                <div class="placeholder-visual__grid">
                  <div v-for="i in 20" :key="i"
                    class="placeholder-visual__cell"
                    :style="{ opacity: Math.random() * 0.6 + 0.2, background: heatColor(i) }"
                  />
                </div>
                <p class="placeholder-visual__hint">ECharts 热力图 · 待接入实时数据</p>
              </div>
            </div>
          </div>

          <!-- 时效与成本趋势（迁自 Dashboard） -->
          <div class="chart-card">
            <div class="chart-card__header">
              <span class="chart-card__title">时效与成本趋势</span>
              <span class="chart-card__sub">订单量 vs 超时惩罚成本</span>
            </div>
            <div class="chart-card__body chart-card__body--placeholder">
              <div class="placeholder-visual">
                <svg viewBox="0 0 400 80" class="mock-chart" preserveAspectRatio="none">
                  <polyline
                    points="0,60 40,50 80,55 120,30 160,35 200,20 240,25 280,15 320,22 360,10 400,18"
                    fill="none" stroke="var(--hl-primary)" stroke-width="2" />
                  <polyline
                    points="0,70 40,65 80,70 120,60 160,62 200,55 240,58 280,50 320,52 360,45 400,42"
                    fill="none" stroke="var(--hl-danger)" stroke-width="2" stroke-dasharray="4 2" />
                </svg>
                <p class="placeholder-visual__hint">ECharts 时序折线图 · 待接入仿真数据流</p>
              </div>
            </div>
          </div>
        </aside>
      </div>
    </div>
  </PageShell>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import PageShell     from '@/components/PageShell/index.vue'
import SectionCard   from './components/SectionCard.vue'
import UnifiedMapView from './components/UnifiedMapView.vue'
import { useSceneStore }  from '@/stores/scene'
import { useSystemStore } from '@/stores/system'
import { useEntityStore } from '@/stores/entity'
import { useOrderStore }  from '@/stores/order'

const sceneStore  = useSceneStore()
const systemStore = useSystemStore()
const entityStore = useEntityStore()
const orderStore  = useOrderStore()

const mapRef = ref<InstanceType<typeof UnifiedMapView> | null>(null)

// initDone 从 store 派生，路由切换不丢失
const initDone    = computed(() => systemStore.running || systemStore.simTime > 0)
const initLoading = ref(false)

interface CtrlLog { type: 'info' | 'success' | 'error' | 'warn'; ts: string; msg: string }
const ctrlLogs = ref<CtrlLog[]>([])

// ── 迁自 Dashboard ──────────────────────────────────────────────────
const kpiList = [
  { icon: '✅', label: '综合任务完成率', value: '—',     change: '—', up: true  },
  { icon: '⏱️', label: '准时送达率',     value: '—',     change: '—', up: true  },
  { icon: '📉', label: '平均订单延迟',   value: '— min', change: '—', up: false },
  { icon: '⚡', label: '总体能耗成本',   value: '— Wh',  change: '—', up: false },
]

const modes = [
  { label: '模式A · 卡车直送',   pct: 0, color: '#2563eb' },
  { label: '模式B · 卡车+无人机', pct: 0, color: '#7c3aed' },
  { label: '模式C · 仓库直飞',   pct: 0, color: '#0891b2' },
  { label: '模式D · 空投补货',   pct: 0, color: '#d97706' },
  { label: '模式E · 多跳中继',   pct: 0, color: '#dc2626' },
]

function heatColor(i: number) {
  const hue = 200 + i * 8
  return `hsl(${hue}, 70%, 55%)`
}

// ── 仿真控制函数（迁自 SimulationBox solver tab）─────────────────────
function _log(type: CtrlLog['type'], msg: string) {
  ctrlLogs.value.unshift({ type, ts: new Date().toLocaleTimeString('zh-CN'), msg })
}

async function doInit() {
  initLoading.value = true
  _log('info', '正在发送初始化请求到后端...')
  try {
    const bounds = sceneStore.context?.road_network.bounds
    const result = await systemStore.initSim({
      bbox:    bounds,
      sceneId: sceneStore.context?.scene_id,
    })
    const s = result.summary
    _log('success', `✅ 初始化成功 — 仓库×${s.depots} · 站点×${s.stations} · 卡车×${s.trucks} · 无人机×${s.drones}`)
  } catch (e: any) {
    _log('error', `❌ 初始化失败：${e.message}`)
  } finally {
    initLoading.value = false
  }
}

async function doReset() {
  await systemStore.reset().catch(() => {})
  _log('warn', '🔄 仿真已重置，请重新初始化')
}

async function doSetSpeed(ratio: number) {
  await systemStore.setSpeed(ratio)
  _log('info', `⚡ 速率已设为 ×${ratio}`)
}
</script>

<style scoped>
/* ── 主体：flex 列布局 ─────────────────────────────────────────────── */
.dispatch-body {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: var(--hl-space-md);
  overflow: hidden;
}

/* ── 顶部控制栏 ──────────────────────────────────────────────────── */
.dispatch-ctrl {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: var(--hl-space-sm);
}

/* 状态栏 */
.sc-status {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 10px 14px; border-radius: var(--hl-card-radius);
  font-size: 12.5px;
  border: 1px solid var(--hl-border);
}
.sc-status--on  { background: #d1fae5; border-color: #6ee7b7; color: #065f46; }
.sc-status--off { background: var(--hl-card-bg); color: var(--hl-text-secondary); }
.sc-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.sc-dot--live { background: #10b981; animation: sc-pulse 1.5s infinite; }
.sc-dot--idle { background: var(--hl-text-muted); }
.sc-sep { color: var(--hl-border); padding: 0 2px; }

@keyframes sc-pulse {
  0%, 100% { opacity: 1; }
  50%       { opacity: 0.4; }
}

/* 控制操作行：左实体摘要，右按钮+速率 */
.sc-ctrl-row {
  display: flex;
  gap: var(--hl-space-md);
  align-items: flex-start;
  flex-wrap: wrap;
}

/* 实体摘要 */
.sc-entity-row { display: flex; gap: 8px; flex-wrap: wrap; flex: 1; min-width: 0; }
.sc-ent {
  flex: 1; min-width: 110px;
  padding: 8px 12px;
  background: var(--hl-card-bg);
  border: 1px solid var(--hl-card-border);
  border-radius: var(--hl-card-radius);
  font-size: 13px; color: var(--hl-text-secondary);
}
.sc-ent strong { color: var(--hl-primary); font-size: 15px; margin-left: 4px; }
.sc-ent--empty strong { color: var(--hl-danger); }

/* 操作区：bbox + 按钮行 + 速率 */
.sc-actions-area { display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; }

/* bbox 提示 */
.sc-bbox-hint {
  font-size: 11.5px; color: var(--hl-text-muted);
  padding: 5px 10px;
  background: var(--hl-content-bg);
  border-radius: var(--hl-border-radius);
}
.sc-bbox-hint--warn { color: #d97706; }

/* 操作按钮行 */
.sc-action-row { display: flex; gap: 8px; flex-wrap: wrap; }
.sc-btn {
  height: 34px; padding: 0 16px; border-radius: var(--hl-border-radius);
  font-size: 13px; font-weight: 500; cursor: pointer;
  transition: background var(--hl-transition), opacity var(--hl-transition);
  border: none;
}
.sc-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.sc-btn--init  { background: var(--hl-primary); color: #fff; flex: 1; min-width: 160px; }
.sc-btn--init:hover:not(:disabled)  { background: #1d4ed8; }
.sc-btn--start { background: var(--hl-success); color: #fff; }
.sc-btn--start:hover:not(:disabled) { background: #15803d; }
.sc-btn--pause { background: var(--hl-warning); color: #fff; }
.sc-btn--pause:hover:not(:disabled) { background: #b45309; }
.sc-btn--reset { background: none; border: 1px solid var(--hl-border); color: var(--hl-text-secondary); }
.sc-btn--reset:hover { background: var(--hl-content-bg); color: var(--hl-danger); border-color: var(--hl-danger); }

/* 速率选择 */
.sc-speed-row { display: flex; align-items: center; gap: 12px; }
.sc-speed-label { font-size: 12px; font-weight: 500; color: var(--hl-text-secondary); white-space: nowrap; }
.sc-speed-btns  { display: flex; gap: 6px; flex-wrap: wrap; }
.sc-spd {
  height: 28px; min-width: 44px; padding: 0 10px;
  border: 1px solid var(--hl-border); border-radius: 6px;
  background: var(--hl-card-bg); font-size: 12px;
  color: var(--hl-text-secondary); cursor: pointer;
  transition: all var(--hl-transition);
}
.sc-spd:hover      { border-color: var(--hl-primary); color: var(--hl-primary); }
.sc-spd--active    { background: var(--hl-primary); color: #fff; border-color: var(--hl-primary); font-weight: 600; }

/* 后端响应日志 */
.sc-log {
  max-height: 140px;
  background: #0f172a;
  border-radius: var(--hl-card-radius);
  overflow-y: auto;
  display: flex; flex-direction: column;
}
.sc-log__head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 14px; position: sticky; top: 0;
  font-size: 12px; font-weight: 500;
  color: #94a3b8; background: #0f172a;
  border-bottom: 1px solid #1e293b;
}
.sc-log__clear {
  font-size: 11px; color: #64748b; background: none; border: none;
  cursor: pointer; padding: 0 4px;
}
.sc-log__clear:hover { color: #94a3b8; }
.sc-log__empty {
  padding: 16px; text-align: center;
  font-size: 12px; color: #475569;
}
.sc-log__row {
  display: flex; gap: 10px; align-items: baseline;
  padding: 3px 14px;
  font-size: 12px; font-family: 'Courier New', monospace;
  border-bottom: 1px solid #1e293b;
}
.sc-log__ts  { color: #475569; white-space: nowrap; flex-shrink: 0; }
.sc-log__msg { color: #e2e8f0; word-break: break-all; }
.sc-log__row--success .sc-log__msg { color: #4ade80; }
.sc-log__row--error   .sc-log__msg { color: #f87171; }
.sc-log__row--warn    .sc-log__msg { color: #fbbf24; }
.sc-log__row--info    .sc-log__msg { color: #94a3b8; }

/* ── 三栏主体 ─────────────────────────────────────────────────────── */
.dispatch-main {
  display: flex;
  flex: 1;
  min-height: 0;
  gap: var(--hl-space-md);
  overflow: hidden;
}

/* 左侧面板 */
.dispatch-aside {
  width: 240px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: var(--hl-space-md);
  overflow-y: auto;
}

/* KPI 网格 */
.kpi-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--hl-space-sm);
}

.kpi-card {
  background: var(--hl-card-bg);
  border: 1px solid var(--hl-card-border);
  border-radius: var(--hl-card-radius);
  padding: 10px 12px;
  box-shadow: var(--hl-card-shadow);
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.kpi-card__icon  { font-size: 16px; }
.kpi-card__value { font-size: 20px; font-weight: 700; color: var(--hl-text); line-height: 1.2; }
.kpi-card__label { font-size: 10.5px; color: var(--hl-text-muted); }
.kpi-card__trend { font-size: 10px; font-weight: 600; margin-top: 2px; }
.trend--up   { color: var(--hl-success); }
.trend--down { color: var(--hl-danger); }

/* 订单统计格 */
.order-stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.order-stat {
  display: flex; flex-direction: column; align-items: center;
  padding: 8px 4px;
  background: var(--hl-content-bg);
  border-radius: var(--hl-border-radius);
}
.order-stat__num   { font-size: 20px; font-weight: 700; color: var(--hl-text); }
.order-stat__num--active  { color: #3b82f6; }
.order-stat__num--success { color: var(--hl-success); }
.order-stat__num--danger  { color: var(--hl-danger); }
.order-stat__lbl   { font-size: 11px; color: var(--hl-text-muted); }

/* 地图区 */
.dispatch-map {
  flex: 1;
  min-width: 0;
  background: var(--hl-card-bg);
  border: 1px solid var(--hl-card-border);
  border-radius: var(--hl-card-radius);
  box-shadow: var(--hl-card-shadow);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.map-placeholder {
  text-align: center;
  padding: 40px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.map-placeholder__icon  { font-size: 48px; }
.map-placeholder__title { font-size: 16px; font-weight: 600; color: var(--hl-text); }
.map-placeholder__desc  { font-size: 12.5px; color: var(--hl-text-muted); line-height: 1.7; }

.go-btn {
  display: inline-block;
  margin-top: 6px;
  padding: 8px 20px;
  background: var(--hl-primary, #1565c0);
  color: #fff;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  text-decoration: none;
  transition: opacity 0.2s;
}
.go-btn:hover { opacity: 0.85; }

/* 右侧详情面板 */
.dispatch-detail {
  width: 280px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: var(--hl-space-md);
  overflow-y: auto;
}

.detail-hint {
  font-size: 11.5px; color: var(--hl-text-muted); margin-bottom: 10px;
}
.detail-tabs { display: flex; gap: 4px; margin-bottom: 12px; }
.detail-tab {
  flex: 1; height: 28px;
  border: 1px solid var(--hl-border);
  border-radius: 4px; background: none;
  font-size: 12px; cursor: pointer;
  color: var(--hl-text-secondary);
  transition: all var(--hl-transition);
}
.detail-tab--active {
  background: var(--hl-primary-alpha);
  border-color: var(--hl-primary);
  color: var(--hl-primary);
  font-weight: 600;
}

.empty-list { font-size: 12px; color: var(--hl-text-muted); padding: 12px 0; text-align: center; }
.alert-badge--ok { font-size: 12px; color: var(--hl-success); }

/* ── 图表卡片（迁自 Dashboard）─────────────────────────────────────── */
.chart-card {
  background: var(--hl-card-bg);
  border: 1px solid var(--hl-card-border);
  border-radius: var(--hl-card-radius);
  box-shadow: var(--hl-card-shadow);
  overflow: hidden;
}
.chart-card__header {
  padding: 10px 14px 8px;
  border-bottom: 1px solid var(--hl-border);
  display: flex; align-items: baseline; gap: 8px;
}
.chart-card__title { font-size: 12.5px; font-weight: 600; color: var(--hl-text); }
.chart-card__sub   { font-size: 10.5px; color: var(--hl-text-muted); }
.chart-card__body  { padding: 12px 14px; }
.chart-card__body--placeholder {
  display: flex; align-items: center; justify-content: center;
}

/* 模式条形 */
.mode-bars { display: flex; flex-direction: column; gap: 8px; }
.mode-bar  { display: flex; align-items: center; gap: 6px; }
.mode-bar__label { font-size: 11px; color: var(--hl-text-secondary); width: 130px; flex-shrink: 0; }
.mode-bar__track {
  flex: 1; height: 5px;
  background: var(--hl-border); border-radius: 3px; overflow: hidden;
}
.mode-bar__fill  { height: 100%; border-radius: 3px; transition: width 0.4s ease; }
.mode-bar__pct   { font-size: 10.5px; color: var(--hl-text-muted); width: 28px; text-align: right; }

/* 占位可视化 */
.placeholder-visual { width: 100%; display: flex; flex-direction: column; align-items: center; gap: 8px; }
.placeholder-visual__grid {
  display: grid; grid-template-columns: repeat(10, 1fr);
  gap: 2px; width: 100%;
}
.placeholder-visual__cell { aspect-ratio: 1; border-radius: 2px; }
.placeholder-visual__hint { font-size: 10.5px; color: var(--hl-text-muted); text-align: center; }
.mock-chart { width: 100%; height: 70px; }
</style>
