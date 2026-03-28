<template>
  <PageShell
    icon="📦"
    title="订单与任务管理"
    desc="订单全量表 · 派送任务拆解 · 履约状态追踪"
    badge="开发中"
    badge-type="dev"
  >
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar__filters">
        <select class="filter-select" disabled>
          <option>全部状态</option>
          <option>待分配</option>
          <option>履约中</option>
          <option>已送达</option>
          <option>已超时</option>
        </select>
        <select class="filter-select" disabled>
          <option>全部模式</option>
          <option>模式 A</option>
          <option>模式 B</option>
          <option>模式 C</option>
          <option>模式 D</option>
          <option>模式 E</option>
        </select>
      </div>
      <div class="toolbar__actions">
        <button class="btn-outline" disabled>导出 CSV</button>
        <button class="btn-primary" disabled>+ 手动下单</button>
      </div>
    </div>

    <!-- 订单表格占位 -->
    <div class="table-card">
      <div class="table-head">
        <span class="table-head__cell" style="width:80px">订单ID</span>
        <span class="table-head__cell" style="width:70px">类型</span>
        <span class="table-head__cell" style="flex:1">客户坐标</span>
        <span class="table-head__cell" style="width:70px">重量 (kg)</span>
        <span class="table-head__cell" style="width:120px">时间窗截止</span>
        <span class="table-head__cell" style="width:80px">实际送达</span>
        <span class="table-head__cell" style="width:100px">状态</span>
        <span class="table-head__cell" style="width:80px">履约模式</span>
      </div>
      <div class="table-empty">
        <div class="table-empty__icon">📭</div>
        <p class="table-empty__text">暂无订单数据</p>
        <p class="table-empty__hint">请在「仿真与配置」中生成仿真场景后查看</p>
      </div>
    </div>

    <!-- 任务拆解说明 -->
    <div class="feature-hint">
      <span class="feature-hint__icon">🚀</span>
      <div>
        <p class="feature-hint__title">派送任务拆解视图（树形表格）</p>
        <p class="feature-hint__desc">
          选中订单后，展开查看无人机 / 卡车完整流转节点：起飞 → 投递 → 中继补能（模式E）→ 汇合
        </p>
      </div>
    </div>
  </PageShell>
</template>

<script setup lang="ts">
import PageShell from '@/components/PageShell/index.vue'
</script>

<style scoped>
/* ── 工具栏 ──────────────────────────────────────────────────────── */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--hl-space-md);
  gap: var(--hl-space-md);
}

.toolbar__filters { display: flex; gap: var(--hl-space-sm); }
.toolbar__actions  { display: flex; gap: var(--hl-space-sm); }

.filter-select {
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--hl-border);
  border-radius: var(--hl-border-radius);
  background: var(--hl-card-bg);
  font-size: 13px;
  color: var(--hl-text-secondary);
  cursor: not-allowed;
  opacity: 0.7;
}

.btn-outline {
  height: 32px;
  padding: 0 14px;
  border: 1px solid var(--hl-border);
  border-radius: var(--hl-border-radius);
  background: none;
  font-size: 13px;
  color: var(--hl-text-secondary);
  cursor: not-allowed;
  opacity: 0.6;
}

.btn-primary {
  height: 32px;
  padding: 0 14px;
  border: none;
  border-radius: var(--hl-border-radius);
  background: var(--hl-primary);
  color: #fff;
  font-size: 13px;
  cursor: not-allowed;
  opacity: 0.6;
}

/* ── 表格 ────────────────────────────────────────────────────────── */
.table-card {
  background: var(--hl-card-bg);
  border: 1px solid var(--hl-card-border);
  border-radius: var(--hl-card-radius);
  box-shadow: var(--hl-card-shadow);
  overflow: hidden;
  margin-bottom: var(--hl-space-md);
}

.table-head {
  display: flex;
  align-items: center;
  padding: 0 16px;
  height: 40px;
  background: var(--hl-content-bg);
  border-bottom: 1px solid var(--hl-border);
  gap: 12px;
}

.table-head__cell {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--hl-text-muted);
  letter-spacing: 0.2px;
  text-transform: uppercase;
  flex-shrink: 0;
}

.table-empty {
  padding: 48px 24px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.table-empty__icon { font-size: 36px; }
.table-empty__text { font-size: 14px; color: var(--hl-text-secondary); font-weight: 500; }
.table-empty__hint { font-size: 12px; color: var(--hl-text-muted); }

/* ── 功能说明条 ──────────────────────────────────────────────────── */
.feature-hint {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  background: var(--hl-primary-light);
  border: 1px solid #bfdbfe;
  border-radius: var(--hl-card-radius);
  padding: 14px 18px;
}

.feature-hint__icon { font-size: 20px; flex-shrink: 0; margin-top: 1px; }
.feature-hint__title { font-size: 13px; font-weight: 600; color: var(--hl-primary-dark); }
.feature-hint__desc  { font-size: 12px; color: var(--hl-text-secondary); margin-top: 4px; line-height: 1.6; }
</style>
