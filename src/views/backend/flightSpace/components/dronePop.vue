<template>
    <div class="video-player-container" ref="videoPlayerContainer" v-draggable="{ isFullScreen }">
        <div class="video-player-content">
            <div class="video-player" ref="videoContainerDrone" v-if="mediaStore.video_type === 1"></div>
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
                            <el-icon style="cursor: pointer" size="20" @click.stop="isShowDroneLive = false"><SwitchButton /></el-icon>
                        </el-tooltip>
                    </div>
                    <!-- 按钮操作 -->
                    <div class="video-btn">
                        <el-button type="primary" @click="handleReturn">一键返航</el-button>
                        <el-button type="success" @click="handleCancelReturn">取消返航</el-button>
                        <el-button type="info" @click="handlePause">航线暂停</el-button>
                        <el-button type="warning" @click="handleResume">航线恢复</el-button>
                    </div>
                    <!-- 变焦操作 -->
                    <div class="video-zoom">
                        <!-- 变焦的倍率 -->

                        <!-- 滑动变焦 -->
                        <!-- <el-steps direction="vertical" :active="1">
                            <el-step title="1" />
                            <el-step title="2" />
                            <el-step title="3" />
                            <el-step title="4" />
                            <el-step title="5" />
                            <el-step title="6" />
                        </el-steps> -->
                    </div>
                    <span class="stream-status">{{ streamStatus }}</span>
                </div>
            </div>
        </div>
    </div>
</template>

<script lang="ts" setup>
import { ref, watch, onUnmounted, provide } from 'vue'
import { createLiveService, type LiveServiceV2 } from '/@/services/liveServiceV2'
import { useMqttStore } from '/@/stores/mqtt'
import { disposition } from '/@/config/disposition'
import { DJIoperations } from '/@/utils/mqttSdk'
import { FullScreen, SwitchButton } from '@element-plus/icons-vue'
import { storeToRefs } from 'pinia'
import { useMapStore } from '/@/stores/map'
import rtmpLive from '/@/components/rtmpLive/index.vue'
import { useRtmpStore } from '/@/stores/rtmp'
import { useMedia } from '/@/stores/media'

// 直播服务实例（延迟创建，根据当前设备 SN）
let liveService: LiveServiceV2 | null = null
let currentDeviceSn: string = ''
let eventsSetup = false

// 获取或创建直播服务
const getLiveService = async () => {
    const gatewaySn = disposition.djiDock.gateway_sn
    const deviceSn = disposition.device.device_sn
    
    // 如果设备 SN 变化，销毁旧服务
    if (liveService && currentDeviceSn !== deviceSn) {
        console.log(`[DronePop] 设备切换: ${currentDeviceSn} -> ${deviceSn}，销毁旧服务`)
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
    currentDeviceSn = deviceSn
    eventsSetup = false
    
    console.log(`[DronePop] 创建直播服务: gatewaySn=${gatewaySn}, deviceSn=${deviceSn}, serviceId=${liveService.serviceInstanceId}`)
    return liveService
}

// 组件卸载时，停止直播并销毁服务
onUnmounted(async () => {
    await stopLive()
    if (liveService) {
        await liveService.destroy()
        liveService = null
    }
})

const mqttStore = useMqttStore()

const { deviceData, droneData } = storeToRefs(mqttStore)

const mapStore = useMapStore()
const { isShowDroneLive } = storeToRefs(mapStore)

const mediaStore = useMedia()

const rtmpStore = useRtmpStore()

const width = ref('600px')
const height = ref('500px')

const left = ref('0')
const top = ref('0')

const videoPlayerContainer = ref<HTMLDivElement>()

const rtmpLiveRef = ref(null)

const isLive = ref(false)

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

// 监听飞行器是否在线
watch(droneData, (newVal, oldVal) => {
    if (Object.keys(newVal).length > 0 && !isLive.value) {
        isLive.value = true
        init()
    } else if (Object.keys(newVal).length === 0 && isLive.value) {
        streamStatus.value = '等待连接...'
        isLive.value = false
        // stopLive()
    }
})

const deviceName = ref('飞行器')
const streamStatus = ref('等待连接...')
provide('streamStatus', streamStatus)

const init = async () => {
    // 等待 MQTT 连接成功
    if (!mqttStore.isConnected) {
        console.log('[DronePop] 等待 MQTT 连接...')
        let waitCount = 0
        while (!mqttStore.isConnected && waitCount < 100) {
            await new Promise(resolve => setTimeout(resolve, 100))
            waitCount++
        }
        if (!mqttStore.isConnected) {
            console.error('[DronePop] MQTT 连接超时')
            streamStatus.value = 'MQTT 连接超时'
            return
        }
        console.log('[DronePop] MQTT 已连接')
    }
    
    if (mediaStore.video_type === 1) {
        await setupEventListeners()
        await startLive()
    } else {
        // getRtmpUrl()
        startRtmpLive()
    }
}

// 开始直播
const startLive = async () => {
    const service = await getLiveService()
    await service.startLive('drone')
}

// 停止直播
const stopLive = async () => {
    if (liveService) {
        await liveService.stopLive('drone')
    }
    streamStatus.value = '等待连接...'
}
const videoContainerDrone = ref<HTMLDivElement>()

// 设置事件监听
const setupEventListeners = async () => {
    // 避免重复设置事件监听
    if (eventsSetup) {
        console.log('[DronePop] 事件监听已设置，跳过')
        return
    }
    
    const service = await getLiveService()
    
    // 视频流已订阅
    service.on('agora:videoTrack', (data: any) => {
        if (videoContainerDrone.value && data.track) {
            data.track.play(videoContainerDrone.value)
            streamStatus.value = '视频流已连接'
            console.log('[DronePop] Agora 视频流已订阅')
        }
    })

    service.on('agora:connectionState', (data: any) => {
        console.log(`[DronePop] Agora 连接状态: ${data.previous} -> ${data.current}`)
    })

    // 直播状态事件
    service.on('live:status', (data: any) => {
        console.log(`[DronePop] Live 状态: ${data.status}`)
    })

    // 错误事件
    service.on('live:error', (data: any) => {
        console.log(`[DronePop] Error ${data.operation} 失败:`, data.error)
    })

    service.on('agora:error', (data: any) => {
        console.log(`[DronePop] Agora Error ${data.operation} 失败:`, data.error)
    })
    
    eventsSetup = true
    console.log('[DronePop] 事件监听已设置')
}

const handleReturn = () => {
    console.log('一键返航')
    DJIoperations.returnHome(disposition.djiDock.gateway_sn)
}

const handleCancelReturn = () => {
    console.log('取消返航')
    DJIoperations.cancelReturnHome(disposition.djiDock.gateway_sn)
}

const handlePause = () => {
    console.log('航线暂停')
    DJIoperations.pauseMission(disposition.djiDock.gateway_sn)
}

const handleResume = () => {
    console.log('航线恢复')
    DJIoperations.resumeMission(disposition.djiDock.gateway_sn)
}

// 开始流媒体直播
const startRtmpLive = async () => {
    // 切换推流设备为飞行器
    rtmpStore.switchPosition('drone')
    console.log('开始流媒体直播')
    await DJIoperations.sendServices(disposition.djiDock.gateway_sn, 'live_start_push', disposition.getDeviceRtmpData(rtmpStore.rtmpUrl))
    await new Promise((resolve) => {
        setTimeout(resolve, 1000)
    })
    rtmpLiveRef.value?.play(rtmpStore.playUrl)
    // rtmpStore.setRtmpUrl()
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
        // .video-info {
        //     bottom: 0;
        //     opacity: 1;
        // }
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
        // bottom: -60px;
        bottom: 0;
        display: flex;
        justify-content: space-between;
        align-content: center;
        color: #fff;
        font-size: 12px;
        text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
        line-height: 60px;
        transition: all 0.5s ease-in-out;
        background-color: rgba(0, 0, 0, 0.5);
        border-radius: 0 0 12px 12px;
        box-shadow: 0 0 10px 0 rgba(0, 0, 0, 0.5);

        .video-options {
            display: flex;
            align-items: center;
            gap: 20px;
        }

        .video-btn {
            display: flex;
            align-items: center;
        }
    }
}
</style>
