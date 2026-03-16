<template>
    <el-dialog
        v-model="visible"
        :title="t('system.update.newVersionAvailable')"
        width="500px"
        :close-on-click-modal="!noticeData.is_force"
        :close-on-press-escape="!noticeData.is_force"
        :show-close="!noticeData.is_force"
        class="update-notice-dialog"
    >
        <div class="notice-content">
            <!-- 版本信息 -->
            <div class="version-info">
                <div class="version-badge">
                    <span class="version-label">{{ noticeData.latest_version }}</span>
                    <el-tag :type="getUpdateTypeTag(noticeData.update_type)" size="small">
                        {{ getUpdateTypeText(noticeData.update_type) }}
                    </el-tag>
                    <el-tag v-if="noticeData.is_force" type="danger" size="small">
                        {{ t('system.update.forceUpdate') }}
                    </el-tag>
                </div>
                <div class="update-title">{{ noticeData.title }}</div>
                <div class="update-time">{{ t('system.update.publishedAt') }}: {{ noticeData.published_at }}</div>
            </div>

            <!-- 更新说明 -->
            <div class="release-notes" v-if="noticeData.release_notes">
                <div class="notes-title">{{ t('system.update.releaseNotes') }}</div>
                <div class="notes-content markdown-body" v-html="renderedNotes"></div>
            </div>
        </div>

        <template #footer>
            <div class="dialog-footer">
                <template v-if="!noticeData.is_force">
                    <el-button @click="handleLater">{{ t('system.update.remindLater') }}</el-button>
                    <el-button @click="handleDismiss" :loading="dismissLoading">
                        {{ t('system.update.ignoreVersion') }}
                    </el-button>
                </template>
                <el-button type="primary" @click="handleUpdate">
                    {{ t('system.update.goToUpdate') }}
                </el-button>
            </div>
        </template>
    </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { marked } from 'marked'
import { useAdminInfo } from '/@/stores/adminInfo'
import { useConfig } from '/@/stores/config'
import { mergeMessage } from '/@/lang/index'
import { getUpdateNotice, dismissUpdateNotice, type UpdateNoticeData } from '/@/api/backend/system/update'

defineOptions({
    name: 'UpdateNotice',
})

const { t } = useI18n()
const router = useRouter()
const adminInfo = useAdminInfo()
const config = useConfig()

const visible = ref(false)
const dismissLoading = ref(false)
const noticeData = ref<Partial<UpdateNoticeData>>({})
const langLoaded = ref(false)
const hasChecked = ref(false)

// 配置 marked
marked.setOptions({
    breaks: true,
    gfm: true,
})

// 将 release_notes 渲染为 HTML
const renderedNotes = computed(() => {
    if (!noticeData.value.release_notes) return ''
    return marked.parse(noticeData.value.release_notes) as string
})

// 检查更新
const checkUpdate = async () => {
    try {
        const res = await getUpdateNotice()
        console.log('[UpdateNotice] API response:', res)
        console.log('[UpdateNotice] has_update:', res.data?.has_update)
        if (res.code === 1 && res.data?.has_update) {
            noticeData.value = res.data
            visible.value = true
            console.log('[UpdateNotice] Showing dialog')
        } else {
            console.log('[UpdateNotice] No update available or has_update is false')
        }
    } catch (error) {
        console.error('[UpdateNotice] Failed to check update:', error)
    }
}

// 加载语言包并检查更新
const loadLangAndCheck = async () => {
    // 防止重复检查
    if (hasChecked.value) return
    hasChecked.value = true

    console.log('[UpdateNotice] Checking, adminInfo.super:', adminInfo.super)

    // 只有超管才检查更新提醒
    if (!adminInfo.super) {
        console.log('[UpdateNotice] Not super admin, skip')
        return
    }

    // 先加载语言包
    if (!langLoaded.value) {
        try {
            const locale = config.lang.defaultLang
            const langModule = await import(`/@/lang/backend/${locale}/system.ts`)
            if (langModule.default) {
                mergeMessage({ system: langModule.default }, '')
            }
            langLoaded.value = true
        } catch (error) {
            console.error('[UpdateNotice] Failed to load language pack:', error)
        }
    }

    checkUpdate()
}

// 组件挂载后延迟检查，等待用户信息加载完成
onMounted(() => {
    console.log('[UpdateNotice] Mounted, adminInfo.super:', adminInfo.super)
    // 延迟 2 秒检查，确保用户信息已加载
    setTimeout(() => {
        loadLangAndCheck()
    }, 2000)
})

// 稍后提醒
const handleLater = () => {
    visible.value = false
}

// 忽略此版本
const handleDismiss = async () => {
    if (!noticeData.value.latest_version) return

    dismissLoading.value = true
    try {
        const res = await dismissUpdateNotice(noticeData.value.latest_version)
        if (res.code === 1) {
            visible.value = false
        }
    } catch (error) {
        console.error('Failed to dismiss notice:', error)
    } finally {
        dismissLoading.value = false
    }
}

// 立即更新
const handleUpdate = () => {
    visible.value = false
    router.push('/admin/system/update')
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
</script>

<style scoped lang="scss">
.update-notice-dialog {
    :deep(.el-dialog__header) {
        background: linear-gradient(135deg, var(--el-color-primary-light-3), var(--el-color-primary));
        color: #fff;
        margin-right: 0;
        padding: 15px 20px;

        .el-dialog__title {
            color: #fff;
        }

        .el-dialog__headerbtn .el-dialog__close {
            color: #fff;
        }
    }
}

.notice-content {
    .version-info {
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
        max-height: 300px;
        overflow-y: auto;

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
}

.dialog-footer {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
}
</style>
