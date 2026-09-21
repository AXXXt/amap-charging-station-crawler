<script setup lang="ts">
import { ChevronLeft, ChevronRight, MapPin, Zap } from 'lucide-vue-next'
import type { Station } from '../types'

const props = defineProps<{
  items: Station[]
  loading: boolean
  total: number
  page: number
  pageSize: number
  selectedKey: string
}>()

const emit = defineEmits<{
  select: [station: Station]
  page: [page: number]
}>()

function pages() {
  return Math.max(1, Math.ceil(props.total / props.pageSize))
}

function priceClass(station: Station) {
  return `price-${station.priceLevel}`
}

function fmtTime(epoch: number) {
  if (!epoch) return '--'
  return new Date(epoch * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}

function pileText(station: Station) {
  if (!station.pileTotal) return '缺失'
  return `${station.pileIdle} / ${station.pileTotal}`
}
</script>

<template>
  <section class="table-panel">
    <div class="table-toolbar">
      <div>
        <strong>站点实时总览</strong>
        <span>{{ total }} 个站点 · 相对电价色阶 · 空闲率独立进度</span>
      </div>
      <div class="legend">
        <span><i class="dot cheap"></i>低价</span>
        <span><i class="dot mid"></i>中位</span>
        <span><i class="dot expensive"></i>高价</span>
        <span><i class="dot missing"></i>缺失</span>
      </div>
    </div>

    <div class="table-scroll">
      <table class="station-table">
        <thead>
          <tr>
            <th>站点</th>
            <th>城市 / 运营商</th>
            <th>当前电价</th>
            <th>分时费率</th>
            <th>快 / 超 / 慢</th>
            <th>空闲</th>
            <th>采集时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading" v-for="index in 8" :key="index" class="skeleton-row">
            <td colspan="7"><span></span></td>
          </tr>
          <tr
            v-else
            v-for="station in items"
            :key="station.sourceKey"
            :class="{ selected: station.sourceKey === selectedKey }"
            @click="emit('select', station)"
          >
            <td>
              <div class="station-name" :title="station.matchedName">{{ station.matchedName || station.requestedName }}</div>
              <div class="station-sub"><MapPin :size="11" /> {{ station.district || station.sourceStationId || '未知区域' }}</div>
            </td>
            <td>
              <div>{{ station.city || '--' }}</div>
              <div class="station-sub">{{ station.operator || '未知运营商' }}</div>
            </td>
            <td>
              <div class="price-cell" :class="priceClass(station)">
                <strong>{{ station.currentPriceText || '--' }}</strong>
                <span v-if="station.currentPriceText">元/kWh</span>
              </div>
            </td>
            <td>
              <span v-if="station.hasTou" class="rate-badge tou">多时段 {{ station.pricePeriodCount }}</span>
              <span v-else-if="station.flatOnly" class="rate-badge flat">全天统一</span>
              <span v-else class="rate-badge missing">无数据</span>
            </td>
            <td>
              <div class="pile-triplet">
                <span><Zap :size="11" />{{ station.fastIdle }}/{{ station.fastTotal }}</span>
                <span class="super">{{ station.superIdle }}/{{ station.superTotal }}</span>
                <span class="slow">{{ station.slowIdle }}/{{ station.slowTotal }}</span>
              </div>
            </td>
            <td>
              <div class="idle-cell">
                <div class="idle-track"><i :style="{ width: `${Math.min(100, station.idleRate)}%` }"></i></div>
                <span>{{ station.idleRate.toFixed(0) }}%</span>
              </div>
              <div class="station-sub">{{ pileText(station) }} 根空闲</div>
            </td>
            <td class="time-cell">{{ fmtTime(station.receivedAt) }}</td>
          </tr>
          <tr v-if="!loading && !items.length">
            <td colspan="7" class="empty-state">没有符合当前条件的站点</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pagination">
      <span>第 {{ page }} / {{ pages() }} 页</span>
      <div>
        <button class="icon-button" :disabled="page <= 1" @click="emit('page', page - 1)"><ChevronLeft :size="15" /></button>
        <button class="icon-button" :disabled="page >= pages()" @click="emit('page', page + 1)"><ChevronRight :size="15" /></button>
      </div>
    </div>
  </section>
</template>
