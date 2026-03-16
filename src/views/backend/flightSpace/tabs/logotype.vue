<template>
    <div class="logotype">
        <div class="logotype-header">
            <div class="logotype-header-left">地图模型</div>
            <el-tooltip class="box-item" effect="dark" content="导入新模型" placement="top">
                <el-icon style="cursor: pointer" @click="popOperate = 'Add'"><Plus /></el-icon>
            </el-tooltip>
        </div>
        <div>
            <el-collapse accordion>
                <el-collapse-item>
                    <template #title="{ isActive }">
                        <div :class="['title-wrapper', { 'is-active': isActive }]">
                            <div class="title-wrapper-left">
                                <span>{{ currentProject.name }}</span>
                            </div>
                            <div class="title-wrapper-right">共{{ mapModel.modelList.length }}处</div>
                        </div>
                    </template>
                    <div class="logotype-content">
                        <div
                            class="logotype-item"
                            v-for="(item, index) in modelList"
                            :key="index"
                            :class="{ 'logotype-item-active': activeIndex == `1-${index}` }"
                            @click="activeIndex = `1-${index}`"
                        >
                            <div class="logotype-item-left">
                                <img class="logotype-item-image" src="/img/image/ban-icon.png" alt="" />
                                <span>{{ item.name }}</span>
                            </div>
                            <div class="logotype-item-right">
                                <!-- 开关 -->
                                <el-tooltip class="box-item" effect="dark" :content="item.status == 1 ? '隐藏' : '显示'" placement="top">
                                    <el-switch v-model="item.status" :active-value="1" :inactive-value="0" @change="handleSwitch(item)" />
                                </el-tooltip>

                                <el-tooltip class="box-item" effect="dark" content="定位" placement="top">
                                    <img class="logotype-item-image" src="/img/image/ban-icon2.png" alt="" @click="handleItem(item)" />
                                </el-tooltip>
                                <el-popconfirm class="box-item" title="是否确认要删除该地图模型？" placement="top" @confirm="handleDelete(item)">
                                    <template #reference>
                                        <img class="logotype-item-image" src="/img/image/ban-icon1.png" alt="" />
                                    </template>
                                </el-popconfirm>
                            </div>
                        </div>
                    </div>
                </el-collapse-item>
            </el-collapse>
        </div>

        <!-- 添加模型 -->
        <AddPop @submitSuccess="getModelList" />
    </div>
</template>

<script setup lang="ts">
import { ref, inject, watch, provide, onMounted } from 'vue'
import { useMqttStore } from '/@/stores/mqtt'
import { baTableApi } from '/@/api/common'
import { Plus } from '@element-plus/icons-vue'
import { useProjectStore } from '/@/stores/project'
import { useMapModel } from '/@/stores/mapModel'
import { storeToRefs } from 'pinia'
import AddPop from '../components/logoType/addPop.vue'
import { ElMessage } from 'element-plus'

onMounted(() => {
    getModelList()
})

const mapModel = useMapModel()
const { modelList } = storeToRefs(mapModel)

const mqttStore = useMqttStore()

const popOperate = ref('')
provide('popOperate', popOperate)

const projectStore = useProjectStore()
const { currentProject, aerodromeList } = storeToRefs(projectStore)

watch(currentProject, async (val) => {
    await getModelList()
})

const activeIndex = ref('')

// 地图模型
const api = new baTableApi('/admin/modemanage/')
provide('api', api)

// 获取模型列表
const getModelList = async () => {
    const res = await api.index({
        search: [{ field: 'project_id', val: currentProject.value?.id, operator: '=' }],
    })
    mapModel.modelList = res.data.list || []
    // 添加地图模型
    mapModel.modelList.forEach((item: any) => {
        mapModel.addModel(item)
    })
}

// 删除地图模型
const handleDelete = async (item: any) => {
    await api.del({
        id: item.id,
    })
    getModelList()
}

// 切换地图模型可见性
const handleSwitch = async (item: any) => {
    handleSwitchStatus(item)
}

// 切换地图可见性接口
const handleSwitchStatus = async (item: any) => {
    try {
        await api.editPost({
            id: item.id,
            status: item.status,
        })
        ElMessage.success('操作成功')
        getModelList()
    } catch (error) {
        console.log(error)
    }
}

const handleItem = (item: any) => {
    // 找到对应的模型
    const model = mapModel.graphicLayer_model.find((layer: any) => layer.id == item.id)
    console.log(model)
    if (model) {
        // 跳转到模型
        model.flyTo()
    }
}
</script>

<style scoped lang="scss">
.logotype {
    width: 100%;
    height: 100%;

    &-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 14px;
        font-weight: bold;
        border-bottom: 1px solid #e5e5e5;
        padding-bottom: 10px;
    }

    .title-wrapper {
        width: 100%;
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-right: 10px;

        .title-wrapper-left {
            display: flex;
            align-items: center;
            gap: 4px;

            .title-wrapper-image {
                width: 16px;
                height: 16px;
            }

            span {
                font-size: 14px;
            }
        }

        .title-wrapper-right {
            color: #00000099;
            font-size: 12px;
        }
    }

    .logotype-content {
        width: 100%;
        padding: 0 10px;
        display: flex;
        flex-direction: column;
        border-left: 1px solid #e5e5e5;

        .logotype-item {
            width: 100%;
            height: 30px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            cursor: pointer;
            border-radius: 4px;
            padding-right: 10px;

            &:hover {
                background: #f6f6f6;
            }

            &-active {
                background: #daf4ff;
            }

            &-left {
                display: flex;
                align-items: center;
                color: #000000;
                padding: 0 10px;
                gap: 10px;

                .logotype-item-image {
                    width: 16px;
                    height: 16px;
                }
            }

            &-right {
                display: flex;
                align-items: center;
                gap: 10px;

                .logotype-item-image {
                    width: 16px;
                    height: 16px;
                }

                .switch-box {
                    width: 30px;
                    height: 16px;
                    padding: 2px;
                    border-radius: 8px;

                    &-active {
                        background: #409eff;
                    }

                    &-inactive {
                        background: #c0c4cc;
                    }

                    .switch-item {
                        width: 12px;
                        height: 12px;
                        border-radius: 50%;
                        transition: all 0.3s ease-in-out;
                        background: #fff;

                        &-active {
                            transform: translateX(14px);
                        }

                        &-inactive {
                            transform: translateX(0);
                        }
                    }
                }
            }
        }
    }
}
</style>
