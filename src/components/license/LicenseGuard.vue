<template>
    <template v-if="hasPermission">
        <slot />
    </template>
    <template v-else>
        <slot name="fallback">
            <div v-if="showFallback" class="license-guard-fallback">
                <el-empty :description="fallbackText">
                    <el-button v-if="showUpgrade" type="primary" @click="handleUpgrade">
                        升级授权
                    </el-button>
                </el-empty>
            </div>
        </slot>
    </template>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useLicenseStore } from '/@/stores/license'

const props = withDefaults(defineProps<{
    /** 功能点ID */
    feature?: string
    /** 模块ID */
    module?: string
    /** 无权限时是否显示占位内容 */
    showFallback?: boolean
    /** 占位文本 */
    fallbackText?: string
    /** 是否显示升级按钮 */
    showUpgrade?: boolean
}>(), {
    showFallback: true,
    fallbackText: '该功能需要授权后使用',
    showUpgrade: true,
})

const emit = defineEmits<{
    (e: 'upgrade'): void
}>()

const licenseStore = useLicenseStore()

const hasPermission = computed(() => {
    if (props.feature) {
        return licenseStore.hasFeature(props.feature)
    }
    if (props.module) {
        return licenseStore.hasModule(props.module)
    }
    return true
})

const handleUpgrade = () => {
    emit('upgrade')
}
</script>

<style scoped lang="scss">
.license-guard-fallback {
    padding: 40px 20px;
    text-align: center;
    background: var(--el-fill-color-light);
    border-radius: 8px;
}
</style>
