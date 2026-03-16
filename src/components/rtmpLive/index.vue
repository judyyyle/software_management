<template>
    <div class="rtmp-live-container">
        <!-- 协议切换按钮 -->
        <div class="protocol-switcher">
            <el-radio-group v-model="currentProtocol" size="small" @change="onProtocolChange">
                <el-radio-button label="webrtc">WebRTC</el-radio-button>
                <el-radio-button label="flv">FLV</el-radio-button>
                <el-radio-button label="hls">HLS</el-radio-button>
            </el-radio-group>
        </div>

        <!-- WebRTC播放器 -->
        <video v-show="currentProtocol === 'webrtc'" ref="webrtcVideoRef" class="live-video" autoplay muted playsinline></video>

        <!-- HLS播放器 -->
        <video v-show="currentProtocol === 'hls'" ref="hlsVideoRef" class="live-video" autoplay muted playsinline></video>

        <!-- FLV播放器 -->
        <video v-show="currentProtocol === 'flv'" ref="flvVideoRef" class="live-video" autoplay muted playsinline></video>

        <!-- 加载状态 -->
        <div v-if="loading" class="loading-overlay">
            <div class="loading-spinner"></div>
            <p>视频加载中...</p>
        </div>

        <!-- 错误提示 -->
        <div v-if="error" class="error-overlay">
            <div class="error-content">
                <p class="error-message">{{ errorMessage }}</p>
                <button @click="retryPlay" class="retry-button">重试</button>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, inject, onBeforeUnmount, nextTick } from 'vue'
import Hls from 'hls.js'
import flvjs from 'flv.js'
import { useRtmpStore, type PlayProtocol } from '/@/stores/rtmp'

// RTMP Store
const rtmpStore = useRtmpStore()

// 直播的状态
const streamStatus = inject('streamStatus', ref('未直播'))

// 当前协议
const currentProtocol = ref<PlayProtocol>(rtmpStore.playProtocol)

// 视频流地址
const videoUrl = ref('')

// 视频元素引用
const hlsVideoRef = ref<HTMLVideoElement | null>(null)
const flvVideoRef = ref<HTMLVideoElement | null>(null)
const webrtcVideoRef = ref<HTMLVideoElement | null>(null)

// 加载状态
const loading = ref(false)

// 错误状态
const error = ref(false)
const errorMessage = ref('')

// 重试计数器
const retryCount = ref(0)
const maxRetries = 3
const retryDelay = 5000

// HLS实例
let hls: Hls | null = null

// FLV实例
let flvPlayer: flvjs.Player | null = null

// WebRTC 实例
let webrtcPc: RTCPeerConnection | null = null

onBeforeUnmount(() => {
    stop()
})

// 监听协议切换
const handleProtocolChange = (protocol: PlayProtocol) => {
    rtmpStore.switchProtocol(protocol)
    if (videoUrl.value) {
        stop()
        setTimeout(() => {
            play(videoUrl.value)
        }, 100)
    }
}

// 包装函数处理 el-radio-group 的 change 事件
const onProtocolChange = (val: string | number | boolean | undefined) => {
    if (typeof val === 'string') {
        handleProtocolChange(val as PlayProtocol)
    }
}

// 初始化HLS播放器
const initHlsPlayer = (url: string) => {
    if (!hlsVideoRef.value) return

    // 销毁旧实例
    if (hls) {
        hls.destroy()
    }

    if (Hls.isSupported()) {
        hls = new Hls({
            enableWorker: true,
            // 直播优化配置 - 平衡延迟和稳定性
            lowLatencyMode: false, // 关闭低延迟模式，提高稳定性
            liveSyncDurationCount: 3, // 同步点：3个分片
            liveMaxLatencyDurationCount: 10, // 最大延迟：10个分片
            maxBufferLength: 30, // 最大缓冲30秒
            maxMaxBufferLength: 60, // 极限缓冲60秒
            maxBufferSize: 60 * 1024 * 1024, // 60MB缓冲
            maxBufferHole: 0.5, // 允许0.5秒的缓冲空洞
            liveDurationInfinity: true,
            startPosition: -1, // 从最新位置开始
            // 网络重试配置 - 增强容错
            fragLoadingTimeOut: 30000, // 分片加载超时30秒
            fragLoadingMaxRetry: 6, // 分片重试6次
            fragLoadingRetryDelay: 2000, // 重试间隔2秒
            manifestLoadingTimeOut: 30000, // 清单加载超时30秒
            manifestLoadingMaxRetry: 6, // 清单重试6次
            manifestLoadingRetryDelay: 2000, // 重试间隔2秒
            levelLoadingTimeOut: 30000, // 级别加载超时
            levelLoadingMaxRetry: 6,
            levelLoadingRetryDelay: 2000,
            // 自动恢复配置
            enableSoftwareAES: false,
            backBufferLength: 30, // 保留30秒回看缓冲
        })

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
            console.log('[HLS] 视频流加载完成')
            loading.value = false
            error.value = false
            streamStatus.value = '直播中'
            hlsVideoRef.value?.play().catch((err) => {
                console.error('[HLS] 自动播放失败:', err)
                // 尝试静音播放
                if (hlsVideoRef.value) {
                    hlsVideoRef.value.muted = true
                    hlsVideoRef.value.play().catch(() => {
                        error.value = true
                        errorMessage.value = '自动播放失败，请点击视频重试'
                    })
                }
            })
        })

        hls.on(Hls.Events.ERROR, (event, data) => {
            console.warn('[HLS] 错误:', data.type, data.details)

            if (data.fatal) {
                console.error('[HLS] 致命错误:', data)

                switch (data.type) {
                    case Hls.ErrorTypes.NETWORK_ERROR:
                        // 网络错误，尝试恢复
                        console.log('[HLS] 网络错误，尝试恢复...')
                        hls?.startLoad()
                        break
                    case Hls.ErrorTypes.MEDIA_ERROR:
                        // 媒体错误，尝试恢复
                        console.log('[HLS] 媒体错误，尝试恢复...')
                        hls?.recoverMediaError()
                        break
                    default:
                        // 其他错误，重试
                        error.value = true
                        errorMessage.value = `HLS播放错误: ${data.type}`
                        handleRetry()
                        break
                }
            }
        })

        // 监听缓冲事件
        hls.on(Hls.Events.FRAG_BUFFERED, () => {
            // 缓冲成功，清除错误状态
            if (error.value) {
                error.value = false
                errorMessage.value = ''
            }
        })

        hls.loadSource(url)
        hls.attachMedia(hlsVideoRef.value)
    } else if (hlsVideoRef.value.canPlayType('application/vnd.apple.mpegurl')) {
        // Safari原生支持
        hlsVideoRef.value.src = url
        hlsVideoRef.value.addEventListener('loadedmetadata', () => {
            loading.value = false
            streamStatus.value = '直播中'
            hlsVideoRef.value?.play()
        })
        hlsVideoRef.value.addEventListener('error', () => {
            error.value = true
            errorMessage.value = 'HLS播放错误'
            handleRetry()
        })
    }
}

// 初始化FLV播放器
const initFlvPlayer = (url: string) => {
    console.log('[FLV] 初始化播放器, URL:', url)
    console.log('[FLV] flvVideoRef:', flvVideoRef.value)

    if (!flvVideoRef.value) {
        console.error('[FLV] 视频元素未找到!')
        error.value = true
        errorMessage.value = 'FLV视频元素未初始化'
        return
    }

    if (!flvjs.isSupported()) {
        console.error('[FLV] 浏览器不支持FLV播放')
        error.value = true
        errorMessage.value = '当前浏览器不支持FLV播放'
        return
    }

    // 销毁旧实例
    if (flvPlayer) {
        try {
            flvPlayer.pause()
            flvPlayer.unload()
            flvPlayer.detachMediaElement()
            flvPlayer.destroy()
        } catch (e) {
            console.warn('[FLV] 销毁旧实例时出错:', e)
        }
        flvPlayer = null
    }

    try {
        flvPlayer = flvjs.createPlayer(
            {
                type: 'flv',
                url: url,
                isLive: true,
                hasAudio: true,
                hasVideo: true,
                cors: true,
                withCredentials: false,
            },
            {
                enableWorker: false, // 禁用 Worker，避免 Vite 环境下的加载问题
                enableStashBuffer: true, // 启用缓冲
                stashInitialSize: 128 * 1024, // 初始缓冲128KB（减小以加快首帧）
                lazyLoad: true, // 启用懒加载
                lazyLoadMaxDuration: 3 * 60, // 最大懒加载3分钟
                lazyLoadRecoverDuration: 30, // 恢复时间30秒
                deferLoadAfterSourceOpen: false,
                autoCleanupSourceBuffer: true,
                autoCleanupMaxBackwardDuration: 30, // 保留30秒回看
                autoCleanupMinBackwardDuration: 15, // 最少保留15秒
                fixAudioTimestampGap: true, // 修复音频时间戳间隙
                accurateSeek: false,
                seekType: 'range',
                reuseRedirectedURL: true,
                referrerPolicy: 'no-referrer-when-downgrade',
            }
        )

        console.log('[FLV] Player创建成功')

        flvPlayer.attachMediaElement(flvVideoRef.value)
        console.log('[FLV] attachMediaElement完成')

        flvPlayer.load()
        console.log('[FLV] load()调用完成')

        flvPlayer.on(flvjs.Events.ERROR, (errorType: string, errorDetail: string, errorInfo: any) => {
            console.error('[FLV] 播放错误:', errorType, errorDetail, errorInfo)

            // Early-EOF 是流断开，不是播放器问题
            if (errorDetail === 'UnrecoverableEarlyEof') {
                console.log('[FLV] 流已断开 (Early-EOF)，可能是推流端断开')
                error.value = true
                errorMessage.value = '直播流已断开，请检查设备推流状态'
                streamStatus.value = '流已断开'
                // 不自动重试，因为是推流端问题
                return
            }

            // 网络错误尝试重连
            if (errorType === flvjs.ErrorTypes.NETWORK_ERROR) {
                console.log('[FLV] 网络错误，5秒后尝试重连...')
                error.value = true
                errorMessage.value = '网络错误，正在重连...'
                setTimeout(() => {
                    if (flvPlayer && videoUrl.value) {
                        try {
                            flvPlayer.unload()
                            flvPlayer.load()
                            // 不立即调用play，等待数据加载
                        } catch (e) {
                            console.error('[FLV] 重连失败:', e)
                        }
                    }
                }, 5000)
                return
            }

            error.value = true
            errorMessage.value = `FLV播放错误: ${errorDetail || errorType}`
            handleRetry()
        })

        // 监听统计信息
        flvPlayer.on(flvjs.Events.STATISTICS_INFO, (info: any) => {
            // 检测卡顿
            if (info.decodedFrames > 0 && info.droppedFrames / info.decodedFrames > 0.1) {
                console.warn('[FLV] 丢帧率过高:', ((info.droppedFrames / info.decodedFrames) * 100).toFixed(1) + '%')
            }
        })

        // 监听媒体信息
        flvPlayer.on(flvjs.Events.MEDIA_INFO, (info: any) => {
            console.log('[FLV] 媒体信息:', info)
        })

        console.log('[FLV] 开始播放...')
        const playPromise = flvPlayer.play()
        if (playPromise) {
            playPromise
                .then(() => {
                    loading.value = false
                    error.value = false
                    streamStatus.value = '直播中'
                    console.log('[FLV] 播放成功')
                })
                .catch((err: Error) => {
                    console.error('[FLV] 播放失败:', err)
                    // 尝试静音播放
                    if (flvVideoRef.value) {
                        flvVideoRef.value.muted = true
                        flvPlayer
                            ?.play()
                            ?.then(() => {
                                loading.value = false
                                error.value = false
                                streamStatus.value = '直播中（静音）'
                                console.log('[FLV] 静音播放成功')
                            })
                            .catch((e: Error) => {
                                console.error('[FLV] 静音播放也失败:', e)
                                error.value = true
                                errorMessage.value = 'FLV播放失败: ' + e.message
                            })
                    }
                })
        }
    } catch (e: any) {
        console.error('[FLV] 初始化异常:', e)
        error.value = true
        errorMessage.value = 'FLV初始化失败: ' + e.message
    }
}

// 初始化WebRTC播放器 (WHEP协议)
const initWebrtcPlayer = async (url: string) => {
    if (!webrtcVideoRef.value) return

    // 销毁旧连接
    if (webrtcPc) {
        webrtcPc.close()
        webrtcPc = null
    }

    try {
        // 创建 RTCPeerConnection
        webrtcPc = new RTCPeerConnection({
            iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
        })

        // 监听远程流
        webrtcPc.ontrack = (event) => {
            console.log('[WebRTC] 收到远程流:', event.streams)
            if (webrtcVideoRef.value && event.streams[0]) {
                webrtcVideoRef.value.srcObject = event.streams[0]
                loading.value = false
                error.value = false
                streamStatus.value = '直播中'
                console.log('[WebRTC] 播放成功')
            }
        }

        webrtcPc.oniceconnectionstatechange = () => {
            console.log('[WebRTC] ICE状态:', webrtcPc?.iceConnectionState)
            if (webrtcPc?.iceConnectionState === 'failed') {
                error.value = true
                errorMessage.value = 'WebRTC连接失败'
                handleRetry()
            }
        }

        // 添加收发器（仅接收）
        webrtcPc.addTransceiver('video', { direction: 'recvonly' })
        webrtcPc.addTransceiver('audio', { direction: 'recvonly' })

        // 创建 Offer
        const offer = await webrtcPc.createOffer()
        await webrtcPc.setLocalDescription(offer)

        // 发送 WHEP 请求
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/sdp' },
            body: offer.sdp,
        })

        if (!response.ok) {
            throw new Error(`WHEP请求失败: ${response.status}`)
        }

        const answerSdp = await response.text()
        await webrtcPc.setRemoteDescription({
            type: 'answer',
            sdp: answerSdp,
        })

        console.log('[WebRTC] 连接建立成功')
    } catch (err: any) {
        console.error('[WebRTC] 初始化失败:', err)
        error.value = true
        errorMessage.value = `WebRTC错误: ${err.message}`
        handleRetry()
    }
}

// 初始化播放器
const initPlayer = async () => {
    if (!videoUrl.value) return

    loading.value = true
    error.value = false
    errorMessage.value = ''
    retryCount.value = 0

    // 等待DOM更新
    await nextTick()

    // 直接使用原始URL，不走代理
    const playUrl = videoUrl.value

    console.log('[RTMP] 初始化播放器, 协议:', currentProtocol.value, 'URL:', playUrl)

    // 根据协议选择播放器
    if (currentProtocol.value === 'webrtc') {
        // WebRTC 使用 WHEP 地址
        const whepUrl = buildWhepUrl(videoUrl.value)
        initWebrtcPlayer(whepUrl)
    } else if (currentProtocol.value === 'hls') {
        // HLS需要.m3u8地址
        let hlsUrl = playUrl
        if (hlsUrl.endsWith('.flv')) {
            hlsUrl = hlsUrl.replace('.flv', '.m3u8')
        }
        initHlsPlayer(hlsUrl)
    } else if (currentProtocol.value === 'flv') {
        // FLV需要.flv地址
        let flvUrl = playUrl
        if (flvUrl.endsWith('.m3u8')) {
            flvUrl = flvUrl.replace('.m3u8', '.flv')
        }
        initFlvPlayer(flvUrl)
    }
}

// 从 FLV/HLS 地址构建 WHEP 地址
const buildWhepUrl = (url: string): string => {
    // 从 http://xxx/live/streamkey.flv 提取 streamkey
    const match = url.match(/\/live\/([^.]+)/)
    const streamKey = match ? match[1] : 'livestream'
    const baseUrl = rtmpStore.srsConfig.httpServer
    return `${baseUrl}/rtc/v1/whep/?app=live&stream=${streamKey}`
}

// 处理重试
const handleRetry = () => {
    if (retryCount.value < maxRetries) {
        retryCount.value++
        console.log(`[RTMP] 第 ${retryCount.value} 次重试...`)
        setTimeout(() => {
            initPlayer()
        }, retryDelay)
    } else {
        errorMessage.value = '多次尝试失败，请检查网络连接或切换播放协议'
    }
}

// 手动重试
const retryPlay = () => {
    retryCount.value = 0
    initPlayer()
}

// 开始播放
const play = async (url: string) => {
    console.log('[RTMP] play() 被调用, URL:', url)
    videoUrl.value = url
    await initPlayer()
}

// 停止播放
const stop = () => {
    // 停止HLS
    if (hls) {
        hls.destroy()
        hls = null
    }

    // 停止FLV
    if (flvPlayer) {
        flvPlayer.pause()
        flvPlayer.unload()
        flvPlayer.detachMediaElement()
        flvPlayer.destroy()
        flvPlayer = null
    }

    // 停止WebRTC
    if (webrtcPc) {
        webrtcPc.close()
        webrtcPc = null
    }

    // 清理视频元素
    if (hlsVideoRef.value) {
        hlsVideoRef.value.pause()
        hlsVideoRef.value.src = ''
    }
    if (flvVideoRef.value) {
        flvVideoRef.value.pause()
        flvVideoRef.value.src = ''
    }
    if (webrtcVideoRef.value) {
        webrtcVideoRef.value.pause()
        webrtcVideoRef.value.srcObject = null
    }

    loading.value = false
    error.value = false
    streamStatus.value = '未直播'
}

defineExpose({
    stop,
    play,
})
</script>

<style scoped lang="scss">
.rtmp-live-container {
    width: 100%;
    height: 100%;
    display: flex;
    justify-content: center;
    align-items: center;
    background-color: #000;
    position: relative;
    overflow: hidden;
}

.protocol-switcher {
    position: absolute;
    top: 60px;
    right: 10px;
    z-index: 20;
    background-color: rgba(0, 0, 0, 0.6);
    padding: 8px;
    border-radius: 4px;
}

.live-video {
    width: 100%;
    height: 100%;
    object-fit: contain;
    background-color: #000;
}

.loading-overlay {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    background-color: rgba(0, 0, 0, 0.7);
    color: #fff;
    z-index: 10;
}

.loading-spinner {
    width: 40px;
    height: 40px;
    border: 4px solid rgba(255, 255, 255, 0.3);
    border-radius: 50%;
    border-top-color: #fff;
    animation: spin 1s ease-in-out infinite;
    margin-bottom: 16px;
}

@keyframes spin {
    to {
        transform: rotate(360deg);
    }
}

.error-overlay {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    display: flex;
    justify-content: center;
    align-items: center;
    background-color: rgba(0, 0, 0, 0.7);
    color: #fff;
    z-index: 10;
}

.error-content {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    padding: 20px;
    background-color: rgba(0, 0, 0, 0.8);
    border-radius: 8px;
    max-width: 80%;
}

.error-message {
    margin-bottom: 16px;
    font-size: 16px;
    text-align: center;
}

.retry-button {
    padding: 8px 16px;
    background-color: #409eff;
    color: #fff;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
    transition: background-color 0.3s;

    &:hover {
        background-color: #66b1ff;
    }

    &:active {
        background-color: #3a8ee6;
    }
}
</style>
