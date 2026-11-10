<script setup>
import { computed, ref, watch } from 'vue'
import { fmtMoney } from '../logic/aggregate'

const props = defineProps({
  rows: { type: Array, required: true } // 已按日期倒序
})

const PAGE = 40
const shown = ref(PAGE)

watch(() => props.rows, () => (shown.value = PAGE))

const page = computed(() => props.rows.slice(0, shown.value))
</script>

<template>
  <div class="table-wrap">
    <table class="tx">
      <thead>
        <tr>
          <th>日期</th>
          <th>时间</th>
          <th>商户</th>
          <th>渠道</th>
          <th>类型</th>
          <th>卡号</th>
          <th style="text-align: right">金额</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in page" :key="r.id">
          <td>{{ r.date }}</td>
          <td style="color: #6b7484">{{ r.time }}</td>
          <td :title="r.merchant">{{ r.merchant || '—' }}</td>
          <td style="color: #98a2b3">{{ r.channel || '—' }}</td>
          <td>
            <span class="tag" :class="{ refund: r.sign === -1 }">{{ r.kind }}</span>
          </td>
          <td style="color: #5d6675">{{ r.card || '—' }}</td>
          <td :class="r.sign === -1 ? 'in' : 'out'">
            {{ r.sign === -1 ? '+' : '' }}{{ fmtMoney(r.signed, 2) }}
          </td>
        </tr>
        <tr v-if="!page.length">
          <td colspan="7">
            <div class="empty">当前筛选条件下没有交易</div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <button v-if="shown < rows.length" class="more" @click="shown += PAGE * 3">
    再显示 {{ Math.min(PAGE * 3, rows.length - shown) }} 条（共 {{ fmtMoney(rows.length) }} 条）
  </button>
</template>
