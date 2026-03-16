/**
 * MQTT Store - 状态层
 * 负责应用状态管理、数据缓存、业务逻辑
 * Requirements: 7.1, 7.2, 7.3, 7.4, 7.5
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getMqttService, MqttService } from '/@/utils/mqtt/MqttService'
import { ConnectionState, ConnectionStatusType, ConnectionError, DeviceOsd, DroneOsd, TopicPatterns } from '/@/utils/mqtt/types'
import { baTableApi } from '/@/api/common'
import { ElNotification } from 'element-plus'
import { useRouter } from 'vue-router'
import { disposition } from '/@/config/disposition'
import { useShipLanes } from './shipLanes'

// 错误历史最大长度
const MAX_ERROR_HISTORY = 100

export const useMqttStore = defineStore(
    'mqtt',
    () => {
        const router = useRouter()

        // ==================== 连接状态 (Requirements 7.1, 7.2, 7.3, 7.4, 7.5) ====================

        // 连接状态
        const connectionStatus = ref<ConnectionStatusType>('disconnected')
        // 当前错误
        const error = ref<string | null>(null)
        // 错误历史 (Requirements 7.5: 限制 100 条)
        const errorHistory = ref<ConnectionError[]>([])
        // 最后连接时间
        const lastConnectedAt = ref<number | undefined>(undefined)
        // 最后断开时间
        const lastDisconnectedAt = ref<number | undefined>(undefined)
        // 重连次数
        const reconnectAttempts = ref<number>(0)

        // ==================== 设备状态 ====================

        // 消息缓存 - 改为按 Topic 存储的 Map 结构，防止不同 Topic 消息互相覆盖
        const messages = ref<Record<string, any>>({})
        // 已订阅主题
        const subscribedTopics = ref<string[]>([])
        // 当前设备 SN（默认值与老版本保持一致）
        const gateway_sn = ref<any>('')
        // 设备 OSD 数据
        const deviceOsds = ref<Record<string, DeviceOsd>>({})
        // 无人机 OSD 数据
        const droneOsds = ref<Record<string, DroneOsd>>({})
        // 所有设备列表
        const allDevice = ref<any[]>([])
        // 是否有正在执行的任务
        const isExecutingTask = ref(false)

        // ==================== DRC 状态 (Requirements 6.1, 6.2, 6.3, 6.4, 6.5) ====================

        // 是否处于 DRC 模式
        const isDrcMode = ref(false)
        // DRC 心跳定时器
        let drcHeartbeatTimer: ReturnType<typeof setInterval> | null = null
        // 定时器间隔（单位：毫秒）
        const drcInterval = 50 // 50秒
        // DRC 心跳序列号
        let drcSeq = 0

        // ==================== MqttService 实例 ====================

        let mqttService: MqttService | null = null

        /**
         * 获取 MqttService 实例
         */
        const getService = (): MqttService => {
            if (!mqttService) {
                mqttService = getMqttService()
            }
            return mqttService
        }

        // ==================== 计算属性 ====================

        // 当前无人机 SN - 与老版本保持一致
        const drone_sn = computed(() => {
            if (gateway_sn.value && deviceOsds.value[gateway_sn.value]?.sub_device) {
                return deviceOsds.value[gateway_sn.value].sub_device!.device_sn
            }
            return ''
        })

        // 当前无人机状态
        const droneData = computed(() => {
            return droneOsds.value[drone_sn.value] || {}
        })

        // 当前无人机枚举值
        const droneEnum = computed(() => {
            return droneData.value.cameras?.[0]?.payload_index || ''
        })

        // 当前相机模式
        const cameraMode = computed(() => {
            return droneData.value.cameras?.[0]?.camera_mode || 0
        })

        // 当前设备状态
        const deviceData = computed(() => {
            return deviceOsds.value[gateway_sn.value] || {}
        })

        // 是否已连接
        const isConnected = computed(() => connectionStatus.value === 'connected')

        // 是否有错误
        const hasError = computed(() => !!error.value)

        // 设备数量
        const deviceCount = computed(() => {
            return Object.keys(deviceOsds.value).length
        })

        // 当前正在执行任务的设备
        const executingDeviceSn = computed(() => {
            return Object.keys(deviceOsds.value).filter((sn) => deviceOsds.value[sn].mode_code !== 0 && deviceOsds.value[sn].mode_code !== 100)
        })

        // 执行中设备数量
        const executingDeviceCount = computed(() => {
            return executingDeviceSn.value.length
        })

        // 空闲中设备数量
        const idleDeviceCount = computed(() => {
            return Object.keys(deviceOsds.value).filter((sn) => deviceOsds.value[sn].mode_code === 0).length
        })

        // 飞行器在线数量
        const droneCount = computed(() => {
            return Object.keys(deviceOsds.value).filter((sn) => {
                if (deviceOsds.value[sn].sub_device) {
                    return deviceOsds.value[sn].sub_device!.device_sn
                }
                return false
            }).length
        })

        // 飞行器空闲数量
        const idleDroneCount = computed(() => {
            return Object.keys(deviceOsds.value).filter((sn) => deviceOsds.value[sn].drone_in_dock === 1).length
        })

        // 当前正在执行任务的飞行器
        const executingDroneSn = computed(() => {
            return Object.keys(deviceOsds.value).filter((sn) => deviceOsds.value[sn].drone_in_dock === 0)
        })

        // 飞行器执行中数量
        const executingDroneCount = computed(() => {
            return executingDroneSn.value.length
        })

        // ==================== 连接状态管理 (Requirements 7.1, 7.2, 7.3, 7.4) ====================

        /**
         * 更新连接状态
         * Requirements 7.1: 更新 connectionStatus 状态
         */
        const updateConnectionState = (state: ConnectionState) => {
            connectionStatus.value = state.status
            reconnectAttempts.value = state.reconnectAttempts

            if (state.lastConnectedAt) {
                lastConnectedAt.value = state.lastConnectedAt
            }
            if (state.lastDisconnectedAt) {
                lastDisconnectedAt.value = state.lastDisconnectedAt
            }

            // Requirements 7.2: 记录错误信息
            if (state.error) {
                addErrorToHistory(state.error)
                error.value = state.error.message
            }

            // Requirements 7.4: 连接恢复正常时清除错误状态
            if (state.status === 'connected') {
                error.value = null
            }
        }

        /**
         * 添加错误到历史记录
         * Requirements 7.2, 7.5: 记录错误信息，限制 100 条
         */
        const addErrorToHistory = (err: ConnectionError) => {
            errorHistory.value.push(err)
            // Requirements 7.5: 超过 100 条时移除最早的记录
            while (errorHistory.value.length > MAX_ERROR_HISTORY) {
                errorHistory.value.shift()
            }
        }

        /**
         * 获取连接状态信息
         * Requirements 7.3: 返回当前状态、最后连接时间和错误历史
         */
        const getConnectionInfo = () => {
            return {
                status: connectionStatus.value,
                lastConnectedAt: lastConnectedAt.value,
                lastDisconnectedAt: lastDisconnectedAt.value,
                reconnectAttempts: reconnectAttempts.value,
                error: error.value,
                errorHistory: errorHistory.value,
            }
        }

        // ==================== 连接管理 ====================

        /**
         * 连接 MQTT
         */
        const connect = async (): Promise<boolean> => {
            error.value = null
            connectionStatus.value = 'connecting'

            try {
                const service = getService()
                const success = await service.connect()
                if (!success) {
                    error.value = '连接失败'
                    connectionStatus.value = 'error'
                } else {
                    connectionStatus.value = 'connected'
                    lastConnectedAt.value = Date.now()
                }
                return success
            } catch (err) {
                error.value = err instanceof Error ? err.message : '连接错误'
                connectionStatus.value = 'error'
                addErrorToHistory({
                    type: 'unknown',
                    message: error.value,
                    timestamp: Date.now(),
                })
                return false
            }
        }

        /**
         * 断开连接
         */
        const disconnect = () => {
            const service = getService()
            service.disconnect()
            connectionStatus.value = 'disconnected'
            lastDisconnectedAt.value = Date.now()
            subscribedTopics.value = []
            gateway_sn.value = ''
            deviceOsds.value = {}
        }

        // ==================== 订阅管理 ====================

        /**
         * 订阅主题
         * Requirements 3.1: 通过统一接口进行订阅
         */
        const subscribe = async (topic: string, qos: number = 0, source: string = 'store'): Promise<boolean> => {
            if (!isConnected.value) {
                error.value = '未连接到MQTT服务器'
                console.warn('⚠️ 订阅失败：未连接到MQTT服务器', topic)
                return false
            }

            const service = getService()
            const success = await service.subscribe(topic, qos, source)
            if (success) {
                if (!subscribedTopics.value.includes(topic)) {
                    subscribedTopics.value.push(topic)
                }
                console.log('✅ 订阅成功:', topic, `(来源: ${source})`)
            } else {
                error.value = `订阅主题失败: ${topic}`
                console.error('❌ 订阅失败:', topic)
            }
            return success
        }

        /**
         * 取消订阅
         */
        const unsubscribe = async (topic: string): Promise<boolean> => {
            if (!isConnected.value) {
                error.value = '未连接到MQTT服务器'
                return false
            }

            const service = getService()
            const success = await service.unsubscribe(topic)
            if (success) {
                const index = subscribedTopics.value.indexOf(topic)
                if (index > -1) {
                    subscribedTopics.value.splice(index, 1)
                }
            } else {
                error.value = `取消订阅失败: ${topic}`
            }
            return success
        }

        /**
         * 发布消息
         */
        const publish = async (topic: string, message: string | Buffer | object, qos: number = 0): Promise<boolean> => {
            if (!isConnected.value) {
                error.value = '未连接到MQTT服务器'
                return false
            }
            console.log('[MQTT] 发布消息:', topic)
            const service = getService()
            const success = await service.publish(topic, message, qos)
            if (!success) {
                error.value = `发布消息失败: ${topic}`
            }
            return success
        }

        // ==================== 消息处理 ====================

        /**
         * 处理 OSD 消息 - 简化版本
         * 直接根据 SN 更新对应的数据存储
         */
        const handleOsdMessage = (topic: string, payload: any) => {
            const sn = topic.split('/')[2]
            if (!sn) return

            // console.log('[OSD] 收到消息:', topic)

            // 解析消息数据
            let data: any
            try {
                if (typeof payload === 'string') {
                    const parsed = JSON.parse(payload)
                    data = parsed.data || parsed
                } else if (payload && typeof payload === 'object') {
                    data = payload.data || payload
                }
            } catch {
                console.error('[OSD] 解析失败:', topic)
                return
            }
            if (!data || typeof data !== 'object') {
                console.warn('[OSD] 数据为空:', topic)
                return
            }

            // 机场 OSD：SN 在 deviceOsds 中
            if (sn in deviceOsds.value) {
                deviceOsds.value[sn] = { ...deviceOsds.value[sn], ...data }
                return
            }

            // 飞行器 OSD：直接更新到 droneOsds
            if (!droneOsds.value[sn]) {
                droneOsds.value[sn] = {}
            }
            droneOsds.value[sn] = { ...droneOsds.value[sn], ...data }
        }

        /**
         * 处理 DRC 消息
         * Requirements 6.4: 合并 DRC OSD 数据到无人机状态
         */
        const handleDrcMessage = (topic: string, payload: any) => {
            try {
                const parsed = typeof payload === 'string' ? JSON.parse(payload) : payload
                if (parsed.method === 'osd_info_push' && parsed.data) {
                    const droneSn = drone_sn.value
                    if (droneSn && droneOsds.value[droneSn]) {
                        const drcData = parsed.data
                        // Requirements 6.4: 合并 DRC OSD 数据
                        droneOsds.value[droneSn] = {
                            ...droneOsds.value[droneSn],
                            latitude: drcData.latitude,
                            longitude: drcData.longitude,
                            height: drcData.height,
                            attitude_head: drcData.attitude_head,
                            horizontal_speed: Math.sqrt((drcData.speed_x || 0) ** 2 + (drcData.speed_y || 0) ** 2),
                            vertical_speed: drcData.speed_z || 0,
                            gimbal_pitch: drcData.gimbal_pitch,
                            gimbal_roll: drcData.gimbal_roll,
                            gimbal_yaw: drcData.gimbal_yaw,
                        }
                    }
                }
            } catch (e) {
                // 忽略解析错误
            }
        }

        /**
         * 统一消息处理器 - 与老版本保持一致，但改为按 Topic 存储
         */
        const handleMessage = (topic: string, payload: any) => {
            // 调试：记录所有收到的消息

            if (topic.includes('osd') || topic.includes('state')) {
                handleOsdMessage(topic, payload)
            } else if (topic.includes('/drc/up')) {
                handleDrcMessage(topic, payload)
            } else {
                // 存储其他消息（services_reply, events 等）- 使用 topic 作为 key
                messages.value[topic] = { topic, payload, _timestamp: Date.now() }
            }
        }

        // ==================== 设备管理 ====================

        /**
         * 获取所有设备
         */
        const getDeviceList = async () => {
            const equipmentApi = new baTableApi('/admin/Equipment/')
            const res = await equipmentApi.index()
            console.log(res.data.list)
            allDevice.value = res.data.list
            res.data.list.forEach((item: any) => {
                deviceOsds.value[item.sn] = {}
            })
            // 订阅所有设备的 OSD
            subscribeDeviceOsd()
        }

        /**
         * 获取无人机 OSD
         */
        const getDroneOsd = async () => {
            droneOsds.value = {}
            for (const sn in deviceOsds.value) {
                if (deviceOsds.value[sn].sub_device) {
                    droneOsds.value[deviceOsds.value[sn].sub_device!.device_sn] = {}
                    subscribe(TopicPatterns.DEVICE_OSD(deviceOsds.value[sn].sub_device!.device_sn), 0, 'drone-osd')
                }
            }
        }

        /**
         * 检查并订阅当前选中设备的无人机 OSD
         */
        const checkAndSubscribeDroneOsd = () => {
            if (gateway_sn.value && deviceOsds.value[gateway_sn.value]?.sub_device) {
                const droneSn = deviceOsds.value[gateway_sn.value].sub_device!.device_sn
                if (droneSn && !droneOsds.value[droneSn]) {
                    droneOsds.value[droneSn] = {}
                    subscribe(TopicPatterns.DEVICE_OSD(droneSn), 0, 'check-drone')
                    console.log('已订阅当前无人机 OSD:', droneSn)
                }
            }
        }

        /**
         * 强制订阅指定设备的无人机 OSD（用于驾驶舱页面）
         */
        const subscribeDroneOsdBySn = async (gatewaySn: string) => {
            if (!gatewaySn) return null

            // 确保 deviceOsds 中有该设备的条目
            if (!deviceOsds.value[gatewaySn]) {
                deviceOsds.value[gatewaySn] = {}
                console.log('📦 初始化 deviceOsds:', gatewaySn)
            }

            // 确保设备 OSD 已订阅
            if (!subscribedTopics.value.includes(TopicPatterns.DEVICE_OSD(gatewaySn))) {
                await subscribe(TopicPatterns.DEVICE_OSD(gatewaySn), 0, 'cockpit')
                console.log('📡 已订阅设备 OSD:', gatewaySn)
            }

            // 等待设备数据，最多等待 10 秒
            let retries = 0
            while (retries < 20) {
                if (deviceOsds.value[gatewaySn]?.sub_device?.device_sn) {
                    const droneSn = deviceOsds.value[gatewaySn].sub_device!.device_sn
                    // 初始化并订阅无人机 OSD
                    if (!droneOsds.value[droneSn]) {
                        droneOsds.value[droneSn] = {}
                        console.log('📦 初始化 droneOsds:', droneSn)
                    }
                    if (!subscribedTopics.value.includes(TopicPatterns.DEVICE_OSD(droneSn))) {
                        await subscribe(TopicPatterns.DEVICE_OSD(droneSn), 0, 'cockpit-drone')
                        console.log('✅ 已订阅无人机 OSD:', droneSn)
                    }
                    return droneSn
                }
                await new Promise((resolve) => setTimeout(resolve, 500))
                retries++
            }
            console.warn('⚠️ 等待无人机数据超时，设备可能未开机或无人机未连接')
            return null
        }

        /**
         * 订阅所有设备的 OSD
         */
        const subscribeDeviceOsd = async () => {
            for (const sn in deviceOsds.value) {
                subscribe(TopicPatterns.DEVICE_OSD(sn), 0, 'device-osd')
            }
            setTimeout(() => {
                getDroneOsd()
            }, 3000)
        }

        /**
         * 取消订阅所有设备的 OSD
         */
        const unsubscribeDeviceOsd = async () => {
            for (const sn in deviceOsds.value) {
                unsubscribe(TopicPatterns.DEVICE_OSD(sn))
                unsubscribe(TopicPatterns.DEVICE_STATE(sn))
                if (deviceOsds.value[sn].sub_device) {
                    const droneSn = deviceOsds.value[sn].sub_device!.device_sn
                    unsubscribe(TopicPatterns.DEVICE_OSD(droneSn))
                    unsubscribe(TopicPatterns.DEVICE_STATE(droneSn))
                }
            }
        }

        // ==================== DRC 模式管理 (Requirements 6.1, 6.2, 6.3, 6.5) ====================

        /**
         * 进入 DRC 模式
         * Requirements 6.1: 订阅 DRC 上行主题
         */
        const enterDrcMode = async (): Promise<boolean> => {
            if (isDrcMode.value) {
                console.log('⚠️ 已经处于 DRC 模式')
                return true
            }

            const droneSn = drone_sn.value
            if (!droneSn) {
                console.error('❌ 无法进入 DRC 模式：无人机 SN 不存在')
                return false
            }

            try {
                // Requirements 6.1: 订阅 DRC 上行主题
                const drcUpTopic = TopicPatterns.DRC_UP(droneSn)
                await subscribe(drcUpTopic, 0, 'drc')
                console.log('📡 已订阅 DRC 上行主题:', drcUpTopic)

                isDrcMode.value = true
                drcSeq = 0

                // Requirements 6.2: 启动 DRC 心跳
                startDrcHeartbeat()

                console.log('✅ 已进入 DRC 模式')
                return true
            } catch (err) {
                console.error('❌ 进入 DRC 模式失败:', err)
                return false
            }
        }

        /**
         * 退出 DRC 模式
         * Requirements 6.5: 取消 DRC 相关主题订阅并停止心跳
         */
        const exitDrcMode = async (): Promise<void> => {
            if (!isDrcMode.value) {
                return
            }

            const droneSn = drone_sn.value

            // 停止心跳
            stopDrcHeartbeat()

            // Requirements 6.5: 取消 DRC 相关主题订阅
            if (droneSn) {
                const drcUpTopic = TopicPatterns.DRC_UP(droneSn)
                const drcDownTopic = TopicPatterns.DRC_DOWN(droneSn)
                await unsubscribe(drcUpTopic)
                await unsubscribe(drcDownTopic)
                console.log('📡 已取消 DRC 主题订阅')
            }

            isDrcMode.value = false
            drcSeq = 0
            console.log('✅ 已退出 DRC 模式')
        }

        /**
         * 启动 DRC 心跳
         * Requirements 6.2: 每 5 秒发送一次 DRC 心跳消息
         */
        const startDrcHeartbeat = () => {
            stopDrcHeartbeat() // 确保没有重复的定时器
            if (!gateway_sn.value) return
            const drcDownTopic = TopicPatterns.DRC_DOWN(gateway_sn.value)
            drcHeartbeatTimer = setInterval(async () => {
                if (!isDrcMode.value) {
                    stopDrcHeartbeat()
                    return
                }

                const heartbeat = {
                    method: 'heart_beat',
                    data: {
                        timestamp: Date.now(),
                    },
                }

                const success = await publish(drcDownTopic, heartbeat, 0)
                if (!success) {
                    console.warn('⚠️ DRC 心跳发送失败')
                }
            }, drcInterval * 1000) // 每 50 秒发送一次

            console.log('💓 DRC 心跳已启动')
        }

        /**
         * 停止 DRC 心跳
         */
        const stopDrcHeartbeat = () => {
            if (drcHeartbeatTimer) {
                clearInterval(drcHeartbeatTimer)
                drcHeartbeatTimer = null
                console.log('💔 DRC 心跳已停止')
            }
        }

        /**
         * 发送 DRC 控制命令
         */
        const sendDrcCommand = async (method: string, data: any): Promise<boolean> => {
            if (!isDrcMode.value) {
                console.error('❌ 未处于 DRC 模式，无法发送控制命令')
                return false
            }

            const droneSn = drone_sn.value
            if (!droneSn) {
                console.error('❌ 无人机 SN 不存在')
                return false
            }

            const drcDownTopic = TopicPatterns.DRC_DOWN(droneSn)
            const command = {
                method,
                data,
                seq: ++drcSeq,
            }

            return publish(drcDownTopic, command, 0)
        }

        // ==================== 任务管理 ====================

        const resetIsExecutingTask = () => {
            isExecutingTask.value = false
        }

        /**
         * 获取任务计划
         */
        const getTaskPlan = async () => {
            const taskPlanApi = new baTableApi('/admin/Flighttask/')
            const res = await taskPlanApi.index()
            const executingTask = res.data.list.filter((item: any) => item.status === 'in_progress')
            console.log('executingTask', executingTask)
            if (executingTask.length == 0) {
                return
            }
            if (executingTask.length > 0) {
                const shipLanesStore = useShipLanes()
                ElNotification({
                    title: '提示',
                    message: '有执行中的任务，为您跳转过去',
                    duration: 3000,
                    type: 'warning',
                })
                const executingDevice = allDevice.value.find((item: any) => item.id === executingTask[0].equipment_id)
                const airlineApi = new baTableApi('/admin/Airline/')
                const res = await airlineApi.edit({ id: executingTask[0].airline_id })
                const airlineRes = res.data.row
                const kmzJson = JSON.parse(airlineRes.kmz_json)
                shipLanesStore.showShipForm(kmzJson)
                const executingDeviceSn = executingDevice.sn
                gateway_sn.value = executingDeviceSn
                isExecutingTask.value = true
                setTimeout(() => {
                    console.log('isExecutingTask', isExecutingTask.value)
                    disposition.djiDock.gateway_sn = executingDeviceSn
                    disposition.device.device_sn = drone_sn.value
                    router.push(`/admin/flightSpace?id=${executingDevice.project_id}&activeTab=2`)
                }, 2000)
            }
        }

        // ==================== 工具方法 ====================

        /**
         * 获取 MQTT 配置
         * 用于 DRC 模式等需要访问 MQTT 认证信息的场景
         */
        const getMqttConfig = () => {
            const service = getService()
            return service.getConfig()
        }

        /**
         * 清空消息
         */
        const clearMessages = () => {
            messages.value = {}
        }

        /**
         * 清空错误
         */
        const clearError = () => {
            error.value = null
        }

        /**
         * 获取特定主题的消息
         */
        const getMessagesByTopic = (topic: string) => {
            return messages.value[topic] || {}
        }

        // ==================== 初始化 ====================

        /**
         * 初始化 MQTT 连接
         */
        const initMqtt = async () => {
            isExecutingTask.value = false
            console.log('🚀 初始化MQTT...')

            // 清理运行时状态
            subscribedTopics.value = []
            deviceOsds.value = {}
            droneOsds.value = {}
            error.value = null
            messages.value = {}
            connectionStatus.value = 'disconnected'

            try {
                const service = getService()

                // 完全清理所有组件状态
                service.getTopicManager().clear()
                service.getMessageRouter().clear()
                service.getConnection().clearSubscribedTopics()

                // 监听连接状态变化
                service.onConnectionChange(updateConnectionState)

                // 设置消息处理器（使用唯一 ID 避免重复注册）
                service.getMessageRouter().unregister('main-handler')
                service.onMessage('#', handleMessage, 'main-handler')

                // 自动连接
                const success = await connect()
                if (success) {
                    console.log('✅ MQTT初始化成功')
                } else {
                    console.error('❌ MQTT初始化失败')
                }
            } catch (err) {
                console.error('💥 MQTT初始化异常:', err)
            }
        }

        return {
            // 连接状态
            connectionStatus,
            error,
            errorHistory,
            lastConnectedAt,
            lastDisconnectedAt,
            reconnectAttempts,

            // 设备状态
            messages,
            subscribedTopics,
            gateway_sn,
            allDevice,
            deviceOsds,
            droneOsds,

            // DRC 状态
            isDrcMode,

            // DRC 模式管理
            enterDrcMode,
            exitDrcMode,
            sendDrcCommand,

            // 计算属性
            isConnected,
            hasError,
            deviceData,
            deviceCount,
            executingDeviceCount,
            idleDeviceCount,
            executingDeviceSn,
            droneCount,
            drone_sn,
            droneData,
            droneEnum,
            cameraMode,
            idleDroneCount,
            executingDroneCount,
            executingDroneSn,

            // 连接管理
            connect,
            disconnect,
            getConnectionInfo,

            // 订阅管理
            subscribe,
            unsubscribe,
            publish,

            // 设备管理
            getDeviceList,
            getDroneOsd,
            checkAndSubscribeDroneOsd,
            subscribeDroneOsdBySn,
            subscribeDeviceOsd,
            unsubscribeDeviceOsd,

            // 任务管理
            getTaskPlan,
            isExecutingTask,
            resetIsExecutingTask,

            // 工具方法
            clearMessages,
            clearError,
            getMessagesByTopic,
            getMqttConfig,
            initMqtt,
        }
    },
    {
        persist: {
            key: 'mqtt-store',
            storage: localStorage,
            // 只持久化必要的配置，不持久化运行时状态
            pick: ['gateway_sn', 'allDevice', 'deviceOsds', 'droneOsds'],
        },
    }
)
