// ECharts 按需引入：只打包用到的图表与组件，减小单文件体积。
import * as echarts from 'echarts/core'
import { BarChart, LineChart, HeatmapChart, CustomChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  VisualMapComponent,
  MarkLineComponent,
  GraphicComponent,
  TitleComponent
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { LabelLayout, UniversalTransition } from 'echarts/features'

echarts.use([
  BarChart, LineChart, HeatmapChart, CustomChart,
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
  VisualMapComponent, MarkLineComponent, GraphicComponent, TitleComponent,
  LabelLayout, UniversalTransition, CanvasRenderer
])

export const PALETTE = {
  accent: '#ff7a45',
  accent2: '#ffc53d',
  teal: '#36cfc9',
  blue: '#4c8dff',
  violet: '#a06bff',
  grid: 'rgba(255,255,255,0.055)',
  axis: '#7d8598',
  text: '#e6e9f0'
}

export const BASE_TEXT = {
  color: PALETTE.text,
  fontFamily: 'system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif'
}

/** 统一的 tooltip 外观 */
export const tooltipStyle = {
  backgroundColor: 'rgba(20,24,32,.96)',
  borderColor: '#333b4a',
  borderWidth: 1,
  padding: [8, 11],
  textStyle: { color: '#e6e9f0', fontSize: 12 },
  extraCssText: 'border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.45);'
}

export const axisStyle = {
  axisLine: { lineStyle: { color: 'rgba(255,255,255,0.12)' } },
  axisTick: { show: false },
  axisLabel: { color: PALETTE.axis, fontSize: 11 },
  splitLine: { lineStyle: { color: PALETTE.grid } }
}

export default echarts
