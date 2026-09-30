<template>
  <section class="page" data-module="equipment">
    <header class="page-head">
      <div>
        <h2>勘探设备管理</h2>
        <p class="page-desc">仪器台账、校准计划清单、领用待办共用同一状态机，所有页面读取同一当前版本。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记勘探仪器</button>
        <button class="btn" type="button" @click="exportRows">导出勘探设备清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
      <span class="version-tag" v-if="revision">台账版本 v{{ revision }}</span>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-item"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <form class="filter-bar" @submit.prevent="reloadActive">
      <label class="filter-item">
        <span>仪器编号</span>
        <input v-model="keyword" placeholder="按仪器编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="s in currentStatuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in currentColumns" :key="column">{{ column }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="`${activeTab}-${String(row.id)}`">
          <td v-for="column in currentColumns" :key="column">{{ display(row, column) }}</td>
          <td class="row-actions">
            <template v-if="activeTab === 'ledger'">
              <button class="link" type="button" @click="openDetail(row)">详情</button>
              <button
                v-for="action in availableActions(row)"
                :key="action"
                class="link"
                type="button"
                :disabled="busyKey === actionKey(action, row)"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </template>
            <span v-else class="muted">只读视图</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="currentColumns.length + 1" class="empty-state">暂无数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="dialog" class="modal-mask" @click.self="closeDialog">
      <div class="modal-card">
        <h3>{{ dialog.title }}</h3>
        <p v-if="dialog.hint" class="modal-hint">{{ dialog.hint }}</p>

        <template v-if="dialog.kind === 'create'">
          <label class="form-line"><span>仪器编号 *</span><input v-model="form.serial" placeholder="同型号设备也以编号唯一区分" /></label>
          <label class="form-line"><span>仪器名称 *</span><input v-model="form.name" /></label>
          <label class="form-line"><span>型号规格 *</span><input v-model="form.model" /></label>
          <label class="form-line"><span>精度指标</span><input v-model="form.precision" /></label>
        </template>

        <template v-else-if="dialog.kind === 'action'">
          <ul class="detail-list">
            <li v-for="column in ledgerColumns" :key="column">
              <span>{{ column }}</span><strong>{{ dialog.row[column] ?? '—' }}</strong>
            </li>
            <li><span>当前版本</span><strong>v{{ dialog.row.version }}</strong></li>
          </ul>
          <label v-if="dialog.action === '领用'" class="form-line">
            <span>经办人 *</span>
            <input v-model="form.operator" placeholder="历史领用关系将保留此前经办人" />
          </label>
        </template>

        <template v-else-if="dialog.kind === 'detail'">
          <ul class="detail-list">
            <li v-for="(value, key) in dialog.row" :key="String(key)">
              <span>{{ key === 'status' ? '状态机状态' : key }}</span>
              <strong>{{ key === 'version' ? `v${value}` : (value ?? '—') }}</strong>
            </li>
          </ul>
        </template>

        <div class="modal-actions">
          <button class="btn ghost" type="button" :disabled="submitting" @click="closeDialog">取消</button>
          <button
            v-if="dialog.kind !== 'detail'"
            class="btn primary"
            type="button"
            :disabled="submitting"
            @click="submitDialog"
          >
            {{ submitting ? '提交中…' : '确认' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type TabKey = 'ledger' | 'calibration' | 'loans'

const ENDPOINT = '/api/equipment'

const ledgerColumns = ['仪器编号', '仪器名称', '型号规格', '精度指标', '检定日期', '有效期至', '使用人员', '仪器状态']
const calibrationColumns = ['仪器编号', '仪器名称', '型号规格', '检定日期', '有效期至', '状态', 'version']
const loanColumns = ['仪器编号', '仪器名称', '型号规格', '经办人', '借出日期', '归还日期', '状态']

const tabs: { key: TabKey; label: string }[] = [
  { key: 'ledger', label: '设备台账' },
  { key: 'calibration', label: '校准计划清单' },
  { key: 'loans', label: '领用待办' },
]

// 每个状态只暴露合法动作，按钮层面就不允许跳步。
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  待核验: ['检定通过', '停用'],
  在库可用: ['领用', '检修', '停用'],
  借出使用: ['归还', '停用'],
  维修中: ['修竣入库', '停用'],
  已停用: [],
}
const STATUS_BY_TAB: Record<TabKey, string[]> = {
  ledger: ['待核验', '在库可用', '借出使用', '维修中', '已停用'],
  calibration: ['待校准', '校准合格', '暂停校准', '计划终止'],
  loans: ['借出未还', '检修中挂账', '已归还'],
}

const rows = ref<Row[]>([])
const total = ref(0)
const revision = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const activeTab = ref<TabKey>('ledger')
const busyKey = ref('')

type Dialog =
  | { kind: 'create'; title: string; hint?: string }
  | { kind: 'action'; title: string; hint?: string; action: string; row: Row }
  | { kind: 'detail'; title: string; hint?: string; row: Row }
  | null
const dialog = ref<Dialog>(null)
const form = ref({ serial: '', name: '', model: '', precision: '', operator: '' })
const submitting = ref(false)

const stats = ref<{ label: string; value: number }[]>([
  { label: '仪器总数', value: 0 },
  { label: '在库可用', value: 0 },
  { label: '借出使用', value: 0 },
  { label: '维修中', value: 0 },
  { label: '待核验', value: 0 },
  { label: '未还待办', value: 0 },
])

const currentColumns = ref<string[]>(ledgerColumns)
const currentStatuses = computed(() => STATUS_BY_TAB[activeTab.value])

function display(row: Row, column: string): string | number | null {
  if (column === 'version') return `v${row[column] ?? 1}`
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : (value as string | number)
}

function availableActions(row: Row): string[] {
  return ACTIONS_BY_STATUS[String(row.status ?? '')] ?? []
}

function actionKey(action: string, row: Row): string {
  return `${action}:${String(row.id)}:${String(row.version ?? 0)}`
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reloadActive()
}

function switchTab(key: TabKey) {
  activeTab.value = key
  statusFilter.value = ''
  currentColumns.value = {
    ledger: ledgerColumns,
    calibration: calibrationColumns,
    loans: loanColumns,
  }[key]
  void reloadActive()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  form.value = { serial: '', name: '', model: '', precision: '', operator: '' }
  dialog.value = { kind: 'create', title: '登记勘探仪器', hint: '新仪器统一进入「待核验」节点，仪器编号全局唯一。' }
}

function openDetail(row: Row) {
  // 详情直接按 id 拉取，保证与侧栏读到同一个版本。
  void fetchDetail(Number(row.id))
}

async function fetchDetail(id: number) {
  try {
    const response = await request(`${ENDPOINT}/${id}`)
    if (!response.ok) throw new Error('详情读取失败')
    const payload = await response.json()
    dialog.value = { kind: 'detail', title: `仪器详情 ${payload['仪器编号'] ?? id}`, row: payload }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '详情读取失败'
  }
}

function runAction(action: string, row: Row) {
  if (busyKey.value) return
  busyKey.value = actionKey(action, row)
  form.value.operator = ''
  dialog.value = {
    kind: 'action',
    title: `执行「${action}」`,
    hint: actionHint(action),
    action,
    row,
  }
}

function actionHint(action: string): string {
  if (action === '领用') return '领用后生成新的领用待办；后续归还不会覆盖本次经办人。'
  if (action === '归还') return '仅结清当前未归还的待办，历史经办人原样保留。'
  if (action === '检修') return '在库仪器进入维修中，校准计划同步暂停。'
  if (action === '停用') return '借出未还也可停用：待办强制结清但历史经办人保留，停用后校准计划终止。'
  if (action === '检定通过') return '待核验仪器检定合格后进入在库可用。'
  return '维修完成，重新回到在库可用。'
}

function closeDialog() {
  if (submitting.value) return
  dialog.value = null
  busyKey.value = ''
}

// 每个动作生成独立幂等键：重复提交（双击/重试）只会回放首次结果。
function newIdemKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

async function submitDialog() {
  const current = dialog.value
  if (!current || submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    if (current.kind === 'create') {
      await postJson('', {
        仪器编号: form.value.serial.trim(),
        仪器名称: form.value.name.trim(),
        型号规格: form.value.model.trim(),
        精度指标: form.value.precision.trim() || null,
      })
    } else if (current.kind === 'action') {
      if (current.action === '领用' && !form.value.operator.trim()) {
        throw new Error('领用必须填写经办人')
      }
      await postJson(`/${current.row.id}/actions`, {
        action: current.action,
        operator: form.value.operator.trim() || null,
        version: Number(current.row.version ?? 1),
        idem_key: newIdemKey(),
      })
    }
    dialog.value = null
    busyKey.value = ''
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    submitting.value = false
  }
}

async function postJson(suffix: string, values: Record<string, unknown>) {
  const response = await request(`${ENDPOINT}${suffix}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok || !payload || payload.ok === false) {
    // 业务失败（前置状态不符/版本冲突）后端已回滚，提示后重新拉取最新版本。
    await reloadAll()
    throw new Error(payload?.message ?? '操作未生效，数据已回滚')
  }
  return payload
}

async function reloadStats() {
  try {
    const response = await request(`${ENDPOINT}/stats`)
    if (!response.ok) throw new Error('统计读取失败')
    const data = await response.json()
    revision.value = Number(data.revision ?? 0)
    stats.value = [
      { label: '仪器总数', value: data.total ?? 0 },
      { label: '在库可用', value: data.available ?? 0 },
      { label: '借出使用', value: data.borrowed ?? 0 },
      { label: '维修中', value: data.repairing ?? 0 },
      { label: '待核验', value: data.pending_check ?? 0 },
      { label: '未还待办', value: data.open_loans ?? 0 },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '统计读取失败'
  }
}

async function reloadActive() {
  errorMessage.value = ''
  const path = {
    ledger: '',
    calibration: '/calibration',
    loans: '/loans',
  }[activeTab.value]
  const query = new URLSearchParams()
  if (keyword.value.trim()) query.set('keyword', keyword.value.trim())
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}${path}?${query.toString()}`)
    if (!response.ok) throw new Error('列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '列表读取失败'
  }
}

// 任何一次流转后，台账/校准/待办/侧栏全部重新拉取，杜绝旧阶段残留视图。
async function reloadAll() {
  await Promise.all([reloadStats(), reloadActive()])
}

onMounted(reloadAll)
</script>

<style scoped>
.version-tag {
  margin-left: auto;
  align-self: center;
  font-size: 12px;
  color: #57606a;
  background: #f6f8fa;
  border: 1px solid var(--border, #d0d7de);
  border-radius: 999px;
  padding: 4px 10px;
}
.tab-bar { display: flex; gap: 8px; margin-bottom: 12px; }
.tab-item {
  border: 1px solid var(--border, #d0d7de);
  background: #fff;
  padding: 6px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}
.tab-item.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
.muted { color: #8c959f; font-size: 12px; }
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.modal-card {
  background: #fff;
  border-radius: 8px;
  width: 480px;
  max-width: calc(100vw - 32px);
  padding: 20px 22px;
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.2);
}
.modal-card h3 { margin: 0 0 8px; font-size: 16px; }
.modal-hint { margin: 0 0 12px; color: #57606a; font-size: 13px; }
.form-line { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; font-size: 13px; }
.form-line span { width: 84px; color: #57606a; flex-shrink: 0; }
.form-line input { flex: 1; padding: 6px 8px; border: 1px solid var(--border, #d0d7de); border-radius: 4px; }
.detail-list { list-style: none; margin: 0 0 12px; padding: 0; max-height: 320px; overflow: auto; }
.detail-list li { display: flex; justify-content: space-between; gap: 12px; padding: 5px 0; border-bottom: 1px dashed #eaeef2; font-size: 13px; }
.detail-list li span { color: #57606a; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
.link:disabled { color: #8c959f; cursor: wait; }
</style>
