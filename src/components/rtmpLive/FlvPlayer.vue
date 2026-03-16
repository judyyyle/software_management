<template>
    <div class="flv-player-container">
        <video ref="videoRef" class="live-video" controls></video>
        <div v-if="error" class="error-overlay">
            <div class="error-content">
                <p class="error-message">{{ errorMessage }}</p>
                <button @click="retryPlay" class="retry-button">重试</button>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, onBeforeUnmount, inject } from 'vue'
import flvjs from 'flv.js'

// 视频元素引用
const videoRef = ref<HTMLVideoElement | null>(null)

// 错误状态
const error = ref(false)
const errorMessage = ref('')

// 直播状态
const streamStatus = inject('streamStatus', ref('未直播'))

// FLV播放器实例
let flvPlayer: flvjs.Player | null = null

// 重试计数
const retryCount = ref(0)
const maxRetries = 3

onBeforeUnmount(() => {
    stop()
})

// 初始化FLV播放器
const initFlvPlayer = (url: string) => {
    if (!videoRef.value) return

    // 检查浏览器是否支持
    if (!flvjs.isSupported()) {
        error.value = true
        errorMessage.value = '当前浏览器不支持FLV播放'
        return
    }

    // 销毁旧实例
    if (flvPlayer) {
        flvPlayer.destroy()
    }

    // 创建FLV播放器
    flvPlayer = flvjs.createPlayer({
        type: 'flv',
        url: url,
        isLive: true,
        hasAudio: true,
        hasVideo: true,
    }, {
        enableWorker: false, // 禁用 Worker，避免加载问题
        enableStashBuffer: false,
        stashInitialSize: 128,
        lazyLoad: false,
        lazyLoadMaxDuration: 3 * 60,
        lazyLoadRecoverDuration: 30,
        deferLoadAfterSourceOpen: false,
        autoCleanupSourceBuffer: true,
        autoCleanupMaxBackwardDuration: 3,
        autoCleanupMinBackwardDuration: 2,
    })

    flvPlayer.attachMediaElement(videoRef.value)
    flvPlayer.load()

    // 事件监听
    flvPlayer.on(flvjs.Events.LOADING_COMPLETE, () => {
        console.log('[FlvPlayer] 加载完成')
    })

    flvPlayer.on(flvjs.Events.ERROR, (errorType, errorDetail) => {
        console.error('[FlvPlayer] 播放错误:', errorType, errorDetail)
        error.value = true
        errorMessage.value = `播放错误: ${errorType}`
        handleRetry(url)
    })

    // 开始播放
    flvPlayer.play().then(() => {
        error.value = false
        streamStatus.value = '直播中'
        console.log('[FlvPlayer] 播放成功')
    }).catch((err) => {
        console.error('[FlvPlayer] 播放失败:', err)
        error.value = true
        errorMessage.value = '播放失败，请重试'
    })
}

// 处理重试
const handleRetry = (url: string) => {
    if (retryCount.value < maxRetries) {
        retryCount.value++
        console.log(`[FlvPlayer] 第 ${retryCount.value} 次重试...`)
        setTimeout(() => {
            initFlvPlayer(url)
        }, 3000)
    } else {
        errorMessage.value = '多次尝试失败，请检查网络连接'
    }
}

// 手动重试
const retryPlay = () => {
    retryCount.value = 0
    error.value = false
}

// 播放
const play = (url: string) => {
    retryCount.value = 0
    error.value = false
    initFlvPlayer(url)
}

// 停止
const stop = () => {
    if (flvPlayer) {
        flvPlayer.pause()
        flvPlayer.unload()
        flvPlayer.detachMediaElement()
        flvPlayer.destroy()
        flvPlayer = null
    }
    streamStatus.value = '未直播'
}

defineExpose({
    play,
    stop,
})
</script>

<style scoped lang="scss">
.flv-player-container {
    width: 100%;
    height: 100%;
    display: flex;
    justify-content: center;
    align-items: center;
    background-color: #000;
    position: relative;
    overflow: hidden;
}

.live-video {
    width: 100%;
    height: 100%;
    object-fit: contain;
    background-color: #000;
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
