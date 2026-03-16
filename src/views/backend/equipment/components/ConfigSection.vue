<template>
    <div class="config-section" v-loading="loading">
        <!-- 顶部控制区域 -->
        <div class="top-controls">
            <div class="left-controls">
                <div class="remote-debug">
                    <span class="control-label">远程调试</span>
                    <el-switch style="--el-switch-on-color: #00386d" v-model="remoteDebug" inline-prompt active-text="开" inactive-text="关" />
                </div>
            </div>

            <!-- <div class="right-controls">
                <el-button type="primary" class="flight-test-btn">机场试飞</el-button>
                <el-button class="feedback-btn">设备定规与反馈</el-button>
            </div> -->
        </div>

        <!-- 机场控制区域 -->
        <div class="control-section">
            <div class="section-header">
                <div class="section-title">
                    <span class="title-text">机场控制</span>
                    <!-- <el-button class="live-btn" @click="livePop = true">直播</el-button> -->
                </div>
            </div>

            <div class="control-grid">
                <!-- 第一行 -->
                <div class="control-row">
                    <div class="airport-system">
                        <div class="card-header">
                            <span class="card-title">机场系统</span>
                            <span class="status-text">正常工作中</span>
                        </div>
                        <div class="status-label" @click="handlePower('device_reboot')">重启</div>
                    </div>

                    <div class="control-card">
                        <div class="card-header">
                            <span class="card-title">舱盖</span>
                        </div>
                        <div class="card-content">
                            <el-button
                                size="small"
                                :class="{ active: deviceData.cover_state == 1 }"
                                class="toggle-btn"
                                @click="handlePower('cover_open')"
                                >开</el-button
                            >
                            <el-button
                                size="small"
                                :class="{ active: deviceData.cover_state == 0 }"
                                class="toggle-btn"
                                @click="handlePower('cover_close')"
                                >关</el-button
                            >
                        </div>
                    </div>

                    <div class="control-card air-condition">
                        <div class="card-header">
                            <span class="card-title">空调</span>
                        </div>
                        <div class="card-content">
                            <el-button
                                size="small"
                                :class="{ active: deviceData.air_conditioner.air_conditioner_state == 0 }"
                                class="toggle-btn"
                                @click="handlePower('air_conditioner_mode_switch', { action: 0 })"
                                >待机</el-button
                            >
                            <el-button
                                size="small"
                                :class="{ active: deviceData.air_conditioner.air_conditioner_state == 1 }"
                                class="toggle-btn"
                                @click="handlePower('air_conditioner_mode_switch', { action: 1 })"
                                >制冷</el-button
                            >
                            <el-button
                                size="small"
                                :class="{ active: deviceData.air_conditioner.air_conditioner_state == 2 }"
                                class="toggle-btn"
                                @click="handlePower('air_conditioner_mode_switch', { action: 2 })"
                                >制热</el-button
                            >
                            <el-button
                                size="small"
                                :class="{ active: deviceData.air_conditioner.air_conditioner_state == 3 }"
                                class="toggle-btn"
                                @click="handlePower('air_conditioner_mode_switch', { action: 3 })"
                                >除湿</el-button
                            >
                        </div>
                    </div>
                </div>

                <!-- 第二行 -->
                <div class="control-row">
                    <div class="control-card">
                        <div class="card-header">
                            <span class="card-title">静音模式</span>
                        </div>
                        <div class="card-content">
                            <el-button type="primary" size="small" class="toggle-btn active">开</el-button>
                            <el-button size="small" class="toggle-btn">关</el-button>
                        </div>
                    </div>

                    <div class="control-card">
                        <div class="card-header">
                            <span class="card-title">机场声光警报</span>
                        </div>
                        <div class="card-content">
                            <el-button
                                size="small"
                                :class="{ active: deviceData.alarm_state == 1 }"
                                class="toggle-btn"
                                @click="handlePower('alarm_state_switch', { action: 1 })"
                                >开</el-button
                            >
                            <el-button
                                size="small"
                                :class="{ active: deviceData.alarm_state == 0 }"
                                class="toggle-btn"
                                @click="handlePower('alarm_state_switch', { action: 0 })"
                                >关</el-button
                            >
                        </div>
                    </div>

                    <div class="control-card empty-card"></div>

                    <!-- <div class="airport-system">
                        <div class="card-header">
                            <span class="card-title">机场存储</span>
                            <span class="status-text">0.1/73.2GB</span>
                        </div>
                        <div class="status-label">格式化</div>
                    </div> -->
                </div>

                <!-- 第三行 -->
                <!-- <div class="control-row">
                    <div class="control-card">
                        <div class="card-header">
                            <span class="card-title">机场增强图传</span>
                        </div>
                        <div class="card-content">
                            <el-button type="primary" size="small" class="toggle-btn active">开</el-button>
                            <el-button size="small" class="toggle-btn">关</el-button>
                        </div>
                    </div>

                    <div class="control-card">
                        <div class="card-header">
                            <span class="card-title">限飞解禁证书</span>
                        </div>
                        <div class="card-content">
                            <el-button type="primary" size="small" class="toggle-btn active">开</el-button>
                            <el-button size="small" class="toggle-btn">关</el-button>
                        </div>
                    </div>

                    <div class="control-card empty-card"></div>

                </div> -->
            </div>
        </div>

        <!-- 飞行器控制区域 -->
        <div class="control-section">
            <div class="section-header">
                <div class="section-title">
                    <span class="title-text">飞行器控制</span>
                </div>
            </div>

            <div class="control-grid">
                <!-- 第一行 -->
                <div class="control-row">
                    <div class="airport-system">
                        <div class="card-header">
                            <span class="card-title">飞行器电源</span>
                            <span class="status-text">{{ deviceData.sub_device.device_online_status == 0 ? '已关机' : '已开机' }}</span>
                        </div>
                        <div
                            class="status-label"
                            @click="handlePower(deviceData.sub_device.device_online_status == 0 ? 'drone_open' : 'drone_close')"
                        >
                            {{ deviceData.sub_device.device_online_status == 0 ? '开机' : '关机' }}
                        </div>
                    </div>

                    <div class="control-card">
                        <div class="card-header">
                            <span class="card-title">飞行器充电</span>
                        </div>
                        <div class="card-content">
                            <el-button
                                size="small"
                                :class="{ active: deviceData.drone_charge_state.state == 1 }"
                                class="toggle-btn"
                                @click="handlePower('charge_open')"
                                >开</el-button
                            >
                            <el-button
                                size="small"
                                :class="{ active: deviceData.drone_charge_state.state == 0 }"
                                class="toggle-btn"
                                @click="handlePower('charge_close')"
                                >关</el-button
                            >
                        </div>
                    </div>

                    <div class="control-card">
                        <div class="card-header">
                            <span class="card-title">增强图传</span>
                        </div>
                        <div class="card-content">
                            <el-button
                                size="small"
                                :class="{ active: deviceData.wireless_link.link_workmode == 0 }"
                                class="toggle-btn"
                                @click="handlePower('sdr_workmode_switch', { link_workmode: 1 })"
                                >开</el-button
                            >
                            <el-button
                                size="small"
                                :class="{ active: deviceData.wireless_link.link_workmode == 1 }"
                                class="toggle-btn"
                                @click="handlePower('sdr_workmode_switch', { link_workmode: 0 })"
                                >关</el-button
                            >
                        </div>
                    </div>
                </div>

                <!-- 第二行 -->
                <!-- <div class="control-row">
                    <div class="airport-system">
                        <div class="card-header">
                            <span class="card-title">飞行器存储</span>
                            <span class="status-text">0.1/73.2GB</span>
                        </div>
                        <div class="status-label">格式化</div>
                    </div>

                    <div class="airport-system">
                        <div class="card-header">
                            <span class="card-title">飞行器增强图传</span>
                        </div>
                        <div class="status-label">设置</div>
                    </div>

                    <div class="control-card empty-card"></div>
                </div> -->
            </div>
        </div>

        <!-- RTMP直播配置区域 -->
        <div class="control-section">
            <div class="section-header">
                <div class="section-title">
                    <span class="title-text">RTMP直播配置</span>
                    <el-tag v-if="rtmpConfigLoaded" type="success" size="small">已加载</el-tag>
                </div>
            </div>

            <div class="rtmp-config-grid">
                <!-- 机舱直播配置 -->
                <div class="rtmp-config-card">
                    <div class="config-card-header">
                        <span class="config-card-title">机舱直播</span>
                    </div>
                    <div class="config-card-content">
                        <el-form label-width="80px" size="small">
                            <el-form-item label="流名称">
                                <el-input v-model="rtmpForm.cabin_stream_key" placeholder="如: cabin_7CTXN3S00B08GE" />
                            </el-form-item>
                            <el-form-item label="推流密钥">
                                <el-input v-model="rtmpForm.cabin_secret" placeholder="SRS直播间的secret" show-password />
                            </el-form-item>
                        </el-form>
                    </div>
                </div>

                <!-- 飞行器直播配置 -->
                <div class="rtmp-config-card">
                    <div class="config-card-header">
                        <span class="config-card-title">飞行器直播</span>
                    </div>
                    <div class="config-card-content">
                        <el-form label-width="80px" size="small">
                            <el-form-item label="流名称">
                                <el-input v-model="rtmpForm.drone_stream_key" placeholder="如: drone_1581F6QAD247P00" />
                            </el-form-item>
                            <el-form-item label="推流密钥">
                                <el-input v-model="rtmpForm.drone_secret" placeholder="SRS直播间的secret" show-password />
                            </el-form-item>
                        </el-form>
                    </div>
                </div>
            </div>

            <!-- 保存按钮 -->
            <div class="rtmp-actions">
                <el-button type="primary" :loading="rtmpSaving" @click="saveRtmpConfig">保存RTMP配置</el-button>
                <el-button @click="loadRtmpConfig">刷新配置</el-button>
            </div>

            <!-- 配置预览 -->
            <div v-if="rtmpConfigLoaded && (rtmpForm.cabin_stream_key || rtmpForm.drone_stream_key)" class="rtmp-preview">
                <div class="preview-title">推流地址预览</div>
                <div v-if="rtmpForm.cabin_stream_key" class="preview-item">
                    <span class="preview-label">机舱推流:</span>
                    <code class="preview-url">{{ srsConfig.rtmpServer }}{{ rtmpForm.cabin_stream_key }}?secret={{ rtmpForm.cabin_secret || '***' }}</code>
                </div>
                <div v-if="rtmpForm.drone_stream_key" class="preview-item">
                    <span class="preview-label">飞行器推流:</span>
                    <code class="preview-url">{{ srsConfig.rtmpServer }}{{ rtmpForm.drone_stream_key }}?secret={{ rtmpForm.drone_secret || '***' }}</code>
                </div>
            </div>
        </div>
    </div>
</template>

<script lang="ts" setup>
import { ref, computed, watch, inject, onMounted } from 'vue'
import { DJIoperations } from '/@/utils/mqttSdk'
import { useMqttStore } from '/@/stores/mqtt'
import { ElMessage } from 'element-plus'
import { disposition } from '/@/config/disposition'
import { getRtmpConfig, updateRtmpConfig } from '/@/api/backend/equipment/rtmp'

const mqttStore = useMqttStore()

const deviceData = computed(() => mqttStore.deviceData)
const gateway_sn = computed(() => mqttStore.gateway_sn)

const livePop = inject<boolean>('livePop')

// 加载中
const loading = ref(false)

// ========== RTMP配置相关 ==========
const rtmpConfigLoaded = ref(false)
const rtmpSaving = ref(false)
const rtmpForm = ref({
    cabin_stream_key: '',
    cabin_secret: '',
    drone_stream_key: '',
    drone_secret: '',
})

// SRS服务器配置（从环境变量读取）
const srsConfig = {
    rtmpServer: import.meta.env.VITE_SRS_RTMP_SERVER || 'rtmp://103.205.254.30:19350/live/',
    httpServer: import.meta.env.VITE_SRS_HTTP_SERVER || 'http://103.205.254.30:20221',
}

// 加载RTMP配置
const loadRtmpConfig = async () => {
    if (!gateway_sn.value) return
    
    try {
        const res = await getRtmpConfig(gateway_sn.value)
        if (res?.code === 1 && res?.data) {
            rtmpForm.value = {
                cabin_stream_key: res.data.cabin?.stream_key || '',
                cabin_secret: res.data.cabin?.secret || '',
                drone_stream_key: res.data.drone?.stream_key || '',
                drone_secret: res.data.drone?.secret || '',
            }
            rtmpConfigLoaded.value = true
            console.log('[ConfigSection] RTMP配置已加载:', rtmpForm.value)
        }
    } catch (error) {
        console.error('[ConfigSection] 加载RTMP配置失败:', error)
    }
}

// 保存RTMP配置
const saveRtmpConfig = async () => {
    if (!gateway_sn.value) {
        ElMessage.warning('设备SN不能为空')
        return
    }
    
    rtmpSaving.value = true
    try {
        const res = await updateRtmpConfig({
            sn: gateway_sn.value,
            ...rtmpForm.value,
        })
        if (res?.code === 1) {
            ElMessage.success('RTMP配置保存成功')
        } else {
            ElMessage.error(res?.msg || '保存失败')
        }
    } catch (error: any) {
        ElMessage.error('保存失败: ' + (error.message || '未知错误'))
    } finally {
        rtmpSaving.value = false
    }
}

// 监听gateway_sn变化，自动加载RTMP配置
watch(gateway_sn, (newVal) => {
    if (newVal) {
        loadRtmpConfig()
    }
}, { immediate: true })

// 是否远程调试
const remoteDebug = ref(deviceData.value.mode_code == 2)
watch(remoteDebug, (newVal: any) => {
    loading.value = true
    if (newVal) {
        DJIoperations.sendServices(gateway_sn.value, 'debug_mode_open')
        DJIoperations.deviceEvents(gateway_sn.value)
    } else {
        DJIoperations.sendServices(gateway_sn.value, 'debug_mode_close')
        DJIoperations.deviceEvents(gateway_sn.value)
    }
})

// 监听发送消息回复
const message_services = computed(() => mqttStore.getMessagesByTopic(`thing/product/${gateway_sn.value}/services_reply`))
watch(message_services, (newVal: any) => {
    if (newVal.payload) {
        // payload 可能是字符串或已解析的对象
        const parsed = typeof newVal.payload === 'string' ? JSON.parse(newVal.payload) : newVal.payload
        console.log('📩 services_reply 消息:', parsed)
        // DJI 服务回复格式: { data: { result: 0, output: {...} } } 或 { result: 0, ... }
        const result = parsed.data?.result ?? parsed.result
        const output = parsed.data?.output ?? parsed.output
        if (result !== 0 && result !== undefined) {
            loading.value = false
            ElMessage.error('操作失败')
        } else if (output?.status === 'ok' || result === 0) {
            // 成功：output.status 为 'ok' 或 result 为 0
            loading.value = false
            ElMessage.success('操作成功')
            DJIoperations.deviceEventsClose(gateway_sn.value)
        }
    }
}, { deep: true })

// 监听当前任务进度
const message_event = computed(() => mqttStore.getMessagesByTopic(`thing/product/${gateway_sn.value}/events`))
watch(message_event, (newVal: any) => {
    if (newVal.payload) {
        // payload 可能是字符串或已解析的对象
        const parsed = typeof newVal.payload === 'string' ? JSON.parse(newVal.payload) : newVal.payload
        console.log('📩 events 消息:', parsed)
        // DJI 事件格式: { data: { result: 0, output: {...} } } 或 { result: 0, output: {...} }
        const result = parsed.data?.result ?? parsed.result
        const output = parsed.data?.output ?? parsed.output
        if (!output && result === undefined) return
        if (result !== 0 && result !== undefined) {
            loading.value = false
            ElMessage.error('操作失败')
        } else if (output?.status === 'ok' || result === 0) {
            loading.value = false
            ElMessage.success('操作成功')
            DJIoperations.deviceEventsClose(gateway_sn.value)
        }
    }
}, { deep: true })

watch(deviceData, (newVal: any) => {
    disposition.device.device_sn = newVal.sub_device.device_sn
    disposition.setDeviceVideoId()
    // if (newVal.mode_code == 2) {
    //     remoteDebug.value = true
    // } else {
    //     remoteDebug.value = false
    // }
    console.log('设备状态改变', newVal)
})

const handlePower = (method: string, data?: any) => {
    if (!remoteDebug.value) {
        return ElMessage.warning('请先进入远程调试模式')
    }
    loading.value = true
    DJIoperations.sendServices(gateway_sn.value, method, data)
    DJIoperations.deviceEvents(gateway_sn.value)
}

// 控制状态
// const remoteDebug = ref(true)
</script>

<style scoped>
.config-section {
    padding: 20px;
    border: 1px solid #0000001a;
    border-radius: 16px;
}

/* 顶部控制区域 */
.top-controls {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 16px;
    border-bottom: 1px solid #0000001a;
}

.left-controls {
    display: flex;
    align-items: center;
}

.remote-debug {
    display: flex;
    align-items: center;
    gap: 12px;
}

.control-label {
    font-size: 14px;
    color: #333;
    font-weight: 400;
}

.right-controls {
    display: flex;
    gap: 12px;
}

.flight-test-btn {
    height: 48px;
    padding: 0 32px;
    border-radius: 12px;
    font-size: 16px;
    background: #00386d;
    border-color: #00386d;
    color: #fff;
}

.flight-test-btn:hover {
    background: #2a5a82;
    border-color: #2a5a82;
}

.feedback-btn {
    height: 48px;
    padding: 0 32px;
    border-radius: 12px;
    font-size: 16px;
    border: 1px solid #0000001a;
    color: #000000;
}

.feedback-btn:hover {
    border-color: #1e4d72;
    background: #fff;
    color: #1e4d72;
}

/* 控制区域 */
.control-section {
    padding-bottom: 16px;
    margin-top: 16px;
    border-bottom: 1px solid #0000001a;
}

.section-header {
    margin-bottom: 16px;
}

.section-title {
    display: flex;
    align-items: center;
    gap: 12px;
}

.title-text {
    font-size: 16px;
    font-weight: bold;
    color: #333;
}

.live-btn {
    height: 32px;
    padding: 0 12px;
    border-radius: 12px;
    font-size: 14px;
    color: #000;
    border: 1px solid #0000001a;
}

/* 控制网格 */
.control-grid {
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.control-row {
    display: flex;
    gap: 12px;
}

.control-card {
    flex: 1;
    background: #f1f5f9;
    border-radius: 8px;
    padding: 16px;
    min-height: 80px;
    display: flex;
    flex-direction: column;
}

.control-card.empty-card {
    background: transparent;
}

.card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.card-title {
    font-size: 13px;
    color: #333;
    font-weight: 400;
}

.card-content {
    flex: 1;
    display: flex;
    align-items: center;
    background: #fff;
    border-radius: 4px;
}

/* 机场系统特殊样式 */
.airport-system {
    flex: 1;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #f1f5f9;
    border-radius: 8px;
    padding: 16px;
}

.airport-system .card-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
}

.status-text {
    font-size: 12px;
    color: #2ba471;
    padding: 2px 6px;
    border-radius: 3px;
    border: 1px solid #2ba471;
}

.status-label {
    padding: 6px 12px;
    font-size: 12px;
    color: #000;
    border: 1px solid #0000001a;
    border-radius: 12px;
    background: #fff;
    cursor: pointer;
}

.status-label:hover {
    background: #fcfbfb1a;
}

/* 按钮样式 */
.refresh-btn,
.power-btn,
.settings-btn {
    width: 20px;
    height: 20px;
    padding: 0;
    border-radius: 50%;
    background: transparent;
    border: none;
    color: #666;
}

.refresh-btn .el-icon,
.power-btn .el-icon,
.settings-btn .el-icon {
    font-size: 14px;
}

/* 切换按钮组 */
.toggle-buttons {
    display: flex;
    gap: 6px;
}

.toggle-btn {
    flex: 1;
    height: 36px;
    padding: 0 12px;
    font-size: 12px;
    color: #666;
    margin: 0;
    border: 0;
    border-radius: 4px;
}

.toggle-btn.active {
    background: #1e4d72;
    color: white;
}

/* 空调按钮 */
.air-condition .card-content {
    flex-wrap: wrap;
}

.ac-buttons {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
}

.ac-btn {
    height: 22px;
    padding: 0 8px;
    font-size: 11px;
    border-radius: 3px;
    background: white;
    border: 1px solid #d9d9d9;
    color: #666;
    min-width: 32px;
}

.ac-btn.active {
    background: #1e4d72;
    border-color: #1e4d72;
    color: white;
}

/* 存储卡片 */
.storage-card .card-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
}

.storage-info {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
}

.storage-usage {
    font-size: 11px;
    color: #52c41a;
    background: #f6ffed;
    padding: 2px 6px;
    border-radius: 3px;
    border: 1px solid #b7eb8f;
}

.format-btn {
    height: 20px;
    padding: 0 6px;
    font-size: 10px;
    border-radius: 3px;
    background: white;
    border: 1px solid #d9d9d9;
    color: #666;
    display: flex;
    align-items: center;
    gap: 2px;
}

.format-btn .el-icon {
    font-size: 10px;
}

/* 飞行器电源 */
.aircraft-power .card-content {
    justify-content: flex-start;
}

.power-status {
    font-size: 12px;
    color: #666;
    background: #f0f0f0;
    padding: 2px 6px;
    border-radius: 3px;
}

/* 设置卡片 */
.settings-card .card-content {
    justify-content: flex-start;
}

.settings-text {
    font-size: 12px;
    color: #666;
}

/* RTMP配置区域样式 */
.rtmp-config-grid {
    display: flex;
    gap: 16px;
    margin-bottom: 16px;
}

.rtmp-config-card {
    flex: 1;
    background: #f8fafc;
    border-radius: 8px;
    padding: 16px;
    border: 1px solid #e2e8f0;
}

.config-card-header {
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid #e2e8f0;
}

.config-card-title {
    font-size: 14px;
    font-weight: 500;
    color: #334155;
}

.config-card-content {
    :deep(.el-form-item) {
        margin-bottom: 12px;
    }
    :deep(.el-form-item:last-child) {
        margin-bottom: 0;
    }
    :deep(.el-form-item__label) {
        color: #64748b;
    }
}

.rtmp-actions {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
}

.rtmp-preview {
    background: #f1f5f9;
    border-radius: 8px;
    padding: 12px 16px;
}

.preview-title {
    font-size: 13px;
    font-weight: 500;
    color: #475569;
    margin-bottom: 8px;
}

.preview-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin-bottom: 6px;
}

.preview-item:last-child {
    margin-bottom: 0;
}

.preview-label {
    font-size: 12px;
    color: #64748b;
    white-space: nowrap;
}

.preview-url {
    font-size: 11px;
    color: #0f172a;
    background: #e2e8f0;
    padding: 2px 6px;
    border-radius: 4px;
    word-break: break-all;
    font-family: monospace;
}
</style>
