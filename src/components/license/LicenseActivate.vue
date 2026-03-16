<template>
    <el-dialog v-model="visible" title="激活授权" width="450px" :close-on-click-modal="false" :close-on-press-escape="false" :show-close="allowClose">
        <div class="license-activate">
            <div class="activate-header">
                <img src="/@/assets/logo.png" alt="Logo" class="logo" />
                <h3>NexusHive 飞控系统</h3>
                <p>请输入授权密钥激活系统</p>
            </div>

            <el-form ref="formRef" :model="form" :rules="rules" label-width="0">
                <el-form-item prop="licenseKey">
                    <el-input v-model="form.licenseKey" placeholder="请输入授权密钥 (NH-XXXX-XXXX-XXXX)" size="large" :prefix-icon="Key" />
                </el-form-item>
            </el-form>

            <div class="activate-info">
                <p>设备指纹: {{ fingerprint }}</p>
            </div>
        </div>

        <template #footer>
            <el-button v-if="allowClose" @click="visible = false">取消</el-button>
            <el-button type="primary" :loading="loading" @click="handleActivate"> 激活 </el-button>
        </template>
    </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { Key } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { ElMessage } from 'element-plus'
import { useLicenseStore } from '/@/stores/license'

const props = withDefaults(
    defineProps<{
        modelValue: boolean
        allowClose?: boolean
    }>(),
    {
        allowClose: true,
    }
)

const emit = defineEmits<{
    (e: 'update:modelValue', value: boolean): void
    (e: 'success'): void
}>()

const visible = computed({
    get: () => props.modelValue,
    set: (val) => emit('update:modelValue', val),
})

const licenseStore = useLicenseStore()
const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({
    licenseKey: '',
})

const rules: FormRules = {
    licenseKey: [
        { required: true, message: '请输入授权密钥', trigger: 'blur' },
        { pattern: /^NH-\d{4}-[A-Z0-9]{8}$/, message: '授权密钥格式不正确', trigger: 'blur' },
    ],
}

const fingerprint = computed(() => {
    const fp = licenseStore.fingerprint
    if (fp) {
        return fp.substring(0, 16) + '...'
    }
    return '生成中...'
})

const handleActivate = async () => {
    if (!formRef.value) return

    await formRef.value.validate()

    loading.value = true

    try {
        const result = await licenseStore.activate(form.licenseKey)

        if (result.success) {
            ElMessage.success(result.message)
            visible.value = false
            emit('success')

            // 启动心跳
            licenseStore.startHeartbeat()
        } else {
            ElMessage.error(result.message)
        }
    } finally {
        loading.value = false
    }
}

onMounted(async () => {
    await licenseStore.initialize()
})
</script>

<style scoped lang="scss">
.license-activate {
    .activate-header {
        text-align: center;
        margin-bottom: 30px;

        .logo {
            width: 60px;
            height: 60px;
            margin-bottom: 15px;
        }

        h3 {
            margin: 0 0 10px;
            font-size: 20px;
        }

        p {
            margin: 0;
            color: var(--el-text-color-secondary);
        }
    }

    .activate-info {
        margin-top: 20px;
        padding: 10px;
        background: var(--el-fill-color-light);
        border-radius: 4px;
        font-size: 12px;
        color: var(--el-text-color-secondary);

        p {
            margin: 0;
        }
    }
}
</style>
