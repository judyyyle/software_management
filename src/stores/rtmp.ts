import { defineStore } from 'pinia'
import * as rtmpApi from '/@/api/frontend/rtmp'

/**
 * 播放协议类型
 * - webrtc: WebRTC 流，延迟最低 (<1秒)
 * - hls: HLS 流 (.m3u8)，兼容性最好，延迟较高 (3-10秒)
 * - flv: HTTP-FLV 流，延迟较低 (1-3秒)
 */
export type PlayProtocol = 'webrtc' | 'hls' | 'flv'

/**
 * 推流设备配置
 */
export interface StreamPosition {
    /** 推流密钥/流名称 */
    streamKey: string
    /** 推流鉴权密钥 */
    secret: string
}

export const useRtmpStore = defineStore('rtmp', {
    state: () => ({
        // SRS 服务器配置（从环境变量读取）
        srsConfig: {
            // RTMP 推流地址
            rtmpServer: import.meta.env.VITE_SRS_RTMP_SERVER || 'rtmp://103.205.254.30/live/',
            // HTTP 播放地址（用于 HLS/FLV）
            httpServer: import.meta.env.VITE_SRS_HTTP_SERVER || 'http://103.205.254.30:20221',
            // API 地址
            apiServer: import.meta.env.VITE_SRS_API_SERVER || 'http://103.205.254.30:20220',
            // API Secret（Bearer Token）
            apiSecret: import.meta.env.VITE_SRS_API_SECRET || 'Bearer srs-v2-faea67d2ef4f451082ccb864a41e1541',
        },
        // 推流设备管理
        position: {
            // 机舱
            cabin: {
                streamKey: 'livestream',
                secret: 'e9de8fd919ef4354b248d3aa87a7b43e',
            } as StreamPosition,
            // 飞行器
            drone: {
                streamKey: 'dronestream',
                secret: 'e9de8fd919ef4354b248d3aa87a7b43e',
            } as StreamPosition,
        },
        // 当前推流设备
        currentPosition: 'cabin' as 'cabin' | 'drone',
        // 当前播放协议（默认FLV，延迟低且SRS默认支持）
        playProtocol: 'flv' as PlayProtocol,
        // 是否开启了云端算法
        isCloudAlgorithm: false,
        // 云端算法数据
        cloudAlgorithmData: [] as any[],
        // 直播状态
        liveStatus: 'idle' as 'idle' | 'pushing' | 'playing' | 'error',
        // 错误信息
        errorMessage: '',
    }),
    getters: {
        /** 当前设备的推流配置 */
        currentStream: (state): StreamPosition => {
            return state.position[state.currentPosition]
        },
        
        /** RTMP 推流地址（给 DJI 设备使用） */
        rtmpUrl: (state): string => {
            const stream = state.position[state.currentPosition]
            return `${state.srsConfig.rtmpServer}${stream.streamKey}?secret=${stream.secret}`
        },
        
        /** HLS 播放地址 (.m3u8) */
        hlsPlayUrl: (state): string => {
            const stream = state.position[state.currentPosition]
            return `${state.srsConfig.httpServer}/live/${stream.streamKey}.m3u8`
        },
        
        /** HTTP-FLV 播放地址 */
        flvPlayUrl: (state): string => {
            const stream = state.position[state.currentPosition]
            return `${state.srsConfig.httpServer}/live/${stream.streamKey}.flv`
        },
        
        /** WebRTC 播放地址 (WHEP) */
        webrtcPlayUrl: (state): string => {
            const stream = state.position[state.currentPosition]
            return `${state.srsConfig.httpServer}/rtc/v1/whep/?app=live&stream=${stream.streamKey}`
        },
        
        /** 根据当前协议返回播放地址 */
        playUrl: (state): string => {
            const stream = state.position[state.currentPosition]
            switch (state.playProtocol) {
                case 'hls':
                    return `${state.srsConfig.httpServer}/live/${stream.streamKey}.m3u8`
                case 'flv':
                    return `${state.srsConfig.httpServer}/live/${stream.streamKey}.flv`
                case 'webrtc':
                    return `${state.srsConfig.httpServer}/rtc/v1/whep/?app=live&stream=${stream.streamKey}`
                default:
                    return `${state.srsConfig.httpServer}/live/${stream.streamKey}.flv`
            }
        },
    },
    actions: {
        /** 切换推流设备 */
        switchPosition(position: 'cabin' | 'drone') {
            this.currentPosition = position
            console.log(`[RtmpStore] 切换推流设备: ${position}`)
        },
        
        /** 切换播放协议 */
        switchProtocol(protocol: PlayProtocol) {
            this.playProtocol = protocol
            console.log(`[RtmpStore] 切换播放协议: ${protocol}`)
        },
        
        /** 更新推流配置 */
        updateStreamConfig(position: 'cabin' | 'drone', config: Partial<StreamPosition>) {
            this.position[position] = { ...this.position[position], ...config }
            console.log(`[RtmpStore] 更新推流配置: ${position}`, config)
        },
        
        /** 查询 SRS 推流密钥 */
        async querySecret() {
            try {
                const res = await rtmpApi.querySecret()
                if (res?.code === 1 && res?.data?.secret) {
                    const secret = res.data.secret
                    // 更新所有设备的密钥
                    this.position.cabin.secret = secret
                    this.position.drone.secret = secret
                    console.log('[RtmpStore] 推流密钥已更新:', secret)
                    return secret
                }
            } catch (error) {
                console.error('[RtmpStore] 查询推流密钥失败:', error)
                throw error
            }
        },
        
        /** 更新推流密钥 */
        async updateSecret(newSecret: string) {
            try {
                const res = await rtmpApi.updateSecret(newSecret)
                if (res?.code === 1) {
                    this.position.cabin.secret = newSecret
                    this.position.drone.secret = newSecret
                    console.log('[RtmpStore] 推流密钥已更新:', newSecret)
                    return true
                }
                return false
            } catch (error) {
                console.error('[RtmpStore] 更新推流密钥失败:', error)
                throw error
            }
        },
        
        /** 查询流状态 */
        async queryStreamStatus(streamKey?: string) {
            const key = streamKey || this.currentStream.streamKey
            try {
                const res = await rtmpApi.queryStreamStatus(key)
                if (res?.code === 1 && res?.data) {
                    const exists = res.data.exists
                    
                    // 更新直播状态
                    if (exists) {
                        this.setLiveStatus('pushing')
                    } else if (this.liveStatus === 'pushing') {
                        this.setLiveStatus('idle')
                    }
                    
                    return res.data
                }
                return null
            } catch (error) {
                console.error('[RtmpStore] 查询流状态失败:', error)
                return null
            }
        },
        
        /** 踢出流（强制断开推流） */
        async kickStream(streamKey?: string) {
            const key = streamKey || this.currentStream.streamKey
            try {
                const res = await rtmpApi.kickStream(key)
                if (res?.code === 1) {
                    console.log('[RtmpStore] 流已踢出:', key)
                    this.setLiveStatus('idle')
                    return res.data
                }
                return null
            } catch (error) {
                console.error('[RtmpStore] 踢出流失败:', error)
                throw error
            }
        },
        
        /** 获取流列表 */
        async getStreamList() {
            try {
                const res = await rtmpApi.getStreamList()
                if (res?.code === 1 && res?.data) {
                    return res.data
                }
                return null
            } catch (error) {
                console.error('[RtmpStore] 获取流列表失败:', error)
                return null
            }
        },
        
        /** 轮询流状态（用于实时监控） */
        startStreamMonitor(interval: number = 3000) {
            return setInterval(async () => {
                await this.queryStreamStatus()
            }, interval)
        },
        
        /** 设置直播状态 */
        setLiveStatus(status: 'idle' | 'pushing' | 'playing' | 'error', errorMessage?: string) {
            this.liveStatus = status
            this.errorMessage = errorMessage || ''
        },
        
        /** 云端算法分析 */
        async startCloudAlgorithm() {
            this.cloudAlgorithmData = []
            this.isCloudAlgorithm = true
            
            const params = new URLSearchParams({
                stream_url: this.rtmpUrl,
                server: '1',
            })
            
            const eventSource = new EventSource(`https://fkbushu.chuangxing.ren/api/video_stream/analyze?${params}`)
            
            eventSource.onmessage = (event) => {
                const data = JSON.parse(event.data)
                console.log('[RtmpStore] 云端算法数据:', data)
                if (data.status || data.content) {
                    this.cloudAlgorithmData.push(data.status || data.content)
                }
                if (data.status === '视频捕获结束') {
                    console.log('[RtmpStore] 视频捕获结束')
                    eventSource.close()
                    this.isCloudAlgorithm = false
                }
            }
            
            eventSource.onerror = () => {
                console.error('[RtmpStore] 云端算法连接错误')
                eventSource.close()
                this.isCloudAlgorithm = false
            }
            
            return eventSource
        },
        
        /** 停止云端算法 */
        stopCloudAlgorithm() {
            this.isCloudAlgorithm = false
        },
    },
})
