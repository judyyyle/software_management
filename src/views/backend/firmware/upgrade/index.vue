<template>
    <div class="default-main ba-table-box">
        <el-card shadow="never">
            <div class="table-header">
                <div class="table-header-left">
                    <el-button type="primary" @click="handleCreate">
                        <el-icon><Plus /></el-icon>
                        创建升级任务
                    </el-button>
                    <el-button type="danger" :disabled="!selectedIds.length" @click="handleBatchDelete">
                        <el-icon><Delete /></el-icon>
                        批量删除
                    </el-button>
                </div>
                <div class="table-header-right">
                    <el-input v-model="searchKeyword" placeholder="搜索任务ID/设备SN" clearable style="width: 200px" @keyup.enter="loadData">
                        <template #prefix><el-icon><Search /></el-icon></template>
                    </el-input>
                    <el-select v-model="filterStatus" placeholder="状态" clearable style="width: 120px; margin-left: 10px" @change="loadData">
                        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
                    </el-select>
                    <el-button style="margin-left: 10px" @click="loadData"><el-icon><Refresh /></el-icon></el-button>
                </div>
            </div>

            <el-table :data="tableData" v-loading="loading" @selection-change="handleSelectionChange" stripe>
                <el-table-column type="selection" width="50" />
                <el-table-column prop="id" label="ID" width="70" />
                <el-table-column prop="task_id" label="任务ID" width="280" show-overflow-tooltip />
                <el-table-column prop="gateway_sn" label="机场SN" width="160" />
                <el-table-column prop="device_sn" label="无人机SN" width="180" />
                <el-table-column prop="upgrade_type_text" label="升级类型" width="100" />
                <el-table-column label="目标版本" width="150">
                    <template #default="{ row }">
                        <div v-if="row.dock_version">机场: {{ row.dock_version }}</div>
                        <div v-if="row.drone_version">无人机: {{ row.drone_version }}</div>
                    </template>
                </el-table-column>
                <el-table-column prop="status" label="状态" width="100">
                    <template #default="{ row }">
                        <el-tag :type="getStatusType(row.status)">{{ row.status_text }}</el-tag>
                    </template>
                </el-table-column>
                <el-table-column label="进度" width="180">
                    <template #default="{ row }">
                        <el-progress :percentage="row.progress" :status="getProgressStatus(row.status)" :stroke-width="10" />
                        <div class="progress-step" v-if="row.current_step_text">{{ row.current_step_text }}</div>
                    </template>
                </el-table-column>
                <el-table-column prop="create_time" label="创建时间" width="170" />
                <el-table-column label="操作" width="100" fixed="right">
                    <template #default="{ row }">
                        <el-button type="danger" link :disabled="!canDelete(row)" @click="handleDelete(row)">删除</el-button>
                    </template>
                </el-table-column>
            </el-table>

            <div class="table-footer">
                <el-pagination v-model:current-page="pagination.page" v-model:page-size="pagination.limit" :total="pagination.total"
                    :page-sizes="[10, 20, 50, 100]" layout="total, sizes, prev, pager, next, jumper" @size-change="loadData" @current-change="loadData" />
            </div>
        </el-card>

        <!-- 创建升级任务弹窗 -->
        <el-dialog v-model="dialogVisible" title="创建升级任务" width="600px" destroy-on-close>
            <el-form ref="formRef" :model="formData" :rules="formRules" label-width="100px">
                <el-form-item label="选择机场" prop="gateway_sn">
                    <el-select v-model="formData.gateway_sn" placeholder="请选择机场" filterable @change="handleDeviceChange">
                        <el-option v-for="item in deviceList" :key="item.gateway_sn" :label="`${item.device_name} (${item.gateway_sn})`" :value="item.gateway_sn" />
                    </el-select>
                </el-form-item>
                <el-form-item label="升级类型" prop="upgrade_type">
                    <el-radio-group v-model="formData.upgrade_type">
                        <el-radio :value="3">普通升级</el-radio>
                        <el-radio :value="2">一致性升级</el-radio>
                    </el-radio-group>
                </el-form-item>
                <el-form-item label="机场固件">
                    <el-select v-model="formData.dock_firmware_id" placeholder="选择机场固件版本" clearable>
                        <el-option v-for="item in dockFirmwareList" :key="item.id" :label="`${item.version} (${item.device_model})`" :value="item.id" />
                    </el-select>
                </el-form-item>
                <el-form-item label="无人机固件">
                    <el-select v-model="formData.drone_firmware_id" placeholder="选择无人机固件版本" clearable>
                        <el-option v-for="item in droneFirmwareList" :key="item.id" :label="`${item.version} (${item.device_model})`" :value="item.id" />
                    </el-select>
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="dialogVisible = false">取消</el-button>
                <el-button type="primary" :loading="submitLoading" @click="handleSubmit">开始升级</el-button>
            </template>
        </el-dialog>
    </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, computed } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'
import { Plus, Delete, Search, Refresh } from '@element-plus/icons-vue'
import { getUpgradeTaskList, createUpgradeTask, delUpgradeTask, getAvailableFirmware, statusOptions, type UpgradeTask, type Firmware } from '/@/api/backend/firmware'
import { useMqttStore } from '/@/stores/mqtt'

const loading = ref(false)
const tableData = ref<UpgradeTask[]>([])
const selectedIds = ref<number[]>([])
const searchKeyword = ref('')
const filterStatus = ref('')

const pagination = reactive({ page: 1, limit: 10, total: 0 })

// 弹窗相关
const dialogVisible = ref(false)
const submitLoading = ref(false)
const formRef = ref<FormInstance>()

const formData = reactive({
    gateway_sn: '',
    device_sn: '',
    upgrade_type: 3,
    dock_firmware_id: null as number | null,
    drone_firmware_id: null as number | null,
})

const formRules = {
    gateway_sn: [{ required: true, message: '请选择机场', trigger: 'change' }],
    upgrade_type: [{ required: true, message: '请选择升级类型', trigger: 'change' }],
}

// 从 MQTT store 获取设备列表
const mqttStore = useMqttStore()
const deviceList = computed(() => {
    // 从 deviceOsds 获取在线设备
    const devices: any[] = []
    for (const [sn, data] of Object.entries(mqttStore.deviceOsds)) {
        if (data && Object.keys(data).length > 0) {
            devices.push({
                gateway_sn: sn,
                device_name: (data as any).nickname || sn,
                firmware_version: (data as any).firmware_version || '',
            })
        }
    }
    return devices
})

const dockFirmwareList = ref<Firmware[]>([])
const droneFirmwareList = ref<Firmware[]>([])

let refreshTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
    loadData()
    loadFirmwareList()
    // 自动刷新进行中的任务
    refreshTimer = setInterval(() => {
        if (tableData.value.some(t => ['sent', 'in_progress'].includes(t.status))) {
            loadData()
        }
    }, 5000)
})

onUnmounted(() => {
    if (refreshTimer) clearInterval(refreshTimer)
})

const loadData = async () => {
    loading.value = true
    try {
        const res = await getUpgradeTaskList({
            page: pagination.page,
            limit: pagination.limit,
            quickSearch: searchKeyword.value,
            status: filterStatus.value,
            order: 'id',
            sort: 'desc',
        })
        if (res.code === 1) {
            tableData.value = res.data.list
            pagination.total = res.data.total
        }
    } finally {
        loading.value = false
    }
}

const loadFirmwareList = async () => {
    const [dockRes, droneRes] = await Promise.all([
        getAvailableFirmware({ device_type: 'dock' }),
        getAvailableFirmware({ device_type: 'drone' }),
    ])
    if (dockRes.code === 1) dockFirmwareList.value = dockRes.data
    if (droneRes.code === 1) droneFirmwareList.value = droneRes.data
}

const handleSelectionChange = (rows: UpgradeTask[]) => {
    selectedIds.value = rows.map(r => r.id)
}

const handleDeviceChange = () => {
    // 从 droneOsds 获取对应的无人机 SN
    const droneOsds = mqttStore.droneOsds
    for (const [sn, data] of Object.entries(droneOsds)) {
        if (data && Object.keys(data).length > 0) {
            formData.device_sn = sn
            break
        }
    }
}

const getStatusType = (status: string) => {
    const item = statusOptions.find(s => s.value === status)
    return (item?.type || 'info') as 'success' | 'warning' | 'info' | 'danger'
}

const getProgressStatus = (status: string) => {
    if (status === 'ok') return 'success'
    if (status === 'failed') return 'exception'
    return undefined
}

const canDelete = (row: UpgradeTask) => {
    return ['ok', 'failed', 'canceled', 'rejected', 'timeout'].includes(row.status)
}

const handleCreate = () => {
    formData.gateway_sn = ''
    formData.device_sn = ''
    formData.upgrade_type = 3
    formData.dock_firmware_id = null
    formData.drone_firmware_id = null
    dialogVisible.value = true
}

const handleSubmit = async () => {
    if (!formRef.value) return
    await formRef.value.validate()

    if (!formData.dock_firmware_id && !formData.drone_firmware_id) {
        ElMessage.warning('请至少选择一个固件版本')
        return
    }

    submitLoading.value = true
    try {
        const res = await createUpgradeTask({
            gateway_sn: formData.gateway_sn,
            device_sn: formData.device_sn || undefined,
            upgrade_type: formData.upgrade_type,
            dock_firmware_id: formData.dock_firmware_id || undefined,
            drone_firmware_id: formData.drone_firmware_id || undefined,
        })
        if (res.code === 1) {
            ElMessage.success('升级任务已下发')
            dialogVisible.value = false
            loadData()
        }
    } finally {
        submitLoading.value = false
    }
}

const handleDelete = async (row: UpgradeTask) => {
    await ElMessageBox.confirm('确定要删除该任务记录吗？', '提示', { type: 'warning' })
    const res = await delUpgradeTask([row.id])
    if (res.code === 1) {
        ElMessage.success('删除成功')
        loadData()
    }
}

const handleBatchDelete = async () => {
    await ElMessageBox.confirm(`确定要删除选中的 ${selectedIds.value.length} 个任务吗？`, '提示', { type: 'warning' })
    const res = await delUpgradeTask(selectedIds.value)
    if (res.code === 1) {
        ElMessage.success('删除成功')
        loadData()
    }
}
</script>

<style scoped lang="scss">
.table-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    &-left, &-right { display: flex; align-items: center; }
}
.table-footer {
    display: flex;
    justify-content: flex-end;
    margin-top: 16px;
}
.progress-step {
    font-size: 12px;
    color: #909399;
    margin-top: 4px;
}
</style>
