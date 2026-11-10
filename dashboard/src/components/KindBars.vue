<script setup>
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'
import { PALETTE, tooltipStyle } from '../echarts'
import { fmtMoney, fmtSigned } from '../logic/aggregate'

/**
 * 交易类型净额柱状图。
 *
 * 为什么不用饼图：退款 net 为负数，饼图/环形图/玫瑰图都按面积编码，负值无法表达——
 * 要么整块消失，要么按绝对值画而让 -3.97 万看起来和 +190 万一样大。柱子有方向，
 * 负值向下延伸是图形本身的语义，不需要任何取巧。
 *
 * 退款只占净额 2%，柱高必然很矮。所以 y 轴下界留出 18% 的空间给负值区，
 * 让标签有地方落，而不是把退款柱放大成不真实的长度。
 */
const props = defineProps({
  items: { type: Array, required: true }, // [{name, net, tx_count, gross}]
  height: { type: [String, Number], default: '240px' },
  active: { type: String, default: '' }
})
const emit = defineEmits(['pick'])

const KIND_COLORS = {
  消费: PALETTE.accent,
  预授权: PALETTE.accent2,
  未分类: '#7d8598',
  退款: PALETTE.teal
}
const FALLBACK = [PALETTE.accent, PALETTE.accent2, PALETTE.blue, PALETTE.teal, PALETTE.violet]

const totalAbs = computed(
  () => props.items.reduce((s, x) => s + Math.abs(x.net), 0) || 1
)

const option = computed(() => {
  const items = props.items
  const peak = Math.max(1, ...items.map((x) => x.net))

  return {
    animationDuration: 650,
    animationEasing: 'cubicOut',
    grid: { left: 56, right: 16, top: 28, bottom: 30 },
    tooltip: {
      trigger: 'item',
      ...tooltipStyle,
      formatter: (p) => {
        const it = items[p.dataIndex]
        if (!it) return ''
        const share = ((Math.abs(it.net) / totalAbs.value) * 100).toFixed(1)
        const avg = it.tx_count ? it.net / it.tx_count : 0
        return `<div style="font-size:12px;color:#8b95a7;margin-bottom:3px">${it.name}</div>
          <div style="font-size:15px;font-weight:600;color:${it.net < 0 ? PALETTE.teal : '#ffb08a'}">
            ${fmtSigned(it.net)} 元</div>
          <div style="font-size:11.5px;color:#8b95a7;margin-top:3px">
            ${fmtMoney(it.tx_count)} 笔 · 笔均 ${fmtSigned(avg)} · 占比 ${share}%</div>`
      }
    },
    xAxis: {
      type: 'category',
      data: items.map((x) => x.name),
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.12)' } },
      axisTick: { show: false },
      axisLabel: { color: PALETTE.axis, fontSize: 11, interval: 0, margin: 8 }
    },
    yAxis: {
      type: 'value',
      min: -peak * 0.11,
      axisLine: { show: false },
      axisTick: { show: false },
      // 负值区只是给退款标签留的落点，那一格的刻度值没有参考意义，不显示
      axisLabel: {
        color: PALETTE.axis,
        fontSize: 10,
        formatter: (v) => (v < 0 ? '' : v === 0 ? '0' : fmtSigned(v))
      },
      splitLine: { lineStyle: { color: PALETTE.grid } }
    },
    series: [
      {
        type: 'bar',
        barWidth: '46%',
        cursor: 'pointer',
        markLine: {
          silent: true,
          symbol: 'none',
          animation: false,
          label: { show: false },
          lineStyle: { color: 'rgba(255,255,255,0.34)', width: 1, type: 'solid' },
          data: [{ yAxis: 0 }]
        },
        data: items.map((it, i) => {
          const neg = it.net < 0
          const dimmed = props.active && props.active !== it.name
          return {
            name: it.name,
            value: Math.round(it.net * 100) / 100,
            itemStyle: {
              color: KIND_COLORS[it.name] || FALLBACK[i % FALLBACK.length],
              borderRadius: neg ? [0, 0, 4, 4] : [4, 4, 0, 0],
              opacity: dimmed ? 0.3 : 1
            },
            label: {
              show: true,
              position: neg ? 'bottom' : 'top',
              distance: 5,
              formatter: () => fmtSigned(it.net),
              color: neg ? PALETTE.teal : '#cfd6e4',
              fontSize: 10.5,
              fontWeight: 600,
              opacity: dimmed ? 0.4 : 1
            }
          }
        })
      }
    ]
  }
})
</script>

<template>
  <BaseChart :option="option" :height="height" @chart-click="(p) => emit('pick', p.name)" />
</template>
