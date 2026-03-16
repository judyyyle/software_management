export class Disposition {
    // 机舱
    cabin = {
        appId: import.meta.env.VITE_AGORA_CABIN_APPID,
        channel: import.meta.env.VITE_AGORA_CABIN_CHANNEL,
        token: '',
        uid: Number(new Date().getTime()).toString().slice(-6), // 使用时间戳作为 UID，确保唯一性 只留后6位
    }

    // 飞行器
    drone = {
        appId: import.meta.env.VITE_AGORA_DRONE_APPID,
        channel: import.meta.env.VITE_AGORA_DRONE_CHANNEL,
        token: '',
        uid: Number(new Date().getTime()).toString().slice(-6), // 使用时间戳作为 UID，确保唯一性 只留后6位
    }

    // 默认值与老版本保持一致
    djiDock = {
        gateway_sn: '7CTXN3S00B08GE',
        videoId: '',
        camera_index: '165-0-7',
        video_index: 'normal-0',
    }

    device = {
        device_sn: '1581F6QAD247P00GJZWY',
        camera_index: '80-0-0',
        video_index: 'normal-0',
        videoId: '',
    }

    constructor() {}

    // 根据机场 SN 设置频道（使用固定频道名，与老版本保持一致）
    async setChannelBySn(sn: string) {
        // 设置 gateway_sn
        if (sn) {
            this.djiDock.gateway_sn = sn
        }

        // 使用环境变量中的固定频道名（与老版本保持一致）
        this.drone.channel = import.meta.env.VITE_AGORA_DRONE_CHANNEL || 'drone'
        this.cabin.channel = import.meta.env.VITE_AGORA_CABIN_CHANNEL || 'cabin'

        // 使用固定 UID（与老版本保持一致）
        this.drone.uid = '54321'
        this.cabin.uid = '12345'

        console.log(`声网频道设置: drone=${this.drone.channel}, cabin=${this.cabin.channel}, gateway_sn=${this.djiDock.gateway_sn}`)

        // 获取 Token（每次都刷新，确保 Token 有效）
        await this.refreshTokens()
    }

    // 刷新声网 Token（通过后端代理获取，避免跨域）
    async refreshTokens() {
        try {
            const createAxios = (await import('/@/utils/axios')).default

            // 通过后端代理批量获取 Token
            const response = await createAxios({
                url: '/api/agora/batchToken',
                method: 'post',
                data: {
                    channels: {
                        cabin: {
                            channelName: this.cabin.channel,
                            uid: this.cabin.uid,
                            tokenExpireTs: 3600,
                            privilegeExpireTs: 3600,
                            serviceRtc: { enable: true, role: 1 },
                        },
                        drone: {
                            channelName: this.drone.channel,
                            uid: this.drone.uid,
                            tokenExpireTs: 3600,
                            privilegeExpireTs: 3600,
                            serviceRtc: { enable: true, role: 1 },
                        },
                    },
                },
            }, { showCodeMessage: false })

            if (response?.code === 1 && response?.data) {
                const tokens = response.data
                if (tokens.cabin?.token) {
                    this.cabin.token = tokens.cabin.token
                }
                if (tokens.drone?.token) {
                    this.drone.token = tokens.drone.token
                }

                console.log('声网 Token 已刷新:', {
                    cabin: { channel: this.cabin.channel, uid: this.cabin.uid, hasToken: !!this.cabin.token },
                    drone: { channel: this.drone.channel, uid: this.drone.uid, hasToken: !!this.drone.token },
                })
            } else {
                console.error('获取声网 Token 失败:', response?.msg)
            }
        } catch (error) {
            console.error('刷新声网 Token 失败:', error)
        }
    }

    // 重置频道设置（用于切换机场时）
    resetChannel() {
        this.cabin.token = ''
        this.drone.token = ''
    }

    // 设置DJI Dock videoId
    setDjiDockVideoId(sn: string) {
        this.djiDock.gateway_sn = sn
        this.djiDock.videoId = `${this.djiDock.gateway_sn}/${this.djiDock.camera_index}/${this.djiDock.video_index}`
    }

    // 设置无人机 videoId
    setDeviceVideoId(sn: string) {
        this.device.device_sn = sn
        this.device.videoId = `${this.device.device_sn}/${this.device.camera_index}/${this.device.video_index}`
    }

    // 获取DJI Dock数据
    getDjiDockData() {
        this.djiDock.videoId = `${this.djiDock.gateway_sn}/${this.djiDock.camera_index}/${this.djiDock.video_index}`
        return {
            url_type: 0,
            url: `channel=${this.cabin.channel}&sn=${this.djiDock.gateway_sn}&token=${encodeURIComponent(this.cabin.token)}&uid=${this.cabin.uid}`,
            video_id: this.djiDock.videoId,
            video_quality: 4,
        }
    }

    // 停止直播
    stopLive(type: string) {
        if (type === 'cabin') {
            return {
                video_id: this.djiDock.videoId,
            }
        } else {
            return {
                video_id: this.device.videoId,
            }
        }
    }

    // 获取DJI Dock数据 rtmp
    getDjiDockRtmpData(url: string) {
        this.djiDock.videoId = `${this.djiDock.gateway_sn}/${this.djiDock.camera_index}/${this.djiDock.video_index}`
        return {
            url_type: 1,
            url,
            video_id: this.djiDock.videoId,
            video_quality: 4,
        }
    }

    // 获取无人机数据 rtmp
    getDeviceRtmpData(url: string) {
        this.device.videoId = `${this.device.device_sn}/${this.device.camera_index}/${this.device.video_index}`
        return {
            url_type: 1,
            url,
            video_id: this.device.videoId,
            video_quality: 4,
        }
    }

    // 无人机的数据
    getDeviceData() {
        this.device.videoId = `${this.device.device_sn}/${this.device.camera_index}/${this.device.video_index}`
        return {
            url_type: 0,
            url: `channel=${this.drone.channel}&sn=${this.device.device_sn}&token=${encodeURIComponent(this.drone.token)}&uid=${this.drone.uid}`,
            video_id: this.device.videoId,
            video_quality: 0, // 0=自适应, 1=流畅, 2=标清, 3=高清, 4=超清
        }
    }

    // 设置飞行器相机索引（根据实际无人机型号设置）
    setDeviceCameraIndex(cameraIndex: string) {
        this.device.camera_index = cameraIndex
        console.log(`飞行器相机索引已设置: ${cameraIndex}`)
    }
}

export const disposition = new Disposition()
