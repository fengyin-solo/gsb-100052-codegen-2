<template>
  <section class="gate-panel">
    <div class="upload-box">
      <div>
        <h3>分批入账闸门</h3>
        <p class="page-desc">
          上传钻孔清单（CSV/TSV，UTF-8）。先按勘探区与孔口坐标做冲突预检，本轮全部通过才允许落库；
          任一孔号命中存量记录则整批退至暂存文件，不允许只导入一半。同一文件按指纹幂等，只生效一次。
        </p>
      </div>
      <div class="upload-controls">
        <input ref="fileInput" type="file" accept=".csv,.tsv,.txt" @change="onFileChosen" />
        <button class="btn primary" type="button" :disabled="uploading" @click="triggerUpload">
          {{ uploading ? '过闸中…' : '上传清单过闸' }}
        </button>
        <button class="btn" type="button" @click="downloadTemplate">下载清单模板</button>
      </div>
    </div>

    <p v-if="resultMessage" :class="lastOk ? 'ok-text' : 'error-text'" class="gate-message">
      {{ resultMessage }}
    </p>

    <h3>导入批次台账</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次号</th>
          <th>文件名</th>
          <th>状态</th>
          <th>已解析/总行</th>
          <th>冲突</th>
          <th>入账孔号</th>
          <th>更新时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="batch in batches" :key="batch.batch_no">
          <td>{{ batch.batch_no }}</td>
          <td>{{ batch.filename }}</td>
          <td>
            <span :class="['badge', badgeClass(batch.status)]">{{ batch.status_label }}</span>
            <span v-if="batch.failed_line" class="muted">失败行：第 {{ batch.failed_line }} 行</span>
          </td>
          <td>{{ batch.parsed_rows }} / {{ batch.total_rows || '—' }}</td>
          <td :class="batch.conflict_count ? 'error-text' : ''">{{ batch.conflict_count }}</td>
          <td>{{ batch.landed_count }}</td>
          <td>{{ batch.updated_at }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(batch.batch_no)">查看暂存</button>
            <button
              v-if="batch.status !== 'landed'"
              class="link"
              type="button"
              @click="openRepair(batch)"
            >
              修复并续走
            </button>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td colspan="8" class="empty-state">暂无导入批次，可先上传一份钻孔清单</td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="detail-box">
      <header class="detail-head">
        <h3>暂存批次 {{ detail.batch_no }}（{{ detail.filename }}）</h3>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </header>

      <p v-if="detail.parse_error" class="error-text">{{ detail.parse_error }}</p>

      <template v-if="detail.conflicts?.length">
        <h4>冲突明细（整批暂存，未写入任何台账记录）</h4>
        <ul class="conflict-list">
          <li v-for="conflict in detail.conflicts" :key="`${conflict.source_line}-${conflict.type}`">
            <span class="muted">第 {{ conflict.source_line }} 行</span>
            <span class="badge warn">{{ conflictTypeLabel(conflict.type) }}</span>
            {{ conflict.message }}
          </li>
        </ul>
      </template>

      <template v-if="detail.notices?.length">
        <h4>现场确认说明</h4>
        <ul class="notice-list">
          <li v-for="(notice, index) in detail.notices" :key="index" class="ok-text">{{ notice }}</li>
        </ul>
      </template>

      <h4>已解析行（{{ detail.parsed_rows?.length ?? 0 }}）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>源文件行</th>
            <th>钻孔编号</th>
            <th>勘探区</th>
            <th>孔口坐标（归一）</th>
            <th>现场确认</th>
            <th>历史别名</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in detail.parsed_rows" :key="row.source_line">
            <td>第 {{ row.source_line }} 行</td>
            <td>{{ row['钻孔编号'] }}</td>
            <td>{{ row['勘探区'] }}</td>
            <td>{{ row['坐标归一'] }}</td>
            <td>{{ row['现场确认'] ? '是' : '否' }}</td>
            <td>{{ (row['历史别名'] || []).join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="repairTarget" class="repair-box">
        <h4>修复后从失败行继续解析</h4>
        <p class="page-desc">
          可整份粘贴修正后的文件内容（推荐：坐标只以当前文件为准，绝不会拿旧坐标顶替）；
          也可只填写失败行（第 {{ repairTarget.failed_line || '?' }} 行）需要覆盖的字段。
        </p>
        <label class="repair-field">
          <span>整份文件内容（含表头，留空则只改行字段）</span>
          <textarea v-model="repairContent" rows="6" placeholder="钻孔编号,勘探区,孔口坐标,…" />
        </label>
        <div class="repair-line">
          <label v-for="field in lineRepairFields" :key="field" class="repair-field">
            <span>{{ field }}</span>
            <input v-model="repairValues[field]" :placeholder="`覆盖第 ${repairTarget.failed_line || '?'} 行的${field}`" />
          </label>
        </div>
        <div class="repair-actions">
          <button class="btn primary" type="button" :disabled="repairing" @click="submitRepair">
            {{ repairing ? '续走中…' : '修复并从失败行续走' }}
          </button>
          <button class="btn ghost" type="button" @click="repairTarget = null">取消</button>
        </div>
        <p v-if="repairError" class="error-text">{{ repairError }}</p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/borehole'

type Batch = {
  batch_no: string
  filename: string
  status: string
  status_label: string
  total_rows: number
  parsed_rows: number
  failed_line: number | null
  parse_error: string | null
  conflict_count: number
  conflicts: { source_line: number; type: string; message: string }[]
  landed_count: number
  updated_at: string
}

const emit = defineEmits<{ (event: 'landed'): void }>()

const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const repairing = ref(false)
const batches = ref<Batch[]>([])
const resultMessage = ref('')
const lastOk = ref(false)
const detail = ref<Record<string, any> | null>(null)
const repairTarget = ref<Batch | null>(null)
const repairContent = ref('')
const repairValues = ref<Record<string, string>>({})
const repairError = ref('')

const lineRepairFields = ['钻孔编号', '勘探区', '孔口坐标', '现场确认', '历史别名', '设计孔深']

const CONFLICT_LABELS: Record<string, string> = {
  CODE_EXISTS: '孔号已存在',
  COORD_EXISTS: '坐标与存量重合',
  ALIAS_COORD_CONFLICT: '别名坐标待现场确认',
  ALIAS_EXISTS: '别名已被占用',
  FILE_DUP_CODE: '文件内孔号重复',
  FILE_DUP_COORD: '文件内坐标重复',
}

function conflictTypeLabel(type: string): string {
  return CONFLICT_LABELS[type] ?? type
}

function badgeClass(status: string): string {
  if (status === 'landed') return 'ok'
  if (status === 'conflict_rejected') return 'warn'
  return 'muted-badge'
}

async function loadBatches() {
  const response = await request(`${ENDPOINT}/imports`)
  if (response.ok) {
    batches.value = await response.json()
  }
}

function triggerUpload() {
  fileInput.value?.click()
}

async function onFileChosen(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  resultMessage.value = ''
  try {
    const form = new FormData()
    form.append('file', file)
    // multipart 边界由浏览器生成，不能手动设置 Content-Type
    const response = await request(`${ENDPOINT}/imports`, { method: 'POST', body: form })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail ?? '清单过闸失败')
    }
    lastOk.value = Boolean(payload.ok)
    resultMessage.value = payload.message
    await loadBatches()
    if (payload.ok) emit('landed')
  } catch (error) {
    lastOk.value = false
    resultMessage.value = error instanceof Error ? error.message : '清单过闸失败'
  } finally {
    uploading.value = false
    input.value = ''
  }
}

async function downloadTemplate() {
  const response = await request(`${ENDPOINT}/imports/template`)
  if (!response.ok) return
  const payload = await response.json()
  const blob = new Blob([payload.csv as string], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = '钻孔清单模板.csv'
  anchor.click()
  URL.revokeObjectURL(url)
}

async function openDetail(batchNo: string) {
  repairTarget.value = null
  const response = await request(`${ENDPOINT}/imports/${batchNo}`)
  if (response.ok) {
    detail.value = await response.json()
  }
}

function openRepair(batch: Batch) {
  repairTarget.value = batch
  repairContent.value = ''
  repairValues.value = {}
  repairError.value = ''
  void openDetail(batch.batch_no)
}

async function submitRepair() {
  if (!repairTarget.value) return
  repairing.value = true
  repairError.value = ''
  const values = Object.fromEntries(Object.entries(repairValues.value).filter(([, value]) => value.trim()))
  const body: Record<string, unknown> = {}
  if (repairContent.value.trim()) body.content = repairContent.value
  if (Object.keys(values).length) {
    body.line = repairTarget.value.failed_line
    body.values = values
  }
  if (!Object.keys(body).length) {
    repairError.value = '请粘贴整份修正内容，或至少覆盖失败行的一个字段'
    repairing.value = false
    return
  }
  try {
    const response = await request(`${ENDPOINT}/imports/${repairTarget.value.batch_no}/resume`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail ?? '续解析失败')
    }
    resultMessage.value = payload.message
    lastOk.value = Boolean(payload.ok)
    await loadBatches()
    await openDetail(repairTarget.value.batch_no)
    if (payload.ok) {
      repairTarget.value = null
      emit('landed')
    } else {
      repairTarget.value = batches.value.find((item) => item.batch_no === repairTarget.value?.batch_no) ?? null
    }
  } catch (error) {
    repairError.value = error instanceof Error ? error.message : '续解析失败'
  } finally {
    repairing.value = false
  }
}

void loadBatches()
defineExpose({ reload: loadBatches })
</script>

<style scoped>
.upload-box {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 12px;
}
.upload-controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 200px;
}
.gate-message {
  margin: 0 0 12px;
  font-size: 13px;
}
.ok-text {
  color: #067647;
}
.badge {
  display: inline-block;
  border-radius: 10px;
  padding: 1px 8px;
  font-size: 12px;
  margin-right: 6px;
}
.badge.ok {
  background: #ecfdf3;
  color: #067647;
}
.badge.warn {
  background: #fef3f2;
  color: #b42318;
}
.badge.muted-badge {
  background: #f2f4f7;
  color: #475467;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
.detail-box {
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.conflict-list,
.notice-list {
  margin: 0 0 12px;
  padding-left: 18px;
  font-size: 13px;
}
.conflict-list li {
  margin-bottom: 4px;
}
.repair-box {
  margin-top: 14px;
  border-top: 1px dashed var(--border);
  padding-top: 12px;
}
.repair-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--muted);
}
.repair-field textarea,
.repair-field input {
  font-size: 13px;
  padding: 6px 8px;
}
.repair-line {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
.repair-actions {
  display: flex;
  gap: 8px;
}
</style>
