<template>
    <el-dialog
        v-model="visible"
        :title="t('system.update.fileList')"
        width="700px"
        destroy-on-close
        @open="loadFiles"
    >
        <div v-loading="loading">
            <!-- 概览信息 -->
            <el-descriptions :column="4" border size="small" class="overview-info">
                <el-descriptions-item :label="t('system.update.packageType')">
                    <el-tag :type="filesData.package_type === 'full' ? 'primary' : 'success'" size="small">
                        {{ filesData.package_type === 'full' ? t('system.update.fullPackage') : t('system.update.incrementalPackage') }}
                    </el-tag>
                </el-descriptions-item>
                <el-descriptions-item :label="t('system.update.targetVersion')">
                    {{ filesData.to_version || '-' }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('system.update.fileCount')">
                    {{ filesData.file_count || 0 }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('system.update.totalSize')">
                    {{ formatSize(filesData.total_size) }}
                </el-descriptions-item>
            </el-descriptions>

            <!-- 分类标签 -->
            <div class="category-tabs" v-if="categories.length">
                <el-tag
                    v-for="cat in categories"
                    :key="cat.key"
                    :type="activeCategory === cat.key ? 'primary' : 'info'"
                    :effect="activeCategory === cat.key ? 'dark' : 'plain'"
                    class="category-tag"
                    @click="activeCategory = cat.key"
                >
                    {{ cat.name }} ({{ cat.count }})
                </el-tag>
            </div>

            <!-- 文件列表 -->
            <el-table
                :data="currentFiles"
                max-height="400"
                border
                stripe
                size="small"
                class="file-table"
            >
                <el-table-column :label="t('system.update.fileName')" prop="name" min-width="300" show-overflow-tooltip />
                <el-table-column :label="t('system.update.fileSize')" width="100">
                    <template #default="{ row }">
                        {{ formatSize(row.size) }}
                    </template>
                </el-table-column>
            </el-table>
        </div>
    </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { getPackageFiles, type PackageFilesData, type FileItem } from '/@/api/backend/system/update'

defineOptions({
    name: 'FileListDialog',
})

const props = defineProps<{
    downloadToken: string
}>()

const visible = defineModel<boolean>({ default: false })

const { t } = useI18n()

const loading = ref(false)
const filesData = ref<Partial<PackageFilesData>>({})
const activeCategory = ref('all')

// 分类列表
const categories = computed(() => {
    const cats = [{ key: 'all', name: t('system.update.allFiles'), count: filesData.value.file_count || 0 }]
    if (filesData.value.categories) {
        Object.entries(filesData.value.categories).forEach(([key, cat]) => {
            cats.push({ key, name: cat.name, count: cat.count })
        })
    }
    return cats
})

// 当前显示的文件列表
const currentFiles = computed<FileItem[]>(() => {
    if (activeCategory.value === 'all') {
        return filesData.value.files || []
    }
    return filesData.value.categories?.[activeCategory.value]?.files || []
})

// 加载文件列表
const loadFiles = async () => {
    if (!props.downloadToken) return

    loading.value = true
    activeCategory.value = 'all'

    try {
        const res = await getPackageFiles(props.downloadToken)
        if (res.code === 1) {
            filesData.value = res.data
        }
    } catch (error) {
        console.error('Failed to load files:', error)
    } finally {
        loading.value = false
    }
}

// 格式化文件大小
const formatSize = (bytes?: number) => {
    if (!bytes) return '0 B'
    const units = ['B', 'KB', 'MB', 'GB']
    let i = 0
    let size = bytes
    while (size >= 1024 && i < units.length - 1) {
        size /= 1024
        i++
    }
    return `${size.toFixed(2)} ${units[i]}`
}

// 监听 token 变化重新加载
watch(() => props.downloadToken, () => {
    if (visible.value && props.downloadToken) {
        loadFiles()
    }
})
</script>

<style scoped lang="scss">
.overview-info {
    margin-bottom: 15px;
}

.category-tabs {
    margin-bottom: 15px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;

    .category-tag {
        cursor: pointer;
        transition: all 0.2s;

        &:hover {
            opacity: 0.8;
        }
    }
}

.file-table {
    :deep(.el-table__cell) {
        padding: 8px 0;
    }
}
</style>
