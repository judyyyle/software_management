<template>
  <PageShell
    icon="🛸"
    title="载具管理调度"
    desc="卡车序列 · 无人机队列 · 实时状态 · 分配策略"
    badge="开发中"
    badge-type="dev"
  >
    <!-- 统计摘要 -->
    <div class="fleet-summary">
      <div v-for="stat in stats" :key="stat.label" class="stat-pill">
        <span class="stat-pill__icon">{{ stat.icon }}</span>
        <span class="stat-pill__value">{{ stat.value }}</span>
        <span class="stat-pill__label">{{ stat.label }}</span>
      </div>
    </div>

    <!-- 两列：卡车 + 无人机 -->
    <div class="fleet-grid">
      <!-- 卡车列表 -->
      <div class="fleet-panel">
        <div class="fleet-panel__header">
          <span>🚚 卡车序列</span>
          <span class="fleet-panel__count">0 辆</span>
        </div>
        <div class="fleet-panel__cols">
          <span>编号</span><span>当前位置</span><span>载货量</span><span>搭载无人机</span><span>路径策略</span>
        </div>
        <div class="fleet-panel__empty">暂无载具数据 · 等待仿真场景接入</div>
      </div>

      <!-- 无人机列表 -->
      <div class="fleet-panel">
        <div class="fleet-panel__header">
          <span>🛸 无人机序列</span>
          <span class="fleet-panel__count">0 架</span>
        </div>
        <div class="fleet-panel__cols">
          <span>编号</span><span>当前电量</span><span>载重状态</span><span>履约模式</span><span>绑定母港</span>
        </div>
        <div class="fleet-panel__empty">暂无无人机数据 · 等待仿真场景接入</div>
      </div>
    </div>
  </PageShell>
</template>

<script setup lang="ts">
import PageShell from '@/components/PageShell/index.vue'

const stats = [
  { icon: '🚚', value: '0', label: '卡车总数'    },
  { icon: '🛸', value: '0', label: '无人机总数'  },
  { icon: '⚡', value: '0', label: '空闲无人机'  },
  { icon: '🔄', value: '0', label: '执行任务中'  },
  { icon: '🔋', value: '—', label: '平均电量'    },
]
</script>

<style scoped>
/* ── 摘要胶囊 ─────────────────────────────────────────────────────── */
.fleet-summary {
  display: flex;
  gap: var(--hl-space-md);
  flex-wrap: wrap;
  margin-bottom: var(--hl-space-lg);
}

.stat-pill {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--hl-card-bg);
  border: 1px solid var(--hl-card-border);
  border-radius: 999px;
  padding: 8px 18px;
  box-shadow: var(--hl-shadow-sm);
}

.stat-pill__icon  { font-size: 16px; }
.stat-pill__value { font-size: 18px; font-weight: 700; color: var(--hl-text); }
.stat-pill__label { font-size: 12px; color: var(--hl-text-muted); }

/* ── 两列网格 ─────────────────────────────────────────────────────── */
.fleet-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--hl-space-md);
}

.fleet-panel {
  background: var(--hl-card-bg);
  border: 1px solid var(--hl-card-border);
  border-radius: var(--hl-card-radius);
  box-shadow: var(--hl-card-shadow);
  overflow: hidden;
}

.fleet-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--hl-border);
  font-size: 13px;
  font-weight: 600;
  background: var(--hl-content-bg);
}

.fleet-panel__count {
  font-size: 11px;
  color: var(--hl-text-muted);
  font-weight: 400;
}

.fleet-panel__cols {
  display: flex;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--hl-border);
  font-size: 11px;
  color: var(--hl-text-muted);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.2px;
}

.fleet-panel__cols span { flex: 1; }

.fleet-panel__empty {
  padding: 36px 16px;
  text-align: center;
  font-size: 12px;
  color: var(--hl-text-muted);
}
</style>
