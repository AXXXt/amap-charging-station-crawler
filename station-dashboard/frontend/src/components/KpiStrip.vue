<script setup lang="ts">
import { Activity, BatteryCharging, CircleDollarSign, DatabaseZap, Gauge, TimerReset } from 'lucide-vue-next'
import type { Overview } from '../types'

defineProps<{ overview: Overview | null }>()

function fmt(value: number | undefined, digits = 1) {
  return value == null ? '--' : value.toFixed(digits)
}

function fmtTime(epoch: number | undefined) {
  if (!epoch) return '--'
  return new Date(epoch * 1000).toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <section class="kpi-strip">
    <article class="kpi-unit">
      <DatabaseZap :size="16" />
      <span>站点总数</span>
      <strong>{{ overview?.total ?? '--' }}</strong>
      <small>结果表实时</small>
    </article>
    <article class="kpi-unit price">
      <CircleDollarSign :size="16" />
      <span>平均电价</span>
      <strong>{{ fmt(overview?.averagePrice, 2) }}<em>元</em></strong>
      <small>{{ overview?.priced ?? 0 }} 条有价格</small>
    </article>
    <article class="kpi-unit tou">
      <Gauge :size="16" />
      <span>分时覆盖率</span>
      <strong>{{ fmt(overview?.touCoverage) }}<em>%</em></strong>
      <small>{{ overview?.withTou ?? 0 }} 条多时段</small>
    </article>
    <article class="kpi-unit pile">
      <BatteryCharging :size="16" />
      <span>电桩覆盖率</span>
      <strong>{{ fmt(overview?.pileCoverage) }}<em>%</em></strong>
      <small>{{ overview?.pileTotal ?? 0 }} 根桩明细</small>
    </article>
    <article class="kpi-unit idle">
      <Activity :size="16" />
      <span>全局空闲率</span>
      <strong>{{ fmt(overview?.idleRate) }}<em>%</em></strong>
      <small>忙碌 {{ fmt(overview?.busyRate) }}%</small>
    </article>
    <article class="kpi-unit updated">
      <TimerReset :size="16" />
      <span>最近入库</span>
      <strong class="time-value">{{ fmtTime(overview?.updatedAt) }}</strong>
      <small>自动刷新 60 秒</small>
    </article>
  </section>
</template>
