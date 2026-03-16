<template>
    <div class="rtmp-debug">
        <el-card header="RTMP 推流调试信息">
            <el-descriptions :column="1" border>
                <el-descriptions-item label="推流地址">
                    <el-input v-model="rtmpUrl" readonly>
                        <template #append>
                            <el-button @click="copyToClipboard(rtmpUrl)">复制</el-button>
                        </template>
                    </el-input>
                </el-descriptions-item>
                
                <el-descriptions-item label="播放地址 (FLV)">
                    <el-input v-model="flvUrl" readonly>
                        <template #append>
                            <el-button @click="copyToClipboard(flvUrl)">复制</el-button>
                        </template>
                    </el-input>
                </el-descriptions-item>
                
                <el-descriptions-item label="播放地址 (HLS)">
                    <el-input v-model="hlsUrl" readonly>
                        <template #append>
                            <el-button @click="copyToClipboard(hlsUrl)">复制</el-button>
                        </template>
                    </el-input>
                </el-descriptions-item>
                
                <el-descriptions-item label="推流密钥">
                    <el-input v-model="secret" readonly>
                        <template #append>
                            <el-button @click="copyToClipboard(secret)">复制</el-button>
                        </template>
                    </el-input>
                </el-descriptions-item>
                
                <el-descriptions-item label="流名称">
                    {{ streamKey }}
                </el-descriptions-item>
                
                <el-descriptions-item label="当前设备">
                    {{ currentPosition }}
                </el-descriptions-item>
            </el-descriptions>
            
            <div style="margin-top: 20px;">
                <h3>测试推流命令 (ffmpeg)</h3>
                <el-input
                    v-model="ffmpegCommand"
                    type="textarea"
                    :rows="3"
                    readonly
                >
                    <template #append>
                        <el-button @click="copyToClipboard(ffmpegCommand)">复制</el-button>
                    </template>
                </el-input>
            </div>
            
            <div style="margin-top: 20px;">
                <h3>测试推流命令 (OBS)</h3>
                <p><strong>服务器:</strong> {{ obsServer }}</p>
                <p><strong>串流密钥:</strong> {{ obsStreamKey }}</p>
            </div>
        </el-card>
    </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { useRtmpStore } from '/@/stores/rtmp'

const rtmpStore = useRtmpStore()

const currentPosition = computed(() => rtmpStore.currentPosition)
const streamKey = computed(() => rtmpStore.currentStream.streamKey)
const secret = computed(() => rtmpStore.currentStream.secret)

const rtmpUrl = computed(() => rtmpStore.rtmpUrl)
const flvUrl = computed(() => rtmpStore.flvPlayUrl)
const hlsUrl = computed(() => rtmpStore.hlsPlayUrl)

const obsServer = computed(() => rtmpStore.srsConfig.rtmpServer)
const obsStreamKey = computed(() => `${streamKey.value}?secret=${secret.value}`)

const ffmpegCommand = computed(() => {
    return `ffmpeg -re -i test.mp4 -c copy -f flv "${rtmpUrl.value}"`
})

const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text).then(() => {
        ElMessage.success('已复制到剪贴板')
    }).catch(() => {
        ElMessage.error('复制失败')
    })
}
</script>

<style scoped lang="scss">
.rtmp-debug {
    padding: 20px;
}

h3 {
    margin-bottom: 10px;
    font-size: 16px;
    font-weight: 600;
}

p {
    margin: 8px 0;
    font-family: monospace;
}
</style>
