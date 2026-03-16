<template>
    <div class="default-main ba-table-box">
        <el-card shadow="never">
            <div class="table-header">
                <div class="table-header-left">
                    <el-button type="primary" @click="handleAdd">
                        <el-icon><Plus /></el-icon>
                        添加固件
                    </el-button>
                    <el-button type="danger" :disabled="!selectedIds.length" @click="handleBatchDelete">
                        <el-icon><Delete /></el-icon>
                        批量删除
                    </el-button>
                </div>
                <div class="table-header-right">
                    <el-input v-model="searchKeyword" placeholder="搜索版本号/文件名" clearable style="width: 200px" @keyup.enter="loadData">
                        <template #prefix>
                            <el-icon><Search /></el-icon>
                        </template>
                    </el-input>
                    <el-select v-model="filterDeviceType" placeholder="设备类型" clearable style="width: 120px; margin-left: 10px" @change="loadData">
                        <el-option v-for="item in deviceTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
                    </el-select>
                </div>
            </div>

            <el-table :data="tableData" v-loading="loading" @selection-change="handleSelectionChange" stripe>
                <el-table-column type="selection" width="50" />
                <el-table-column prop="id" label="ID" width="80" />
                <el-table-column prop="device_type" label="设备类型" width="100">
                    <template #default="{ row }">
                        <el-tag :type="row.device_type === 'dock' ? 'primary' : 'success'">
                            {{ row.device_type_text }}
                        </el-tag>
                    </template>
                </el-table-column>
                <el-table-column prop="device_model" label="设备型号" width="120" />
                <el-table-column prop="version" label="版本号" width="120" />
                <el-table-column prop="file_name" label="文件名" min-width="200" show-overflow-tooltip />
                <el-table-column prop="file_size_text" label="文件大小" width="100" />
                <el-table-column prop="status" label="状态" width="80">
                    <template #default="{ row }">
                        <el-tag :type="row.status === 1 ? 'success' : 'info'">
                            {{ row.status === 1 ? '启用' : '禁用' }}
                        </el-tag>
                    </template>
                </el-table-column>
                <el-table-column prop="create_time" label="创建时间" width="170" />
                <el-table-column label="操作" width="150" fixed="right">
                    <template #default="{ row }">
                        <el-button type="primary" link @click="handleEdit(row)">编辑</el-button>
                        <el-button type="danger" link @click="handleDelete(row)">删除</el-button>
                    </template>
                </el-table-column>
            </el-table>

            <div class="table-footer">
                <el-pagination
                    v-model:current-page="pagination.page"
                    v-model:page-size="pagination.limit"
                    :total="pagination.total"
                    :page-sizes="[10, 20, 50, 100]"
                    layout="total, sizes, prev, pager, next, jumper"
                    @size-change="loadData"
                    @current-change="loadData"
                />
            </div>
        </el-card>

        <!-- 添加/编辑弹窗 -->
        <el-dialog v-model="dialogVisible" :title="dialogTitle" width="600px" destroy-on-close>
            <el-form ref="formRef" :model="formData" :rules="formRules" label-width="100px">
                <el-form-item label="设备类型" prop="device_type">
                    <el-select v-model="formData.device_type" placeholder="请选择" @change="handleDeviceTypeChange">
                        <el-option v-for="item in deviceTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
                    </el-select>
                </el-form-item>
                <el-form-item label="设备型号" prop="device_model">
                    <el-select v-model="formData.device_model" placeholder="请选择">
                        <el-option v-for="item in currentModelOptions" :key="item.value" :label="item.label" :value="item.value" />
                    </el-select>
                </el-form-item>
                <el-form-item label="版本号" prop="version">
                    <el-input v-model="formData.version" placeholder="如: 1.00.223" />
                </el-form-item>
                <el-form-item label="固件文件" prop="file_url">
                    <el-input v-model="formData.file_url" placeholder="OSS文件地址">
                        <template #append>
                            <el-button @click="handleUpload">上传</el-button>
                        </template>
                    </el-input>
                </el-form-item>
                <el-form-item label="文件名" prop="file_name">
                    <el-input v-model="formData.file_name" placeholder="固件文件名" />
                </el-form-item>
                <el-form-item label="文件大小" prop="file_size">
                    <el-input-number v-model="formData.file_size" :min="0" placeholder="字节" />
                </el-form-item>
                <el-form-item label="MD5" prop="md5">
                    <el-input v-model="formData.md5" placeholder="文件MD5校验值" />
                </el-form-item>
                <el-form-item label="更新说明" prop="release_note">
                    <el-input v-model="formData.release_note" type="textarea" :rows="3" placeholder="版本更新说明" />
                </el-form-item>
                <el-form-item label="状态" prop="status">
                    <el-switch v-model="formData.status" :active-value="1" :inactive-value="0" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="dialogVisible = false">取消</el-button>
                <el-button type="primary" :loading="submitLoading" @click="handleSubmit">确定</el-button>
            </template>
        </el-dialog>
    </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'
import { Plus, Delete, Search } from '@element-plus/icons-vue'
import { getFirmwareList, addFirmware, editFirmware, delFirmware, deviceTypeOptions, deviceModelOptions, type Firmware } from '/@/api/backend/firmware'

const loading = ref(false)
const tableData = ref<Firmware[]>([])
const selectedIds = ref<number[]>([])
const searchKeyword = ref('')
const filterDeviceType = ref('')

const pagination = reactive({
    page: 1,
    limit: 10,
    total: 0,
})

// 弹窗相关
const dialogVisible = ref(false)
const dialogTitle = ref('添加固件')
const submitLoading = ref(false)
const formRef = ref<FormInstance>()
const editingId = ref<number | null>(null)

const formData = reactive({
    device_type: '' as 'dock' | 'drone' | '',
    device_model: '',
    version: '',
    file_url: '',
    file_name: '',
    file_size: 0,
    md5: '',
    release_note: '',
    status: 1,
})

const formRules = {
    device_type: [{ required: true, message: '请选择设备类型', trigger: 'change' }],
    device_model: [{ required: true, message: '请选择设备型号', trigger: 'change' }],
    version: [{ required: true, message: '请输入版本号', trigger: 'blur' }],
    file_url: [{ required: true, message: '请输入固件文件地址', trigger: 'blur' }],
    file_name: [{ required: true, message: '请输入文件名', trigger: 'blur' }],
}

const currentModelOptions = computed(() => {
    if (!formData.device_type) return []
    return deviceModelOptions[formData.device_type] || []
})

onMounted(() => {
    loadData()
})

const loadData = async () => {
    loading.value = true
    try {
        const res = await getFirmwareList({
            page: pagination.page,
            limit: pagination.limit,
            quickSearch: searchKeyword.value,
            device_type: filterDeviceType.value,
        })
        if (res.code === 1) {
            tableData.value = res.data.list
            pagination.total = res.data.total
        }
    } finally {
        loading.value = false
    }
}

const handleSelectionChange = (rows: Firmware[]) => {
    selectedIds.value = rows.map((r) => r.id)
}

const handleDeviceTypeChange = () => {
    formData.device_model = ''
}

const resetForm = () => {
    formData.device_type = ''
    formData.device_model = ''
    formData.version = ''
    formData.file_url = ''
    formData.file_name = ''
    formData.file_size = 0
    formData.md5 = ''
    formData.release_note = ''
    formData.status = 1
    editingId.value = null
}

const handleAdd = () => {
    resetForm()
    dialogTitle.value = '添加固件'
    dialogVisible.value = true
}

const handleEdit = (row: Firmware) => {
    resetForm()
    dialogTitle.value = '编辑固件'
    editingId.value = row.id
    Object.assign(formData, {
        device_type: row.device_type,
        device_model: row.device_model,
        version: row.version,
        file_url: row.file_url,
        file_name: row.file_name,
        file_size: row.file_size,
        md5: row.md5,
        release_note: row.release_note,
        status: row.status,
    })
    dialogVisible.value = true
}

const handleSubmit = async () => {
    if (!formRef.value) return
    await formRef.value.validate()

    submitLoading.value = true
    try {
        const data = { ...formData }
        if (editingId.value) {
            const res = await editFirmware({ id: editingId.value, ...data })
            if (res.code === 1) {
                ElMessage.success('更新成功')
                dialogVisible.value = false
                loadData()
            }
        } else {
            const res = await addFirmware(data)
            if (res.code === 1) {
                ElMessage.success('添加成功')
                dialogVisible.value = false
                loadData()
            }
        }
    } finally {
        submitLoading.value = false
    }
}

const handleDelete = async (row: Firmware) => {
    await ElMessageBox.confirm('确定要删除该固件吗？', '提示', { type: 'warning' })
    const res = await delFirmware([row.id])
    if (res.code === 1) {
        ElMessage.success('删除成功')
        loadData()
    }
}

const handleBatchDelete = async () => {
    await ElMessageBox.confirm(`确定要删除选中的 ${selectedIds.value.length} 个固件吗？`, '提示', { type: 'warning' })
    const res = await delFirmware(selectedIds.value)
    if (res.code === 1) {
        ElMessage.success('删除成功')
        loadData()
    }
}

const handleUpload = () => {
    // TODO: 实现文件上传到 OSS
    ElMessage.info('请手动上传固件到 OSS 并填写地址')
}
</script>

<style scoped lang="scss">
.table-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;

    &-left,
    &-right {
        display: flex;
        align-items: center;
    }
}

.table-footer {
    display: flex;
    justify-content: flex-end;
    margin-top: 16px;
}
</style>
