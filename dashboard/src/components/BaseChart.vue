<script setup>
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import echarts from '../echarts'

const props = defineProps({
  option: { type: Object, default: null },
  // 允许传 272 或 '272px'，数字自动补单位，避免容器高度为 0
  height: { type: [String, Number], default: '260px' },
  // notMerge=true 用于维度完全变化的场景（如切换下钻对象），否则平滑过渡
  notMerge: { type: Boolean, default: false }
})
const emit = defineEmits(['chart-click'])

const styleHeight = computed(() =>
  typeof props.height === 'number' ? `${props.height}px` : props.height
)

const box = ref(null)
const chart = shallowRef(null)
let ro = null

function draw() {
  if (!chart.value || !props.option) return
  chart.value.setOption(props.option, { notMerge: props.notMerge, lazyUpdate: true })
}

onMounted(() => {
  chart.value = echarts.init(box.value, null, { renderer: 'canvas' })
  chart.value.on('click', (p) => emit('chart-click', p))
  draw()
  ro = new ResizeObserver(() => chart.value && chart.value.resize())
  ro.observe(box.value)
})

onBeforeUnmount(() => {
  if (ro) ro.disconnect()
  if (chart.value) chart.value.dispose()
})

watch(() => props.option, draw, { deep: false })
</script>

<template>
  <div ref="box" :style="{ height: styleHeight, width: '100%' }"></div>
</template>
