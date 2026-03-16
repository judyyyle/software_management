<template>
    <el-dialog
        :model-value="['Setting'].includes(baTable.form.operate!)"
        title=""
        width="800px"
        @close="close"
        class="aircraft-detail-dialog"
        :close-on-click-modal="false"
        align-center
    >
        <!-- 使用 Element Plus Tabs 作为标题栏 -->
        <template #header>
            <div class="dialog-header">
                <el-tabs v-model="activeTab" class="dialog-tabs">
                    <el-tab-pane label="状态" name="status"></el-tab-pane>
                    <el-tab-pane label="配置" name="config"></el-tab-pane>
                </el-tabs>
            </div>
        </template>

        <!-- 弹窗内容 -->
        <div class="dialog-content">
            <!-- 状态标签页 -->
            <div v-if="activeTab === 'status'">
                <StatusSection />
            </div>

            <!-- 配置标签页 -->
            <div v-else-if="activeTab === 'config'">
                <ConfigSection />
            </div>
        </div>
    </el-dialog>
</template>

<script lang="ts" setup>
import { ref, computed, inject, watch, onMounted } from 'vue'
import StatusSection from '../components/StatusSection.vue'
import ConfigSection from '../components/ConfigSection.vue'
import type baTableClass from '/@/utils/baTable'
import { DJIoperations } from '/@/utils/mqttSdk'
import { useMqttStore } from '/@/stores/mqtt'

const baTable = inject('baTable') as baTableClass

watch(
    () => baTable.form.operate,
    (newVal: string | undefined) => {
        if (newVal === 'Setting') {
            DJIoperations.osd(baTable.form.operateIds![0])
            mqttStore.gateway_sn = baTable.form.operateIds![0]
            // 订阅设备服务回复
            DJIoperations.deviceServicesReply(baTable.form.operateIds![0])
        }
    }
)

const mqttStore = useMqttStore()

const messages = computed(() => mqttStore.getMessagesByTopic(`thing/product/${baTable.form.operateIds![0]}/osd`))

watch(messages, (newVal: any) => {
    if (newVal.payload) {
        const sn = baTable.form.operateIds![0]
        // payload 可能是字符串或已解析的对象
        const parsed = typeof newVal.payload === 'string' ? JSON.parse(newVal.payload) : newVal.payload
        const data = parsed.data
        // 更新 deviceOsds 中对应设备的数据
        if (data && mqttStore.deviceOsds[sn]) {
            mqttStore.deviceOsds[sn] = {
                ...mqttStore.deviceOsds[sn],
                ...data,
            }
        }
    }
})

const activeTab = ref('status')

const close = () => {
    DJIoperations.osdClose(baTable.form.operateIds![0])
    DJIoperations.deviceServicesReplyClose(baTable.form.operateIds![0])
    baTable.toggleForm()
}
</script>

<style scoped>
/* 弹窗样式重置 */
:deep(.aircraft-detail-dialog) {
    border-radius: 12px;
    overflow: hidden;
}

/* 使用 Element Plus Tabs 的标题栏 */
.dialog-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: white;
}

.dialog-tabs {
    flex: 1;
}

:deep(.dialog-tabs .el-tabs__item) {
    padding: 8px 0;
    margin-right: 40px;
    border: none;
    background: transparent;
    color: rgba(0, 0, 0, 0.4);
    font-size: 24px;
    font-weight: 600;
    height: auto;
    line-height: 1;
}

:deep(.dialog-tabs .el-tabs__item.is-active) {
    color: #000;
    font-weight: 600;
}

:deep(.dialog-tabs .el-tabs__active-bar) {
    height: 2px;
    background-color: #000;
}
</style>
