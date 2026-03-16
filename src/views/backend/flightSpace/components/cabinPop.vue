<template>
    <div class="video-player-container" ref="videoPlayerContainer" v-if="isShowCabinLive" v-draggable="{ isFullScreen }">
        <div class="video-player-content">
            <div class="video-player" ref="videoContainer" v-if="mediaStore.video_type === 1"></div>
            <rtmpLive class="video-player" ref="rtmpLiveRef" v-else></rtmpLive>

            <div class="video-overlay">
                <div class="live-indicator">
                    <span class="live-dot"></span>
                    <span class="live-text">LIVE</span>
                </div>
                <span class="device-name">{{ deviceName }}</span>
                <div class="video-info">
                    <div class="video-options">
                        <el-tooltip class="box-item" effect="dark" :content="isFullScreen ? '退出全屏' : '全屏'" placement="top">
                            <el-icon style="cursor: pointer" size="20" @click.stop="isFullScreen = !isFullScreen"><FullScreen /></el-icon>
                        </el-tooltip>
                        <el-tooltip class="box-item" effect="dark" content="关闭" placement="top">
                            <el-icon style="cursor: pointer" size="20" @click.stop="isShowCabinLive = false"><SwitchButton /></el-icon>
                        </el-tooltip>
                    </div>
                    <span class="stream-status">{{ streamStatus }}</span>
                </div>
            </div>
        </div>
    </div>
</template>

<script lang="ts" setup>
import { ref, watch, provide, onUnmounted } from 'vue'
import { createLiveService, type LiveServiceV2 } from '/@/services/liveServiceV2'
import { disposition } from '/@/config/disposition'
import { DJIoperations } from '/@/utils/mqttSdk'
import { FullScreen, SwitchButton } from '@element-plus/icons-vue'
import { storeToRefs } from 'pinia'
import { useMapStore } from '/@/stores/map'
import { useMedia } from '/@/stores/media'
import { useMqttStore } from '/@/stores/mqtt'
import rtmpLive from '/@/components/rtmpLive/index.vue'
import { useRtmpStore } from '/@/stores/rtmp'
import { liveSessionManager } from '/@/services/LiveSessionManager'

const mapStore = useMapStore()
const { isShowCabinLive } = storeToRefs(mapStore)

const mediaStore = useMedia()

const rtmpStore = useRtmpStore()

// RTMP直播会话ID
let rtmpSessionId: string = ''

// 直播服务实例（延迟创建，根据当前设备 SN）
let liveService: LiveServiceV2 | null = null
let currentGatewaySn: string = ''
let eventsSetup = false

// 获取或创建直播服务
const getLiveService = async () => {
    const gatewaySn = disposition.djiDock.gateway_sn
    const deviceSn = disposition.device.device_sn

    // 如果设备 SN 变化，销毁旧服务
    if (liveService && currentGatewaySn !== gatewaySn) {
        console.log(`[CabinPop] 设备切换: ${currentGatewaySn} -> ${gatewaySn}，销毁旧服务`)
        await liveService.destroy()
        liveService = null
        eventsSetup = false
    }

    // 如果已有服务，复用
    if (liveService) {
        return liveService
    }

    // 创建新的直播服务
    liveService = createLiveService({
        gatewaySn,
        deviceSn,
        cabinCameraIndex: disposition.djiDock.camera_index,
        cabinVideoIndex: disposition.djiDock.video_index,
        droneCameraIndex: disposition.device.camera_index,
        droneVideoIndex: disposition.device.video_index,
    })
    currentGatewaySn = gatewaySn
    eventsSetup = false

    console.log(`[CabinPop] 创建直播服务: gatewaySn=${gatewaySn}, serviceId=${liveService.serviceInstanceId}`)
    return liveService
}

// 组件卸载时销毁服务
onUnmounted(async () => {
    if (liveService) {
        await liveService.destroy()
        liveService = null
    }
})

const width = ref('600px')
const height = ref('500px')
// 记录全屏前的位置
const left = ref('0')
const top = ref('0')

const rtmpLiveRef = ref<InstanceType<typeof rtmpLive> | null>(null)

const videoPlayerContainer = ref<HTMLDivElement>()

watch(isShowCabinLive, async (newVal: boolean) => {
    if (newVal) {
        init()
    } else {
        if (mediaStore.video_type === 1) {
            await stopLive()
        } else {
            rtmpLiveRef.value?.stop()
            // 使用LiveSessionManager停止RTMP直播
            await stopRtmpLive()
        }
    }
})

// 是否全屏
const isFullScreen = ref(false)

watch(isFullScreen, (newVal: boolean) => {
    if (newVal) {
        videoPlayerContainer.value!.style.transition = 'all 0.3s ease-in-out'
        left.value = videoPlayerContainer.value!.style.left
        top.value = videoPlayerContainer.value!.style.top
        width.value = '100%'
        height.value = '100%'
        videoPlayerContainer.value!.style.left = '0'
        videoPlayerContainer.value!.style.top = '0'
    } else {
        width.value = '600px'
        height.value = '500px'
        videoPlayerContainer.value!.style.left = left.value
        videoPlayerContainer.value!.style.top = top.value
        setTimeout(() => {
            videoPlayerContainer.value!.style.transition = ''
        }, 300)
    }
})

const deviceName = ref('机舱')
const streamStatus = ref('等待连接...')
provide('streamStatus', streamStatus)

// 获取 mqttStore
const mqttStore = useMqttStore()

const init = async () => {
    // 等待 MQTT 连接成功
    if (!mqttStore.isConnected) {
        console.log('[CabinPop] 等待 MQTT 连接...')
        let waitCount = 0
        while (!mqttStore.isConnected && waitCount < 100) {
            await new Promise((resolve) => setTimeout(resolve, 100))
            waitCount++
        }
        if (!mqttStore.isConnected) {
            console.error('[CabinPop] MQTT 连接超时')
            streamStatus.value = 'MQTT 连接超时'
            return
        }
        console.log('[CabinPop] MQTT 已连接')
    }

    if (mediaStore.video_type === 1) {
        await setupEventListeners()
        await startLive()
    } else {
        // getRtmpUrl()
        startRtmpLive()
    }
}

// 开始流媒体直播（使用LiveSessionManager获取设备配置）
const startRtmpLive = async () => {
    console.log('[CabinPop] 开始RTMP直播')

    const gatewaySn = disposition.djiDock.gateway_sn
    rtmpSessionId = `cabin_rtmp_${gatewaySn}`

    try {
        // 创建RTMP直播会话
        const session = await liveSessionManager.createSession({
            sessionId: rtmpSessionId,
            type: 'cabin',
            gatewaySn: gatewaySn,
            cameraIndex: disposition.djiDock.camera_index,
            videoIndex: disposition.djiDock.video_index,
        })

        // 监听RTMP播放地址事件
        liveSessionManager.on('rtmp:playUrl', (data: any) => {
            if (data.sessionId === rtmpSessionId && data.type === 'cabin') {
                // 根据当前协议选择播放地址
                const playUrl = rtmpStore.playProtocol === 'hls' ? data.hlsUrl : data.playUrl
                console.log('[CabinPop] 收到RTMP播放地址:', playUrl, '协议:', rtmpStore.playProtocol)
                streamStatus.value = '等待推流建立...'

                // 延迟播放，等待推流稳定（给设备足够时间建立连接）
                const delay = rtmpStore.playProtocol === 'hls' ? 8000 : 5000
                console.log(`[CabinPop] 等待 ${delay / 1000} 秒后开始播放...`)
                setTimeout(() => {
                    streamStatus.value = '正在连接...'
                    rtmpLiveRef.value?.play(playUrl)
                }, delay)
            }
        })

        // 开始直播（LiveSessionManager会从后端获取设备RTMP配置）
        await liveSessionManager.startLive(rtmpSessionId)
    } catch (error: any) {
        console.error('[CabinPop] RTMP直播启动失败:', error)
        streamStatus.value = error.message || 'RTMP直播启动失败'
    }
}

// 停止RTMP直播
const stopRtmpLive = async () => {
    if (rtmpSessionId) {
        try {
            await liveSessionManager.stopLive(rtmpSessionId)
            await liveSessionManager.destroySession(rtmpSessionId)
        } catch (error) {
            console.error('[CabinPop] 停止RTMP直播失败:', error)
        }
        rtmpSessionId = ''
    }
}

// 开始直播
const startLive = async () => {
    const service = await getLiveService()
    service.setRetryConfig(5, 3000) // 最大重试5次，每次间隔3秒
    await service.startLive('cabin')
}

// 停止直播
const stopLive = async () => {
    if (liveService) {
        await liveService.stopLive('cabin')
    }
    streamStatus.value = '等待连接...'
}
const videoContainer = ref<HTMLDivElement>()

// 设置事件监听
const setupEventListeners = async () => {
    // 避免重复设置事件监听
    if (eventsSetup) {
        console.log('[CabinPop] 事件监听已设置，跳过')
        return
    }

    const service = await getLiveService()

    // 视频流已订阅
    service.on('agora:videoTrack', (data: any) => {
        if (videoContainer.value && data.track) {
            data.track.play(videoContainer.value)
            streamStatus.value = '视频流已连接'
            console.log('[CabinPop] Agora 视频流已订阅')
        }
    })

    service.on('agora:connectionState', (data: any) => {
        console.log(`[CabinPop] Agora 连接状态: ${data.previous} -> ${data.current}`)
    })

    // 直播状态事件
    service.on('live:status', (data: any) => {
        console.log(`[CabinPop] Live 状态: ${data.status}`)
    })

    // 错误事件
    service.on('live:error', (data: any) => {
        console.log(`[CabinPop] Error ${data.operation} 失败:`, data.error)
    })

    service.on('agora:error', (data: any) => {
        console.log(`[CabinPop] Agora Error ${data.operation} 失败:`, data.error)
    })

    eventsSetup = true
    console.log('[CabinPop] 事件监听已设置')
}
</script>

<style scoped lang="scss">
.video-player-container {
    width: v-bind(width);
    height: v-bind(height);
    background: #fff;
    border-radius: 12px;
    user-select: none; /* 防止拖动时选中文字 */
    position: absolute;
    top: 0;
    left: 0;
    z-index: 100;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    box-shadow: 0px 0px 4px 0px #0000001a;

    .video-player-content {
        flex: 1;
        width: 100%;
        display: flex;
        position: relative;
    }
}

.video-player {
    flex: 1;
    width: 100%;
    background-color: #000;
}

.video-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    padding: 20px;
    z-index: 99;

    &:hover {
        .video-info {
            bottom: 0;
            opacity: 1;
        }
    }

    .live-indicator {
        position: absolute;
        top: 20px;
        left: 20px;
        display: flex;
        align-items: center;
        gap: 8px;
        background-color: rgba(255, 0, 0, 0.8);
        color: #fff;
        padding: 6px 12px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: bold;

        .live-dot {
            width: 8px;
            height: 8px;
            background-color: #fff;
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
            0% {
                opacity: 1;
            }
            50% {
                opacity: 0.5;
            }
            100% {
                opacity: 1;
            }
        }
    }

    .device-name {
        position: absolute;
        top: 20px;
        right: 20px;
        color: #fff;
        font-size: 14px;
        text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
    }

    .video-info {
        width: 100%;
        height: 60px;
        padding: 0 20px;
        position: absolute;
        left: 0;
        bottom: -60px;
        display: flex;
        justify-content: space-between;
        align-content: center;
        color: #fff;
        font-size: 12px;
        text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
        line-height: 60px;
        transition: all 0.5s ease-in-out;
        opacity: 0;
        background-color: rgba(0, 0, 0, 0.5);
        border-radius: 0 0 12px 12px;
        box-shadow: 0 0 10px 0 rgba(0, 0, 0, 0.5);

        .video-options {
            display: flex;
            align-items: center;
            gap: 20px;
        }
    }
}
</style>
