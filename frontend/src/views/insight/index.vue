<template>
  <section class="page" data-module="insight">
    <header class="page-head">
      <div>
        <h2>洞察推送管理</h2>
        <p class="page-desc">管理数据洞察报告的多渠道推送：产物/报告/回执共享生成版本，成功渠道复用、失败渠道按缺口续发。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="seedDemo">构造续发样例</button>
        <button class="btn ghost" type="button" @click="resetDispatch">重置调度</button>
        <button class="btn" type="button" @click="exportRows">导出清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

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
          <td>{{ formatCell(row, '报告编号') }}</td>
          <td>{{ formatCell(row, '报告标题') }}</td>
          <td>{{ formatCell(row, '分析主题') }}</td>
          <td>
            <span class="badge version">{{ formatCell(row, '生成版本') || '—' }}</span>
            <span class="badge version sub">{{ formatCell(row, '报告版本') || '—' }}</span>
          </td>
          <td>{{ formatCell(row, '推送渠道') }}</td>
          <td><span :class="['badge', pushBadgeClass(row)]">{{ formatCell(row, '推送状态') || '未推送' }}</span></td>
          <td>
            <template v-if="pendingList(row).length">
              <span v-for="item in pendingList(row)" :key="item" class="badge pending">{{ item }}</span>
            </template>
            <span v-else class="muted">—</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="runSimpleAction('提交生成', row)">提交生成</button>
            <button class="link" type="button" @click="openPush(row)">推送报告</button>
            <button class="link" type="button" @click="openRetry(row)">失败续发</button>
            <button class="link" type="button" @click="openRevise(row)">产物换版</button>
            <button class="link" type="button" @click="openDetail(row)">调度详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无洞察推送数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条洞察推送记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 推送配置弹窗：编排每个渠道的返回结果，用于验收成功/超时/错误/空回执 -->
    <div v-if="pushDialog.open" class="modal-mask" @click.self="pushDialog.open = false">
      <div class="modal">
        <h3>推送报告 {{ pushDialog.reportCode }}（{{ pushDialog.mode === 'retry' ? '失败续发' : '渠道推送' }}）</h3>
        <div class="form-grid">
          <label class="filter-item">
            <span>接收范围（逗号分隔）</span>
            <input v-model="pushDialog.scopesText" :disabled="pushDialog.mode === 'retry'" />
          </label>
          <label class="filter-item">
            <span>每渠道分段数</span>
            <input v-model.number="pushDialog.segmentCount" type="number" min="1" max="10"
                   :disabled="pushDialog.mode === 'retry'" />
          </label>
          <label class="filter-item">
            <span>调度任务标识</span>
            <input v-model="pushDialog.scheduler" />
          </label>
        </div>
        <table class="data-table inner">
          <thead>
            <tr><th>渠道</th><th>本次返回结果</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in pushDialog.channels" :key="item.name">
              <td>
                <label class="inline-check">
                  <input v-model="item.enabled" type="checkbox" :disabled="pushDialog.mode === 'retry'" />
                  {{ item.name }}
                </label>
              </td>
              <td>
                <select v-model="item.outcome">
                  <option value="ok">成功（带回执号）</option>
                  <option value="timeout">超时（留缺口）</option>
                  <option value="error">错误（渠道不可用）</option>
                  <option value="empty">空回执（不落地）</option>
                </select>
              </td>
              <td class="muted">{{ outcomeHint(item.outcome) }}</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="pushDialog.open = false">取消</button>
          <button class="btn primary" type="button" :disabled="pushDialog.loading" @click="submitPush">
            {{ pushDialog.loading ? '执行中…' : '确认执行' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 产物换版弹窗 -->
    <div v-if="reviseDialog.open" class="modal-mask" @click.self="reviseDialog.open = false">
      <div class="modal small">
        <h3>报表产物换版 {{ reviseDialog.reportCode }}</h3>
        <p class="muted">换版后当前产物下的渠道回执全部转为「已失效」仅作历史，新版本推送状态从零统计。</p>
        <label class="filter-item">
          <span class="inline-check">
            <input v-model="reviseDialog.changeReportVersion" type="checkbox" />
            报告内容同步变更（报告版本递增 r1 → r2）
          </span>
        </label>
        <label class="filter-item">
          <span>备注</span>
          <input v-model="reviseDialog.note" placeholder="例如：指标口径调整后重新出报" />
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="reviseDialog.open = false">取消</button>
          <button class="btn primary" type="button" :disabled="reviseDialog.loading" @click="submitRevise">确认换版</button>
        </div>
      </div>
    </div>

    <!-- 调度详情弹窗：版本一致性、四类计数、渠道回执、历史回执、执行记录 -->
    <div v-if="detail.open" class="modal-mask" @click.self="detail.open = false">
      <div class="modal wide">
        <h3>调度详情 {{ detail.reportCode }}</h3>
        <div v-if="detail.loading" class="muted">加载中…</div>
        <template v-else-if="detail.data">
          <div class="version-line">
            <span :class="['badge', detail.data['版本一致'] ? 'ok' : 'failed']">
              {{ detail.data['版本一致'] ? '报告与产物版本一致' : '版本不一致' }}
            </span>
            <span class="muted">
              生成版本 {{ detail.data['报告']['生成版本'] || '—' }} ·
              报告版本 {{ detail.data['报告']['报告版本'] || '—' }} ·
              推送状态 {{ detail.data['推送状态'] }}
            </span>
            <button class="btn" type="button" @click="runConcurrentDemo">并发推送演练</button>
            <button class="btn ghost" type="button" @click="refreshDetail">刷新</button>
          </div>

          <div class="stat-row counters">
            <article v-for="(value, label) in detail.data['计数']" :key="label" class="stat-card">
              <span class="stat-label">{{ label }}</span>
              <strong class="stat-value" :class="counterClass(String(label))">{{ value }}</strong>
            </article>
          </div>

          <div v-if="detail.data['待补渠道'].length" class="pending-box">
            <strong>待补渠道：</strong>
            <span v-for="item in detail.data['待补渠道']" :key="item" class="badge pending">{{ item }}</span>
          </div>

          <h4>当前版本渠道回执（每四元组唯一，分段不重复）</h4>
          <table class="data-table inner">
            <thead>
              <tr>
                <th>渠道/接收范围</th><th>产物/报告版本</th><th>回执状态</th><th>分段</th>
                <th>失败原因</th><th>尝试次数</th><th>最近发送</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="receipt in detail.data['渠道回执']" :key="receipt.id">
                <td>{{ receipt['渠道'] }} / {{ receipt['接收范围'] }}</td>
                <td>{{ receipt['产物版本'] }} · {{ receipt['报告版本'] }}</td>
                <td>
                  <span :class="['badge', receiptBadgeClass(receipt['回执状态'])]">{{ receipt['回执状态'] }}</span>
                  <span v-if="receipt['渠道结论']" class="badge sub">{{ receipt['渠道结论'] }}</span>
                </td>
                <td>
                  <span v-for="seg in receipt['分段明细']" :key="seg['分段序号']"
                        :class="['seg-dot', seg['状态'] === '已送达' ? 'delivered' : 'gap']"
                        :title="segTip(seg)">
                    {{ seg['分段序号'] }}
                  </span>
                  <span class="muted">（{{ receipt['成功段数'] }}/{{ receipt['分段总数'] }} 送达）</span>
                </td>
                <td class="reason-cell">
                  <template v-if="Object.keys(receipt['失败原因']).length">
                    <div v-for="(reason, key) in receipt['失败原因']" :key="key">{{ key }}：{{ reason }}</div>
                  </template>
                  <span v-else class="muted">—</span>
                </td>
                <td>{{ receipt['尝试次数'] }}</td>
                <td>{{ receipt['最近发送时间'] }}</td>
              </tr>
              <tr v-if="!detail.data['渠道回执'].length">
                <td colspan="7" class="empty-state">当前版本暂无生效回执（可能已换版或尚未推送）</td>
              </tr>
            </tbody>
          </table>

          <h4>历史回执（产物换版后失效，仅作查询，不参与推送状态）</h4>
          <table class="data-table inner">
            <thead>
              <tr><th>渠道/接收范围</th><th>旧版本</th><th>状态</th><th>失效原因/时间</th></tr>
            </thead>
            <tbody>
              <tr v-for="receipt in detail.data['历史回执']" :key="receipt.id">
                <td>{{ receipt['渠道'] }} / {{ receipt['接收范围'] }}</td>
                <td>{{ receipt['产物版本'] }} · {{ receipt['报告版本'] }}</td>
                <td><span class="badge stale">{{ receipt['回执状态'] }}</span></td>
                <td>{{ receipt['失效原因'] }}（{{ receipt['失效时间'] }}）</td>
              </tr>
              <tr v-if="!detail.data['历史回执'].length">
                <td colspan="4" class="empty-state">暂无历史回执</td>
              </tr>
            </tbody>
          </table>

          <h4>调度执行记录</h4>
          <table class="data-table inner">
            <thead>
              <tr><th>执行编号</th><th>动作</th><th>调度任务</th><th>版本</th><th>命中/补发/冲突/失效</th><th>渠道结果与异常</th></tr>
            </thead>
            <tbody>
              <tr v-for="run in detail.data['执行记录']" :key="run.id">
                <td>{{ run['执行编号'] }}</td>
                <td>{{ run['动作类型'] }}</td>
                <td>{{ run['调度任务'] }}</td>
                <td>{{ run['产物版本'] }} · {{ run['报告版本'] }}</td>
                <td>
                  <span class="counter-chip hit">命{{ run['计数']['命中'] }}</span>
                  <span class="counter-chip retry">补{{ run['计数']['补发'] }}</span>
                  <span class="counter-chip conflict">冲{{ run['计数']['冲突'] }}</span>
                  <span class="counter-chip stale">失{{ run['计数']['失效'] }}</span>
                </td>
                <td class="reason-cell">
                  <div v-for="cr in run['渠道结果']" :key="cr['渠道'] + cr['接收范围']">
                    {{ cr['渠道'] }}/{{ cr['接收范围'] }}：{{ cr['处理结果'] }}
                    <template v-if="cr['回执状态']">（{{ cr['回执状态'] }}）</template>
                  </div>
                  <div v-for="item in run['冲突明细']" :key="item" class="error-text">冲突：{{ item }}</div>
                  <div v-for="item in run['空回执渠道']" :key="item" class="error-text">空回执：{{ item }}</div>
                </td>
              </tr>
            </tbody>
          </table>
        </template>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="detail.open = false">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, unknown>

interface CounterMap {
  命中: number
  补发: number
  冲突: number
  失效: number
}

interface Segment {
  分段序号: number
  状态: string
  回执号: string | null
  失败原因: string | null
  发送时间: string | null
}

interface Receipt {
  id: number
  渠道: string
  接收范围: string
  产物版本: string
  报告版本: string
  生成版本: string
  回执状态: string
  渠道结论: string | null
  生效: boolean
  历史: boolean
  分段总数: number
  成功段数: number
  缺口段数: number
  分段明细: Segment[]
  失败原因: Record<string, string>
  尝试次数: number
  最近发送时间: string
  失效时间: string | null
  失效原因?: string
}

interface RunRecord {
  id: number
  执行编号: string
  动作类型: string
  调度任务: string
  产物版本: string
  报告版本: string
  计数: CounterMap
  渠道结果: Array<Record<string, string>>
  冲突明细: string[]
  空回执渠道: string[]
}

interface DispatchStatus {
  报告: Row
  当前产物: Row | null
  版本一致: boolean
  推送状态: string
  待补渠道: string[]
  渠道回执: Receipt[]
  历史回执: Receipt[]
  计数: CounterMap
  执行记录: RunRecord[]
}

interface ChannelOption {
  name: string
  enabled: boolean
  outcome: 'ok' | 'timeout' | 'error' | 'empty'
}

const ENDPOINT = '/api/insight'
const columns = ['报告编号', '报告标题', '分析主题', '版本', '推送渠道', '推送状态', '待补渠道']
const filterFields = ['报告编号', '报告标题', '分析主题']
const ALL_CHANNELS = ['邮件', '短信', '站内信', '企业微信']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})

const stats = ref([
  { label: '洞察报告总数', value: 0 },
  { label: '全部完成', value: 0 },
  { label: '待补/异常报告', value: 0 },
])

const pushDialog = reactive({
  open: false,
  loading: false,
  mode: 'push' as 'push' | 'retry',
  reportId: 0,
  reportCode: '',
  scopesText: '默认接收范围',
  segmentCount: 3,
  scheduler: 'manual',
  channels: [] as ChannelOption[],
})

const reviseDialog = reactive({
  open: false,
  loading: false,
  reportId: 0,
  reportCode: '',
  changeReportVersion: true,
  note: '',
})

const detail = reactive({
  open: false,
  loading: false,
  reportId: 0,
  reportCode: '',
  data: null as DispatchStatus | null,
})

function asText(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

function formatCell(row: Row, field: string): string {
  if (field === '版本') {
    return [asText(row['生成版本']), asText(row['报告版本'])].filter(Boolean).join(' / ') || '—'
  }
  return asText(row[field]) || '—'
}

function pendingList(row: Row): string[] {
  const value = row['待补渠道']
  return Array.isArray(value) ? value.map(asText) : []
}

function pushBadgeClass(row: Row): string {
  const status = asText(row['推送状态'])
  if (status === '全部完成') return 'ok'
  if (status === '部分完成') return 'pending'
  if (status === '全部失败') return 'failed'
  return 'sub'
}

function receiptBadgeClass(status: string): string {
  if (status === '成功') return 'ok'
  if (status === '部分完成') return 'pending'
  if (status === '已失效') return 'stale'
  return 'failed'
}

function counterClass(label: string): string {
  return { 命中: 'num-hit', 补发: 'num-retry', 冲突: 'num-conflict', 失效: 'num-stale' }[label] || ''
}

function outcomeHint(outcome: string): string {
  return {
    ok: '渠道返回带回执号的正常应答，分段记为已送达',
    timeout: '渠道超时无送达确认，分段保留缺口，续发只补这些分段',
    error: '渠道明确报错，失败原因写入回执，仍可后续续发',
    empty: '渠道无任何回执内容，不写回执、不计成功，只登记待补',
  }[outcome] || ''
}

function segTip(seg: Segment): string {
  return `分段${seg['分段序号']}：${seg['状态']}${seg['失败原因'] ? `（${seg['失败原因']}）` : ''}`
}

async function postJson(path: string, payload?: unknown): Promise<unknown> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(payload ?? {}),
  })
  if (!response.ok) {
    let detail = ''
    try {
      detail = JSON.stringify(await response.json())
    } catch {
      detail = response.statusText
    }
    throw new Error(`接口返回 ${response.status}：${detail}`)
  }
  return response.json()
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('洞察报告列表读取失败')
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((r) => asText(r['推送状态']) === '全部完成').length
    stats.value[2].value = rows.value.filter((r) => pendingList(r).length > 0).length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '洞察推送列表读取失败'
  }
}

async function runSimpleAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    await postJson(`${ENDPOINT}/${asText(row.id)}/actions`, { values: { action } })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '动作执行失败'
  }
}

function buildChannelOptions(row: Row): ChannelOption[] {
  const configured = asText(row['推送渠道'])
    .split(/[、,]/)
    .map((item) => item.trim())
    .filter(Boolean)
  const names = configured.length ? configured : ALL_CHANNELS.slice(0, 2)
  return names.map((name) => ({
    name,
    enabled: true,
    outcome: name === '短信' ? 'timeout' : 'ok',
  }))
}

function openPush(row: Row) {
  pushDialog.mode = 'push'
  pushDialog.reportId = Number(row.id)
  pushDialog.reportCode = asText(row['报告编号'])
  pushDialog.channels = buildChannelOptions(row)
  pushDialog.scopesText = '默认接收范围'
  pushDialog.segmentCount = 3
  pushDialog.scheduler = 'manual'
  pushDialog.open = true
}

function openRetry(row: Row) {
  pushDialog.mode = 'retry'
  pushDialog.reportId = Number(row.id)
  pushDialog.reportCode = asText(row['报告编号'])
  // 续发渠道由引擎按缺口自动选择，这里只用于编排缺口渠道的返回结果
  pushDialog.channels = buildChannelOptions(row)
  pushDialog.scheduler = 'retry-job'
  pushDialog.open = true
}

async function submitPush() {
  pushDialog.loading = true
  errorMessage.value = ''
  try {
    const selected = pushDialog.channels.filter((item) => item.enabled)
    const scopes = pushDialog.scopesText.split(/[，,]/).map((item) => item.trim()).filter(Boolean)
    const overrides: Record<string, Array<Record<string, unknown>>> = {}
    for (const item of selected) {
      // 前端把「该渠道本次返回什么」展开成每个分段一条的编排序列
      overrides[item.name] = Array.from({ length: pushDialog.segmentCount }, () => ({ outcome: item.outcome }))
    }
    const path = pushDialog.mode === 'retry' ? 'retry' : 'push'
    const payload = pushDialog.mode === 'retry'
      ? { scheduler: pushDialog.scheduler, overrides }
      : {
          channels: selected.map((item) => item.name),
          scopes,
          segment_count: pushDialog.segmentCount,
          scheduler: pushDialog.scheduler,
          overrides,
        }
    const result = (await postJson(`${ENDPOINT}/${pushDialog.reportId}/${path}`, payload)) as Row
    const counters = result['计数'] as CounterMap
    errorMessage.value = ''
    pushDialog.open = false
    window.alert(
      `执行完成：${asText((result['报告推送状态'] as Row)?.['推送状态'])}\n` +
      `命中 ${counters['命中']} · 补发 ${counters['补发']} · 冲突 ${counters['冲突']} · 失效 ${counters['失效']}`,
    )
    await reload()
    if (detail.open && detail.reportId === pushDialog.reportId) await refreshDetail()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '推送执行失败'
  } finally {
    pushDialog.loading = false
  }
}

function openRevise(row: Row) {
  reviseDialog.reportId = Number(row.id)
  reviseDialog.reportCode = asText(row['报告编号'])
  reviseDialog.changeReportVersion = true
  reviseDialog.note = ''
  reviseDialog.open = true
}

async function submitRevise() {
  reviseDialog.loading = true
  errorMessage.value = ''
  try {
    const result = (await postJson(`${ENDPOINT}/${reviseDialog.reportId}/artifact`, {
      change_report_version: reviseDialog.changeReportVersion,
      note: reviseDialog.note,
    })) as Row
    const counters = result['计数'] as CounterMap
    reviseDialog.open = false
    window.alert(`产物已换版，旧回执失效 ${counters['失效']} 条，仅作历史保留`)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '产物换版失败'
  } finally {
    reviseDialog.loading = false
  }
}

async function openDetail(row: Row) {
  detail.open = true
  detail.loading = true
  detail.reportId = Number(row.id)
  detail.reportCode = asText(row['报告编号'])
  detail.data = null
  await refreshDetail()
}

async function refreshDetail() {
  detail.loading = true
  try {
    const response = await request(`${ENDPOINT}/${detail.reportId}/dispatch`)
    if (!response.ok) throw new Error('调度详情读取失败')
    detail.data = (await response.json()) as DispatchStatus
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度详情读取失败'
  } finally {
    detail.loading = false
  }
}

async function runConcurrentDemo() {
  errorMessage.value = ''
  try {
    // 两个调度任务并发推送同一新渠道，服务端按四元组互斥，后来者记冲突、不产生重复回执
    const overrides = { 站内信: Array.from({ length: 3 }, () => ({ outcome: 'ok', delay: 0.15 })) }
    const payload = { channels: ['站内信'], scheduler: 'concurrent-demo', overrides }
    const [first, second] = await Promise.all([
      postJson(`${ENDPOINT}/${detail.reportId}/push`, payload),
      postJson(`${ENDPOINT}/${detail.reportId}/push`, payload),
    ])
    const conflict =
      ((first as Row)['计数'] as CounterMap)['冲突'] + ((second as Row)['计数'] as CounterMap)['冲突']
    window.alert(conflict > 0 ? `并发演练完成：拦截冲突 ${conflict} 次，回执仍保持唯一` : '两个任务未发生碰撞，可再试一次')
    await refreshDetail()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '并发演练失败'
  }
}

async function seedDemo() {
  errorMessage.value = ''
  try {
    await postJson(`${ENDPOINT}/dispatch/demo`)
    await reload()
    errorMessage.value = ''
    window.alert('续发样例已就绪：INSI-0003 邮件成功、短信超时，报告为部分完成，可执行「失败续发」')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '样例构造失败'
  }
}

async function resetDispatch() {
  errorMessage.value = ''
  try {
    await postJson(`${ENDPOINT}/dispatch/reset`)
    detail.open = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重置失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; }
.muted { color: var(--muted); font-size: 12px; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px;
  border: 1px solid var(--border); background: #f1f5f9; white-space: nowrap;
}
.badge.version { background: #eef4ff; border-color: #c7dbff; color: #1d4ed8; }
.badge.sub { background: #f8fafc; color: var(--muted); margin-left: 4px; }
.badge.ok { background: #ecfdf3; border-color: #a6f4c5; color: #027a48; }
.badge.pending { background: #fffaeb; border-color: #fedf89; color: #b54708; margin: 2px; }
.badge.failed { background: #fef3f2; border-color: #fda29b; color: #b42318; }
.badge.stale { background: #f2f4f7; border-color: #d0d5dd; color: #475467; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: flex-start; justify-content: center; padding: 32px 16px;
  z-index: 50; overflow-y: auto;
}
.modal {
  background: #fff; border-radius: 10px; padding: 20px 24px; width: 640px; max-width: 100%;
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.25);
}
.modal.small { width: 480px; }
.modal.wide { width: 1080px; }
.modal h3 { margin: 0 0 12px; }
.modal h4 { margin: 18px 0 8px; font-size: 14px; }
.form-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 12px; }
.inline-check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.data-table.inner { font-size: 12px; }
.data-table.inner th, .data-table.inner td { padding: 6px 8px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.version-line { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.counters .stat-card { max-width: 160px; }
.num-hit { color: #027a48; }
.num-retry { color: #1d4ed8; }
.num-conflict { color: #b54708; }
.num-stale { color: #475467; }
.pending-box { margin: 10px 0; padding: 8px 10px; background: #fffaeb; border: 1px solid #fedf89; border-radius: 8px; }
.seg-dot {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%; font-size: 11px; margin-right: 4px;
}
.seg-dot.delivered { background: #ecfdf3; color: #027a48; border: 1px solid #a6f4c5; }
.seg-dot.gap { background: #fef3f2; color: #b42318; border: 1px solid #fda29b; }
.reason-cell { max-width: 320px; line-height: 1.6; }
.counter-chip {
  display: inline-block; min-width: 34px; text-align: center;
  border-radius: 6px; padding: 2px 6px; margin-right: 4px; font-size: 12px;
}
.counter-chip.hit { background: #ecfdf3; color: #027a48; }
.counter-chip.retry { background: #eef4ff; color: #1d4ed8; }
.counter-chip.conflict { background: #fffaeb; color: #b54708; }
.counter-chip.stale { background: #f2f4f7; color: #475467; }
select {
  border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; font-size: 13px;
  background: #fff;
}
</style>
