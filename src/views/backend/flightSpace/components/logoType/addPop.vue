<template>
    <el-dialog class="ba-operate-dialog" :close-on-click-modal="false" :model-value="['Add', 'Edit'].includes(popOperate)" @close="toggleForm()">
        <template #header>
            <div class="title" v-drag="['.ba-operate-dialog', '.el-dialog__header']" v-zoom="'.ba-operate-dialog'">
                {{ popOperate === 'Add' ? '添加模型' : '编辑模型' }}
            </div>
        </template>
        <el-scrollbar v-loading="loading" class="ba-table-form-scrollbar">
            <div class="ba-operate-form" :class="'ba-' + popOperate + '-form'">
                <el-form v-if="!loading" :model="form" ref="formRef">
                    <!-- 模型名称 -->
                    <FormItem label="模型名称" type="string" v-model="form.name" prop="name" placeholder="请输入模型名称" />
                    <!-- 模型地址 -->
                    <FormItem label="模型地址" type="string" v-model="form.url" prop="url" placeholder="请输入模型地址" />
                    <!-- 是否显示 -->
                    <el-form-item label="是否显示" prop="status">
                        <el-switch v-model="form.status" :active-value="1" :inactive-value="0" />
                    </el-form-item>
                </el-form>
            </div>
        </el-scrollbar>
        <template #footer>
            <div :style="'width: calc(100% - ' + form.labelWidth! / 1.8 + 'px)'">
                <el-button @click="toggleForm()">取消</el-button>
                <el-button v-blur :loading="loading" @click="onSubmit()" type="primary">
                    {{ popOperate === 'Add' ? '添加' : '保存' }}
                </el-button>
            </div>
        </template>
    </el-dialog>
</template>

<script setup lang="ts">
import type { FormItemRule } from 'element-plus'
import { inject, reactive, useTemplateRef, ref, computed } from 'vue'
import FormItem from '/@/components/formItem/index.vue'
import { buildValidatorData } from '/@/utils/validate'
import { useProjectStore } from '/@/stores/project'
import { ElMessage } from 'element-plus'

// ------------------------- 生命周期 -------------------------

// ------------------------- 计算属性 -------------------------

// ------------------------- 变量 -------------------------
// 提交成功事件
const emit = defineEmits(['submitSuccess'])
// 项目信息
const projectStore = useProjectStore()
// 项目列表
const currentProject = computed(() => projectStore.currentProject)
// 弹窗操作类型
const popOperate = inject('popOperate')
// 地图模型 API
const api = inject('api')
// 表单数据
const form = ref<any>({
    name: '',
    url: '',
    status: 1,
    project_id: currentProject.value?.id || '',
})
// 加载状态
const loading = ref(false)

// ------------------------- 监听 -------------------------

// ------------------------- 方法 -------------------------

const toggleForm = () => {
    popOperate.value = ''
}

const onSubmit = async () => {
    // 校验表单
    if (Object.keys(form.value).some((key) => !form.value[key])) {
        ElMessage.error('请填写完整信息')
        return
    }
    try {
        loading.value = true
        if (popOperate.value === 'Add') {
            await api.add(form.value)
        } else {
            await api.editPost(form.value)
        }
        toggleForm()
        ElMessage.success(popOperate.value === 'Add' ? '添加成功' : '保存成功')
        emit('submitSuccess')
    } catch (error) {
    } finally {
        loading.value = false
    }
}
</script>

<style scoped lang="less"></style>
