<template>
    <div class="default-main system-update">
        <!-- 版本信息卡片 -->
        <el-card class="version-card" shadow="never">
            <template #header>
                <div class="card-header">
                    <span>{{ t('system.update.versionInfo') }}</span>
                </div>
            </template>
            <el-descriptions :column="2" border v-loading="state.versionLoading">
                <el-descriptions-item :label="t('system.update.currentVersion')">
                    {{ state.versionInfo.current_version || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('system.update.productCode')">
                    {{ state.versionInfo.product_code || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('system.update.phpVersion')">
                    {{ state.versionInfo.php_version || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('system.update.osInfo')">
                    {{ state.versionInfo.os_info || '-' }}
                </el-descriptions-item>
            </el-descriptions>
        </el-card>

        <!-- 更新信息卡片 -->
        <el-card class="update-card" shadow="never">
            <template #header>
                <div class="card-header">
                    <span>{{ t('system.update.updateInfo') }}</span>
                    <el-button type="primary" :loading="state.checkLoading" @click="handleCheckUpdate">
                        {{ t('system.update.checkUpdate') }}
                    </el-button>
                </div>
            </template>
            <div v-loading="state.checkLoading">
                <!-- 无更新 -->
                <el-empty v-if="!state.hasChecked" :description="t('system.update.clickToCheck')" />
                <el-result v-else-if="!state.updateResult.has_update" icon="success" :title="t('system.update.alreadyLatest')">
                    <template #sub-title>
                        <p>{{ state.updateResult.message || t('system.update.noUpdateAvailable') }}</p>
                    </template>
                </el-result>
                <!-- 有更新 -->
                <div v-else class="update-info">
                    <div class="update-header">
                        <div class="version-badge">
                            <span class="version-label">{{ state.updateResult.latest_version }}</span>
                            <el-tag :type="getUpdateTypeTag(state.updateResult.update_type)" size="small">
                                {{ getUpdateTypeText(state.updateResult.update_type) }}
                            </el-tag>
                            <el-tag v-if="state.updateResult.is_force" type="danger" size="small">
                                {{ t('system.update.forceUpdate') }}
                            </el-tag>
                        </div>
                        <div class="update-title">{{ state.updateResult.title }}</div>
                        <div class="update-time">{{ t('system.update.publishedAt') }}: {{ state.updateResult.published_at }}</div>
                    </div>
                    <div class="release-notes" v-if="state.updateResult.release_notes">
                        <div class="notes-title">{{ t('system.update.releaseNotes') }}</div>
                        <div class="notes-content ba-markdown" v-html="parseMarkdown(state.updateResult.release_notes)"></div>
                    </div>
                    <!-- 更新包列表 -->
                    <div class="packages-list" v-if="state.updateResult.packages?.length">
                        <div class="packages-title">{{ t('system.update.availablePackages') }}</div>
                        <el-table :data="state.updateResult.packages" border stripe>
                            <el-table-column :label="t('system.update.packageType')" width="120">
                                <template #default="{ row }">
                                    <el-tag :type="row.type === 'full' ? 'primary' : 'success'">
                                        {{ row.type === 'full' ? t('system.update.fullPackage') : t('system.update.incrementalPackage') }}
                                    </el-tag>
                                </template>
                            </el-table-column>
                            <el-table-column :label="t('system.update.fromVersion')" prop="from_version" width="150">
                                <template #default="{ row }">
                                    {{ row.from_version || '-' }}
                                </template>
                            </el-table-column>
                            <el-table-column :label="t('system.update.fileSize')" prop="file_size_format" width="120" />
                            <el-table-column :label="t('system.update.actions')" width="200">
                                <template #default="{ row }">
                                    <el-button type="primary" link @click="handleViewFiles(row)">
                                        {{ t('system.update.viewFiles') }}
                                    </el-button>
                                    <el-button
                                        type="success"
                                        :loading="state.updateLoading && state.currentToken === row.download_token"
                                        @click="handleExecuteUpdate(row)"
                                    >
                                        {{ t('system.update.executeUpdate') }}
                                    </el-button>
                                </template>
                            </el-table-column>
                        </el-table>
                    </div>
                </div>
            </div>
        </el-card>

        <!-- 双栏布局：时间轴和更新记录 -->
        <el-row :gutter="20">
            <el-col :xs="24" :sm="24" :md="12" :lg="12">
                <el-card class="timeline-card" shadow="never">
                    <template #header>
                        <div class="card-header">
                            <span>{{ t('system.update.versionTimeline') }}</span>
                            <el-button type="primary" link :loading="state.timelineLoading" @click="loadTimeline">
                                <el-icon><Refresh /></el-icon>
                            </el-button>
                        </div>
                    </template>
                    <div v-loading="state.timelineLoading">
                        <el-empty v-if="!state.timeline.length" :description="t('system.update.noTimeline')" />
                        <el-timeline v-else>
                            <el-timeline-item
                                v-for="item in state.timeline"
                                :key="item.version"
                                :type="item.is_current ? 'primary' : item.is_latest ? 'success' : 'info'"
                                :hollow="!item.is_current && !item.is_latest"
                                size="large"
                            >
                                <div class="timeline-item">
                                    <div class="timeline-header">
                                        <span class="timeline-version">{{ item.version }}</span>
                                        <el-tag v-if="item.is_current" type="primary" size="small">
                                            {{ t('system.update.currentVersion') }}
                                        </el-tag>
                                        <el-tag v-if="item.is_latest" type="success" size="small">
                                            {{ t('system.update.latestVersion') }}
                                        </el-tag>
                                        <el-tag :type="getUpdateTypeTag(item.update_type)" size="small">
                                            {{ getUpdateTypeText(item.update_type) }}
                                        </el-tag>
                                    </div>
                                    <div class="timeline-title">{{ item.title }}</div>
                                    <div class="timeline-time">{{ item.published_at }}</div>
                                </div>
                            </el-timeline-item>
                        </el-timeline>
                    </div>
                </el-card>
            </el-col>
            <el-col :xs="24" :sm="24" :md="12" :lg="12">
                <el-card class="history-card" shadow="never">
                    <template #header>
                        <div class="card-header">
                            <span>{{ t('system.update.updateHistory') }}</span>
                            <el-button type="primary" link :loading="state.historyLoading" @click="loadHistory">
                                <el-icon><Refresh /></el-icon>
                            </el-button>
                        </div>
                    </template>
                    <div v-loading="state.historyLoading">
                        <el-empty v-if="!state.history.length" :description="t('system.update.noHistory')" />
                        <el-timeline v-else>
                            <el-timeline-item
                                v-for="item in state.history"
                                :key="item.id"
                                :type="getStatusType(item.status)"
                                size="large"
                            >
                                <div class="history-item">
                                    <div class="history-header">
                                        <span class="history-version">{{ item.from_version }} → {{ item.to_version }}</span>
                                        <el-tag :type="getStatusType(item.status)" size="small">
                                            {{ getStatusText(item.status) }}
                                        </el-tag>
                                    </div>
                                    <div class="history-info">
                                        <span>IP: {{ item.ip_address }}</span>
                                        <span>{{ item.create_time }}</span>
                                    </div>
                                    <div v-if="item.status === 'failed' && item.error_message" class="history-error">
                                        {{ item.error_message }}
                                    </div>
                                </div>
                            </el-timeline-item>
                        </el-timeline>
                        <el-pagination
                            v-if="state.historyTotal > state.historyLimit"
                            :current-page="state.historyPage"
                            :page-size="state.historyLimit"
                            :total="state.historyTotal"
                            layout="prev, pager, next"
                            @current-change="onHistoryPageChange"
                        />
                    </div>
                </el-card>
            </el-col>
        </el-row>

        <!-- 文件列表弹窗 -->
        <FileListDialog v-model="state.fileDialogVisible" :download-token="state.currentToken" />
    </div>
</template>

<script setup lang="ts">
import { reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { marked } from 'marked'
import {
    getVersionInfo,
    checkUpdate,
    executeUpdate,
    getVersionTimeline,
    getUpdateHistory,
    type VersionInfo,
    type UpdateCheckResult,
    type TimelineItem,
    type HistoryItem,
    type UpdatePackage,
} from '/@/api/backend/system/update'
import FileListDialog from './fileListDialog.vue'

defineOptions({
    name: 'system/update',
})

const { t } = useI18n()

// 配置 marked
marked.setOptions({
    breaks: true,
    gfm: true,
})

// Markdown 转 HTML 函数
const parseMarkdown = (md: string): string => {
    if (!md) return ''
    return marked.parse(md) as string
}

const state = reactive({
    // 版本信息
    versionInfo: {} as VersionInfo,
    versionLoading: false,
    // 更新检查
    hasChecked: false,
    checkLoading: false,
    updateResult: {} as UpdateCheckResult,
    // 执行更新
    updateLoading: false,
    currentToken: '',
    // 时间轴
    timeline: [] as TimelineItem[],
    timelineLoading: false,
    // 更新历史
    history: [] as HistoryItem[],
    historyLoading: false,
    historyPage: 1,
    historyLimit: 10,
    historyTotal: 0,
    // 文件列表弹窗
    fileDialogVisible: false,
})

onMounted(() => {
    loadVersionInfo()
    loadTimeline()
    loadHistory()
    // 自动检查更新
    handleCheckUpdate()
})

// 加载版本信息
const loadVersionInfo = async () => {
    state.versionLoading = true
    try {
        const res = await getVersionInfo()
        if (res.code === 1) {
            state.versionInfo = res.data
        }
    } catch (error) {
        console.error('Failed to load version info:', error)
    } finally {
        state.versionLoading = false
    }
}

// 检查更新
const handleCheckUpdate = async () => {
    state.checkLoading = true
    try {
        const res = await checkUpdate()
        if (res.code === 1) {
            state.updateResult = res.data
            state.hasChecked = true
        } else {
            ElMessage.error(res.msg || t('system.update.checkFailed'))
        }
    } catch (error) {
        ElMessage.error(t('system.update.checkFailed'))
    } finally {
        state.checkLoading = false
    }
}

// 查看文件列表
const handleViewFiles = (pkg: UpdatePackage) => {
    state.currentToken = pkg.download_token
    state.fileDialogVisible = true
}

// 执行更新
const handleExecuteUpdate = async (pkg: UpdatePackage) => {
    try {
        await ElMessageBox.confirm(
            t('system.update.confirmUpdate', { version: state.updateResult.latest_version }),
            t('system.update.confirmTitle'),
            {
                confirmButtonText: t('system.update.confirm'),
                cancelButtonText: t('system.update.cancel'),
                type: 'warning',
            }
        )

        state.updateLoading = true
        state.currentToken = pkg.download_token

        const res = await executeUpdate(pkg.download_token, state.updateResult.latest_version!)
        if (res.code === 1) {
            ElMessage.success(t('system.update.updateStarted'))
            // 刷新更新历史
            loadHistory()
        } else {
            ElMessage.error(res.msg || t('system.update.updateFailed'))
        }
    } catch (error) {
        if (error !== 'cancel') {
            ElMessage.error(t('system.update.updateFailed'))
        }
    } finally {
        state.updateLoading = false
        state.currentToken = ''
    }
}

// 加载时间轴
const loadTimeline = async () => {
    state.timelineLoading = true
    try {
        const res = await getVersionTimeline()
        if (res.code === 1) {
            state.timeline = res.data.timeline
        }
    } catch (error) {
        console.error('Failed to load timeline:', error)
    } finally {
        state.timelineLoading = false
    }
}

// 加载更新历史
const loadHistory = async () => {
    state.historyLoading = true
    try {
        const res = await getUpdateHistory(state.historyPage, state.historyLimit)
        if (res.code === 1) {
            state.history = res.data.list
            state.historyTotal = res.data.total
        }
    } catch (error) {
        console.error('Failed to load history:', error)
    } finally {
        state.historyLoading = false
    }
}

// 历史分页切换
const onHistoryPageChange = (page: number) => {
    state.historyPage = page
    loadHistory()
}

// 获取更新类型标签颜色
const getUpdateTypeTag = (type?: string) => {
    const map: Record<string, string> = {
        major: 'danger',
        minor: 'warning',
        patch: 'info',
        hotfix: 'success',
    }
    return (map[type || ''] || 'info') as 'danger' | 'warning' | 'info' | 'success'
}

// 获取更新类型文本
const getUpdateTypeText = (type?: string) => {
    const map: Record<string, string> = {
        major: t('system.update.typeMajor'),
        minor: t('system.update.typeMinor'),
        patch: t('system.update.typePatch'),
        hotfix: t('system.update.typeHotfix'),
    }
    return map[type || ''] || type || '-'
}

// 获取状态类型
const getStatusType = (status: string) => {
    const map: Record<string, string> = {
        success: 'success',
        failed: 'danger',
        rollback: 'warning',
        downloading: 'info',
        installing: 'primary',
    }
    return (map[status] || 'info') as 'success' | 'danger' | 'warning' | 'info' | 'primary'
}

// 获取状态文本
const getStatusText = (status: string) => {
    const map: Record<string, string> = {
        success: t('system.update.statusSuccess'),
        failed: t('system.update.statusFailed'),
        rollback: t('system.update.statusRollback'),
        downloading: t('system.update.statusDownloading'),
        installing: t('system.update.statusInstalling'),
    }
    return map[status] || status
}
</script>

<style scoped lang="scss">
.system-update {
    .el-card {
        margin-bottom: 20px;
    }

    .card-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
    }

    .version-card {
        :deep(.el-descriptions__label) {
            width: 120px;
        }
    }

    .update-info {
        .update-header {
            margin-bottom: 20px;

            .version-badge {
                display: flex;
                align-items: center;
                gap: 10px;
                margin-bottom: 10px;

                .version-label {
                    font-size: 24px;
                    font-weight: bold;
                    color: var(--el-color-primary);
                }
            }

            .update-title {
                font-size: 16px;
                color: var(--el-text-color-primary);
                margin-bottom: 5px;
            }

            .update-time {
                font-size: 13px;
                color: var(--el-text-color-secondary);
            }
        }

        .release-notes {
            background: var(--el-fill-color-light);
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 20px;

            .notes-title {
                font-weight: bold;
                margin-bottom: 10px;
                color: var(--el-text-color-primary);
            }

            .notes-content {
                color: var(--el-text-color-regular);
                line-height: 1.8;

                // Markdown 样式
                :deep(h1),
                :deep(h2),
                :deep(h3),
                :deep(h4) {
                    margin: 12px 0 8px;
                    font-weight: 600;
                    color: var(--el-text-color-primary);
                }

                :deep(h1) {
                    font-size: 18px;
                }

                :deep(h2) {
                    font-size: 16px;
                }

                :deep(h3) {
                    font-size: 14px;
                }

                :deep(ul),
                :deep(ol) {
                    padding-left: 20px;
                    margin: 8px 0;
                }

                :deep(li) {
                    margin: 4px 0;
                }

                :deep(p) {
                    margin: 8px 0;
                }

                :deep(code) {
                    background: var(--el-fill-color);
                    padding: 2px 6px;
                    border-radius: 4px;
                    font-family: monospace;
                    font-size: 13px;
                }

                :deep(pre) {
                    background: var(--el-fill-color);
                    padding: 12px;
                    border-radius: 6px;
                    overflow-x: auto;
                    margin: 8px 0;

                    code {
                        background: none;
                        padding: 0;
                    }
                }

                :deep(blockquote) {
                    border-left: 3px solid var(--el-color-primary);
                    padding-left: 12px;
                    margin: 8px 0;
                    color: var(--el-text-color-secondary);
                }

                :deep(a) {
                    color: var(--el-color-primary);
                    text-decoration: none;

                    &:hover {
                        text-decoration: underline;
                    }
                }

                :deep(strong) {
                    font-weight: 600;
                }

                :deep(hr) {
                    border: none;
                    border-top: 1px solid var(--el-border-color);
                    margin: 12px 0;
                }
            }
        }

        .packages-list {
            .packages-title {
                font-weight: bold;
                margin-bottom: 10px;
                color: var(--el-text-color-primary);
            }
        }
    }

    .timeline-card,
    .history-card {
        min-height: 400px;

        .el-timeline {
            padding-left: 5px;
        }
    }

    .timeline-item,
    .history-item {
        .timeline-header,
        .history-header {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 5px;

            .timeline-version,
            .history-version {
                font-weight: bold;
                color: var(--el-text-color-primary);
            }
        }

        .timeline-title {
            color: var(--el-text-color-regular);
            margin-bottom: 5px;
        }

        .timeline-time,
        .history-info {
            font-size: 12px;
            color: var(--el-text-color-secondary);

            span {
                margin-right: 15px;
            }
        }

        .history-error {
            margin-top: 5px;
            padding: 8px;
            background: var(--el-color-danger-light-9);
            border-radius: 4px;
            color: var(--el-color-danger);
            font-size: 12px;
        }
    }

    .el-pagination {
        margin-top: 15px;
        justify-content: center;
    }
}

@media screen and (max-width: 992px) {
    .system-update {
        .el-row > .el-col {
            margin-bottom: 20px;
        }
    }
}
</style>
