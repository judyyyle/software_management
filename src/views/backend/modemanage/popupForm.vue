<template>
    <!-- 对话框表单 -->
    <!-- 建议使用 Prettier 格式化代码 -->
    <!-- el-form 内可以混用 el-form-item、FormItem、ba-input 等输入组件 -->
    <el-dialog
        class="ba-operate-dialog"
        :close-on-click-modal="false"
        :model-value="['Add', 'Edit'].includes(baTable.form.operate!)"
        @close="baTable.toggleForm"
    >
        <template #header>
            <div class="title" v-drag="['.ba-operate-dialog', '.el-dialog__header']" v-zoom="'.ba-operate-dialog'">
                {{ baTable.form.operate ? t(baTable.form.operate) : '' }}
            </div>
        </template>
        <el-scrollbar v-loading="baTable.form.loading" class="ba-table-form-scrollbar">
            <div
                class="ba-operate-form"
                :class="'ba-' + baTable.form.operate + '-form'"
                :style="config.layout.shrink ? '' : 'width: calc(100% - ' + baTable.form.labelWidth! / 2 + 'px)'"
            >
                <el-form
                    v-if="!baTable.form.loading"
                    ref="formRef"
                    @submit.prevent=""
                    @keyup.enter="baTable.onSubmit(formRef)"
                    :model="baTable.form.items"
                    :label-position="config.layout.shrink ? 'top' : 'right'"
                    :label-width="baTable.form.labelWidth + 'px'"
                    :rules="rules"
                >
                    <!-- 模型名称 -->
                    <FormItem label="模型名称" type="string" v-model="baTable.form.items!.name" prop="name" placeholder="请输入模型名称" />
                    <!-- 模型地址 -->
                    <FormItem label="模型地址" type="string" v-model="baTable.form.items!.url" prop="url" placeholder="请输入模型地址" />
                    <!-- 是否显示 -->
                    <el-form-item label="是否显示" prop="status">
                        <el-switch v-model="baTable.form.items!.status" :active-value="1" :inactive-value="0" />
                    </el-form-item>
                    <!-- <FormItem
                        :label="t('modemanage.status')"
                        type="switch"
                        v-model="baTable.form.items!.status"
                        prop="status"
                        :input-attr="{ size: 'large' }"
                        :placeholder="t('Please switch field', { field: t('modemanage.status') })"
                    /> -->
                    <FormItem
                        label="所属项目"
                        type="remoteSelect"
                        v-model="baTable.form.items!.project_id"
                        prop="project_id"
                        :input-attr="{ pk: 'project.id', field: 'name', remoteUrl: '/admin/Project/index' }"
                        placeholder="请选择项目"
                    />
                </el-form>
            </div>
        </el-scrollbar>
        <template #footer>
            <div :style="'width: calc(100% - ' + baTable.form.labelWidth! / 1.8 + 'px)'">
                <el-button @click="baTable.toggleForm()">{{ t('Cancel') }}</el-button>
                <el-button v-blur :loading="baTable.form.submitLoading" @click="baTable.onSubmit(formRef)" type="primary">
                    {{ baTable.form.operateIds && baTable.form.operateIds.length > 1 ? t('Save and edit next item') : t('Save') }}
                </el-button>
            </div>
        </template>
    </el-dialog>
</template>

<script setup lang="ts">
import type { FormItemRule } from 'element-plus'
import { inject, reactive, useTemplateRef } from 'vue'
import { useI18n } from 'vue-i18n'
import FormItem from '/@/components/formItem/index.vue'
import type baTableClass from '/@/utils/baTable'
import { buildValidatorData } from '/@/utils/validate'
import { useConfig } from '/@/stores/config'
// ------------------------- 生命周期 -------------------------

// ------------------------- 计算属性 -------------------------

// ------------------------- 变量 -------------------------
const config = useConfig()
const formRef = useTemplateRef('formRef')
const baTable = inject('baTable') as baTableClass

const { t } = useI18n()

const rules: Partial<Record<string, FormItemRule[]>> = reactive({
    name: [buildValidatorData({ name: 'required', title: '模型名称' })],
    url: [buildValidatorData({ name: 'required', title: '模型地址' })],
    project_id: [buildValidatorData({ name: 'required', title: '所属项目' })],
})
// ------------------------- 监听 -------------------------

// ------------------------- 方法 -------------------------
</script>

<style scoped lang="less"></style>
