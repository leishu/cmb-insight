<script setup>
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'
import { axisStyle, PALETTE, tooltipStyle } from '../echarts'
import { fmtMoney, fmtShort } from '../logic/aggregate'

const props = defineProps({
  months: { type: Array, required: true }, // [{name, tx_count, net, gross}]
  activeMonth: { type: String, default: '' },
  height: { type: String, default: '250px' }
})
const emit = defineEmits(['pick'])

const BAR_FILL = {
  type: 'linear',
  x: 0, y: 0, x2: 0, y2: 1,
  colorStops: [
    { offset: 0, color: 'rgba(255,122,69,.95)' },
    { offset: 1, color: 'rgba(255,122,69,.32)' }
  ]
}

const option = computed(() => {
  const names = props.months.map((m) => m.name)
  const nets = props.months.map((m) => Math.round(m.net))
  const avg = nets.length ? Math.round(nets.reduce((a, b) => a + b, 0) / nets.length) : 0
  const step = Math.max(1, Math.ceil(names.length / 14))

  return {
    animationDuration: 620,
    animationEasing: 'cubicOut',
    grid: { left: 6, right: 14, top: 28, bottom: 22, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(255,255,255,.045)' } },
      ...tooltipStyle,
      formatter: (ps) => {
        const m = props.months[ps[0].dataIndex]
        if (!m) return ''
        return `<div style="font-size:12px;color:#8b95a7;margin-bottom:4px">${m.name}</div>
          <div style="font-size:15px;font-weight:600;color:#ffb08a">¥${fmtMoney(m.net)}</div>
          <div style="font-size:11.5px;color:#8b95a7;margin-top:3px">${m.tx_count} 笔 · 笔均 ¥${fmtMoney(m.net / Math.max(1, m.tx_count))}</div>
          <div style="font-size:10.5px;color:#5d6675;margin-top:4px">点击可按该月筛选</div>`
      }
    },
    xAxis: {
      type: 'category',
      data: names,
      ...axisStyle,
      splitLine: { show: false },
      axisLabel: {
        color: PALETTE.axis,
        fontSize: 10.5,
        interval: (i) => i % step === 0,
        formatter: (v) => (v ? v.slice(2) : v)
      }
    },
    yAxis: {
      type: 'value',
      ...axisStyle,
      axisLine: { show: false },
      axisLabel: { color: PALETTE.axis, fontSize: 10.5, formatter: (v) => fmtShort(v) }
    },
    series: [
      {
        name: '净支出',
        type: 'bar',
        barMaxWidth: 30,
        data: nets.map((v, i) => ({
          value: v,
          itemStyle: {
            borderRadius: [3, 3, 0, 0],
            color: names[i] === props.activeMonth ? PALETTE.accent2 : BAR_FILL
          }
        })),
        emphasis: { itemStyle: { color: PALETTE.accent2 } },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: 'rgba(255,197,61,.45)', type: 'dashed', width: 1 },
          label: {
            formatter: `月均 ¥${fmtShort(avg)}`,
            color: '#8b95a7',
            fontSize: 10.5,
            position: 'insideEndTop'
          },
          data: [{ yAxis: avg }]
        }
      }
    ]
  }
})
</script>

<template>
  <BaseChart :option="option" :height="height" @chart-click="(p) => emit('pick', p.name)" />
</template>
