<script setup>
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'
import { tooltipStyle } from '../echarts'
import { WEEK, fmtMoney, fmtShort } from '../logic/aggregate'

const props = defineProps({
  grid: { type: Object, required: true }, // { cells, max, count }
  height: { type: String, default: '240px' }
})

const hours = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}`)

const option = computed(() => ({
  animationDuration: 700,
  grid: { left: 4, right: 12, top: 10, bottom: 40, containLabel: true },
  tooltip: {
    ...tooltipStyle,
    formatter: (p) => {
      const [h, d, v] = p.data
      return `<div style="font-size:12px;color:#8b95a7">${WEEK[d]} ${hours[h]}:00</div>
        <div style="font-size:14px;font-weight:600;color:#ffb08a;margin-top:2px">¥${fmtMoney(v)}</div>`
    }
  },
  xAxis: {
    type: 'category',
    data: hours,
    splitArea: { show: false },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#7d8598', fontSize: 10, interval: 1 }
  },
  yAxis: {
    type: 'category',
    data: WEEK,
    splitArea: { show: false },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#98a2b3', fontSize: 11 }
  },
  visualMap: {
    min: 0,
    max: Math.max(1, props.grid.max),
    calculable: false,
    orient: 'horizontal',
    left: 'center',
    bottom: 0,
    itemWidth: 9,
    itemHeight: 90,
    textStyle: { color: '#5d6675', fontSize: 10 },
    inRange: {
      color: ['#1c2029', '#4a2a22', '#8a3d24', '#c85a2a', '#ff7a45', '#ffc53d']
    }
  },
  series: [
    {
      type: 'heatmap',
      data: props.grid.cells,
      itemStyle: { borderRadius: 2, borderColor: '#0d0f14', borderWidth: 1.5 },
      emphasis: { itemStyle: { borderColor: '#ffc53d', borderWidth: 1.5 } },
      progressive: 2000
    }
  ]
}))
</script>

<template>
  <BaseChart :option="option" :height="height" />
</template>
