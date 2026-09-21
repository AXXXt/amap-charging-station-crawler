<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { LineChart } from 'echarts/charts'
import { GridComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { Activity, BatteryCharging, Clock3, MapPin, X, Zap } from 'lucide-vue-next'
import type { HistoryPoint, PileDetail, PricePeriod, StationDetail } from '../types'

echarts.use([LineChart, GridComponent, TooltipComponent, TitleComponent, CanvasRenderer])

const props = defineProps<{
  detail: StationDetail | null
  history: HistoryPoint[]
  loading: boolean
}>()

const emit = defineEmits<{ close: [] }>()

const priceChartRef = ref<HTMLDivElement | null>(null)
const historyChartRef = ref<HTMLDivElement | null>(null)
let priceChart: echarts.EChartsType | null = null
let historyChart: echarts.EChartsType | null = null
let resizeObserver: ResizeObserver | null = null
let chartRetryTimer: number | undefined

const periods = computed<PricePeriod[]>(() => {
  if (!props.detail) return []
  return [...props.detail.fastPrices, ...props.detail.slowPrices]
})

function numeric(value: string) {
  const number = Number.parseFloat(value)
  return Number.isFinite(number) ? number : null
}

function renderPriceChart() {
  if (!priceChartRef.value) return
  if (priceChartRef.value.clientWidth < 20 || priceChartRef.value.clientHeight < 20) {
    scheduleChartRetry()
    return
  }
  if (priceChart && priceChart.getDom() !== priceChartRef.value) {
    priceChart.dispose()
    priceChart = null
  }
  priceChart ??= echarts.init(priceChartRef.value)
  priceChart.resize()
  if (!periods.value.length) {
    priceChart.clear()
    priceChart.setOption({ backgroundColor: 'transparent', title: { text: '暂无分时价格', left: 'center', top: 'middle', textStyle: { color: '#59766d', fontSize: 12, fontWeight: 400 } } })
    return
  }
  priceChart.setOption({
    animationDuration: 400,
    grid: { left: 44, right: 12, top: 22, bottom: 28 },
    tooltip: { trigger: 'axis', backgroundColor: '#0c1513', borderColor: '#274038', textStyle: { color: '#dcebe5', fontSize: 11 } },
    xAxis: {
      type: 'category',
      data: periods.value.map(item => item.time || '当前时段'),
      axisLabel: { color: '#78958b', fontSize: 10, interval: 0, rotate: periods.value.length > 4 ? 18 : 0 },
      axisLine: { lineStyle: { color: '#223832' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#78958b', fontSize: 10 },
      splitLine: { lineStyle: { color: '#182a25' } },
    },
    series: [{
      type: 'line',
      step: 'end',
      symbolSize: 7,
      data: periods.value.map(item => numeric(item.totalPrice)),
      lineStyle: { color: '#31d17d', width: 2 },
      itemStyle: { color: '#31d17d' },
      areaStyle: { color: 'rgba(49,209,125,.10)' },
    }],
  }, true)
}

function renderHistoryChart() {
  if (!historyChartRef.value) return
  if (historyChartRef.value.clientWidth < 20 || historyChartRef.value.clientHeight < 20) {
    scheduleChartRetry()
    return
  }
  if (historyChart && historyChart.getDom() !== historyChartRef.value) {
    historyChart.dispose()
    historyChart = null
  }
  historyChart ??= echarts.init(historyChartRef.value)
  historyChart.resize()
  if (!props.history.length) {
    historyChart.clear()
    historyChart.setOption({ backgroundColor: 'transparent', title: { text: '暂无历史快照', left: 'center', top: 'middle', textStyle: { color: '#59766d', fontSize: 12, fontWeight: 400 } } })
    return
  }
  historyChart.setOption({
    animationDuration: 400,
    grid: { left: 44, right: 42, top: 20, bottom: 28 },
    tooltip: { trigger: 'axis', backgroundColor: '#0c1513', borderColor: '#274038', textStyle: { color: '#dcebe5', fontSize: 11 } },
    xAxis: {
      type: 'category',
      data: props.history.map(item => new Date(item.capturedAt * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })),
      axisLabel: { color: '#78958b', fontSize: 9, hideOverlap: true },
      axisLine: { lineStyle: { color: '#223832' } },
    },
    yAxis: [
      { type: 'value', axisLabel: { color: '#78958b', fontSize: 9 }, splitLine: { lineStyle: { color: '#182a25' } } },
      { type: 'value', min: 0, max: 100, axisLabel: { color: '#78958b', fontSize: 9, formatter: '{value}%' }, splitLine: { show: false } },
    ],
    series: [
      { name: '电价', type: 'line', smooth: true, data: props.history.map(item => item.currentPrice || null), symbolSize: 5, lineStyle: { color: '#f0b43d', width: 2 }, itemStyle: { color: '#f0b43d' } },
      { name: '空闲率', type: 'line', yAxisIndex: 1, smooth: true, data: props.history.map(item => item.pileIdle + item.pileBusy > 0 ? Math.round(item.pileIdle * 100 / (item.pileIdle + item.pileBusy)) : null), symbolSize: 4, lineStyle: { color: '#31d17d', width: 2 }, itemStyle: { color: '#31d17d' } },
    ],
  }, true)
}

function scheduleChartRetry() {
  if (chartRetryTimer) window.clearTimeout(chartRetryTimer)
  chartRetryTimer = window.setTimeout(() => {
    renderPriceChart()
    renderHistoryChart()
  }, 120)
}

async function refreshCharts() {
  await nextTick()
  if (!props.detail) {
    priceChart?.dispose()
    historyChart?.dispose()
    priceChart = null
    historyChart = null
    return
  }
  window.requestAnimationFrame(() => {
    renderPriceChart()
    renderHistoryChart()
    observeChartContainers()
  })
}

function observeChartContainers() {
  if (typeof ResizeObserver === 'undefined') return
  resizeObserver ??= new ResizeObserver(() => {
    priceChart?.resize()
    historyChart?.resize()
  })
  if (priceChartRef.value) resizeObserver.observe(priceChartRef.value)
  if (historyChartRef.value) resizeObserver.observe(historyChartRef.value)
}

watch(() => [props.detail?.sourceKey, props.history.length, props.loading], () => void refreshCharts())
onMounted(() => {
  void refreshCharts()
  window.addEventListener('resize', refreshCharts)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', refreshCharts)
  if (chartRetryTimer) window.clearTimeout(chartRetryTimer)
  resizeObserver?.disconnect()
  priceChart?.dispose()
  historyChart?.dispose()
})

function fmtTime(epoch: number) {
  if (!epoch) return '--'
  return new Date(epoch * 1000).toLocaleString('zh-CN', { hour12: false })
}

function statusClass(status: string) {
  if (!status.trim()) return 'busy'
  if (status === '空闲') return 'idle'
  if (['充电中', '使用中', '占用', '已满'].includes(status)) return 'busy'
  if (['故障', '离线', '维护中', '不可用'].includes(status)) return 'fault'
  return 'unknown'
}

function pileGroup(pile: PileDetail) {
  if (pile.chargingType.includes('超')) return '超充'
  if (pile.chargingType.includes('慢')) return '慢充'
  return '快充'
}
</script>

<template>
  <aside class="detail-panel">
    <button v-if="detail" class="detail-close icon-button" title="关闭详情" @click="emit('close')"><X :size="16" /></button>

    <div v-if="!detail && !loading" class="detail-empty">
      <BatteryCharging :size="34" />
      <strong>选择站点查看详情</strong>
      <span>电价阶梯、逐桩状态和历史快照会显示在这里</span>
    </div>

    <template v-else-if="detail">
      <div class="detail-title">
        <div class="detail-kicker">{{ detail.city }}{{ detail.district ? ` · ${detail.district}` : '' }}</div>
        <h2>{{ detail.matchedName }}</h2>
        <p><MapPin :size="12" />{{ detail.address || detail.sourceStationId }}</p>
      </div>

      <div class="detail-price-row">
        <div>
          <span>当前电价</span>
          <strong>{{ detail.currentPriceText || '--' }}<em v-if="detail.currentPriceText">元/kWh</em></strong>
        </div>
        <span class="rate-badge" :class="detail.hasTou ? 'tou' : detail.flatOnly ? 'flat' : 'missing'">
          {{ detail.hasTou ? `${detail.pricePeriodCount} 个时段` : detail.flatOnly ? '全天统一' : '无分时数据' }}
        </span>
      </div>

      <section class="detail-section">
        <div class="section-head"><Zap :size="14" /> 分时价格阶梯</div>
        <div ref="priceChartRef" class="chart-box"></div>
      </section>

      <section class="detail-section">
        <div class="section-head"><Activity :size="14" /> 历史快照趋势</div>
        <div ref="historyChartRef" class="chart-box"></div>
        <div class="history-meta"><Clock3 :size="11" /> 最近一次采集 {{ fmtTime(detail.receivedAt) }}</div>
      </section>

      <section class="detail-section">
        <div class="section-head"><BatteryCharging :size="14" /> 电桩状态</div>
        <div class="pile-type-summary">
          <div>
            <span>快充</span>
            <strong>{{ detail.fastTotal }}</strong>
            <small>空闲 {{ detail.fastIdle }} · 忙碌 {{ detail.fastBusy }}</small>
          </div>
          <div>
            <span>超充</span>
            <strong>{{ detail.superTotal }}</strong>
            <small>空闲 {{ detail.superIdle }} · 忙碌 {{ detail.superBusy }}</small>
          </div>
          <div>
            <span>慢充</span>
            <strong>{{ detail.slowTotal }}</strong>
            <small>空闲 {{ detail.slowIdle }} · 忙碌 {{ detail.slowBusy }}</small>
          </div>
        </div>
        <div class="pile-summary">
          <span>空闲 <strong>{{ detail.pileIdle }}</strong></span>
          <span>忙碌 <strong>{{ detail.pileBusy }}</strong></span>
          <span v-if="detail.pileUnknown > 0">未知 <strong>{{ detail.pileUnknown }}</strong></span>
          <span>总数 <strong>{{ detail.pileTotal }}</strong></span>
        </div>
        <div v-if="detail.piles.length" class="pile-list">
          <div v-for="pile in detail.piles" :key="pile.deviceId" class="pile-row">
            <div>
              <strong>{{ pile.deviceId }}</strong>
              <span>{{ pileGroup(pile) }} · {{ pile.ratedPower || '功率未知' }} · {{ pile.ratedCurrent || '电流未知' }}</span>
            </div>
            <span class="pile-status" :class="statusClass(pile.status)">{{ pile.status || '忙碌' }}</span>
          </div>
        </div>
        <div v-else class="mini-empty">该站点本轮没有逐桩明细</div>
      </section>
    </template>
  </aside>
</template>
