<template>
    <div class="rtmp-manager">
        <el-card class="box-card">
            <template #header>
                <div class="card-header">
                    <span>RTMP 流媒体管理</span>
                    <el-button type="primary" @click="refreshStreams">刷新</el-button>
                </div>
            </template>

            <!-- 推流密钥管理 -->
            <el-row :gutter="20" class="mb-20">
                <el-col :span="12">
                    <el-card shadow="hover">
                        <template #header>
                            <span>推流密钥</span>
                        </template>
                        <el-form label-width="100px">
                            <el-form-item label="当前密钥">
                                <el-input v-model="currentSecret" readonly>
                                    <template #append>
                                        <el-button @click="copySecret">复制</el-button>
                                    </template>
                                </el-input>
                            </el-form-item>
                            <el-form-item label="新密钥">
                                <el-input v-model="newSecret" placeholder="输入新的推流密钥">
                                    <template #append>
                                        <el-button type="primary" @click="handleUpdateSecret">更新</el-button>
                                    </template>
                                </el-input>
                            </el-form-item>
                        </el-form>
                    </el-card>
                </el-col>

                <el-col :span="12">
                    <el-card shadow="hover">
                        <template #header>
                            <span>推流配置</span>
                        </template>
                        <el-descriptions :column="1" border>
                            <el-descriptions-item label="机舱流名称">
                                {{ rtmpStore.position.cabin.streamKey }}
                            </el-descriptions-item>
                            <el-descriptions-item label="无人机流名称">
                                {{ rtmpStore.position.drone.streamKey }}
                            </el-descriptions-item>
                            <el-descriptions-item label="RTMP服务器">
                                {{ rtmpStore.srsConfig.rtmpServer }}
                            </el-descriptions-item>
                            <el-descriptions-item label="HTTP服务器">
                                {{ rtmpStore.srsConfig.httpServer }}
                            </el-descriptions-item>
                        </el-descriptions>
                    </el-card>
                </el-col>
            </el-row>

            <!-- 在线流列表 -->
            <el-card shadow="hover">
                <template #header>
                    <div class="card-header">
                        <span>在线流列表 ({{ streamList.length }})</span>
                        <el-tag :type="streamList.length > 0 ? 'success' : 'info'">
                            {{ streamList.length > 0 ? '有流在线' : '无流在线' }}
                        </el-tag>
                    </div>
                </template>

                <el-table :data="streamList" style="width: 100%" v-loading="loading">
                    <el-table-column prop="name" label="流名称" width="150" />
                    <el-table-column prop="app" label="应用" width="100" />
                    <el-table-column label="在线时长" width="120">
                        <template #default="{ row }">
                            {{ formatDuration(row.live_ms) }}
                        </template>
                    </el-table-column>
                    <el-table-column prop="clients" label="观看人数" width="100" />
                    <el-table-column label="发送流量" width="120">
                        <template #default="{ row }">
                            {{ formatBytes(row.send_bytes) }}
                        </template>
                    </el-table-column>
                    <el-table-column label="接收流量" width="120">
                        <template #default="{ row }">
                            {{ formatBytes(row.recv_bytes) }}
                        </template>
                    </el-table-column>
                    <el-table-column label="播放地址" min-width="200">
                        <template #default="{ row }">
                            <el-dropdown trigger="click">
                                <el-button type="text">查看播放地址</el-button>
                                <template #dropdown>
                                    <el-dropdown-menu>
                                        <el-dropdown-item @click="copyUrl(row.playUrls.flv)">
                                            FLV: {{ row.playUrls.flv }}
                                        </el-dropdown-item>
                                        <el-dropdown-item @click="copyUrl(row.playUrls.hls)">
                                            HLS: {{ row.playUrls.hls }}
                                        </el-dropdown-item>
                                        <el-dropdown-item @click="copyUrl(row.playUrls.rtmp)">
                                            RTMP: {{ row.playUrls.rtmp }}
                                        </el-dropdown-item>
                                    </el-dropdown-menu>
                                </template>
                            </el-dropdown>
                        </template>
                    </el-table-column>
                    <el-table-column label="操作" width="150" fixed="right">
                        <template #default="{ row }">
                            <el-button type="primary" size="small" @click="previewStream(row)">
                                预览
                            </el-button>
                            <el-button type="danger" size="small" @click="handleKickStream(row.name)">
                                踢出
                            </el-button>
                        </template>
                    </el-table-column>
                </el-table>
            </el-card>
        </el-card>

        <!-- 预览对话框 -->
        <el-dialog v-model="previewVisible" title="流预览" width="800px" :close-on-click-modal="false">
            <div class="preview-container">
                <el-radio-group v-model="previewProtocol" class="mb-10">
                    <el-radio-button label="flv">FLV</el-radio-button>
                    <el-radio-button label="hls">HLS</el-radio-button>
                </el-radio-group>
                <rtmp-live ref="previewPlayerRef" style="width: 100%; height: 450px;" />
            </div>
        </el-dialog>
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRtmpStore } from '/@/stores/rtmp'
import rtmpLive from '/@/components/rtmpLive/index.vue'

const rtmpStore = useRtmpStore()

// 状态
const loading = ref(false)
const currentSecret = ref('')
const newSecret = ref('')
const streamList = ref<any[]>([])

// 预览
const previewVisible = ref(false)
const previewProtocol = ref<'flv' | 'hls'>('flv')
const previewPlayerRef = ref<InstanceType<typeof rtmpLive> | null>(null)
const currentPreviewStream = ref<any>(null)

// 自动刷新定时器
let refreshTimer: number | null = null

onMounted(() => {
    init()
    // 每5秒自动刷新流列表
    refreshTimer = window.setInterval(() => {
        refreshStreams(false)
    }, 5000)
})

onUnmounted(() => {
    if (refreshTimer) {
        clearInterval(refreshTimer)
    }
})

// 初始化
const init = async () => {
    await loadSecret()
    await refreshStreams()
}

// 加载推流密钥
const loadSecret = async () => {
    try {
        const secret = await rtmpStore.querySecret()
        if (secret) {
            currentSecret.value = secret
        }
    } catch (error) {
        console.error('加载推流密钥失败:', error)
    }
}

// 刷新流列表
const refreshStreams = async (showMessage = true) => {
    loading.value = true
    try {
        const data = await rtmpStore.getStreamList()
        if (data) {
            streamList.value = data.streams || []
            if (showMessage) {
                ElMessage.success(`刷新成功，当前在线流: ${streamList.value.length}`)
            }
        }
    } catch (error) {
        console.error('刷新流列表失败:', error)
        if (showMessage) {
            ElMessage.error('刷新失败')
        }
    } finally {
        loading.value = false
    }
}

// 更新推流密钥
const handleUpdateSecret = async () => {
    if (!newSecret.value) {
        ElMessage.warning('请输入新的推流密钥')
        return
    }

    try {
        await ElMessageBox.confirm('确定要更新推流密钥吗？更新后需要重新配置DJI设备推流地址', '确认', {
            type: 'warning',
        })

        const success = await rtmpStore.updateSecret(newSecret.value)
        if (success) {
            currentSecret.value = newSecret.value
            newSecret.value = ''
            ElMessage.success('推流密钥更新成功')
        } else {
            ElMessage.error('推流密钥更新失败')
        }
    } catch (error) {
        console.log('取消更新')
    }
}

// 踢出流
const handleKickStream = async (streamKey: string) => {
    try {
        await ElMessageBox.confirm(`确定要踢出流 "${streamKey}" 吗？`, '确认', {
            type: 'warning',
        })

        await rtmpStore.kickStream(streamKey)
        ElMessage.success('流已踢出')
        await refreshStreams(false)
    } catch (error) {
        console.log('取消踢出')
    }
}

// 预览流
const previewStream = (stream: any) => {
    currentPreviewStream.value = stream
    previewVisible.value = true
    
    // 延迟播放，等待对话框打开
    setTimeout(() => {
        const url = previewProtocol.value === 'flv' ? stream.playUrls.flv : stream.playUrls.hls
        previewPlayerRef.value?.play(url)
    }, 300)
}

// 复制密钥
const copySecret = () => {
    navigator.clipboard.writeText(currentSecret.value)
    ElMessage.success('密钥已复制到剪贴板')
}

// 复制URL
const copyUrl = (url: string) => {
    navigator.clipboard.writeText(url)
    ElMessage.success('播放地址已复制到剪贴板')
}

// 格式化时长
const formatDuration = (ms: number) => {
    const seconds = Math.floor(ms / 1000)
    const hours = Math.floor(seconds / 3600)
    const minutes = Math.floor((seconds % 3600) / 60)
    const secs = seconds % 60
    return `${hours}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
}

// 格式化字节
const formatBytes = (bytes: number) => {
    if (bytes === 0) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i]
}
</script>

<style scoped lang="scss">
.rtmp-manager {
    padding: 20px;
}

.card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.mb-20 {
    margin-bottom: 20px;
}

.mb-10 {
    margin-bottom: 10px;
}

.preview-container {
    display: flex;
    flex-direction: column;
}
</style>
