<template>
  <section class="page" data-module="borehole">
    <header class="page-head">
      <div>
        <h2>钻孔编录管理</h2>
        <p class="page-desc">分批入账闸门：清单先按勘探区与孔口坐标冲突预检，整批通过才落库并回写台账与编录待办。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="togglePanel('import')">分批导入</button>
        <button class="btn" type="button" @click="exportRows">导出钻孔编录清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 分批入账闸门 -->
    <section v-if="activePanel === 'import'" class="gate-panel">
      <h3>分批入账闸门</h3>
      <p class="page-desc">
        上传钻孔清单（CSV，首行需含「钻孔编号、勘探区、孔口坐标」等列）。任一孔号命中存量或坐标冲突，
        整批退回暂存文件，不会只导入一半；同一文件按指纹只生效一次。
      </p>

      <form class="filter-bar" @submit.prevent="onUpload">
        <label class="filter-item">
          <span>清单文件（CSV）</span>
          <input type="file" accept=".csv,text/csv,text/plain" @change="onFileChange" />
        </label>
        <button class="btn primary" type="submit" :disabled="!fileContent || uploading">
          {{ uploading ? '预检入账中…' : '上传并预检入账' }}
        </button>
        <button class="btn ghost" type="button" @click="downloadTemplate">下载清单模板</button>
      </form>

      <div v-if="uploadResult" class="result-box" :class="uploadResult.ok ? 'ok' : 'warn'">
        <strong>{{ uploadResult.ok ? '✔' : '✕' }} {{ uploadResult.message }}</strong>
        <ul class="kv-list">
          <li>批次号：{{ uploadResult.batch?.id }}</li>
          <li>批次状态：{{ uploadResult.batch?.批次状态 }}</li>
          <li>总行数：{{ uploadResult.batch?.总行数 }}　入账：{{ uploadResult.batch?.入账数 ?? 0 }}</li>
          <li v-if="uploadResult.batch?.失败行">解析失败行：第 {{ uploadResult.batch.失败行 }} 行</li>
          <li v-if="uploadResult.reused">该文件指纹已处理过，本次为幂等回放，未重复建台账。</li>
        </ul>

        <table v-if="uploadResult.batch?.冲突?.length" class="data-table conflict-table">
          <thead>
            <tr><th>行号</th><th>钻孔编号</th><th>勘探区</th><th>孔口坐标</th><th>冲突类型</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in uploadResult.batch.冲突" :key="`${c.行号}-${c.钻孔编号}`">
              <td>{{ c.行号 }}</td><td>{{ c.钻孔编号 }}</td><td>{{ c.勘探区 }}</td>
              <td>{{ c.孔口坐标 }}</td><td>{{ c.冲突类型 }}</td><td>{{ c.说明 }}</td>
            </tr>
          </tbody>
        </table>

        <div class="row-actions" style="margin-top: 8px">
          <button
            v-if="uploadResult.batch?.暂存文件"
            class="link"
            type="button"
            @click="downloadStaging(uploadResult.batch.id)"
          >
            下载暂存原件
          </button>
          <button
            v-if="uploadResult.batch?.批次状态 === '解析中断'"
            class="link"
            type="button"
            @click="prepareResume(uploadResult.batch.id)"
          >
            从第 {{ uploadResult.batch.失败行 }} 行续传（选择修正后的文件）
          </button>
        </div>
      </div>

      <div class="gate-cols">
        <div>
          <h4>导入批次台账</h4>
          <div class="row-actions" style="margin-bottom:6px">
            <button class="btn" type="button" @click="loadBatches">刷新批次</button>
          </div>
          <table class="data-table">
            <thead><tr><th>批次</th><th>状态</th><th>入账/总行</th><th>时间</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="b in batches" :key="b.id">
                <td>{{ b.id }}</td>
                <td>{{ b.批次状态 }}</td>
                <td>{{ b.入账数 }}/{{ b.总行数 }}</td>
                <td>{{ b.创建时间 }}</td>
                <td>
                  <button v-if="b.暂存文件" class="link" type="button" @click="downloadStaging(b.id)">暂存</button>
                  <button
                    v-if="b.批次状态 === '解析中断'"
                    class="link"
                    type="button"
                    @click="prepareResume(b.id)"
                  >续传</button>
                </td>
              </tr>
              <tr v-if="!batches.length"><td colspan="5" class="empty-state">暂无导入批次</td></tr>
            </tbody>
          </table>
        </div>

        <div>
          <h4>编录待办清单</h4>
          <div class="row-actions" style="margin-bottom:6px">
            <button class="btn" type="button" @click="loadTodos">刷新待办</button>
          </div>
          <table class="data-table">
            <thead><tr><th>孔号</th><th>勘探区</th><th>状态</th><th>来源批次</th></tr></thead>
            <tbody>
              <tr v-for="t in todos" :key="t.id">
                <td>{{ t.钻孔编号 }}</td><td>{{ t.勘探区 }}</td><td>{{ t.待办状态 }}</td><td>{{ t.来源批次 }}</td>
              </tr>
              <tr v-if="!todos.length"><td colspan="4" class="empty-state">暂无待编录项</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 历史别名迁移 -->
      <div class="migrate-box">
        <h4>早期孔号历史别名迁移</h4>
        <p class="page-desc">
          每行格式：<code>孔号 | 别名1,别名2 | 现场确认坐标(可选)</code>。
          补齐别名后，旧孔号再上传会被识别为同一孔；坐标冲突时以现场确认坐标为准。
        </p>
        <textarea v-model="migrateText" rows="3" class="migrate-input"
          placeholder="BORE-0002 | OLD-2,旧2号 | X=520/Y=330"></textarea>
        <div class="row-actions">
          <button class="btn primary" type="button" @click="onMigrate">提交迁移</button>
        </div>
      </div>

      <p v-if="gateMessage" :class="gateOk ? 'ok-text' : 'error-text'" style="margin-top:8px">{{ gateMessage }}</p>
    </section>

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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无钻孔编录数据，可先分批导入</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条钻孔编录记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
interface Conflict {
  行号: number
  钻孔编号: string
  勘探区: string
  孔口坐标: string
  冲突类型: string
  说明: string
}
interface Batch {
  id: number
  批次状态: string
  总行数: number
  入账数: number
  创建时间: string
  暂存文件: string | null
  失败行?: number
  冲突?: Conflict[]
}
interface ImportResponse {
  ok: boolean
  reused: boolean
  message: string
  batch: Batch & { 冲突?: Conflict[] }
}
interface Todo {
  id: number
  钻孔编号: string
  勘探区: string
  待办状态: string
  来源批次: number
}

const ENDPOINT = '/api/borehole'
const columns = ['钻孔编号', '勘探区', '孔口坐标', '设计孔深', '终孔深度', '开孔日期', '终孔日期', '钻孔状态']
const actions = ['开始钻进', '登记终孔', '执行封孔']
const statuses = ['待施工', '钻进中', '已终孔', '已封孔', '已废弃']
const stats = [{ label: '施工中钻孔', value: 0 }, { label: '已终孔钻孔', value: 0 }, { label: '已封孔钻孔', value: 0 }]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

// 分批闸门相关状态
const activePanel = ref<'' | 'import'>('')
const fileContent = ref('')
const fileName = ref('')
const uploading = ref(false)
const uploadResult = ref<ImportResponse | null>(null)
const batches = ref<Batch[]>([])
const todos = ref<Todo[]>([])
const migrateText = ref('')
const resumeBatchId = ref<number | null>(null)
const gateMessage = ref('')
const gateOk = ref(false)

function togglePanel(panel: 'import') {
  activePanel.value = activePanel.value === panel ? '' : panel
  if (activePanel.value === 'import') {
    void loadBatches()
    void loadTodos()
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) {
    fileContent.value = ''
    return
  }
  fileName.value = file.name
  const reader = new FileReader()
  reader.onload = () => {
    fileContent.value = String(reader.result ?? '')
  }
  reader.readAsText(file, 'utf-8')
}

function downloadTemplate() {
  const header = '钻孔编号,勘探区,孔口坐标,设计孔深,终孔深度,开孔日期,终孔日期,钻孔状态'
  const sample = 'ZK-001,北区,X=500/Y=300,120,,,2026-09-10,,'
  const blob = new Blob([`﻿${header}\n${sample}\n`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = '钻孔清单模板.csv'
  a.click()
  URL.revokeObjectURL(url)
}

async function onUpload() {
  if (!fileContent.value) return
  uploading.value = true
  gateMessage.value = ''
  try {
    const body: Record<string, unknown> = { content: fileContent.value, filename: fileName.value }
    if (resumeBatchId.value !== null) {
      body.resume_batch_id = resumeBatchId.value
    }
    const response = await request(`${ENDPOINT}/imports`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}))
      throw new Error(detail.detail ?? '预检入账失败')
    }
    uploadResult.value = (await response.json()) as ImportResponse
    resumeBatchId.value = null
    await Promise.all([reload(), loadBatches(), loadTodos()])
  } catch (error) {
    gateOk.value = false
    gateMessage.value = error instanceof Error ? error.message : '分批导入失败'
  } finally {
    uploading.value = false
  }
}

function prepareResume(batchId: number) {
  resumeBatchId.value = batchId
  gateOk.value = true
  gateMessage.value = `已选择从批次 ${batchId} 的失败行续传：请选择“修正后、从失败行开始”的文件后点击上传。`
  uploadResult.value = null
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

async function loadBatches() {
  try {
    const response = await request(`${ENDPOINT}/imports`)
    if (response.ok) {
      const payload = await response.json()
      batches.value = (payload.items ?? []).slice().reverse()
    }
  } catch {
    // 批次台账读取失败不阻塞主列表
  }
}

async function loadTodos() {
  try {
    const response = await request(`${ENDPOINT}/todos`)
    if (response.ok) {
      const payload = await response.json()
      todos.value = payload.items ?? []
    }
  } catch {
    // 待办读取失败不阻塞主列表
  }
}

function downloadStaging(batchId: number) {
  window.open(`${ENDPOINT}/imports/${batchId}/staging-file`, '_blank')
}

async function onMigrate() {
  gateMessage.value = ''
  const items = migrateText.value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [code = '', aliasPart = '', coord = ''] = line.split('|').map((s) => s.trim())
      return {
        钻孔编号: code,
        历史别名: aliasPart ? aliasPart.split(/[,，]/).map((s) => s.trim()).filter(Boolean) : [],
        现场确认坐标: coord || null,
      }
    })
  if (!items.length) {
    gateOk.value = false
    gateMessage.value = '请至少填写一行迁移内容'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/alias-migrations`, {
      method: 'POST',
      body: JSON.stringify({ items }),
    })
    const payload = await response.json()
    gateOk.value = Boolean(payload.ok)
    if (payload.ok) {
      gateMessage.value = payload.message
      migrateText.value = ''
      await reload()
    } else {
      const detail = payload.entry?.errors?.map((e: { 钻孔编号: string; 问题: string }) => `${e.钻孔编号}：${e.问题}`).join('；')
      gateMessage.value = detail ? `${payload.message}（${detail}）` : payload.message
    }
  } catch (error) {
    gateOk.value = false
    gateMessage.value = error instanceof Error ? error.message : '别名迁移失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
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
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '钻孔编录列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.gate-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}
.gate-panel h3 { margin: 0 0 6px; }
.gate-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 12px; }
.gate-cols h4 { margin: 6px 0; }
.result-box { border-radius: 6px; padding: 10px 12px; margin-top: 10px; font-size: 13px; }
.result-box.ok { background: #ecfdf3; border: 1px solid #86efac; }
.result-box.warn { background: #fef3f2; border: 1px solid #fda29b; }
.kv-list { margin: 6px 0 0; padding-left: 18px; color: #334155; }
.conflict-table { margin-top: 8px; }
.migrate-box { margin-top: 14px; border-top: 1px dashed var(--border); padding-top: 10px; }
.migrate-input { width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-family: inherit; }
.ok-text { color: #067647; }
</style>
