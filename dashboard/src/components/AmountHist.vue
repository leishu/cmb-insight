<script setup>
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'
import { axisStyle, tooltipStyle } from '../echarts'
import { fmtMoney, fmtShort } from '../logic/aggregate'

const props = defineProps({
  bins: { type: Array, required: true }, // [{label, count, net}]
  height: { type: String, default: '220px' }
})

const option = computed(() => ({
  animationDuration: 620,
  grid: { left: 6, right: 10, top: 26, bottom: 22, containLabel: true },
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(255,255,255,.045)' } },
    ...tooltipStyle,
    formatter: (ps) => {
      const b = props.bins[ps[0].dataIndex]
      return `<div style="font-size:12px;color:#8b95a7;margin-bottom:3px">金额区间 ¥${b.label}</div>
        <div style="font-size:14px;font-weight:600;color:#ffb08a">${b.count} 笔</div>
        <div style="font-size:11.5px;color:#8b95a7;margin-top:2px">合计 ¥${fmtMoney(b.net)}</div>`
    }
  },
  xAxis: {
    type: 'category',
    data: props.bins.map((b) => b.label),
    ...axisStyle,
    splitLine: { show: false },
    axisLabel: { color: '#7d8598', fontSize: 10, rotate: 0 }
  },
  yAxis: [
    {
      type: 'value',
      ...axisStyle,
      axisLine: { show: false },
      axisLabel: { color: '#7d8598', fontSize: 10 }
    },
    {
      type: 'value',
      show: false
    }
  ],
  series: [
    {
      name: '笔数',
      type: 'bar',
      barMaxWidth: 30,
      data: props.bins.map((b) => b.count),
      itemStyle: {
        borderRadius: [3, 3, 0, 0],
        color: {
          type: 'linear',
          x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(76,141,255,.9)' },
            { offset: 1, color: 'rgba(76,141,255,.22)' }
          ]
        }
      }
    },
    {
      name: '金额',
      type: 'line',
      yAxisIndex: 1,
      smooth: true,
      symbol: 'circle',
      symbolSize: 4,
      data: props.bins.map((b) => Math.round(b.net)),
      lineStyle: { color: 'rgba(255,197,61,.75)', width: 1.6 },
      itemStyle: { color: '#ffc53d' }
    }
  ]
}))
</script>

<template>
  <BaseChart :option="option" :height="height" />
</template>
