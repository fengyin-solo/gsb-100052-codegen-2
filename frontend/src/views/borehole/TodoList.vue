<template>
  <section>
    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>待办状态</span>
        <select v-model="status">
          <option value="">全部</option>
          <option value="待编录">待编录</option>
          <option value="已完成">已完成</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
    </form>
    <table class="data-table">
      <thead>
        <tr>
          <th>钻孔编号</th>
          <th>勘探区</th>
          <th>孔口坐标</th>
          <th>来源批次</th>
          <th>状态</th>
          <th>创建时间</th>
          <th>完成时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="todo in todos" :key="String(todo.id)">
          <td>{{ todo['钻孔编号'] }}</td>
          <td>{{ todo['勘探区'] }}</td>
          <td>{{ todo['孔口坐标'] }}</td>
          <td>{{ todo['批次号'] }}</td>
          <td>{{ todo.status }}</td>
          <td>{{ todo.created_at }}</td>
          <td>{{ todo.completed_at ?? '—' }}</td>
          <td>
            <button
              v-if="todo.status !== '已完成'"
              class="link"
              type="button"
              @click="completeTodo(todo.id)"
            >
              完成编录
            </button>
            <span v-else class="muted">已闭环</span>
          </td>
        </tr>
        <tr v-if="!todos.length">
          <td colspan="8" class="empty-state">暂无编录待办，清单过闸入账后会同步生成待办</td>
        </tr>
      </tbody>
    </table>
    <footer class="page-foot">
      <span>共 {{ total }} 条编录待办</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Todo = Record<string, string | number | null>

const ENDPOINT = '/api/borehole'

const todos = ref<Todo[]>([])
const total = ref(0)
const status = ref('')
const errorMessage = ref('')

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (status.value) query.set('status', status.value)
  try {
    const response = await request(`${ENDPOINT}/todos?${query.toString()}`)
    if (!response.ok) throw new Error('编录待办读取失败')
    const payload = await response.json()
    todos.value = payload.items ?? []
    total.value = payload.total ?? 0
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '编录待办读取失败'
  }
}

async function completeTodo(todoId: number | string | null) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/todos/${todoId}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action: '完成编录' } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '待办未完成')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '待办操作失败'
  }
}

onMounted(reload)
defineExpose({ reload })
</script>
