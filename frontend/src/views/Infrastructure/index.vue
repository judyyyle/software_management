<template>
  <PageShell
    icon="🏗️"
    title="基础设施配置"
    desc="仓库网点 · 充换电站网络 · 站点部署与参数配置"
    badge="开发中"
    badge-type="dev"
  >
    <div class="infra-grid">
      <!-- 充换电站列表 -->
      <div class="infra-panel">
        <div class="infra-panel__header">
          <div>
            <span class="infra-panel__title">⚡ 充换电站</span>
            <span class="infra-panel__count">0 个站点</span>
          </div>
          <button class="btn-add" disabled>+ 新增站点</button>
        </div>
        <div class="infra-table-head">
          <span style="flex:1">站点ID</span>
          <span style="flex:2">坐标</span>
          <span style="width:80px">并行容量</span>
          <span style="width:100px">补能模式</span>
          <span style="width:80px">τ_swap (s)</span>
          <span style="width:60px">操作</span>
        </div>
        <div class="infra-empty">
          <span>🔋 暂无站点配置 · 请在仿真场景中部署</span>
        </div>
      </div>

      <!-- 仓库列表 -->
      <div class="infra-panel">
        <div class="infra-panel__header">
          <div>
            <span class="infra-panel__title">🏭 仓库网点</span>
            <span class="infra-panel__count">0 个仓库</span>
          </div>
          <button class="btn-add" disabled>+ 新增仓库</button>
        </div>
        <div class="infra-table-head">
          <span style="flex:1">仓库ID</span>
          <span style="flex:2">位置坐标</span>
          <span style="width:100px">备用无人机数</span>
          <span style="width:80px">库存上限</span>
          <span style="width:60px">操作</span>
        </div>
        <div class="infra-empty">
          <span>🏭 暂无仓库配置 · 请在仿真场景中设置 Depot</span>
        </div>
      </div>
    </div>

    <!-- 配置规则说明 -->
    <div class="infra-note">
      <p class="infra-note__title">⚙️ 关于补能模式参数</p>
      <ul class="infra-note__list">
        <li><strong>换电模式</strong>：τ_swap 为固定换电耗时（秒），与剩余电量无关</li>
        <li><strong>充电模式</strong>：T_charge = α · ΔE，充电时间与缺电量成正比</li>
        <li>E_arrive 预估低于安全阈值时，调度系统自动触发 <strong>模式 E（多跳中继）</strong></li>
      </ul>
    </div>
  </PageShell>
</template>

<script setup lang="ts">
import PageShell from '@/components/PageShell/index.vue'
</script>

<style scoped>
/* ── 两列网格 ─────────────────────────────────────────────────────── */
.infra-grid {
  display: flex;
  flex-direction: column;
  gap: var(--hl-space-md);
  margin-bottom: var(--hl-space-md);
}

.infra-panel {
  background: var(--hl-card-bg);
  border: 1px solid var(--hl-card-border);
  border-radius: var(--hl-card-radius);
  box-shadow: var(--hl-card-shadow);
  overflow: hidden;
}

.infra-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--hl-border);
  background: var(--hl-content-bg);
  gap: 8px;
}

.infra-panel__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--hl-text);
  margin-right: 8px;
}

.infra-panel__count {
  font-size: 11px;
  color: var(--hl-text-muted);
}

.btn-add {
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--hl-primary);
  border-radius: var(--hl-border-radius);
  background: none;
  color: var(--hl-primary);
  font-size: 12px;
  cursor: not-allowed;
  opacity: 0.5;
}

.infra-table-head {
  display: flex;
  align-items: center;
  padding: 7px 16px;
  gap: 12px;
  border-bottom: 1px solid var(--hl-border);
  font-size: 11px;
  font-weight: 600;
  color: var(--hl-text-muted);
  text-transform: uppercase;
}

.infra-empty {
  padding: 32px 16px;
  text-align: center;
  font-size: 12px;
  color: var(--hl-text-muted);
}

/* ── 说明框 ──────────────────────────────────────────────────────── */
.infra-note {
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: var(--hl-card-radius);
  padding: 14px 18px;
}

.infra-note__title {
  font-size: 13px;
  font-weight: 600;
  color: #92400e;
  margin-bottom: 8px;
}

.infra-note__list {
  list-style: disc;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.infra-note__list li {
  font-size: 12.5px;
  color: var(--hl-text-secondary);
  line-height: 1.6;
}
</style>
