<template>
  <section class="page" data-module="borehole">
    <header class="page-head">
      <div>
        <h2>钻孔编录管理</h2>
        <p class="page-desc">
          钻孔清单通过「分批入账闸门」导入：先按勘探区与孔口坐标冲突预检，全部通过才落库，
          落库结果同步回写台账与编录待办；任一孔号命中存量记录则整批暂存，不允许只导入一半。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出钻孔编录清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :class="['tab-btn', { active: activeTab === tab.key }]"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <div v-show="activeTab === 'ledger'">
      <form class="filter-bar" @submit.prevent="reload">
        <label v-for="field in filterFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="filters[field]" :placeholder="`按${field}检索`" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
            <th>历史别名</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>{{ aliasText(row) }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 2" class="empty-state">暂无钻孔编录数据，可在「分批入账」页上传清单</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条钻孔编录记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </div>

    <div v-show="activeTab === 'todos'">
      <TodoList ref="todoPanel" />
    </div>

    <div v-show="activeTab === 'gate'">
      <ImportGate ref="gatePanel" @landed="onLanded" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

import ImportGate from './ImportGate.vue'
import TodoList from './TodoList.vue'

type Row = Record<string, string | number | string[] | null>

const ENDPOINT = '/api/borehole'
const columns = ["钻孔编号", "勘探区", "孔口坐标", "设计孔深", "终孔深度", "开孔日期", "终孔日期", "钻孔状态"]
const actions = ["开始钻进", "登记终孔", "执行封孔"]
const stats = ref([
  { label: '台账钻孔', value: 0 },
  { label: '待编录待办', value: 0 },
  { label: '暂存批次', value: 0 },
])

const tabs = [
  { key: 'ledger', label: '钻孔编录台账' },
  { key: 'todos', label: '编录待办清单' },
  { key: 'gate', label: '分批入账闸门' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('ledger')
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const todoPanel = ref<InstanceType<typeof TodoList> | null>(null)
const gatePanel = ref<InstanceType<typeof ImportGate> | null>(null)

function aliasText(row: Row): string {
  const aliases = row['历史别名']
  return Array.isArray(aliases) && aliases.length ? aliases.join('、') : '—'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('钻孔编录动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '钻孔编录操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('钻孔列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '钻孔编录列表读取失败'
  }
}

async function refreshCounters() {
  try {
    const [todoResponse, batchResponse] = await Promise.all([
      request(`${ENDPOINT}/todos?status=待编录&size=1`),
      request(`${ENDPOINT}/imports`),
    ])
    if (todoResponse.ok) {
      stats.value[1].value = (await todoResponse.json()).total ?? 0
    }
    if (batchResponse.ok) {
      const batches = await batchResponse.json()
      stats.value[2].value = batches.filter((item: { status: string }) => item.status !== 'landed').length
    }
  } catch {
    // 统计卡片失败不阻塞主列表
  }
}

async function onLanded() {
  await reload()
  await refreshCounters()
  todoPanel.value?.reload()
}

onMounted(async () => {
  await reload()
  await refreshCounters()
})
</script>

<style scoped>
.tab-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.tab-btn {
  border: none;
  background: none;
  padding: 8px 14px;
  cursor: pointer;
  font-size: 14px;
  color: var(--muted);
  border-bottom: 2px solid transparent;
}
.tab-btn.active {
  color: var(--brand);
  border-bottom-color: var(--brand);
  font-weight: 600;
}
</style>
