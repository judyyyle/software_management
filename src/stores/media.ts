import { defineStore } from 'pinia'
import { baTableApi } from '/@/api/common'

export const useMedia = defineStore('media', {
    state: (): any => {
        return {
            mediaConfig: {},
            // 直播模式 1:声网 2:RTMP流媒体（从环境变量读取，默认为1）
            video_type: Number(import.meta.env.VITE_VIDEO_TYPE) || 1,
        }
    },
    actions: {
        async getMediaConfig() {
            const res = await new baTableApi('/admin/routine.Config/').index()
            this.mediaConfig = res.data
        },
        changeVideoType: (type: number) => {
            this.video_type = type
        },
    },
    getters: {
        uploadList: (state: any) => {
            if (state.mediaConfig.list) {
                return state.mediaConfig.list.upload.list
            } else {
                return []
            }
        },
        uploadApi: (state: any) => {
            return 'https://' + state.uploadList[state.uploadList.length - 1].value
        },
    },
})
