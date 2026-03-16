<template>
    <div class="aircraft-market" v-loading="loading">
        <!-- 搜索区域 -->
        <div class="search-section">
            <div class="search-input-wrapper">
                <el-input v-model="searchKeyword" placeholder="搜索飞行器" style="width: 400px" clearable size="large" :prefix-icon="Search" />
            </div>
            <div class="toolbar-actions">
                <el-button size="large" @click="downloadImportTemplate">下载导入模板</el-button>
                <el-button size="large" :loading="importing" @click="triggerImport">批量导入</el-button>
                <el-button type="primary" size="large" @click="openAddDialog">新增无人机</el-button>
            </div>
        </div>

        <input ref="importInputRef" class="hidden-file-input" type="file" accept=".csv,.xlsx,.xls" @change="handleImportChange" />

        <!-- 飞行器卡片网格 -->
        <div class="aircraft-grid" v-if="aircrafts.length > 0">
            <div v-for="aircraft in aircrafts" :key="aircraft.id" class="aircraft-card" @click="handleAircraftClick(aircraft)">
                <!-- 飞行器图片区域 -->
                <div class="aircraft-image">
                    <div class="image-placeholder"></div>
                </div>

                <div class="card-actions" v-if="isLocalAircraft(aircraft)" @click.stop>
                    <el-button link type="primary" @click.stop="openEditDialog(aircraft)">编辑</el-button>
                    <el-button link type="danger" @click.stop="deleteAircraft(aircraft)">删除</el-button>
                </div>

                <!-- 飞行器信息区域 -->
                <div class="aircraft-info">
                    <div class="aircraft-details">
                        <div class="aircraft-name">{{ aircraft.name }}</div>
                        <div class="aircraft-specs">型号 {{ aircraft.model || '--' }} · 数量 {{ aircraft.quantity ?? 1 }} 架</div>
                    </div>
                    <div class="aircraft-category-tag">
                        {{ aircraft.scenarios }}
                    </div>
                </div>
            </div>
        </div>

        <!-- 空数据提示 -->
        <EmptyState
            v-else
            :icon="searchKeyword ? 'Search' : 'Box'"
            :title="searchKeyword ? '未找到相关飞行器' : '暂无飞行器数据'"
            :description="emptyDescription"
            :show-action="!!searchKeyword"
            action-text="清除搜索"
            @action="clearSearch"
        />

        <el-dialog v-model="showFormDialog" :title="dialogTitle" width="680px" destroy-on-close>
            <el-form ref="addFormRef" :model="addForm" :rules="addRules" label-width="130px" label-position="left">
                <el-row :gutter="16">
                    <el-col :span="12">
                        <el-form-item label="无人机型号" prop="model">
                            <el-input v-model="addForm.model" placeholder="请输入无人机型号" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="数量" prop="quantity">
                            <el-input-number v-model="addForm.quantity" :min="1" :precision="0" controls-position="right" style="width: 100%" />
                        </el-form-item>
                    </el-col>

                    <el-col :span="12">
                        <el-form-item label="无人机价格(元)" prop="price">
                            <el-input-number v-model="addForm.price" :min="0" :precision="2" controls-position="right" style="width: 100%" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="最大飞行速度(m/s)" prop="max_speed">
                            <el-input-number v-model="addForm.max_speed" :min="0" :precision="2" controls-position="right" style="width: 100%" />
                        </el-form-item>
                    </el-col>

                    <el-col :span="12">
                        <el-form-item label="最大载荷质量(kg)" prop="max_payload_mass">
                            <el-input-number v-model="addForm.max_payload_mass" :min="0" :precision="2" controls-position="right" style="width: 100%" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="无人机自身质量(kg)" prop="drone_mass">
                            <el-input-number v-model="addForm.drone_mass" :min="0" :precision="2" controls-position="right" style="width: 100%" />
                        </el-form-item>
                    </el-col>

                    <el-col :span="12">
                        <el-form-item label="电池容量(Wh)" prop="battery_capacity">
                            <el-input-number v-model="addForm.battery_capacity" :min="0" :precision="0" controls-position="right" style="width: 100%" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="旋翼半径(m)" prop="rotor_radius">
                            <el-input-number v-model="addForm.rotor_radius" :min="0" :precision="2" controls-position="right" style="width: 100%" />
                        </el-form-item>
                    </el-col>

                    <el-col :span="12">
                        <el-form-item label="固件版本" prop="firmware_version">
                            <el-input v-model="addForm.firmware_version" placeholder="例如 v1.0.0" />
                        </el-form-item>
                    </el-col>
                    <el-col :span="12">
                        <el-form-item label="应用场景" prop="scenarios">
                            <el-input v-model="addForm.scenarios" placeholder="例如 物流配送" />
                        </el-form-item>
                    </el-col>
                </el-row>
            </el-form>

            <template #footer>
                <el-button @click="showFormDialog = false">取消</el-button>
                <el-button type="primary" @click="submitForm">{{ dialogMode === 'add' ? '保存' : '保存修改' }}</el-button>
            </template>
        </el-dialog>
    </div>
</template>

<script lang="ts" setup>
import type { FormInstance, FormRules } from 'element-plus'
import { ref, computed, onMounted, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import EmptyState from '/@/components/emptyState/index.vue'
import { baTableApi } from '/@/api/common'
import * as XLSX from 'xlsx'

const router = useRouter()
const searchKeyword = ref('')
const loading = ref(false)
const importing = ref(false)

const api = new baTableApi('/admin/equipment.Aircraft/')
const localStorageKey = 'customAircraftList'

interface Aircraft {
    id: number | string
    name: string
    model?: string
    quantity?: number
    price?: number
    max_speed?: number
    max_payload_mass?: number
    drone_mass?: number
    battery_capacity?: number
    rotor_radius?: number
    scenarios: string
    pic: string
    create_time: number
    update_time: number
    content: string
    firmware_version: string
}

interface AircraftForm {
    model: string
    quantity: number
    price: number
    max_speed: number
    max_payload_mass: number
    drone_mass: number
    battery_capacity: number
    rotor_radius: number
    firmware_version: string
    scenarios: string
}

const showFormDialog = ref(false)
const dialogMode = ref<'add' | 'edit'>('add')
const editingAircraftId = ref<string | number>('')
const dialogTitle = computed(() => (dialogMode.value === 'add' ? '新增无人机' : '编辑无人机参数'))
const addFormRef = ref<FormInstance>()
const importInputRef = ref<HTMLInputElement>()
const remoteAircrafts = ref<Aircraft[]>([])
const customAircrafts = ref<Aircraft[]>([])

const addForm = reactive<AircraftForm>({
    model: '',
    quantity: 1,
    price: 0,
    max_speed: 0,
    max_payload_mass: 0,
    drone_mass: 0,
    battery_capacity: 0,
    rotor_radius: 0,
    firmware_version: '',
    scenarios: '物流配送',
})

const addRules: FormRules = {
    model: [{ required: true, message: '请输入无人机型号', trigger: 'blur' }],
    quantity: [{ required: true, type: 'number', min: 1, message: '数量至少为 1', trigger: 'change' }],
    price: [{ required: true, type: 'number', min: 0, message: '价格不能小于 0', trigger: 'change' }],
    max_speed: [{ required: true, type: 'number', min: 0, message: '最大飞行速度不能小于 0', trigger: 'change' }],
    max_payload_mass: [{ required: true, type: 'number', min: 0, message: '最大载荷质量不能小于 0', trigger: 'change' }],
    drone_mass: [{ required: true, type: 'number', min: 0, message: '无人机自身质量不能小于 0', trigger: 'change' }],
    battery_capacity: [{ required: true, type: 'number', min: 0, message: '电池容量不能小于 0', trigger: 'change' }],
    rotor_radius: [{ required: true, type: 'number', min: 0, message: '旋翼半径不能小于 0', trigger: 'change' }],
}

const aircrafts = computed(() => {
    const keyword = searchKeyword.value.trim().toLowerCase()
    const mergedList = [...customAircrafts.value, ...remoteAircrafts.value]

    if (!keyword) return mergedList
    return mergedList.filter((item) => {
        return [item.name, item.model, item.scenarios, item.firmware_version].some((field) =>
            String(field ?? '')
                .toLowerCase()
                .includes(keyword)
        )
    })
})

const loadCustomAircrafts = () => {
    const raw = localStorage.getItem(localStorageKey)
    if (!raw) {
        customAircrafts.value = []
        return
    }
    try {
        const parsed = JSON.parse(raw)
        customAircrafts.value = Array.isArray(parsed) ? parsed : []
    } catch {
        customAircrafts.value = []
    }
}

const saveCustomAircrafts = () => {
    localStorage.setItem(localStorageKey, JSON.stringify(customAircrafts.value))
}

const resetAddForm = () => {
    addForm.model = ''
    addForm.quantity = 1
    addForm.price = 0
    addForm.max_speed = 0
    addForm.max_payload_mass = 0
    addForm.drone_mass = 0
    addForm.battery_capacity = 0
    addForm.rotor_radius = 0
    addForm.firmware_version = ''
    addForm.scenarios = '物流配送'
}

const openAddDialog = () => {
    dialogMode.value = 'add'
    editingAircraftId.value = ''
    resetAddForm()
    showFormDialog.value = true
}

const isLocalAircraft = (aircraft: Aircraft) => {
    return String(aircraft.id).startsWith('local-')
}

const openEditDialog = (aircraft: Aircraft) => {
    if (!isLocalAircraft(aircraft)) {
        ElMessage.info('当前仅支持编辑本地新增的无人机参数')
        return
    }
    dialogMode.value = 'edit'
    editingAircraftId.value = aircraft.id
    addForm.model = aircraft.model || aircraft.name || ''
    addForm.quantity = aircraft.quantity ?? 1
    addForm.price = aircraft.price ?? 0
    addForm.max_speed = aircraft.max_speed ?? 0
    addForm.max_payload_mass = aircraft.max_payload_mass ?? 0
    addForm.drone_mass = aircraft.drone_mass ?? 0
    addForm.battery_capacity = aircraft.battery_capacity ?? 0
    addForm.rotor_radius = aircraft.rotor_radius ?? 0
    addForm.firmware_version = aircraft.firmware_version || 'v1.0.0'
    addForm.scenarios = aircraft.scenarios || '物流配送'
    showFormDialog.value = true
}

const submitForm = async () => {
    if (!addFormRef.value) return
    await addFormRef.value.validate()

    const now = Math.floor(Date.now() / 1000)
    if (dialogMode.value === 'add') {
        const localAircraft: Aircraft = {
            id: `local-${Date.now()}`,
            name: addForm.model,
            model: addForm.model,
            quantity: addForm.quantity,
            price: addForm.price,
            max_speed: addForm.max_speed,
            max_payload_mass: addForm.max_payload_mass,
            drone_mass: addForm.drone_mass,
            battery_capacity: addForm.battery_capacity,
            rotor_radius: addForm.rotor_radius,
            scenarios: addForm.scenarios,
            pic: '',
            create_time: now,
            update_time: now,
            content: '',
            firmware_version: addForm.firmware_version || 'v1.0.0',
        }
        customAircrafts.value.unshift(localAircraft)
        ElMessage.success('无人机已添加')
    } else {
        const index = customAircrafts.value.findIndex((item) => String(item.id) === String(editingAircraftId.value))
        if (index < 0) {
            ElMessage.error('未找到待编辑的无人机数据')
            return
        }
        const source = customAircrafts.value[index]
        customAircrafts.value[index] = {
            ...source,
            name: addForm.model,
            model: addForm.model,
            quantity: addForm.quantity,
            price: addForm.price,
            max_speed: addForm.max_speed,
            max_payload_mass: addForm.max_payload_mass,
            drone_mass: addForm.drone_mass,
            battery_capacity: addForm.battery_capacity,
            rotor_radius: addForm.rotor_radius,
            scenarios: addForm.scenarios,
            firmware_version: addForm.firmware_version || 'v1.0.0',
            update_time: now,
        }
        ElMessage.success('无人机参数已更新')
    }

    saveCustomAircrafts()
    showFormDialog.value = false
    resetAddForm()
}

const deleteAircraft = async (aircraft: Aircraft) => {
    if (!isLocalAircraft(aircraft)) {
        ElMessage.info('当前仅支持删除本地新增的无人机数据')
        return
    }
    try {
        await ElMessageBox.confirm(`确认删除无人机 ${aircraft.name} 吗？`, '提示', {
            type: 'warning',
            confirmButtonText: '删除',
            cancelButtonText: '取消',
        })
        customAircrafts.value = customAircrafts.value.filter((item) => String(item.id) !== String(aircraft.id))
        saveCustomAircrafts()
        ElMessage.success('删除成功')
    } catch {
        // 用户取消
    }
}

const triggerImport = () => {
    importInputRef.value?.click()
}

const parseNumber = (value: unknown, fallback = 0) => {
    if (value === null || value === undefined || value === '') return fallback
    const normalized = String(value).replace(/[^\d.-]/g, '')
    const num = Number(normalized)
    return Number.isFinite(num) ? num : fallback
}

const pickField = (row: Record<string, unknown>, keys: string[]) => {
    for (const key of keys) {
        if (Object.prototype.hasOwnProperty.call(row, key)) {
            const val = row[key]
            if (val !== null && val !== undefined && String(val).trim() !== '') {
                return val
            }
        }
    }
    return ''
}

const mapImportRow = (row: Record<string, unknown>, index: number): Aircraft | null => {
    const modelValue = String(pickField(row, ['无人机型号', '型号', 'model'])).trim()
    if (!modelValue) return null

    const quantity = Math.max(1, Math.round(parseNumber(pickField(row, ['数量', 'quantity']), 1)))
    const price = parseNumber(pickField(row, ['无人机价格', '价格', 'price']), 0)
    const maxSpeed = parseNumber(pickField(row, ['最大飞行速度', 'max_speed']), 0)
    const maxPayloadMass = parseNumber(pickField(row, ['最大载荷质量', 'max_payload_mass']), 0)
    const droneMass = parseNumber(pickField(row, ['无人机自身质量', '自身质量', 'drone_mass']), 0)
    const batteryCapacity = parseNumber(pickField(row, ['电池容量', 'battery_capacity']), 0)
    const rotorRadius = parseNumber(pickField(row, ['旋翼半径', 'rotor_radius']), 0)
    const firmwareVersion = String(pickField(row, ['固件版本', 'firmware_version'])).trim() || 'v1.0.0'
    const scenarios = String(pickField(row, ['应用场景', '场景', 'scenarios'])).trim() || '物流配送'

    const now = Math.floor(Date.now() / 1000)
    return {
        id: `local-${Date.now()}-${index}`,
        name: modelValue,
        model: modelValue,
        quantity,
        price,
        max_speed: maxSpeed,
        max_payload_mass: maxPayloadMass,
        drone_mass: droneMass,
        battery_capacity: batteryCapacity,
        rotor_radius: rotorRadius,
        scenarios,
        pic: '',
        create_time: now,
        update_time: now,
        content: '',
        firmware_version: firmwareVersion,
    }
}

const handleImportChange = async (event: Event) => {
    const input = event.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return

    importing.value = true
    try {
        const ext = file.name.split('.').pop()?.toLowerCase()
        if (!ext || !['csv', 'xlsx', 'xls'].includes(ext)) {
            ElMessage.error('仅支持导入 CSV、XLSX、XLS 文件')
            return
        }

        const workbook = ext === 'csv' ? XLSX.read(await file.text(), { type: 'string' }) : XLSX.read(await file.arrayBuffer(), { type: 'array' })

        const sheetName = workbook.SheetNames[0]
        const sheet = workbook.Sheets[sheetName]
        const rows = XLSX.utils.sheet_to_json<Record<string, unknown>>(sheet, { defval: '' })

        if (!rows.length) {
            ElMessage.warning('导入文件为空或缺少数据行')
            return
        }

        const imported: Aircraft[] = []
        const failedRows: number[] = []

        rows.forEach((row, idx) => {
            const mapped = mapImportRow(row, idx)
            if (mapped) {
                imported.push(mapped)
            } else {
                failedRows.push(idx + 2)
            }
        })

        if (imported.length) {
            customAircrafts.value = [...imported.reverse(), ...customAircrafts.value]
            saveCustomAircrafts()
        }

        ElMessage.success(`导入完成：成功 ${imported.length} 条，失败 ${failedRows.length} 条`)
        if (failedRows.length) {
            ElMessage.warning(`失败行号：${failedRows.join(', ')}`)
        }
    } catch {
        ElMessage.error('导入失败，请检查文件格式和字段')
    } finally {
        importing.value = false
        input.value = ''
    }
}

const downloadImportTemplate = () => {
    const headers = ['无人机型号', '数量', '无人机价格', '最大飞行速度', '最大载荷质量', '无人机自身质量', '电池容量', '旋翼半径', '固件版本', '应用场景']
    const sample = ['物流机A1', '10', '120000', '18', '40', '52', '2000', '0.42', 'v1.0.0', '物流配送']
    const csv = [headers.join(','), sample.join(',')].join('\n')
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = '无人机参数导入模板.csv'
    a.click()
    URL.revokeObjectURL(url)
}

// 空数据描述
const emptyDescription = computed(() => {
    if (searchKeyword.value) {
        return `没有找到包含"${searchKeyword.value}"的飞行器，请尝试其他关键词`
    }
    return '当前飞行器市场暂无可用产品，请稍后再试'
})

// 清除搜索
const clearSearch = () => {
    searchKeyword.value = ''
}

// 处理飞行器点击
const handleAircraftClick = (aircraft: any) => {
    router.push(`/admin/equipment/aircraft/detail?id=${aircraft.id}`)
}

onMounted(() => {
    loadCustomAircrafts()
    loading.value = true
    api.index()
        .then((res) => {
            remoteAircrafts.value = (res.data.list || []).map((item: Aircraft) => {
                return {
                    ...item,
                    model: item.model || item.name,
                    quantity: item.quantity ?? 1,
                    pic: item.pic ? import.meta.env.VITE_AXIOS_BASE_URL + item.pic : '',
                }
            })
        })
        .catch(() => {
            remoteAircrafts.value = []
        })
        .finally(() => {
            loading.value = false
        })
})
</script>

<style scoped lang="scss">
:deep(.el-input__wrapper) {
    box-shadow: none !important;
    background: #f7f7f7;
    border-radius: 12px;
}

.page-header {
    display: flex;
    align-items: center;

    .page-title {
        font-size: 24px;
        font-weight: 600;
        color: #000000;
        margin: 0;
    }
}

.aircraft-market {
    padding: 15px;
    background: #fff;
    height: 100%;
}

.search-section {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}

.toolbar-actions {
    display: flex;
    align-items: center;
    gap: 8px;
}

.hidden-file-input {
    display: none;
}

.search-input-wrapper {
    width: 400px;
    margin-top: 20px;
}

.search-input {
    width: 100%;
}

.aircraft-grid {
    padding: 28px 0;
    display: grid;
    grid-template-columns: repeat(6);
    gap: 16px;
}

.aircraft-card {
    width: 100%;
    height: 231px;
    border-radius: 10px;
    border: 1px solid rgba(0, 0, 0, 0.1);
    overflow: hidden;
    cursor: pointer;
    transition: all 0.3s ease;
    background: white;
    position: relative;
}

.aircraft-card:hover {
    transform: translateY(-8px);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
    border-color: rgba(0, 0, 0, 0.2);
}

.aircraft-image {
    width: 100%;
    height: 164px;
    position: relative;
}

.image-placeholder {
    width: 100%;
    height: 100%;
    background: #ba7171;
}

.aircraft-info {
    height: 61px;
    background: white;
    border-top: 1px solid rgba(0, 0, 0, 0.1);
    padding: 10px;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
}

.card-actions {
    position: absolute;
    top: 8px;
    right: 8px;
    display: flex;
    align-items: center;
    gap: 2px;
    border-radius: 8px;
    padding: 2px 6px;
    background: rgba(255, 255, 255, 0.92);
    z-index: 4;
}

.aircraft-details {
    flex: 1;
}

.aircraft-name {
    font-size: 14px;
    font-family: 'PingFang SC', sans-serif;
    font-weight: 400;
    color: #000;
    margin-bottom: 4px;
    line-height: 1.2;
}

.aircraft-specs {
    font-size: 12px;
    font-family: 'PingFang SC', sans-serif;
    font-weight: 400;
    color: rgba(0, 0, 0, 0.4);
    line-height: 1.2;
}

.aircraft-category-tag {
    width: 5em;
    height: 21px;
    border: 1px solid #00386d;
    border-radius: 4px;
    font-size: 12px;
    font-family: 'PingFang SC', sans-serif;
    font-weight: 400;
    color: #00386d;
    padding: 0 4px;
    flex-shrink: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* 响应式设计 */
@media screen and (max-width: 1920px) {
    .aircraft-grid {
        grid-template-columns: repeat(auto-fit, 280px);
        justify-content: flex-start;
    }
}

@media screen and (max-width: 768px) {
    .aircraft-grid {
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 16px;
        padding: 16px;
    }
}
:deep(.el-input__icon) {
    color: #000000;
}
</style>
