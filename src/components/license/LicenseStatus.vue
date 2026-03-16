<template>
    <div class="license-status" v-if="showStatus">
        <el-tooltip :content="statusTooltip" placement="bottom">
            <div class="status-badge" :class="statusClass" @click="showDetail = true">
                <el-icon><component :is="statusIcon" /></el-icon>
                <span class="status-text">{{ statusText }}</span>
            </div>
        </el-tooltip>

        <!-- 授权详情弹窗 -->
        <el-dialog v-model="showDetail" title="授权信息" width="500px">
            <el-descriptions :column="2" border>
                <el-descriptions-item label="授权状态">
                    <el-tag :type="statusType">{{ statusText }}</el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="剩余天数">
                    <span :class="remainingClass">{{ licenseStore.remainingDays }} 天</span>
                </el-descriptions-item>
                <el-descriptions-item label="到期时间" :span="2">
                    {{ licenseStore.licenseData?.expires_at || '-' }}
                </el-descriptions-item>
                <el-descriptions-item label="授权模块" :span="2">
                    <el-tag
                        v-for="module in licenseStore.modules"
                        :key="module"
                        size="small"
                        class="module-tag"
                    >
                        {{ module }}
                    </el-tag>
                    <span v-if="licenseStore.modules.length === 0">-</span>
                </el-descriptions-item>
                <el-descriptions-item label="离线模式">
                    {{ licenseStore.licenseData?.offline_mode ? '已启用' : '未启用' }}
                </el-descriptions-item>
                <el-descriptions-item label="离线天数">
                    {{ licenseStore.offlineDays }} / {{ licenseStore.licenseData?.max_offline_days || 7 }} 天
                </el-descriptions-item>
            </el-descriptions>

            <template v-if="licenseStore.licenseData?.limits">
                <el-divider content-position="left">使用限制</el-divider>
                <el-row :gutter="20">
                    <el-col :span="8" v-if="licenseStore.limits.max_devices">
                        <div class="limit-item">
                            <div class="limit-label">设备数量</div>
                            <el-progress
                                :percentage="getPercentage('devices')"
                                :status="getLimitStatus('devices')"
                            />
                            <div class="limit-text">
                                {{ licenseStore.limits.used_devices || 0 }} / {{ licenseStore.limits.max_devices }}
                            </div>
                        </div>
                    </el-col>
                    <el-col :span="8" v-if="licenseStore.limits.max_projects">
                        <div class="limit-item">
                            <div class="limit-label">项目数量</div>
                            <el-progress
                                :percentage="getPercentage('projects')"
                                :status="getLimitStatus('projects')"
                            />
                            <div class="limit-text">
                                {{ licenseStore.limits.used_projects || 0 }} / {{ licenseStore.limits.max_projects }}
                            </div>
                        </div>
                    </el-col>
                    <el-col :span="8" v-if="licenseStore.limits.max_users">
                        <div class="limit-item">
                            <div class="limit-label">用户数量</div>
                            <el-progress
                                :percentage="getPercentage('users')"
                                :status="getLimitStatus('users')"
                            />
                            <div class="limit-text">
                                {{ licenseStore.limits.used_users || 0 }} / {{ licenseStore.limits.max_users }}
                            </div>
                        </div>
                    </el-col>
                </el-row>
            </template>

            <template #footer>
                <el-button @click="showDetail = false">关闭</el-button>
                <el-button type="primary" @click="refreshLicense">刷新授权</el-button>
            </template>
        </el-dialog>
    </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { CircleCheck, Warning, CircleClose } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useLicenseStore } from '/@/stores/license'

const licenseStore = useLicenseStore()
const showDetail = ref(false)

const showStatus = computed(() => licenseStore.isActivated)

const statusClass = computed(() => {
    if (!licenseStore.isValid) return 'status-error'
    if (licenseStore.isExpired) return 'status-error'
    if (licenseStore.isExpiringSoon) return 'status-warning'
    return 'status-success'
})

const statusType = computed(() => {
    if (!licenseStore.isValid) return 'danger'
    if (licenseStore.isExpired) return 'danger'
    if (licenseStore.isExpiringSoon) return 'warning'
    return 'success'
})

const statusIcon = computed(() => {
    if (!licenseStore.isValid || licenseStore.isExpired) return CircleClose
    if (licenseStore.isExpiringSoon) return Warning
    return CircleCheck
})

const statusText = computed(() => {
    if (!licenseStore.isValid) return '授权无效'
    if (licenseStore.isExpired) return '已过期'
    if (licenseStore.isExpiringSoon) return `${licenseStore.remainingDays}天后到期`
    return '已授权'
})

const statusTooltip = computed(() => {
    if (!licenseStore.isValid) return '授权验证失败，请联系管理员'
    if (licenseStore.isExpired) return '授权已过期，请续期'
    if (licenseStore.isExpiringSoon) return `授权将在 ${licenseStore.remainingDays} 天后到期`
    return '授权正常'
})

const remainingClass = computed(() => {
    if (licenseStore.remainingDays <= 0) return 'text-danger'
    if (licenseStore.remainingDays <= 7) return 'text-warning'
    return 'text-success'
})

const getPercentage = (type: 'devices' | 'projects' | 'users') => {
    const used = licenseStore.limits[`used_${type}` as keyof typeof licenseStore.limits] as number || 0
    const max = licenseStore.limits[`max_${type}` as keyof typeof licenseStore.limits] as number || 1
    return Math.min(100, Math.round((used / max) * 100))
}

const getLimitStatus = (type: 'devices' | 'projects' | 'users') => {
    const percentage = getPercentage(type)
    if (percentage >= 90) return 'exception'
    if (percentage >= 70) return 'warning'
    return 'success'
}

const refreshLicense = async () => {
    const result = await licenseStore.verify()
    if (result) {
        ElMessage.success('授权信息已刷新')
    } else {
        ElMessage.error('刷新失败')
    }
}
</script>

<style scoped lang="scss">
.license-status {
    .status-badge {
        display: flex;
        align-items: center;
        gap: 5px;
        padding: 5px 10px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 12px;
        transition: all 0.2s;

        &.status-success {
            background: var(--el-color-success-light-9);
            color: var(--el-color-success);
        }

        &.status-warning {
            background: var(--el-color-warning-light-9);
            color: var(--el-color-warning);
        }

        &.status-error {
            background: var(--el-color-danger-light-9);
            color: var(--el-color-danger);
        }

        &:hover {
            opacity: 0.8;
        }
    }
}

.module-tag {
    margin: 2px;
}

.limit-item {
    .limit-label {
        margin-bottom: 5px;
        font-size: 12px;
    }

    .limit-text {
        margin-top: 3px;
        text-align: right;
        font-size: 12px;
        color: var(--el-text-color-secondary);
    }
}

.text-danger { color: var(--el-color-danger); }
.text-warning { color: var(--el-color-warning); }
.text-success { color: var(--el-color-success); }
</style>
