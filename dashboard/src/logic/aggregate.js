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
 * 环比/同比：取最近一个「完整月」与上月、去年同月比。
 *
 * 当月还没走完时（库内最后一笔不在月末）要跳过它，
 * 否则拿半个月去比整月，环比永远是大跌。
 */
export function periodCompare(rows) {
  const netByMonth = new Map()
  const lastDay = new Map()
  for (const r of rows) {
    if (!r.month) continue
    netByMonth.set(r.month, (netByMonth.get(r.month) || 0) + r.signed)
    if (r.date > (lastDay.get(r.month) || '')) lastDay.set(r.month, r.date)
  }
  const months = [...netByMonth.keys()].sort()
  if (months.length < 2) return null

  const complete = (m) => {
    const [y, mo] = m.split('-').map(Number)
    const days = new Date(y, mo, 0).getDate()
    const last = lastDay.get(m)
    return !!last && Number(last.slice(8, 10)) >= days
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
  for (const r of rows) {
    net += r.signed
    gross += r.amount
    count++
    if (r.signed > max) max = r.signed
    if (r.sign === -1) refunds++
    if (r.merchant) merchants.add(r.merchant)
    if (r.month) months.add(r.month)
  }
  return {
    count,
    net,
    gross,
    refunds,
    max,
    avg: count ? gross / count : 0,
    merchants: merchants.size,
    months: months.size,
    perMonth: months.size ? net / months.size : 0
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
