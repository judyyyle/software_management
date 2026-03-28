import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Depot, Truck, Station, Drone } from '@/types'

/** 实体状态：仓库 / 卡车 / 充换电站 / 无人机 */
export const useEntityStore = defineStore('entity', () => {
  const depots   = ref<Depot[]>([])
  const trucks   = ref<Truck[]>([])
  const stations = ref<Station[]>([])
  const drones   = ref<Drone[]>([])

  function setAll(data: { depots: Depot[]; trucks: Truck[]; stations: Station[]; drones: Drone[] }) {
    depots.value   = data.depots
    trucks.value   = data.trucks
    stations.value = data.stations
    drones.value   = data.drones
  }

  return { depots, trucks, stations, drones, setAll }
})
