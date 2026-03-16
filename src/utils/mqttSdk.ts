import { useMqttStore } from '/@/stores/mqtt'

/**
 * 获取 MqttStore 实例
 * 延迟获取以确保 Pinia 已初始化
 */
const getMqttStore = () => useMqttStore()

function generateUUID() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        const r = (Math.random() * 16) | 0
        const v = c === 'x' ? r : (r & 0x3) | 0x8
        return v.toString(16)
    })
}

export const DJIoperations = {
    // 打开服务
    sendServices: (sn: string, method: string, data: any = null, customBid?: string) => {
        return getMqttStore().publish(`thing/product/${sn}/services`, JSON.stringify({ method, data, timestamp: Date.now(), tid: generateUUID(), bid: customBid || generateUUID() }))
    },
    // 服务进度
    eventsReply: (sn: string, method: string, data: any = null, customBid?: string) => {
        return getMqttStore().publish(`thing/product/${sn}/events_reply`, JSON.stringify({ method, data, timestamp: Date.now(), tid: generateUUID(), bid: customBid || generateUUID() }))
    },
    // 订阅设备拓扑更新
    statusUpdate: (sn: string) => {
        return getMqttStore().subscribe(`sys/product/${sn}/status`)
    },
    // 关闭订阅设备拓扑更新
    statusUpdateClose: (sn: string) => {
        return getMqttStore().unsubscribe(`sys/product/${sn}/status`)
    },
    // 获取设备拓扑消息
    statusMessage: (sn: string) => {
        return getMqttStore().getMessagesByTopic(`sys/product/${sn}/status`)
    },
    // 订阅设备属性
    osd: (sn: string) => {
        return getMqttStore().subscribe(`thing/product/${sn}/osd`)
    },
    // 关闭订阅设备属性
    osdClose: (sn: string) => {
        return getMqttStore().unsubscribe(`thing/product/${sn}/osd`)
    },
    // 订阅设备状态
    deviceStatus: (sn: string) => {
        return getMqttStore().subscribe(`thing/product/${sn}/state`)
    },
    // 关闭订阅设备状态
    deviceStatusClose: (sn: string) => {
        return getMqttStore().unsubscribe(`thing/product/${sn}/state`)
    },
    // 订阅设备服务回复
    deviceServicesReply: (sn: string) => {
        return getMqttStore().subscribe(`thing/product/${sn}/services_reply`)
    },
    // 关闭订阅设备服务回复
    deviceServicesReplyClose: (sn: string) => {
        return getMqttStore().unsubscribe(`thing/product/${sn}/services_reply`)
    },
    // 订阅进度回复
    deviceEvents: (sn: string) => {
        return getMqttStore().subscribe(`thing/product/${sn}/events`)
    },
    // 关闭订阅进度回复
    deviceEventsClose: (sn: string) => {
        return getMqttStore().unsubscribe(`thing/product/${sn}/events`)
    },
    // 终止任务
    terminateMission: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'flighttask_stop', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 一键返航
    returnHome: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'return_home', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 取消返航
    cancelReturnHome: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'return_home_cancel', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 航线暂停
    pauseMission: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'flighttask_pause', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 航线恢复
    resumeMission: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'flighttask_recovery', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 取消任务
    flighttaskUndo: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'flighttask_undo', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 进入指令飞行
    drcModeEnter: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'drc_mode_enter', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 飞行控制权抢夺
    flighAuthorityGrab: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'flight_authority_grab', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 负载控制权抢夺
    payloadAuthorityGrab: (sn: string, data: any = {}) => {
        return getMqttStore().publish(
            `thing/product/${sn}/services`,
            JSON.stringify({ method: 'payload_authority_grab', data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() })
        )
    },
    // 设备属性设置
    deviceAttributes: (sn: string, data: any = {}) => {
        return getMqttStore().publish(`thing/product/${sn}/property/set`, JSON.stringify({ data, timestamp: Date.now(), tid: generateUUID(), bid: generateUUID() }))
    },
}
