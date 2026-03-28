import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Order } from '@/types'

/** 订单状态 */
export const useOrderStore = defineStore('order', () => {
  const orders   = ref<Order[]>([])
  const pending  = ref<Order[]>([])
  const finished = ref<Order[]>([])

  function addOrder(order: Order) {
    orders.value.push(order)
    pending.value.push(order)
  }

  function completeOrder(id: string) {
    const idx = pending.value.findIndex(o => o.id === id)
    if (idx !== -1) {
      finished.value.push(...pending.value.splice(idx, 1))
    }
  }

  return { orders, pending, finished, addOrder, completeOrder }
})
