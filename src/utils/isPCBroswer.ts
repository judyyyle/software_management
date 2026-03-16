/**
 * 浏览器
 *
 * @returns {void}
 */
export function isPCBroswer() {
    const sUserAgent = navigator.userAgent.toLowerCase()

    const bIsIpad = (sUserAgent.match(/ipad/i) as unknown as string) === 'ipad'
    const bIsIphoneOs = (sUserAgent.match(/iphone/i) as unknown as string) === 'iphone'
    const bIsMidp = (sUserAgent.match(/midp/i) as unknown as string) === 'midp'
    const bIsUc7 = (sUserAgent.match(/rv:1.2.3.4/i) as unknown as string) === 'rv:1.2.3.4'
    const bIsUc = (sUserAgent.match(/ucweb/i) as unknown as string) === 'ucweb'
    const bIsAndroid = (sUserAgent.match(/android/i) as unknown as string) === 'android'
    const bIsCE = (sUserAgent.match(/windows ce/i) as unknown as string) === 'windows ce'
    const bIsWM = (sUserAgent.match(/windows mobile/i) as unknown as string) === 'windows mobile'
    if (bIsIpad || bIsIphoneOs || bIsMidp || bIsUc7 || bIsUc || bIsAndroid || bIsCE || bIsWM) {
        return false
    } else {
        return true
    }
}
