<template>
    <div class="license-page">
        <!-- 授权状态卡片 -->
        <el-card class="status-card" :class="statusClass">
            <div class="status-header">
                <div class="status-icon">
                    <el-icon :size="48">
                        <CircleCheckFilled v-if="isValid && !isExpired" />
                        <WarningFilled v-else-if="isExpiringSoon" />
                        <CircleCloseFilled v-else />
                    </el-icon>
                </div>
                <div class="status-info">
                    <h2 class="status-title">{{ statusTitle }}</h2>
                    <p class="status-desc">{{ statusDesc }}</p>
                </div>
                <div class="status-actions">
                    <el-button type="primary" @click="refreshLicense" :loading="loading">
                        <el-icon><Refresh /></el-icon>
                        刷新状态
                    </el-button>
                    <el-button v-if="!isActivated" type="success" @click="showActivateDialog = true">
                        <el-icon><Key /></el-icon>
                        激活授权
                    </el-button>
                </div>
            </div>
        </el-card>

        <el-row :gutter="20" v-if="isActivated">
            <!-- 基本信息 -->
            <el-col :span="12">
                <el-card class="info-card">
                    <template #header>
                        <div class="card-header">
                            <el-icon><Document /></el-icon>
                            <span>授权信息</span>
                        </div>
                    </template>
                    <el-descriptions :column="1" border>
                        <el-descriptions-item label="授权码">
                            <span class="license-key">{{ maskedLicenseKey }}</span>
                            <el-button link type="primary" @click="copyLicenseKey" style="margin-left: 8px;">
                                <el-icon><CopyDocument /></el-icon>
                            </el-button>
                        </el-descriptions-item>
                        <el-descriptions-item label="授权类型">
                            <el-tag :type="licenseTypeTag">{{ licenseType }}</el-tag>
                        </el-descriptions-item>
                        <el-descriptions-item label="到期时间">
                            {{ licenseData?.expires_at || '-' }}
                        </el-descriptions-item>
                        <el-descriptions-item label="剩余天数">
                            <span :class="remainingDaysClass">
                                {{ remainingDays > 0 ? remainingDays + ' 天' : '已过期' }}
                            </span>
                        </el-descriptions-item>
                        <el-descriptions-item label="离线模式">
                            <el-tag :type="licenseData?.offline_mode ? 'success' : 'info'" size="small">
                                {{ licenseData?.offline_mode ? '支持' : '不支持' }}
                            </el-tag>
                            <span v-if="licenseData?.offline_mode" class="offline-days">
                                (最长 {{ licenseData?.max_offline_days || 7 }} 天)
                            </span>
                        </el-descriptions-item>
                    </el-descriptions>
                </el-card>
            </el-col>

            <!-- 设备信息 -->
            <el-col :span="12">
                <el-card class="info-card">
                    <template #header>
                        <div class="card-header">
                            <el-icon><Monitor /></el-icon>
                            <span>设备信息</span>
                        </div>
                    </template>
                    <el-descriptions :column="1" border>
                        <el-descriptions-item label="设备指纹">
                            <span class="fingerprint">{{ maskedFingerprint }}</span>
                        </el-descriptions-item>
                        <el-descriptions-item label="最后验证">
                            {{ lastVerifyTimeStr }}
                        </el-descriptions-item>
                        <el-descriptions-item label="离线天数" v-if="offlineDays > 0">
                            <el-tag type="warning">{{ offlineDays }} 天</el-tag>
                        </el-descriptions-item>
                    </el-descriptions>
                </el-card>
            </el-col>
        </el-row>

        <!-- 使用限制 -->
        <el-card class="info-card" v-if="isActivated && hasLimits">
            <template #header>
                <div class="card-header">
                    <el-icon><DataLine /></el-icon>
                    <span>使用限制</span>
                </div>
            </template>
            <el-row :gutter="20">
                <el-col :span="8" v-if="limits.max_devices">
                    <div class="limit-item">
                        <div class="limit-label">设备数量</div>
                        <el-progress 
                            :percentage="getUsagePercent('devices')" 
                            :status="getUsageStatus('devices')"
                        />
                        <div class="limit-value">
                            {{ limits.used_devices || 0 }} / {{ limits.max_devices }}
                        </div>
                    </div>
                </el-col>
                <el-col :span="8" v-if="limits.max_projects">
                    <div class="limit-item">
                        <div class="limit-label">项目数量</div>
                        <el-progress 
                            :percentage="getUsagePercent('projects')" 
                            :status="getUsageStatus('projects')"
                        />
                        <div class="limit-value">
                            {{ limits.used_projects || 0 }} / {{ limits.max_projects }}
                        </div>
                    </div>
                </el-col>
                <el-col :span="8" v-if="limits.max_users">
                    <div class="limit-item">
                        <div class="limit-label">用户数量</div>
                        <el-progress 
                            :percentage="getUsagePercent('users')" 
                            :status="getUsageStatus('users')"
                        />
                        <div class="limit-value">
                            {{ limits.used_users || 0 }} / {{ limits.max_users }}
                        </div>
                    </div>
                </el-col>
            </el-row>
        </el-card>

        <!-- 功能模块 -->
        <el-row :gutter="20" v-if="isActivated">
            <el-col :span="12">
                <el-card class="info-card">
                    <template #header>
                        <div class="card-header">
                            <el-icon><Grid /></el-icon>
                            <span>授权模块</span>
                        </div>
                    </template>
                    <div class="module-list" v-if="modules.length > 0">
                        <el-tag v-for="mod in modules" :key="mod" class="module-tag" type="success">
                            {{ mod }}
                        </el-tag>
                    </div>
                    <el-empty v-else description="暂无授权模块" :image-size="60" />
                </el-card>
            </el-col>
            <el-col :span="12">
                <el-card class="info-card">
                    <template #header>
                        <div class="card-header">
                            <el-icon><List /></el-icon>
                            <span>授权功能</span>
                        </div>
                    </template>
                    <div class="feature-list" v-if="features.length > 0">
                        <el-tag v-for="feat in features" :key="feat" class="feature-tag">
                            {{ feat }}
                        </el-tag>
                    </div>
                    <el-empty v-else description="暂无授权功能" :image-size="60" />
                </el-card>
            </el-col>
        </el-row>

        <!-- 未激活提示 -->
        <el-card v-if="!isActivated" class="activate-card">
            <el-empty description="系统尚未激活授权">
                <el-button type="primary" @click="showActivateDialog = true">
                    <el-icon><Key /></el-icon>
                    立即激活
                </el-button>
            </el-empty>
        </el-card>

        <!-- 激活对话框 -->
        <el-dialog v-model="showActivateDialog" title="激活授权" width="500px">
            <el-form :model="activateForm" label-width="80px">
                <el-form-item label="授权码">
                    <el-input 
                        v-model="activateForm.licenseKey" 
                        placeholder="请输入授权码"
                        clearable
                    />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="showActivateDialog = false">取消</el-button>
                <el-button type="primary" @click="handleActivate" :loading="activating">
                    激活
                </el-button>
            </template>
        </el-dialog>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { 
    CircleCheckFilled, WarningFilled, CircleCloseFilled, 
    Refresh, Key, Document, Monitor, DataLine, Grid, List, CopyDocument 
} from '@element-plus/icons-vue'
import { useLicenseStore } from '/@/stores/license'
import { ElMessage } from 'element-plus'
import { useClipboard } from '@vueuse/core'

const licenseStore = useLicenseStore()
const { copy } = useClipboard()

const loading = ref(false)
const activating = ref(false)
const showActivateDialog = ref(false)
const activateForm = ref({ licenseKey: '' })

// 计算属性
const isActivated = computed(() => licenseStore.isActivated)
const isValid = computed(() => licenseStore.isValid)
const isExpired = computed(() => licenseStore.isExpired)
const isExpiringSoon = computed(() => licenseStore.isExpiringSoon)
const licenseData = computed(() => licenseStore.licenseData)
const modules = computed(() => licenseStore.modules)
const features = computed(() => licenseStore.features)
const limits = computed(() => licenseStore.limits)
const remainingDays = computed(() => licenseStore.remainingDays)
const offlineDays = computed(() => licenseStore.offlineDays)

const hasLimits = computed(() => {
    const l = limits.value
    return l.max_devices || l.max_projects || l.max_users
})

const statusClass = computed(() => {
    if (!isActivated.value) return 'status-inactive'
    if (isExpired.value) return 'status-expired'
    if (isExpiringSoon.value) return 'status-warning'
    return 'status-valid'
})

const statusTitle = computed(() => {
    if (!isActivated.value) return '未激活'
    if (isExpired.value) return '授权已过期'
    if (isExpiringSoon.value) return '授权即将到期'
    return '授权有效'
})

const statusDesc = computed(() => {
    if (!isActivated.value) return '请输入授权码激活系统'
    if (isExpired.value) return '请联系管理员续期授权'
    if (isExpiringSoon.value) return `授权将在 ${remainingDays.value} 天后到期，请及时续期`
    return `授权有效期还剩 ${remainingDays.value} 天`
})

const licenseType = computed(() => {
    if (!licenseData.value) return '-'
    return remainingDays.value <= 30 ? '试用版' : '正式版'
})

const licenseTypeTag = computed(() => {
    return remainingDays.value <= 30 ? 'warning' : 'success'
})

const maskedLicenseKey = computed(() => {
    const key = licenseStore.licenseKey
    if (!key) return '-'
    if (key.length <= 8) return key
    return key.slice(0, 4) + '****' + key.slice(-4)
})

const maskedFingerprint = computed(() => {
    const fp = licenseStore.fingerprint
    if (!fp) return '-'
    return fp.slice(0, 8) + '...' + fp.slice(-8)
})

const lastVerifyTimeStr = computed(() => {
    const time = licenseStore.lastVerifyTime
    if (!time) return '-'
    return new Date(time).toLocaleString()
})

const remainingDaysClass = computed(() => {
    if (remainingDays.value <= 0) return 'text-danger'
    if (remainingDays.value <= 7) return 'text-warning'
    return 'text-success'
})

// 方法
const getUsagePercent = (type: 'devices' | 'projects' | 'users') => {
    const max = limits.value[`max_${type}` as keyof typeof limits.value] as number
    const used = limits.value[`used_${type}` as keyof typeof limits.value] as number
    if (!max) return 0
    return Math.round((used || 0) / max * 100)
}

const getUsageStatus = (type: 'devices' | 'projects' | 'users') => {
    const percent = getUsagePercent(type)
    if (percent >= 90) return 'exception'
    if (percent >= 70) return 'warning'
    return 'success'
}

const refreshLicense = async () => {
    loading.value = true
    try {
        await licenseStore.verify()
        ElMessage.success('刷新成功')
    } catch (error) {
        ElMessage.error('刷新失败')
    } finally {
        loading.value = false
    }
}

const handleActivate = async () => {
    if (!activateForm.value.licenseKey) {
        ElMessage.warning('请输入授权码')
        return
    }
    
    activating.value = true
    try {
        const result = await licenseStore.activate(activateForm.value.licenseKey)
        if (result.success) {
            ElMessage.success(result.message)
            showActivateDialog.value = false
            activateForm.value.licenseKey = ''
        } else {
            ElMessage.error(result.message)
        }
    } finally {
        activating.value = false
    }
}

const copyLicenseKey = async () => {
    const key = licenseStore.licenseKey
    if (key) {
        await copy(key)
        ElMessage.success('已复制到剪贴板')
    }
}

onMounted(async () => {
    await licenseStore.initialize()
    if (isActivated.value) {
        await licenseStore.verify()
    }
})
</script>


<style scoped lang="scss">
.license-page {
    padding: 20px;
}

.status-card {
    margin-bottom: 20px;
    
    &.status-valid {
        :deep(.el-card__body) {
            background: linear-gradient(135deg, #e8f5e9 0%, #c8e6c9 100%);
        }
        .status-icon { color: #4caf50; }
    }
    
    &.status-warning {
        :deep(.el-card__body) {
            background: linear-gradient(135deg, #fff3e0 0%, #ffe0b2 100%);
        }
        .status-icon { color: #ff9800; }
    }
    
    &.status-expired, &.status-inactive {
        :deep(.el-card__body) {
            background: linear-gradient(135deg, #ffebee 0%, #ffcdd2 100%);
        }
        .status-icon { color: #f44336; }
    }
}

.status-header {
    display: flex;
    align-items: center;
    gap: 20px;
}

.status-icon {
    flex-shrink: 0;
}

.status-info {
    flex: 1;
    
    .status-title {
        margin: 0 0 8px 0;
        font-size: 24px;
        font-weight: 600;
    }
    
    .status-desc {
        margin: 0;
        color: #666;
    }
}

.status-actions {
    display: flex;
    gap: 10px;
}

.info-card {
    margin-bottom: 20px;
    
    .card-header {
        display: flex;
        align-items: center;
        gap: 8px;
        font-weight: 500;
    }
}

.license-key {
    font-family: monospace;
    font-size: 14px;
}

.fingerprint {
    font-family: monospace;
    font-size: 12px;
    color: #666;
}

.offline-days {
    margin-left: 8px;
    color: #999;
    font-size: 12px;
}

.limit-item {
    text-align: center;
    padding: 16px;
    
    .limit-label {
        margin-bottom: 12px;
        color: #666;
    }
    
    .limit-value {
        margin-top: 8px;
        font-size: 14px;
        color: #333;
    }
}

.module-list, .feature-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.module-tag, .feature-tag {
    margin: 0;
}

.activate-card {
    margin-top: 20px;
}

.text-success { color: #67c23a; }
.text-warning { color: #e6a23c; }
.text-danger { color: #f56c6c; }
</style>
