/**
 * 数据加载与聚合。
 *
 * 载荷格式（由 db.dashboard_payload 生成）：
 *   { cols: ['date','time','merchant','channel','kind','amount','sign','card'],
 *     txs: [[...], ...], repays: [[date, category, amount], ...], meta: {...} }
 *
 * 列名驱动而不是硬编码下标，后端加列不会把前端弄崩。
 */

export const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

/** 解 gzip + base64（面板单文件内联数据时用） */
async function inflate(b64) {
  const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
  const stream = new Blob([bin]).stream().pipeThrough(new DecompressionStream('gzip'))
  return JSON.parse(await new Response(stream).text())
}

export async function loadPayload() {
  const holder = document.getElementById('cmb-data')
  const raw = holder ? holder.textContent.trim() : ''
  // 占位符没被替换 = 不是 MCP 运行时注入的：dev 下回退到 /data.json
  // （由 vite.config.js 的 devDataPlugin 从 dashboard/data.dev.json 提供）
  if (raw && !raw.startsWith('__CMB')) return inflate(raw)
  const res = await fetch('data.json')
  if (!res.ok) throw new Error('数据未注入，且找不到 data.json')
  return res.json()
}

/** 宽表 → 规范化对象数组 */
export function decode(payload) {
  const idx = {}
  payload.cols.forEach((c, i) => (idx[c] = i))
  const rows = payload.txs.map((r, i) => {
    const date = r[idx.date] || ''
    const time = r[idx.time] || ''
    return {
      id: i,
      date,
      time,
      month: date.slice(0, 7),
      year: Number(date.slice(0, 4)) || 0,
      dow: date ? new Date(date + 'T00:00:00').getDay() : 0,
      hour: Number(time.slice(0, 2)) || 0,
      merchant: r[idx.merchant] || '',
      channel: r[idx.channel] || '',
      kind: r[idx.kind] || '未分类',
      amount: r[idx.amount] || 0,
      sign: r[idx.sign] ?? 1,
      card: r[idx.card] || '',
      signed: (r[idx.amount] || 0) * (r[idx.sign] ?? 1)
    }
  })
  rows.sort((a, b) => (a.date + a.time).localeCompare(b.date + b.time))
  return rows
}

/**
 * 全局筛选（年份/类型/卡号/渠道/关键词）。
 *
 * 「其他」是长尾合成项：调用方传入当前渠道图长尾的成员集合 otherMembers，
 * 命中成员里的真实渠道。成员必须随筛选实时重算（见 App.vue 的 otherMembers），
 * 冻结一份旧集合会在切年份/类型/下钻月份后筛出偏小甚至空的视图。
 * 不传 channel 时即「渠道无关基底」，供渠道图自身使用，避免选中后图表自反馈。
 */
export function applyFilters(rows, f, otherMembers = []) {
  const kw = (f.keyword || '').toLowerCase()
  return rows.filter((r) => {
    if (f.year && r.year !== Number(f.year)) return false
    if (f.kind && r.kind !== f.kind) return false
    if (f.card && r.card !== f.card) return false
    if (f.channel) {
      const ch = r.channel || '未标注'
      if (f.channel === '其他') {
        if (!otherMembers.includes(ch)) return false
      } else if (ch !== f.channel) return false
    }
    if (kw && !(r.merchant.toLowerCase().includes(kw) || r.channel.toLowerCase().includes(kw))) {
      return false
    }
    return true
  })
}

/**
 * 环比/同比：取最近一个「完整月」与上月、去年同月比。
 *
 * 完整月口径与后端 compare_periods 一致：月末日期 ≤ 数据截止日。
 * 截止日必须传全局最后一笔交易日期（dataLastDate），不能从传入 rows 里推：
 * rows 是筛选后的子集，筛「退款」时最后退款日可能停在月中，
 * 会把本来完整的月份误判成未走完，KPI 悄悄退到上上个月。
 * 不能要求「月末当天恰好有交易」——月末那天没消费会把整个月误跳过。
 * 当月还没走完时跳过它，否则拿半个月去比整月，环比永远是大跌。
 *
 * 启发式的已知边界（有意保留）：截止日取「最后一笔交易」而不是日历今天。
 * 若最新月份月底没有交易、又没有更晚月份的数据（例如 10-25 之后就没再刷卡），
 * 这个日历上已走完的月份仍会被判为「未走完」而跳过。这是保守取舍：
 * trailing 缺口也可能是邮件没同步齐，拿可能缺数的月份下结论更危险。
 * 用例见 tests/aggregate.test.js「完整月启发式」组。
 */
export function periodCompare(rows, dataLastDate = '') {
  const netByMonth = new Map()
  let lastTx = dataLastDate || ''
  for (const r of rows) {
    if (!r.month) continue
    netByMonth.set(r.month, (netByMonth.get(r.month) || 0) + r.signed)
    if (!dataLastDate && r.date > lastTx) lastTx = r.date
  }
  const months = [...netByMonth.keys()].sort()
  if (months.length < 2) return null

  const complete = (m) => {
    const [y, mo] = m.split('-').map(Number)
    const end = `${m}-${String(new Date(y, mo, 0).getDate()).padStart(2, '0')}`
    return !!lastTx && end <= lastTx
  }

  let idx = months.length - 1
  let skipped = ''
  if (!complete(months[idx])) {
    skipped = months[idx]
    idx -= 1
  }
  if (idx < 1) return null

  const cur = months[idx]
  const prev = months[idx - 1]
  const yoyKey = `${Number(cur.slice(0, 4)) - 1}${cur.slice(4)}`
  const curNet = netByMonth.get(cur)
  const prevNet = netByMonth.get(prev) || 0
  const yoyNet = netByMonth.has(yoyKey) ? netByMonth.get(yoyKey) : null
  const pct = (now, base) => (base ? Math.round(((now - base) / Math.abs(base)) * 1000) / 10 : null)

  return {
    current: { month: cur, net: curNet },
    prev: { month: prev, net: prevNet },
    mom: pct(curNet, prevNet),
    momDelta: Math.round((curNet - prevNet) * 100) / 100,
    yoy: yoyNet === null ? null : pct(curNet, yoyNet),
    yoyMonth: yoyKey,
    skipped
  }
}

export function summarize(rows) {
  let net = 0
  let gross = 0
  let count = 0
  let max = 0
  let refunds = 0
  const merchants = new Set()
  const months = new Set()
  const amounts = []
  for (const r of rows) {
    net += r.signed
    gross += r.amount
    count++
    if (r.signed > max) max = r.signed
    if (r.sign === -1) refunds++
    if (r.merchant) merchants.add(r.merchant)
    if (r.month) months.add(r.month)
    amounts.push(r.amount)
  }
  // 中位金额所在区间：随筛选实时变化，不再写死
  amounts.sort((a, b) => a - b)
  let median = 0
  if (amounts.length) {
    const mid = Math.floor(amounts.length / 2)
    median = amounts.length % 2 ? amounts[mid] : (amounts[mid - 1] + amounts[mid]) / 2
  }
  const bucket = BUCKETS.find(([lo, hi]) => median >= lo && median < hi) || BUCKETS[BUCKETS.length - 1]
  return {
    count,
    net,
    gross,
    refunds,
    max,
    avg: count ? gross / count : 0,
    merchants: merchants.size,
    months: months.size,
    perMonth: months.size ? net / months.size : 0,
    // 笔数/月（交易笔数卡用），区别于 perMonth 的金额/月
    perMonthCount: months.size ? count / months.size : 0,
    median,
    medianBucket: bucket[2]
  }
}

function groupSum(rows, keyFn) {
  const map = new Map()
  for (const r of rows) {
    const k = keyFn(r)
    let g = map.get(k)
    if (!g) {
      g = { name: k, tx_count: 0, net: 0, gross: 0 }
      map.set(k, g)
    }
    g.tx_count++
    g.net += r.signed
    g.gross += r.amount
  }
  return [...map.values()]
}

export function byMonth(rows) {
  return groupSum(rows, (r) => r.month).sort((a, b) => a.name.localeCompare(b.name))
}

export function byKind(rows) {
  return groupSum(rows, (r) => r.kind).sort((a, b) => b.net - a.net)
}

export function byChannel(rows, limit = 7) {
  const list = groupSum(rows, (r) => r.channel || '未标注').sort((a, b) => b.net - a.net)
  if (list.length <= limit) return list
  const head = list.slice(0, limit)
  const tail = list.slice(limit)
  head.push({
    name: '其他',
    // 记录长尾成员，点击「其他」时据此筛选真实渠道，而不是匹配一个不存在的 channel
    members: tail.map((x) => x.name),
    tx_count: tail.reduce((s, x) => s + x.tx_count, 0),
    net: tail.reduce((s, x) => s + x.net, 0),
    gross: tail.reduce((s, x) => s + x.gross, 0)
  })
  return head
}

export function byMerchant(rows, limit = 10) {
  const list = groupSum(rows, (r) => r.merchant || '未知')
    .filter((x) => x.name !== '未知')
    .sort((a, b) => b.net - a.net)
  if (list.length <= limit) return list
  const head = list.slice(0, limit)
  const tail = list.slice(limit)
  head.push({
    name: `其他 ${tail.length} 家`,
    tx_count: tail.reduce((s, x) => s + x.tx_count, 0),
    net: tail.reduce((s, x) => s + x.net, 0),
    gross: tail.reduce((s, x) => s + x.gross, 0)
  })
  return head
}

/** 7×24 热力网格，返回 { cells: [[hour,dow,value]], max, count } */
export function heatGrid(rows) {
  const grid = Array.from({ length: 7 }, () => new Array(24).fill(0))
  const counts = Array.from({ length: 7 }, () => new Array(24).fill(0))
  for (const r of rows) {
    if (r.dow < 0 || r.dow > 6 || r.hour < 0 || r.hour > 23) continue
    grid[r.dow][r.hour] += r.signed
    counts[r.dow][r.hour]++
  }
  const cells = []
  let max = 0
  let count = 0
  for (let d = 0; d < 7; d++) {
    for (let h = 0; h < 24; h++) {
      const v = grid[d][h]
      if (v) {
        cells.push([h, d, Math.round(v)])
        if (v > max) max = v
      }
      count += counts[d][h]
    }
  }
  return { cells, max, count }
}

const BUCKETS = [
  [0, 10, '≤10'],
  [10, 30, '10-30'],
  [30, 50, '30-50'],
  [50, 100, '50-100'],
  [100, 200, '100-200'],
  [200, 500, '200-500'],
  [500, 1000, '500-1k'],
  [1000, 3000, '1k-3k'],
  [3000, Infinity, '>3k']
]

export function amountHist(rows) {
  const bins = BUCKETS.map(([lo, hi, label]) => ({ label, lo, hi, count: 0, net: 0 }))
  for (const r of rows) {
    const b = bins.find((x) => r.amount >= x.lo && r.amount < x.hi) || bins[bins.length - 1]
    b.count++
    b.net += r.signed
  }
  return bins
}

/** 从全部数据里抽取筛选项，避免给出 0 结果的空选项 */
export function facetOptions(rows) {
  const years = [...new Set(rows.map((r) => r.year))].filter(Boolean).sort()

  // 类型按出现次数降序，"未分类" 固定沉底，避免稀疏标签挤到前面
  const kindCount = new Map()
  for (const r of rows) kindCount.set(r.kind, (kindCount.get(r.kind) || 0) + 1)
  const kinds = [...kindCount.entries()]
    .sort((a, b) => {
      if (a[0] === '未分类') return 1
      if (b[0] === '未分类') return -1
      return b[1] - a[1]
    })
    .map((x) => x[0])

  const cards = [...new Set(rows.map((r) => r.card))].filter(Boolean).sort()
  return { years, kinds, cards }
}

export function fmtMoney(v, digits = 0) {
  const n = Number(v) || 0
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })
}

/** 金额口语化：1.23 万 / 4567 */
export function fmtShort(v) {
  const n = Math.abs(Number(v) || 0)
  if (n >= 10000) return (n / 10000).toFixed(n >= 100000 ? 0 : 1) + ' 万'
  return fmtMoney(n)
}

/** 带符号的口语化金额，退款会显示为 -4.0 万 */
export function fmtSigned(v) {
  const n = Number(v) || 0
  return (n < 0 ? '-' : '') + fmtShort(n)
}
