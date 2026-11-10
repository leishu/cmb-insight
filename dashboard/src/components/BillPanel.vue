<script setup>
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'
import { fmtMoney, fmtShort, fmtSigned } from '../logic/aggregate'

const props = defineProps({
  bills: { type: Array, default: () => [] },
  credit: { type: Array, default: () => [] }
})

/* bills: [月, 到期日, 应还, 最低, 本期消费, 利息, 对账差额] */
const latest = computed(() => props.bills[props.bills.length - 1] || null)

const daysLeft = computed(() => {
  if (!latest.value || !latest.value[1]) return null
  const due = new Date(`${latest.value[1]}T00:00:00`)
  const now = new Date()
  return Math.round((due - now) / 86400000)
})

const dueTone = computed(() => {
  const d = daysLeft.value
  if (d === null) return 'var(--text)'
  if (d < 0) return 'var(--accent)'
  if (d <= 7) return 'var(--accent-2)'
  return 'var(--text)'
})

const lastAvail = computed(() => {
  const c = props.credit
  return c.length ? c[c.length - 1] : null
})

const usedPct = computed(() => {
  const c = lastAvail.value
  if (!c || !c[3]) return null
  return Math.round(((c[2] || 0) / c[3]) * 1000) / 10
})

/* 每期取月末最后一个点，画额度使用率；有临时额度的月份（可用 > 固定额度）跳过 */
const usage = computed(() => {
  const byMonth = new Map()
  for (const [dt, avail, used, limit] of props.credit) {
    if (!limit || avail > limit) continue
    byMonth.set(dt.slice(0, 7), Math.round((used / limit) * 1000) / 10)
  }
  return [...byMonth.entries()].slice(-36)
})

const option = computed(() => ({
  grid: { left: 42, right: 12, top: 18, bottom: 26 },
  tooltip: {
    trigger: 'axis',
    backgroundColor: '#1b2029',
    borderColor: '#262c38',
    textStyle: { color: '#e6e9f0', fontSize: 11 },
    valueFormatter: (v) => `${v}%`
  },
  xAxis: {
    type: 'category',
    data: usage.value.map((d) => d[0].slice(2)),
    axisLine: { lineStyle: { color: '#262c38' } },
    axisLabel: { color: '#5d6675', fontSize: 10, interval: Math.max(0, Math.floor(usage.value.length / 8) - 1) },
    axisTick: { show: false }
  },
  yAxis: {
    type: 'value',
    axisLabel: { color: '#5d6675', fontSize: 10, formatter: '{value}%' },
    splitLine: { lineStyle: { color: 'rgba(38,44,56,.7)' } }
  },
  series: [
    {
      type: 'line',
      smooth: true,
      symbol: 'none',
      data: usage.value.map((d) => d[1]),
      lineStyle: { width: 2, color: '#36cfc9' },
      areaStyle: {
        color: {
          type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(54,207,201,.28)' },
            { offset: 1, color: 'rgba(54,207,201,0)' }
          ]
        }
      },
      markLine: {
        silent: true,
        symbol: 'none',
        label: { position: 'insideStartTop', color: '#5d6675', fontSize: 10, formatter: '30%' },
        lineStyle: { color: 'rgba(255,197,61,.55)', type: 'dashed' },
        data: [{ yAxis: 30 }]
      }
    }
  ]
}))

const recent = computed(() => [...props.bills].slice(-6).reverse())

function diffTone(v) {
  if (v === null || v === undefined) return 'var(--dim)'
  return Math.abs(v) > 1000 ? 'var(--accent-2)' : 'var(--muted)'
}
</script>

<template>
  <div class="bill-wrap">
    <div class="bill-kpis">
      <div class="tile">
        <div class="k">本期应还</div>
        <div class="v">¥{{ fmtMoney(latest ? latest[2] : 0) }}</div>
        <div class="s">最低 ¥{{ fmtMoney(latest ? latest[3] : 0) }}</div>
      </div>
      <div class="tile">
        <div class="k">到期还款日</div>
        <div class="v" :style="{ color: dueTone }">{{ latest ? latest[1] || '—' : '—' }}</div>
        <div class="s">
          <template v-if="daysLeft !== null">
            {{ daysLeft >= 0 ? `还有 ${daysLeft} 天` : `已逾期 ${-daysLeft} 天` }}
          </template>
        </div>
      </div>
      <div class="tile">
        <div class="k">可用额度</div>
        <div class="v">¥{{ fmtMoney(lastAvail ? lastAvail[1] : 0) }}</div>
        <div class="s">额度上限 ¥{{ fmtShort(lastAvail ? lastAvail[3] : 0) }}</div>
      </div>
      <div class="tile">
        <div class="k">已用额度</div>
        <div class="v" :style="{ color: usedPct > 70 ? 'var(--accent)' : 'var(--text)' }">
          {{ usedPct === null ? '—' : `${usedPct}%` }}
        </div>
        <div class="s">已用 ¥{{ fmtShort(lastAvail ? lastAvail[2] : 0) }}</div>
      </div>
    </div>

    <div class="bill-body">
      <div class="bill-chart">
        <div class="cap">额度使用率（按月）</div>
        <BaseChart :option="option" height="188px" />
      </div>
      <div class="bill-table">
        <div class="cap">最近账单 · 与提醒流对账</div>
        <table>
          <thead>
            <tr><th>账单月</th><th>应还</th><th>本期消费</th><th>差额</th></tr>
          </thead>
          <tbody>
            <tr v-for="b in recent" :key="b[0]">
              <td>{{ b[0] }}</td>
              <td class="num">{{ fmtMoney(b[2]) }}</td>
              <td class="num">{{ b[4] === null ? '—' : fmtMoney(b[4]) }}</td>
              <td class="num" :style="{ color: diffTone(b[6]) }">
                {{ b[6] === null || b[6] === undefined ? '—' : fmtSigned(b[6]) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bill-wrap { display: flex; flex-direction: column; gap: 14px; }
.bill-kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.tile {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel-2);
  padding: 10px 12px;
}
.tile .k { font-size: 11px; color: var(--muted); }
.tile .v { font-size: 19px; font-weight: 500; margin-top: 5px; letter-spacing: .3px; }
.tile .s { font-size: 11px; color: var(--dim); margin-top: 4px; }
.bill-body { display: grid; grid-template-columns: 1.35fr 1fr; gap: 14px; }
.bill-chart, .bill-table { min-width: 0; }
.cap { font-size: 11.5px; color: var(--muted); margin-bottom: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
th {
  text-align: right;
  font-weight: 400;
  color: var(--dim);
  padding: 4px 6px;
  border-bottom: 1px solid var(--line);
}
th:first-child, td:first-child { text-align: left; }
td { padding: 5px 6px; border-bottom: 1px solid rgba(38, 44, 56, .55); color: var(--text); }
td.num { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
@media (max-width: 900px) {
  .bill-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .bill-body { grid-template-columns: 1fr; }
}
</style>
