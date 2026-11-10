<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  amountHist, byChannel, byKind, byMerchant, byMonth, decode,
  facetOptions, fmtMoney, fmtShort, heatGrid, loadPayload, periodCompare, summarize, WEEK
} from './logic/aggregate'
import TrendChart from './components/TrendChart.vue'
import KindBars from './components/KindBars.vue'
import RankBar from './components/RankBar.vue'
import HeatMap from './components/HeatMap.vue'
import AmountHist from './components/AmountHist.vue'
import TxTable from './components/TxTable.vue'
import BillPanel from './components/BillPanel.vue'

const loading = ref(true)
const error = ref('')
const rows = ref([])
const meta = ref({})
const bills = ref([])
const credit = ref([])

const filters = reactive({ year: '', kind: '', card: '', channel: '', keyword: '' })
const keywordInput = ref('')
const activeMonth = ref('')
const playing = ref(false)
let playTimer = null

/* ---------------- 数据加载 ---------------- */
onMounted(async () => {
  try {
    const payload = await loadPayload()
    rows.value = decode(payload)
    meta.value = payload.meta || {}
    bills.value = payload.bills || []
    credit.value = payload.credit || []
  } catch (e) {
    error.value = e.message || String(e)
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => playTimer && clearInterval(playTimer))

/* ---------------- 关键词防抖 ---------------- */
let kwTimer = null
watch(keywordInput, (v) => {
  clearTimeout(kwTimer)
  kwTimer = setTimeout(() => (filters.keyword = v.trim()), 260)
})

/* ---------------- 筛选 ---------------- */
const facets = computed(() => facetOptions(rows.value))

// scope：全局筛选（年份/类型/卡号/关键词），下钻不影响它
const scope = computed(() => {
  const kw = filters.keyword.toLowerCase()
  return rows.value.filter((r) => {
    if (filters.year && r.year !== Number(filters.year)) return false
    if (filters.kind && r.kind !== filters.kind) return false
    if (filters.card && r.card !== filters.card) return false
    if (filters.channel && (r.channel || '未标注') !== filters.channel) return false
    if (kw && !(r.merchant.toLowerCase().includes(kw) || r.channel.toLowerCase().includes(kw))) {
      return false
    }
    return true
  })
})

// focused：再叠加下钻月份，除趋势图外都看这一份
const focused = computed(() =>
  activeMonth.value ? scope.value.filter((r) => r.month === activeMonth.value) : scope.value
)

const stat = computed(() => summarize(focused.value))
const months = computed(() => byMonth(scope.value))
// 环比/同比跟着筛选走：只看某一年时，就是那一年的月度变化
const cmp = computed(() => periodCompare(scope.value))
const pctText = (v) => (v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v}%`)

/* ---------------- KPI 数字滚动 ---------------- */
const anim = reactive({ net: 0, count: 0, avg: 0, merchants: 0 })

watch(
  stat,
  (s) => {
    const target = { net: s.net, count: s.count, avg: s.avg, merchants: s.merchants }
    const from = { ...anim }
    const t0 = performance.now()
    const dur = 560
    const step = (t) => {
      const p = Math.min(1, (t - t0) / dur)
      const e = 1 - Math.pow(1 - p, 3)
      for (const k in target) anim[k] = from[k] + (target[k] - from[k]) * e
      if (p < 1) requestAnimationFrame(step)
    }
    requestAnimationFrame(step)
  },
  { immediate: true }
)

/* ---------------- 下钻 / 播放 ---------------- */
function pickMonth(name) {
  stopPlay()
  activeMonth.value = activeMonth.value === name ? '' : name
}

function stopPlay() {
  playing.value = false
  if (playTimer) {
    clearInterval(playTimer)
    playTimer = null
  }
}

function togglePlay() {
  if (playing.value) return stopPlay()
  const list = months.value.map((m) => m.name)
  if (!list.length) return
  playing.value = true
  let i = Math.max(0, list.indexOf(activeMonth.value))
  activeMonth.value = list[i]
  playTimer = setInterval(() => {
    i += 1
    if (i >= list.length) return stopPlay()
    activeMonth.value = list[i]
  }, 430)
}

function reset() {
  stopPlay()
  filters.year = ''
  filters.kind = ''
  filters.card = ''
  filters.channel = ''
  filters.keyword = ''
  keywordInput.value = ''
  activeMonth.value = ''
}

const hasFilter = computed(
  () => !!(filters.year || filters.kind || filters.card || filters.channel
    || filters.keyword || activeMonth.value)
)

const range = computed(() => {
  const m = meta.value
  if (m.tx_date_range) return `${m.tx_date_range[0]} ~ ${m.tx_date_range[1]}`
  return ''
})

/* ---------------- 图表数据 ---------------- */
const kindItems = computed(() => byKind(focused.value))
const channelItems = computed(() => byChannel(focused.value, 6))
const merchantItems = computed(() => byMerchant(focused.value, 9))
const heat = computed(() => heatGrid(focused.value))
const hist = computed(() => amountHist(focused.value))
const tableRows = computed(() => [...focused.value].reverse())

const peakHour = computed(() => {
  const map = new Map()
  for (const r of focused.value) map.set(r.hour, (map.get(r.hour) || 0) + 1)
  let best = [0, 0]
  for (const [h, n] of map) if (n > best[1]) best = [h, n]
  return best
})
</script>

<template>
  <div v-if="loading" class="loading">
    <span class="dot"></span><span class="dot"></span><span class="dot"></span>
    <span style="margin-left: 6px">正在加载交易数据…</span>
  </div>

  <div v-else-if="error" class="loading" style="flex-direction: column">
    <div style="color: #ff7a45; margin-bottom: 8px">数据加载失败</div>
    <div style="font-size: 12px; color: #8b95a7">{{ error }}</div>
  </div>

  <div v-else class="shell">
    <!-- 头部 -->
    <header class="head">
      <h1>
        招行信用卡消费洞察
        <small>
          {{ range }} · 数据源 SQLite（{{ meta.db_file || 'cmb.db' }}） · 共 {{ fmtMoney(rows.length) }} 笔交易
        </small>
      </h1>
      <div class="spacer"></div>
      <div class="filters">
        <div class="search">
          <span style="color: #5d6675; font-size: 12px">⌕</span>
          <input v-model="keywordInput" placeholder="搜商户 / 渠道" />
        </div>
        <div class="chips">
          <span class="chip" :class="{ on: !filters.year }" @click="filters.year = ''">全部年份</span>
          <span
            v-for="y in facets.years"
            :key="y"
            class="chip"
            :class="{ on: filters.year === String(y) }"
            @click="filters.year = filters.year === String(y) ? '' : String(y)"
          >{{ y }}</span>
        </div>
        <button class="btn-reset" :disabled="!hasFilter" @click="reset">重置</button>
      </div>
    </header>

    <!-- 第二行筛选 -->
    <div class="filters" style="margin-top: 12px">
      <span style="font-size: 11.5px; color: #5d6675">类型</span>
      <div class="chips">
        <span class="chip teal" :class="{ on: !filters.kind }" @click="filters.kind = ''">全部</span>
        <span
          v-for="k in facets.kinds"
          :key="k"
          class="chip teal"
          :class="{ on: filters.kind === k }"
          @click="filters.kind = filters.kind === k ? '' : k"
        >{{ k }}</span>
      </div>
      <span style="font-size: 11.5px; color: #5d6675; margin-left: 8px">卡号</span>
      <div class="chips">
        <span class="chip blue" :class="{ on: !filters.card }" @click="filters.card = ''">全部</span>
        <span
          v-for="c in facets.cards"
          :key="c"
          class="chip blue"
          :class="{ on: filters.card === c }"
          @click="filters.card = filters.card === c ? '' : c"
        >{{ c }}</span>
      </div>
      <div class="spacer" style="flex: 1"></div>
      <button class="btn-reset" @click="togglePlay">
        {{ playing ? '■ 停止' : '▶ 按月播放' }}
      </button>
      <span v-if="activeMonth" class="chip on" style="cursor: default">
        下钻：{{ activeMonth }} ✕
      </span>
    </div>

    <!-- KPI -->
    <section class="kpis">
      <div class="kpi">
        <div class="label"><span>净支出</span><span>{{ activeMonth || filters.year || '全部' }}</span></div>
        <div class="value">¥{{ fmtMoney(anim.net) }}</div>
        <div class="sub">
          原额 <b>¥{{ fmtShort(stat.gross) }}</b> · 退款 <b>{{ stat.refunds }} 笔</b>
        </div>
        <div class="sub cmp" v-if="cmp && !activeMonth">
          环比
          <b :class="cmp.mom === null ? '' : cmp.mom >= 0 ? 'up' : 'down'">{{ pctText(cmp.mom) }}</b>
          · 同比
          <b :class="cmp.yoy === null ? '' : cmp.yoy >= 0 ? 'up' : 'down'">{{ pctText(cmp.yoy) }}</b>
          <span class="hint">{{ cmp.current.month }} vs {{ cmp.prev.month }} / {{ cmp.yoyMonth }}</span>
        </div>
      </div>
      <div class="kpi k2">
        <div class="label"><span>交易笔数</span><span>均 {{ fmtMoney(stat.perMonth, 0) }}/月</span></div>
        <div class="value">{{ fmtMoney(Math.round(anim.count)) }}<span class="unit">笔</span></div>
        <div class="sub">覆盖 <b>{{ stat.months }}</b> 个月 · 单笔最高 <b>¥{{ fmtShort(stat.max) }}</b></div>
      </div>
      <div class="kpi k3">
        <div class="label"><span>笔均金额</span><span>中位区间 50-100</span></div>
        <div class="value">¥{{ fmtMoney(anim.avg, 1) }}</div>
        <div class="sub">高频时段 <b>{{ String(peakHour[0]).padStart(2, '0') }}:00</b>（{{ peakHour[1] }} 笔）</div>
      </div>
      <div class="kpi k4">
        <div class="label"><span>涉及商户</span><span>去重</span></div>
        <div class="value">{{ fmtMoney(Math.round(anim.merchants)) }}<span class="unit">家</span></div>
        <div class="sub">累计还款 <b>¥{{ fmtShort(meta.repay_total || 0) }}</b> / {{ meta.repay_records || 0 }} 次</div>
      </div>
    </section>

    <!-- 图表 -->
    <section class="grid">
      <div class="card col-12" v-if="bills.length">
        <header>
          <h2>账单与额度<span>账本级 · 来自电子账单与每日信用管家</span></h2>
          <span class="hint">差额 = 账单消费 − 同期提醒流净支出</span>
        </header>
        <BillPanel :bills="bills" :credit="credit" />
      </div>

      <div class="card col-8">
        <header>
          <h2>月度净支出<span>{{ months.length }} 个月</span></h2>
          <span class="hint">点击柱子下钻该月 · 虚线为月均</span>
        </header>
        <TrendChart :months="months" :active-month="activeMonth" @pick="pickMonth" />
      </div>

      <div class="card col-4">
        <header>
          <h2>交易类型<span>净额，退款为负</span></h2>
          <span class="hint">点击筛选</span>
        </header>
        <KindBars
          :items="kindItems"
          height="272px"
          :active="filters.kind"
          @pick="(n) => (filters.kind = filters.kind === n ? '' : n)"
        />
      </div>

      <div class="card col-6">
        <header>
          <h2>商户排行<span>按净支出</span></h2>
          <span class="hint">TOP {{ merchantItems.length }}</span>
        </header>
        <RankBar :items="merchantItems" height="300px" color="#ff7a45" :label-width="104" />
      </div>

      <div class="card col-6">
        <header>
          <h2>支付渠道<span>按净支出</span></h2>
          <span class="hint">点击筛选</span>
        </header>
        <RankBar
          :items="channelItems"
          height="300px"
          color="#36cfc9"
          :label-width="86"
          :active="filters.channel || ''"
          @pick="(n) => (filters.channel = filters.channel === n ? '' : n)"
        />
      </div>

      <div class="card col-8">
        <header>
          <h2>消费时间热力<span>星期 × 小时</span></h2>
          <span class="hint">颜色越亮，该时段净支出越高</span>
        </header>
        <HeatMap :grid="heat" height="252px" />
      </div>

      <div class="card col-4">
        <header>
          <h2>金额分布<span>笔数 vs 合计</span></h2>
        </header>
        <AmountHist :bins="hist" height="252px" />
      </div>

      <div class="card col-12">
        <header>
          <h2>交易明细<span>{{ fmtMoney(tableRows.length) }} 笔</span></h2>
          <span class="hint">按时间倒序 · 退款显示为绿色正号</span>
        </header>
        <TxTable :rows="tableRows" />
      </div>
    </section>

    <footer class="foot">
      <span>交易口径：消费提醒口径的净额（消费减退款），预授权只占用额度、不计入</span>
      <span>账本级：额度/应还/到期日/利息来自电子账单，按账单周期与提醒流对账</span>
      <span>来源：{{ meta.mails || 0 }} 封邮件解析</span>
      <span>Vue 3 + ECharts · 数据由 SQLite 实时生成</span>
    </footer>
  </div>
</template>
