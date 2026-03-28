<template>
  <PageShell
    icon="🗺️"
    title="实时指挥中心"
    desc="多维 GIS 地图可视化 · 实时调度干预 · 实体状态监控"
    badge="开发中"
    badge-type="dev"
  >
    <div class="dispatch-layout">
      <!-- 左侧状态面板 -->
      <aside class="dispatch-aside">
        <SectionCard title="动态订单队列" icon="📋">
          <div class="empty-list">
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
        <div class="map-placeholder">
          <div class="map-placeholder__icon">🌐</div>
          <p class="map-placeholder__title">Mars3D / Leaflet 调度地图</p>
          <p class="map-placeholder__desc">
            将展示：静态设施层 · 地面动态层（卡车）·<br />
            空中动态层（无人机）· 任务热力层 · 能量拓扑层
          </p>
          <div class="layer-tags">
            <span class="layer-tag" style="--c:#2563eb">🏭 设施层</span>
            <span class="layer-tag" style="--c:#16a34a">🚚 地面层</span>
            <span class="layer-tag" style="--c:#7c3aed">🛸 空中层</span>
            <span class="layer-tag" style="--c:#d97706">📦 热力层</span>
            <span class="layer-tag" style="--c:#0891b2">⚡ 能量层</span>
          </div>
        </div>
      </div>

      <!-- 右侧实体详情 -->
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
      </aside>
    </div>
  </PageShell>
</template>

<script setup lang="ts">
import PageShell  from '@/components/PageShell/index.vue'
import SectionCard from './components/SectionCard.vue'
</script>

<style scoped>
.dispatch-layout {
  display: flex;
  gap: var(--hl-space-md);
  height: 100%;
}

/* 侧边栏 */
.dispatch-aside {
  width: 240px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: var(--hl-space-md);
}

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
}

.map-placeholder {
  text-align: center;
  padding: 40px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.map-placeholder__icon { font-size: 48px; }

.map-placeholder__title {
  font-size: 16px;
  font-weight: 600;
  color: var(--hl-text);
}

.map-placeholder__desc {
  font-size: 12.5px;
  color: var(--hl-text-muted);
  line-height: 1.7;
}

.layer-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  margin-top: 4px;
}

.layer-tag {
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--c, var(--hl-primary));
  color: var(--c, var(--hl-primary));
  background: color-mix(in srgb, var(--c, var(--hl-primary)) 8%, white);
}

/* 右侧详情 */
.dispatch-detail {
  width: 220px;
  flex-shrink: 0;
}

.detail-hint {
  font-size: 11.5px;
  color: var(--hl-text-muted);
  margin-bottom: 10px;
}

.detail-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 12px;
}

.detail-tab {
  flex: 1;
  height: 28px;
  border: 1px solid var(--hl-border);
  border-radius: 4px;
  background: none;
  font-size: 12px;
  cursor: pointer;
  color: var(--hl-text-secondary);
  transition: all var(--hl-transition);
}

.detail-tab--active {
  background: var(--hl-primary-alpha);
  border-color: var(--hl-primary);
  color: var(--hl-primary);
  font-weight: 600;
}

.empty-list {
  font-size: 12px;
  color: var(--hl-text-muted);
  padding: 12px 0;
  text-align: center;
}

.alert-badge--ok {
  font-size: 12px;
  color: var(--hl-success);
}
</style>
