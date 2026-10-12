import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import SafetyAlert from './components/SafetyAlert.vue'

describe('危机支持与医疗提醒', () => {
  it('自伤支持先关心人，不默认猜测所在地或显示其他国家的热线', async () => {
    const wrapper = mount(SafetyAlert, { props: { risk: { level: 'emergency', kind: 'self_harm', message: '请让信任的人陪着你。', reasons: [], source_ids: [] } } })
    expect(wrapper.text()).toContain('先让你得到陪伴和支持')
    expect(wrapper.text()).not.toContain('先关注紧急情况')
    expect(wrapper.find('a[href^="tel:"]').exists()).toBe(false)
    await wrapper.get('select').setValue('CN')
    expect(wrapper.find('a[href^="tel:"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('当前没有可提供的已核实本地区心理支持热线')
    await wrapper.get('select').setValue('SG')
    expect(wrapper.get('a[href^="tel:"]').attributes('href')).toBe('tel:1767?oai_link_source=model_response_hotline')
    await wrapper.get('select').setValue('other')
    expect(wrapper.find('a[href^="tel:"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('医疗风险保留原因和不能排除疾病的提醒', () => {
    const wrapper = mount(SafetyAlert, { props: { risk: { level: 'emergency', kind: 'medical', message: '及时获得急救帮助', reasons: ['当前危险信号'], source_ids: [] } } })
    expect(wrapper.text()).toContain('先关注紧急情况')
    expect(wrapper.text()).toContain('当前危险信号')
    expect(wrapper.text()).toContain('也不能排除疾病')
    expect(wrapper.find('select').exists()).toBe(false)
    wrapper.unmount()
  })
})
