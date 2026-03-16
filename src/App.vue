<template>
    <el-config-provider :value-on-clear="() => null" :locale="lang">
        <router-view></router-view>
        <!-- <div class="banquan">
            <div class="text-box">
            </div>
        </div> -->
    </el-config-provider>
</template>
<script setup lang="ts">
import { onMounted, watch, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { useConfig } from '/@/stores/config'
import { setTitleFromRoute } from '/@/utils/common'
import iconfontInit from '/@/utils/iconfont'
import { init as viteInit } from '/@/utils/vite'
import { useMqttStore } from '/@/stores/mqtt'
import { useRtmpStore } from '/@/stores/rtmp'

import createAxios from '/@/utils/axios'
import { disposition } from '/@/config/disposition'
import { useMedia } from '/@/stores/media'
// modules import mark, Please do not remove.
const route = useRoute()
const config = useConfig()
const mqttStore = useMqttStore()
const mediaStore = useMedia()
const rtmpStore = useRtmpStore()

// 初始化 element 的语言包
const { getLocaleMessage } = useI18n()
const lang = getLocaleMessage(config.lang.defaultLang) as any
onMounted(async () => {
    // 获取推流密钥
    // await rtmpStore.getRtmpUrl()
    // 获取当前页面的URL参数
    const urlParams = window.location.href.split('?')[1]
    // 如果有sn参数
    if (urlParams) {
        const sn = urlParams.split('=')[1]
        mqttStore.gateway_sn = sn
    }
    viteInit()
    iconfontInit()
    await getAgoraToken()
    console.log('🚀 开始初始化MQTT连接...')
    await mqttStore.initMqtt()
    console.log('✅ MQTT初始化完成')
    await getEquipmentList()
    // 检查是否有正在执行的航线
    // mqttStore.getTaskPlan()
    // 页面关闭时断开MQTT连接
    window.addEventListener('beforeunload', () => {
        mqttStore.disconnect()
        mqttStore.unsubscribeDeviceOsd()
    })
    // 获取媒体库配置
    await mediaStore.getMediaConfig()
    // Modules onMounted mark, Please do not remove.
})

// 获取声网的token（通过后端代理）
const getAgoraToken = async () => {
    try {
        // 批量获取 Token
        const response = await createAxios(
            {
                url: '/api/agora/batchToken',
                method: 'post',
                data: {
                    channels: {
                        cabin: {
                            channelName: disposition.cabin.channel,
                            uid: disposition.cabin.uid,
                            tokenExpireTs: 3600,
                            privilegeExpireTs: 3600,
                            serviceRtc: { enable: true, role: 1 },
                        },
                        drone: {
                            channelName: disposition.drone.channel,
                            uid: disposition.drone.uid,
                            tokenExpireTs: 3600,
                            privilegeExpireTs: 3600,
                            serviceRtc: { enable: true, role: 1 },
                        },
                    },
                },
            },
            { showCodeMessage: false }
        )
        if (response?.code === 1 && response?.data) {
            const tokens = response.data
            if (tokens.cabin?.token) disposition.cabin.token = tokens.cabin.token
            if (tokens.drone?.token) disposition.drone.token = tokens.drone.token
            console.log('声网token获取成功', disposition)
        }
    } catch (error) {
        console.warn('获取声网Token失败，直播功能可能不可用:', error)
    }
}

// 获取设备列表
const getEquipmentList = async () => {
    await mqttStore.getDeviceList()
}

onUnmounted(() => {
    window.removeEventListener('beforeunload', () => {
        mqttStore.disconnect()
        mqttStore.unsubscribeDeviceOsd()
    })
    // mqttStore.disconnect()
    // mqttStore.unsubscribeDeviceOsd()
})

// 监听路由变化时更新浏览器标题
watch(
    () => route.path,
    () => {
        setTitleFromRoute()
    }
)
</script>

<style scoped lang="scss">
.banquan {
    position: absolute;
    bottom: 0;
    right: 50%;
    transform: translateX(50%);
    z-index: 9999;
    display: flex;
    align-items: center;
    gap: 5px;

    .logo {
        width: 30px;
        height: 30px;
    }

    .text-box {
        font-size: 12px;
        display: flex;
        flex-direction: column;
        font-weight: bold;
    }
}
</style>
