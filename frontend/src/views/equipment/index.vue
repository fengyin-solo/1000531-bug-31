<template>
  <section class="page equipment-page" data-module="equipment">
    <header class="page-head">
      <div>
        <h2>勘探设备管理</h2>
        <p class="page-desc">
          仪器台账、校准计划、领用待办由同一状态机驱动；侧栏与详情读取同一个台账版本（当前第 {{ workspace?.version ?? '—' }} 版）。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记勘探仪器</button>
        <button class="btn" type="button" @click="exportRows">导出设备台账</button>
      </div>
    </header>

    <div v-if="workspace" class="equipment-layout">
      <aside class="equipment-side">
        <article class="side-block">
          <h3>状态概览</h3>
          <ul class="side-stats">
            <li v-for="item in statItems" :key="item.label">
              <span>{{ item.label }}</span>
              <strong>{{ item.value }}</strong>
            </li>
          </ul>
        </article>

        <article class="side-block">
          <h3>领用待办 <em class="side-count">{{ workspace.todos.length }}</em></h3>
          <ul class="side-list">
            <li v-for="todo in workspace.todos" :key="todo.id" class="side-item" @click="focusSerial(todo.仪器编号)">
              <span class="side-title">{{ todo.仪器编号 }}</span>
              <span class="side-meta">经办人：{{ todo.经办人 }} · 领用 {{ todo.领用日期 }}</span>
            </li>
            <li v-if="!workspace.todos.length" class="side-empty">暂无未归还仪器</li>
          </ul>
        </article>

        <article class="side-block">
          <h3>校准计划清单 <em class="side-count">{{ workspace.plans.length }}</em></h3>
          <ul class="side-list">
            <li v-for="plan in workspace.plans" :key="plan.id" class="side-item" @click="focusSerial(plan.仪器编号)">
              <span class="side-title">{{ plan.仪器编号 }} · {{ plan.plan_status }}</span>
              <span class="side-meta">有效期至 {{ plan.有效期至 || '—' }}</span>
            </li>
            <li v-if="!workspace.plans.length" class="side-empty">校准计划均已闭环</li>
          </ul>
        </article>
      </aside>

      <div class="equipment-main">
        <form class="filter-bar" @submit.prevent="reload">
          <label class="filter-item">
            <span>仪器编号</span>
            <input v-model="keyword" placeholder="按仪器编号检索" />
          </label>
          <label class="filter-item">
            <span>仪器状态</span>
            <select v-model="statusFilter">
              <option value="">全部状态</option>
              <option v-for="s in workspace.statuses" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
          <button class="btn" type="submit">查询</button>
          <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
        </form>

        <table class="data-table">
          <thead>
            <tr>
              <th v-for="column in columns" :key="column">{{ column }}</th>
              <th>版本</th>
              <th>可执行动作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in workspace.items" :key="String(row.equipment.id)"
                :class="{ 'row-selected': row.equipment.仪器编号 === selectedSerial }">
              <td v-for="column in columns" :key="column">{{ displayValue(row, column) }}</td>
              <td>v{{ row.equipment.version }}</td>
              <td class="row-actions">
                <button class="link" type="button" @click="openDetail(row)">详情</button>
                <button
                  v-for="action in row.allowed_actions"
                  :key="action"
                  class="link"
                  type="button"
                  :disabled="submitting"
                  @click="openAction(action, row)"
                >
                  {{ action }}
                </button>
                <span v-if="!row.allowed_actions.length" class="muted-text">无可执行动作</span>
              </td>
            </tr>
            <tr v-if="!workspace.items.length">
              <td :colspan="columns.length + 2" class="empty-state">暂无匹配的勘探仪器</td>
            </tr>
          </tbody>
        </table>

        <footer class="page-foot">
          <span>共 {{ workspace.total }} 条台账记录 · 台账版本 v{{ workspace.version }}</span>
          <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
        </footer>
      </div>
    </div>

    <!-- 详情抽屉：台账 / 校准计划 / 领用待办同版本展示 -->
    <div v-if="detail" class="drawer-mask" @click.self="closeDetail">
      <div class="drawer">
        <header class="drawer-head">
          <h3>{{ detail.equipment.仪器编号 }} · {{ detail.equipment.仪器名称 }}</h3>
          <button class="btn ghost" type="button" @click="closeDetail">关闭</button>
        </header>
        <p class="version-line">
          当前状态 <strong>{{ detail.equipment.status }}</strong> · 台账版本 v{{ detail.equipment.version }}
        </p>
        <section class="drawer-section">
          <h4>设备台账</h4>
          <table class="data-table compact">
            <tbody>
              <tr v-for="column in columns" :key="column">
                <th>{{ column }}</th>
                <td>{{ displayValue(detail, column) }}</td>
              </tr>
              <tr><th>上次经办人</th><td>{{ detail.equipment.上次经办人 || '—' }}</td></tr>
            </tbody>
          </table>
        </section>
        <section class="drawer-section">
          <h4>校准计划清单</h4>
          <table class="data-table compact">
            <thead><tr><th>计划状态</th><th>检定日期</th><th>有效期至</th><th>台账版本</th><th>备注</th></tr></thead>
            <tbody>
              <tr v-for="plan in detail.plans" :key="plan.id">
                <td>{{ plan.plan_status }}</td>
                <td>{{ plan.检定日期 || '—' }}</td>
                <td>{{ plan.有效期至 || '—' }}</td>
                <td>v{{ plan.ledger_version }}</td>
                <td>{{ plan.备注 || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </section>
        <section class="drawer-section">
          <h4>领用待办</h4>
          <table class="data-table compact">
            <thead><tr><th>待办状态</th><th>经办人</th><th>领用日期</th><th>归还日期</th><th>台账版本</th></tr></thead>
            <tbody>
              <tr v-for="todo in detail.todos" :key="todo.id">
                <td>{{ todo.todo_status }}</td>
                <td>{{ todo.经办人 }}</td>
                <td>{{ todo.领用日期 }}</td>
                <td>{{ todo.归还日期 || '—' }}</td>
                <td>v{{ todo.ledger_version }}</td>
              </tr>
              <tr v-if="!detail.todos.length"><td colspan="5" class="empty-state">暂无领用记录</td></tr>
            </tbody>
          </table>
        </section>
        <footer class="drawer-foot">
          <button
            v-for="action in detail.allowed_actions"
            :key="action"
            class="btn primary"
            type="button"
            :disabled="submitting"
            @click="openAction(action, detail)"
          >
            {{ action }}
          </button>
        </footer>
      </div>
    </div>

    <!-- 动作弹窗：按动作收集参数，提交时带 expected_version 与 request_id -->
    <div v-if="actionForm" class="drawer-mask" @click.self="closeAction">
      <form class="modal" @submit.prevent="submitAction">
        <h3>{{ actionForm.action }} · {{ actionForm.detail.equipment.仪器编号 }}</h3>
        <p class="version-line">基于台账版本 v{{ actionForm.detail.equipment.version }} 提交，重复点击不会重复入账。</p>

        <label v-if="actionForm.action === '领用仪器'" class="modal-field">
          <span>经办人 *</span>
          <input v-model="actionForm.values.经办人" placeholder="领用人姓名" />
        </label>
        <template v-if="actionForm.action === '送检检定'">
          <label class="modal-field">
            <span>检定结论 *</span>
            <select v-model="actionForm.values.检定结论">
              <option value="检定合格">检定合格（转在库可用）</option>
              <option value="检定不合格">检定不合格（转维修中）</option>
            </select>
          </label>
          <label class="modal-field">
            <span>检定日期</span>
            <input v-model="actionForm.values.检定日期" type="date" />
          </label>
        </template>
        <label v-if="actionForm.action === '归还仪器'" class="modal-field">
          <span>归还经办人</span>
          <input v-model="actionForm.values.经办人" placeholder="留空则按原领用人归还" />
        </label>

        <p class="modal-hint">仪器编号随请求一并提交，与台账编号不一致时后端拒绝写入。</p>
        <footer class="modal-foot">
          <button class="btn ghost" type="button" @click="closeAction">取消</button>
          <button class="btn primary" type="submit" :disabled="submitting">
            {{ submitting ? '提交中…' : '确认提交' }}
          </button>
        </footer>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Equipment = {
  id: number
  仪器编号: string
  仪器名称: string
  型号规格: string
  精度指标?: string
  检定日期?: string
  有效期至?: string
  使用人员?: string
  仪器状态?: string
  status: string
  version: number
  上次经办人?: string
}

type Plan = {
  id: number
  仪器编号: string
  plan_status: string
  检定日期: string
  有效期至: string
  ledger_version: number
  备注: string
}

type Todo = {
  id: number
  仪器编号: string
  todo_status: string
  经办人: string
  领用日期: string
  归还日期: string
  ledger_version: number
}

type Detail = {
  equipment: Equipment
  allowed_actions: string[]
  active_plan: Plan | null
  plans: Plan[]
  open_todo: Todo | null
  todos: Todo[]
}

type Workspace = {
  version: number
  page: number
  size: number
  total: number
  stats: Record<string, number>
  items: Detail[]
  todos: Todo[]
  plans: Plan[]
  statuses: string[]
  actions: string[]
}

const ENDPOINT = '/api/equipment'
const columns = ['仪器编号', '仪器名称', '型号规格', '精度指标', '检定日期', '有效期至', '使用人员', '仪器状态']

const workspace = ref<Workspace | null>(null)
const detail = ref<Detail | null>(null)
const selectedSerial = ref('')
const errorMessage = ref('')
const submitting = ref(false)
const keyword = ref('')
const statusFilter = ref('')
const actionForm = ref<{ action: string; detail: Detail; values: Record<string, string>; requestId: string } | null>(null)

const statItems = computed(() => {
  const stats = workspace.value?.stats ?? {}
  return [
    { label: '在库可用', value: stats['在库可用'] ?? 0 },
    { label: '借出使用', value: stats['借出使用'] ?? 0 },
    { label: '维修中', value: stats['维修中'] ?? 0 },
    { label: '待核验', value: stats['待核验'] ?? 0 },
    { label: '已停用', value: stats['已停用'] ?? 0 },
    { label: '待校准', value: stats['待校准'] ?? 0 },
  ]
})

function displayValue(row: Detail, column: string): string {
  if (column === '仪器状态') return row.equipment.status
  const value = row.equipment[column as keyof Equipment]
  return value === undefined || value === null || value === '' ? '—' : String(value)
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}/workspace?${query.toString()}`)
    if (!response.ok) throw new Error('勘探仪器工作区读取失败')
    const next = (await response.json()) as Workspace
    const openDetailId = detail.value?.equipment.id
    workspace.value = next
    // 侧栏与详情只能读同一版本：工作区换版本后，打开的详情一律按新版本重拉。
    if (openDetailId !== undefined) {
      await refreshDetail(openDetailId)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '勘探仪器工作区读取失败'
  }
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '新仪器请通过台账登记入口录入，登记后自动进入待核验节点'
}

function focusSerial(serial: string) {
  keyword.value = serial
  selectedSerial.value = serial
  void reload()
}

async function openDetail(row: Detail) {
  await refreshDetail(row.equipment.id)
}

async function refreshDetail(id: number) {
  try {
    const response = await request(`${ENDPOINT}/${id}/detail`)
    if (!response.ok) throw new Error('仪器详情读取失败')
    detail.value = (await response.json()) as Detail
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '仪器详情读取失败'
  }
}

function closeDetail() {
  detail.value = null
}

function newRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function openAction(action: string, row: Detail) {
  const initial: Record<string, string> = { action, 仪器编号: row.equipment.仪器编号 }
  if (action === '送检检定') initial.检定结论 = '检定合格'
  actionForm.value = { action, detail: row, values: initial, requestId: newRequestId() }
}

function closeAction() {
  if (submitting.value) return
  actionForm.value = null
}

async function submitAction() {
  if (!actionForm.value) return
  const form = actionForm.value
  submitting.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${form.detail.equipment.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        values: form.values,
        request_id: form.requestId,
        expected_version: form.detail.equipment.version,
      }),
    })
    const payload = (await response.json()) as { ok: boolean; message: string }
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '动作未生效，请基于当前版本重试')
    }
    actionForm.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '勘探设备操作失败'
    // 版本冲突/跳步被拒时，弹窗保留并同步到最新版本，便于基于当前版本重试。
    await refreshDetail(form.detail.equipment.id)
    if (detail.value) {
      const latest = detail.value
      const allowed = latest.allowed_actions.includes(form.action)
      if (!allowed) actionForm.value = null
      else actionForm.value = { ...form, detail: latest, values: { ...form.values }, requestId: newRequestId() }
    }
  } finally {
    submitting.value = false
  }
}

onMounted(reload)
</script>
