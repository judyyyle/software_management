<template>
  <div class="sim-box">
    <!-- Tab 导航栏 -->
    <div class="sim-box__tabs" role="tablist" aria-label="仿真配置选项卡">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        class="sim-box__tab"
        :class="{ 'sim-box__tab--active': activeTab === tab.id }"
        :aria-selected="activeTab === tab.id"
        role="tab"
        @click="activeTab = tab.id"
      >
        <span class="sim-tab__icon">{{ tab.icon }}</span>
        <span class="sim-tab__label">{{ tab.label }}</span>
        <span v-if="tab.badge" class="sim-tab__badge" :class="`sim-tab__badge--${tab.badgeType}`">
          {{ tab.badge }}
        </span>
      </button>
    </div>

    <!-- Tab 内容区 -->
    <div class="sim-box__content" role="tabpanel">
      <!-- Tab 0：地图选取与环境构建（GeoTool） -->
      <GeoToolView v-if="activeTab === 'geo'" />

      <!-- Tab 1：动态订单生成器（占位） -->
      <div v-else-if="activeTab === 'orders'" class="sim-placeholder">
        <div class="sim-placeholder__icon">📦</div>
        <h3 class="sim-placeholder__title">动态订单生成器</h3>
        <p class="sim-placeholder__desc">
          配置仿真区域内的订单空间分布、时间频率与属性边界，<br />
          驱动调度求解器在仿真时间轴上动态生成接单事件。
        </p>
        <div class="config-grid">
          <ConfigItem icon="📍" title="空间分布" desc="均匀 / 泊松 / 商业区热力聚集" />
          <ConfigItem icon="⏱️" title="时间分布" desc="接单频率、高峰期涌入率" />
          <ConfigItem icon="📏" title="属性边界" desc="重量 q_i 范围、时间窗宽容度 t_deadline" />
        </div>
      </div>

      <!-- Tab 2：调度求解器参数配置（占位） -->
      <div v-else-if="activeTab === 'solver'" class="sim-placeholder">
        <div class="sim-placeholder__icon">🧠</div>
        <h3 class="sim-placeholder__title">调度求解器参数配置</h3>
        <p class="sim-placeholder__desc">
          调整容量约束、惩罚权重与履约模式开关，<br />
          直接影响调度算法的决策边界与优化目标。
        </p>
        <div class="config-grid">
          <ConfigItem icon="⚖️" title="容量约束" desc="C_truck · C_drone · E_max" />
          <ConfigItem icon="💰" title="惩罚权重" desc="超时罚金系数 vs 耗电成本系数" />
          <ConfigItem icon="🔀" title="履约模式开关" desc="单独开启/关闭 模式 C / D / E" />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import GeoToolView from '@/views/GeoTool/index.vue'
import ConfigItem  from './components/ConfigItem.vue'

const activeTab = ref<'geo' | 'orders' | 'solver'>('geo')

const tabs = [
  { id: 'geo'    as const, icon: '🗺️', label: '地图选取与环境构建', badge: 'LIVE', badgeType: 'live' },
  { id: 'orders' as const, icon: '📦', label: '动态订单生成器',     badge: '待开发', badgeType: 'dev'  },
  { id: 'solver' as const, icon: '🧠', label: '调度求解器参数',     badge: '待开发', badgeType: 'dev'  },
]
</script>

<style scoped>
/* ── 整体容器 ─────────────────────────────────────────────────────── */
.sim-box {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Tab 导航栏 ──────────────────────────────────────────────────── */
.sim-box__tabs {
  display: flex;
  align-items: stretch;
  gap: 0;
  border-bottom: 1px solid var(--hl-border);
  background: var(--hl-card-bg);
  flex-shrink: 0;
  padding: 0 var(--hl-page-padding);
}

.sim-box__tab {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 0 18px;
  height: 44px;
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  font-size: 13.5px;
  font-weight: 500;
  color: var(--hl-text-muted);
  cursor: pointer;
  transition: color var(--hl-transition), border-color var(--hl-transition);
  white-space: nowrap;
}

.sim-box__tab:hover {
  color: var(--hl-text);
}

.sim-box__tab--active {
  color: var(--hl-primary);
  border-bottom-color: var(--hl-primary);
  font-weight: 600;
}

.sim-tab__icon  { font-size: 15px; line-height: 1; }
.sim-tab__label { }

.sim-tab__badge {
  font-size: 9px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 3px;
  letter-spacing: 0.3px;
}

.sim-tab__badge--live { background: #dcfce7; color: #166534; }
.sim-tab__badge--dev  { background: #fef3c7; color: #92400e; }

/* ── Tab 内容区 ──────────────────────────────────────────────────── */
.sim-box__content {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* ── 占位内容 ─────────────────────────────────────────────────────── */
.sim-placeholder {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 48px var(--hl-page-padding);
  overflow-y: auto;
}

.sim-placeholder__icon  { font-size: 52px; line-height: 1; }
.sim-placeholder__title { font-size: 18px; font-weight: 700; color: var(--hl-text); }
.sim-placeholder__desc  {
  font-size: 13px;
  color: var(--hl-text-muted);
  text-align: center;
  line-height: 1.8;
}

.config-grid {
  display: flex;
  gap: var(--hl-space-md);
  flex-wrap: wrap;
  justify-content: center;
  margin-top: 8px;
}
</style>
