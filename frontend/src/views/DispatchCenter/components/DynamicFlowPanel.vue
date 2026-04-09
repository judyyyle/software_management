<template>
  <div class="dynamic-flow">
    <!-- 卡车实时状态板 -->
    <div class="flow-section">
      <div class="flow-title">📦 卡车配送实时追踪</div>
      <div v-if="trucks.length === 0" class="flow-empty">暂无卡车数据</div>
      <div v-for="truck in trucks" :key="truck.truck_id" class="truck-card">
        <div class="truck-header">
          <span class="truck-icon">🚛</span>
          <span class="truck-name">{{ truck.name || truck.truck_id }}</span>
          <span class="truck-status" :class="`status-${truck.status.toLowerCase()}`">
            {{ getStatusLabel(truck.status) }}
          </span>
        </div>
        <div class="truck-details">
          <div class="detail-row">
            <span class="label">位置</span>
            <span class="value">{{ truck.lng.toFixed(5) }}, {{ truck.lat.toFixed(5) }}</span>
          </div>
          <div class="detail-row">
            <span class="label">货物</span>
            <span class="value">{{ truck.inventory_count }} / {{ truck.max_inventory }} 件</span>
          </div>
          <div class="detail-row">
            <span class="label">停靠无人机</span>
            <span class="value">{{ truck.docked_drones.length }} 架</span>
          </div>
          <div v-if="truck.docked_drones.length > 0" class="drone-list">
            <span v-for="did in truck.docked_drones" :key="did" class="drone-tag">🚁 {{ did }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 无人机状态板 -->
    <div class="flow-section">
      <div class="flow-title">🚁 无人机配送实时追踪</div>
      <div v-if="drones.length === 0" class="flow-empty">暂无无人机数据</div>
      <div v-for="drone in drones" :key="drone.drone_id" class="drone-card">
        <div class="drone-header">
          <span class="drone-icon">🚁</span>
          <span class="drone-name">{{ drone.name || drone.drone_id }}</span>
          <span class="drone-status" :class="`status-${drone.status.toLowerCase()}`">
            {{ getStatusLabel(drone.status) }}
          </span>
        </div>
        <div class="drone-details">
          <div class="detail-row">
            <span class="label">位置</span>
            <span class="value">{{ drone.lng?.toFixed(5) || '—' }}, {{ drone.lat?.toFixed(5) || '—' }}</span>
          </div>
          <div class="detail-row">
            <span class="label">电量</span>
            <div class="battery-bar">
              <div class="battery-fill" :style="{ width: (drone.battery_percent ?? 0) + '%' }"></div>
              <span class="battery-text">{{ (drone.battery_percent ?? 0).toFixed(0) }}%</span>
            </div>
          </div>
          <div class="detail-row">
            <span class="label">速度</span>
            <span class="value">{{ (drone.cruise_speed ?? 0).toFixed(1) }} m/s</span>
          </div>
          <div v-if="drone.payload_capacity" class="detail-row">
            <span class="label">可载重</span>
            <span class="value">{{ drone.payload_capacity }} kg</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 实时事件日志 -->
    <div class="flow-section">
      <div class="flow-title">📜 事件日志</div>
      <div class="event-log">
        <div v-if="events.length === 0" class="flow-empty">等待事件...</div>
        <div v-for="(evt, i) in events" :key="i" class="event-item">
          <span class="event-time">{{ evt.time.toFixed(2) }}s</span>
          <span class="event-icon">{{ evt.icon }}</span>
          <span class="event-msg">{{ evt.msg }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useEntityStore } from '@/stores/entity'
import { useSystemStore } from '@/stores/system'

const entityStore = useEntityStore()
const systemStore = useSystemStore()

const trucks = ref<any[]>([])
const drones = ref<any[]>([])
const events = ref<Array<{ time: number; icon: string; msg: string }>>([])

const statusLabelMap: Record<string, string> = {
  'IDLE': '空闲',
  'DRIVING': '行驶中',
  'CHARGING': '充电中',
  'RECOVERY': '回收中',
  'ASSIGNED': '已分配',
  'FLYING': '飞行中',
  'LANDING': '落地中',
  'CHARGING_WAIT': '等待充电',
}

function getStatusLabel(status: string): string {
  return statusLabelMap[status] || status
}

// 监听实体变化
watch(
  () => entityStore.trucks,
  (newTrucks) => {
    trucks.value = newTrucks || []
  },
  { deep: true }
)

watch(
  () => entityStore.drones,
  (newDrones) => {
    drones.value = newDrones || []
  },
  { deep: true }
)

// 添加事件日志
function addEvent(icon: string, msg: string) {
  const e = { time: systemStore.simTime, icon, msg }
  events.value.unshift(e)
  if (events.value.length > 20) events.value.pop()
}

defineExpose({ addEvent })
</script>

<style scoped>
.dynamic-flow {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 12px;
  height: 100%;
  overflow-y: auto;
  font-size: 0.85rem;
}

.flow-section {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px;
  background: #f8fafc;
}

.flow-title {
  font-weight: 600;
  color: #334155;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 2px solid #cbd5e1;
}

.flow-empty {
  text-align: center;
  color: #94a3b8;
  padding: 16px;
  font-style: italic;
}

/* 卡车卡片 */
.truck-card {
  background: white;
  border: 1px solid #dbeafe;
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 8px;
}

.truck-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-weight: 600;
}

.truck-icon {
  font-size: 1.2rem;
}

.truck-name {
  flex: 1;
  color: #1e40af;
}

.truck-status {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 600;
  background: #dbeafe;
  color: #1e40af;
}

.status-driving {
  background: #d1fae5 !important;
  color: #059669 !important;
}

.status-idle {
  background: #f3e8ff !important;
  color: #7c3aed !important;
}

.status-charging {
  background: #fed7aa !important;
  color: #dc2626 !important;
}

.truck-details {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 0.8rem;
}

.detail-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.label {
  font-weight: 600;
  color: #475569;
  min-width: 60px;
}

.value {
  color: #0f172a;
  font-family: 'Monaco', 'Courier New', monospace;
}

.drone-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 5px;
}

.drone-tag {
  display: inline-block;
  padding: 2px 6px;
  background: #e0e7ff;
  border-radius: 3px;
  color: #4338ca;
  font-size: 0.75rem;
}

/* 无人机卡片 */
.drone-card {
  background: white;
  border: 1px solid #e9d5ff;
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 8px;
}

.drone-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-weight: 600;
}

.drone-icon {
  font-size: 1.2rem;
}

.drone-name {
  flex: 1;
  color: #7c3aed;
}

.drone-status {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 600;
  background: #e9d5ff;
  color: #7c3aed;
}

.drone-details {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.battery-bar {
  width: 100px;
  height: 16px;
  border: 1px solid #cbd5e1;
  border-radius: 2px;
  background: #f1f5f9;
  overflow: hidden;
  position: relative;
}

.battery-fill {
  height: 100%;
  background: linear-gradient(90deg, #7c3aed, #2563eb);
  transition: width 0.2s;
}

.battery-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.65rem;
  font-weight: 600;
  color: #0f172a;
}

/* 事件日志 */
.event-log {
  max-height: 250px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.event-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-bottom: 1px solid #e2e8f0;
  font-size: 0.8rem;
}

.event-time {
  color: #94a3b8;
  font-weight: 600;
  min-width: 50px;
  font-family: 'Monaco', monospace;
}

.event-icon {
  font-size: 1rem;
  min-width: 20px;
}

.event-msg {
  flex: 1;
  color: #334155;
}
</style>
