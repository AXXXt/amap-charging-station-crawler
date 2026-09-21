<script setup lang="ts">
import { reactive, watch } from 'vue'
import { Filter, RotateCcw, Search } from 'lucide-vue-next'
import type { Meta, StationFilters } from '../types'

const props = defineProps<{
  filters: StationFilters
  meta: Meta | null
  total: number
}>()

const emit = defineEmits<{
  update: [filters: StationFilters]
  reset: []
}>()

const draft = reactive<StationFilters>({ ...props.filters })

watch(() => props.filters, value => Object.assign(draft, value), { deep: true })

function apply() {
  emit('update', { ...draft, page: 1 })
}
</script>

<template>
  <aside class="filter-panel">
    <div class="panel-heading">
      <div><Filter :size="15" /> 筛选条件</div>
      <button class="icon-button" title="重置筛选" @click="emit('reset')"><RotateCcw :size="14" /></button>
    </div>

    <label class="search-field">
      <Search :size="15" />
      <input v-model="draft.keyword" placeholder="站点、POI 或城市" @keyup.enter="apply" />
    </label>

    <label>
      <span>城市</span>
      <select v-model="draft.city">
        <option value="">全部城市</option>
        <option v-for="item in meta?.cities" :key="item.name" :value="item.name">{{ item.name }} · {{ item.count }}</option>
      </select>
    </label>

    <label>
      <span>运营商</span>
      <select v-model="draft.operator">
        <option value="">全部运营商</option>
        <option v-for="item in meta?.operators" :key="item.name" :value="item.name">{{ item.name }} · {{ item.count }}</option>
      </select>
    </label>

    <div class="filter-group">
      <span>电价水平</span>
      <div class="segmented">
        <button :class="{ active: draft.priceBand === '' }" @click="draft.priceBand = ''">全部</button>
        <button :class="{ active: draft.priceBand === 'cheap' }" @click="draft.priceBand = 'cheap'">低价</button>
        <button :class="{ active: draft.priceBand === 'mid' }" @click="draft.priceBand = 'mid'">中位</button>
        <button :class="{ active: draft.priceBand === 'expensive' }" @click="draft.priceBand = 'expensive'">高价</button>
      </div>
    </div>

    <div class="filter-group">
      <span>分时费率</span>
      <div class="segmented vertical">
        <button :class="{ active: draft.tou === '' }" @click="draft.tou = ''">全部站点</button>
        <button :class="{ active: draft.tou === 'has' }" @click="draft.tou = 'has'">有多时段</button>
        <button :class="{ active: draft.tou === 'flat' }" @click="draft.tou = 'flat'">全天统一价</button>
        <button :class="{ active: draft.tou === 'none' }" @click="draft.tou = 'none'">无分时数据</button>
      </div>
    </div>

    <div class="filter-group">
      <span>电桩明细</span>
      <div class="segmented">
        <button :class="{ active: draft.piles === '' }" @click="draft.piles = ''">全部</button>
        <button :class="{ active: draft.piles === 'with' }" @click="draft.piles = 'with'">有</button>
        <button :class="{ active: draft.piles === 'without' }" @click="draft.piles = 'without'">无</button>
      </div>
    </div>

    <label>
      <span>排序</span>
      <select v-model="draft.sort">
        <option value="updated">最近采集</option>
        <option value="priceAsc">电价从低到高</option>
        <option value="priceDesc">电价从高到低</option>
        <option value="periods">分时段数最多</option>
        <option value="name">站点名称</option>
      </select>
    </label>

    <button class="primary-button apply-button" @click="apply">应用筛选</button>
    <div class="filter-foot">{{ total }} 条匹配结果</div>
  </aside>
</template>
