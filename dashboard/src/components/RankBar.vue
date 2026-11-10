<script setup>
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'
import { tooltipStyle } from '../echarts'
import { fmtMoney, fmtShort } from '../logic/aggregate'

const props = defineProps({
  items: { type: Array, required: true }, // [{name, net, tx_count}]，调用方按金额降序传
  height: { type: String, default: '270px' },
  color: { type: String, default: '#ff7a45' },
  active: { type: String, default: '' },
  labelWidth: { type: Number, default: 96 }
})
const emit = defineEmits(['pick'])

const option = computed(() => {
  // 横向条形图从下往上画，倒序后最大的排在最上面
  const list = [...props.items].reverse()
  return {
    animationDuration: 650,
    animationEasing: 'cubicOut',
    grid: { left: 4, right: 46, top: 8, bottom: 6, containLabel: true },
    tooltip: {
      trigger: 'item',
      ...tooltipStyle,
      formatter: (p) => {
        const it = list[p.dataIndex]
        return `<div style="font-size:12px;color:#e6e9f0;margin-bottom:3px;max-width:240px;white-space:normal">${it.name}</div>
          <div style="font-size:14px;font-weight:600;color:#ffb08a">¥${fmtMoney(it.net)}</div>
          <div style="font-size:11.5px;color:#8b95a7;margin-top:2px">${it.tx_count} 笔 · 笔均 ¥${fmtMoney(it.net / Math.max(1, it.tx_count))}</div>`
      }
    },
    xAxis: { type: 'value', show: false },
    yAxis: {
      type: 'category',
      data: list.map((x) => x.name),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: '#98a2b3',
        fontSize: 11,
        width: props.labelWidth,
        overflow: 'truncate',
        formatter: (v) => (v.length > 12 ? v.slice(0, 11) + '…' : v)
      }
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 15,
        data: list.map((x) => ({
          value: Math.round(x.net),
          itemStyle: {
            borderRadius: [0, 3, 3, 0],
            color: x.name === props.active
              ? '#ffc53d'
              : {
                  type: 'linear',
                  x: 0, y: 0, x2: 1, y2: 0,
                  colorStops: [
                    { offset: 0, color: hexA(props.color, 0.28) },
                    { offset: 1, color: hexA(props.color, 0.92) }
                  ]
                }
          }
        })),
        label: {
          show: true,
          position: 'right',
          color: '#8b95a7',
          fontSize: 10.5,
          formatter: (p) => fmtShort(p.value)
        },
        emphasis: { itemStyle: { color: '#ffc53d' } }
      }
    ]
  }
})

function hexA(hex, a) {
  const n = parseInt(hex.replace('#', ''), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}
</script>

<template>
  <BaseChart :option="option" :height="height" @chart-click="(p) => emit('pick', p.name)" />
</template>
