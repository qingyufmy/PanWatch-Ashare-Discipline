import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api/client'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

type Signal = {
  signal_id: string
  trade_date: string
  symbol: string
  action: string
  raw_action: string
  status: string
  source_agent: string
  plan_version: number | null
  daily_plan_version: number | null
  generated_at: string
}

type Decision = { rule_id: string; decision: string; reason_codes: string[] }
type Execution = {
  execution_id: string
  actual_action: string
  actual_qty: number
  result: string
  reconcile_status: string
}
type SignalDetail = Signal & {
  actionable: { approved_qty: number | null; policy_version: string } | null
  policy_decisions: Decision[]
  executions: Execution[]
  lifecycle: Array<{ to_status: string; reason: string; occurred_at: string }>
}
type DailyPlan = {
  id: number
  trade_date: string
  version: number
  status: string
  model_run_id: string | null
  macro_evidence_snapshot_id: number | null
  market_context: {
    indices: Array<{ name: string; change_pct?: number | null; quality: string }>
    global_tech: Array<{ name: string; change_pct?: number | null; quality: string }>
    candidate_breadth: { breadth_up_pct?: number | null; sample_size?: number; regime?: string }
    risk_tone: string
  } | null
  payload: { portfolio_rationale?: string; proposals: Array<{ symbol: string; action: string; target_weight?: number | null; qty_hint: number | null; rationale: string }> }
}
type WorkflowRun = { run_id: string; step: string; slot: string; status: string; payload: Record<string, unknown> }
type Notification = { notification_id: string; title: string; symbol: string | null; delivery_status: string; reason: string; queued_at: string }
type IssueSummary = { issue_count: number; occurrences: number; unresolved: number }

const actions = ['OPEN', 'ADD', 'REDUCE', 'EXIT']

export default function SignalJournalPage() {
  const { toast } = useToast()
  const [signals, setSignals] = useState<Signal[]>([])
  const [dailyPlan, setDailyPlan] = useState<DailyPlan | null>(null)
  const [workflowRuns, setWorkflowRuns] = useState<WorkflowRun[]>([])
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [issues, setIssues] = useState<IssueSummary | null>(null)
  const [selected, setSelected] = useState<SignalDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [actualAction, setActualAction] = useState('REDUCE')
  const [quantity, setQuantity] = useState('')
  const [price, setPrice] = useState('')
  const [notes, setNotes] = useState('')
  const [recording, setRecording] = useState(false)
  const pendingExecution = useRef<{ fingerprint: string; requestId: string; executedAt: string } | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const tradeDate = new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' })
      const [nextSignals, plans, runs, notices, issueSummary] = await Promise.all([
        fetchAPI<Signal[]>('/portfolio-discipline/signals?limit=100'),
        fetchAPI<DailyPlan[]>(`/portfolio-workflow/plans?trade_date=${tradeDate}&limit=5`),
        fetchAPI<WorkflowRun[]>(`/portfolio-workflow/runs?trade_date=${tradeDate}&limit=100`),
        fetchAPI<Notification[]>(`/portfolio-workflow/notifications?trade_date=${tradeDate}&limit=100`),
        fetchAPI<IssueSummary>('/system-issues/summary/daily'),
      ])
      setSignals(nextSignals)
      setDailyPlan(plans[0] || null)
      setWorkflowRuns(runs)
      setNotifications(notices)
      setIssues(issueSummary)
    } catch (error) {
      toast(error instanceof Error ? error.message : '读取信号失败', 'error')
    } finally {
      setLoading(false)
    }
  }, [toast])

  const openSignal = useCallback(async (signalId: string) => {
    try {
      const detail = await fetchAPI<SignalDetail>(`/portfolio-discipline/signals/${signalId}`)
      setSelected(detail)
      setActualAction(actions.includes(detail.action) ? detail.action : 'REDUCE')
    } catch (error) {
      toast(error instanceof Error ? error.message : '读取详情失败', 'error')
    }
  }, [toast])

  useEffect(() => { void refresh() }, [refresh])

  const ignore = async () => {
    if (!selected) return
    const reason = window.prompt('填写忽略原因（至少两个字）')
    if (!reason || reason.trim().length < 2) return
    try {
      await fetchAPI(`/portfolio-discipline/signals/${selected.signal_id}/ignore`, {
        method: 'POST', body: JSON.stringify({ reason: reason.trim() }),
      })
      toast('已记录忽略原因', 'success')
      await Promise.all([refresh(), openSignal(selected.signal_id)])
    } catch (error) {
      toast(error instanceof Error ? error.message : '记录失败', 'error')
    }
  }

  const recordExecution = async () => {
    if (!selected || recording) return
    const actualQty = Number(quantity)
    const actualPrice = price.trim() ? Number(price) : null
    if (!Number.isInteger(actualQty) || actualQty <= 0 || (actualPrice !== null && !(actualPrice > 0))) {
      toast('请输入有效的实际数量和价格', 'error')
      return
    }
    const fingerprint = JSON.stringify([selected.signal_id, actualAction, actualQty, actualPrice, notes])
    if (pendingExecution.current?.fingerprint !== fingerprint) {
      pendingExecution.current = { fingerprint, requestId: crypto.randomUUID(), executedAt: new Date().toISOString() }
    }
    const request = pendingExecution.current
    if (!request) return
    setRecording(true)
    try {
      const result = await fetchAPI<Execution>(`/portfolio-discipline/signals/${selected.signal_id}/executions`, {
        method: 'POST',
        body: JSON.stringify({
          actual_action: actualAction, actual_qty: actualQty,
          actual_price: actualPrice, executed_at: request.executedAt,
          client_request_id: request.requestId, notes,
        }),
      })
      toast(result.result === 'USER_REPORTED_OUT_OF_POLICY' ? '已登记计划外用户报告，待持仓对账' : '已登记用户报告，待持仓对账', 'success')
      pendingExecution.current = null
      setQuantity('')
      setPrice('')
      setNotes('')
      await openSignal(selected.signal_id)
    } catch (error) {
      toast(error instanceof Error ? error.message : '记录失败', 'error')
    } finally {
      setRecording(false)
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">信号与纪律日志</h1>
          <p className="mt-1 text-sm text-muted-foreground">查看模型建议、确定性门禁和人工执行记录。只有状态为 APPROVED 的信号具有可执行资格。</p>
        </div>
        <Button variant="outline" onClick={() => void refresh()} disabled={loading}>刷新</Button>
      </header>
      <section className="card p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-base font-semibold">今日持仓工作流</h2>
          <span className="text-xs text-muted-foreground">{workflowRuns.length} 个时间槽已留痕 · 今日未解决问题 {issues?.unresolved ?? '—'}</span>
        </div>
        {dailyPlan ? (
          <>
            <p className="text-sm">盘前计划 v{dailyPlan.version} · {dailyPlan.status} · 覆盖 {dailyPlan.payload.proposals.length} 只持仓</p>
            {dailyPlan.market_context && <div className="rounded-lg border p-3 text-xs space-y-1">
              <p>指数：{dailyPlan.market_context.indices.map(item => `${item.name} ${item.change_pct == null ? '无数据' : `${item.change_pct.toFixed(2)}%`} (${item.quality})`).join(' · ')}</p>
              <p>情绪样本：{dailyPlan.market_context.candidate_breadth.regime || '未知'} · 上涨占比 {dailyPlan.market_context.candidate_breadth.breadth_up_pct?.toFixed(1) ?? '—'}% · 样本 {dailyPlan.market_context.candidate_breadth.sample_size ?? '—'} 只；不是全市场统计。</p>
              <p>全球科技：{dailyPlan.market_context.global_tech.map(item => `${item.name} ${item.change_pct == null ? '无数据' : `${item.change_pct.toFixed(2)}%`} (${item.quality})`).join(' · ')}</p>
              <p>盘中风险状态：{dailyPlan.market_context.risk_tone}。缺来源时刻的数据不得作为加仓确认。</p>
            </div>}
            {dailyPlan.payload.portfolio_rationale && <p className="text-sm">组合仓位判断：{dailyPlan.payload.portfolio_rationale}</p>}
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {dailyPlan.payload.proposals.map(item => (
                <div key={item.symbol} className="rounded-lg border px-3 py-2 text-sm">
                  <div className="flex justify-between font-medium"><span>{item.symbol}</span><span>{item.action}</span></div>
                  <p className="mt-1 text-xs">建议目标仓位：{item.target_weight == null ? '待证据确认' : `${(item.target_weight * 100).toFixed(1)}%`}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{item.rationale}</p>
                  <p className="mt-1 text-xs text-muted-foreground">模型方向待证据与 Policy 核对；模型提示数量不构成授权。</p>
                </div>
              ))}
            </div>
          </>
        ) : <p className="text-sm text-muted-foreground">今日盘前计划尚未生成；08:50 后刷新查看。若任务失败，请查看下方时间槽状态。</p>}
        {workflowRuns.length > 0 && <div className="flex flex-wrap gap-2 text-xs">
          {workflowRuns.slice(0, 12).map(run => <span key={run.run_id} className="rounded-full border px-2 py-1">{run.step} · {run.status}</span>)}
        </div>}
      </section>
      <section className="card p-4 space-y-2">
        <h2 className="text-base font-semibold">今日消息投递</h2>
        <p className="text-xs text-muted-foreground">
          入队 {notifications.length} · 平台接受 {notifications.filter(item => item.delivery_status === 'SENT_ACCEPTED').length} ·
          投递未知 {notifications.filter(item => item.delivery_status === 'DELIVERY_UNKNOWN').length} ·
          失败待处理 {notifications.filter(item => item.delivery_status === 'FAILED_RETRYABLE').length}。
          平台接受不代表已读或成交。
        </p>
        {notifications.slice(0, 5).map(item => <p key={item.notification_id} className="border-t pt-2 text-xs">
          {item.title} · {item.delivery_status} · {item.reason}
        </p>)}
      </section>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(330px,1fr)]">
        <section className="card overflow-hidden">
          <div className="border-b px-4 py-3 text-sm font-medium">最近信号 · {signals.length}</div>
          {signals.length === 0 ? <p className="p-6 text-sm text-muted-foreground">暂无信号，调度运行后在此查看。</p> : (
            <div className="max-h-[70vh] overflow-auto">
              {signals.map(item => (
                <button key={item.signal_id} type="button" onClick={() => void openSignal(item.signal_id)}
                  className={`w-full border-b px-4 py-3 text-left hover:bg-muted/50 ${selected?.signal_id === item.signal_id ? 'bg-muted/60' : ''}`}>
                  <div className="flex items-center justify-between gap-3 text-sm font-medium">
                    <span>{item.symbol} · {item.action}</span><span>{item.status}</span>
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">{item.source_agent || '未知来源'} · {item.trade_date} · 持仓计划 v{item.plan_version ?? '—'} · 日计划 v{item.daily_plan_version ?? '—'}</div>
                </button>
              ))}
            </div>
          )}
        </section>
        <section className="card p-4">
          {!selected ? <p className="text-sm text-muted-foreground">选择左侧信号查看裁决和执行记录。</p> : (
            <div className="space-y-5">
              <div>
                <h2 className="text-lg font-semibold">{selected.symbol} · {selected.action}</h2>
                <p className="text-xs text-muted-foreground">状态 {selected.status} · 持仓计划 v{selected.plan_version ?? '—'} · 日计划 v{selected.daily_plan_version ?? '—'} · {selected.signal_id}</p>
                {selected.actionable && <p className="mt-2 text-sm">政策批准数量：{selected.actionable.approved_qty ?? '未指定'} · {selected.actionable.policy_version}</p>}
                {selected.actionable?.approved_qty != null && <p className="mt-1 text-xs text-muted-foreground">
                  用户已报告 {selected.executions.filter(item => item.result === 'USER_REPORTED').reduce((sum, item) => sum + item.actual_qty, 0)} 股；
                  按报告计算剩余 {Math.max(0, selected.actionable.approved_qty - selected.executions.filter(item => item.result === 'USER_REPORTED').reduce((sum, item) => sum + item.actual_qty, 0))} 股，仍需券商对账。
                </p>}
              </div>
              <div>
                <h3 className="mb-2 text-sm font-medium">政策裁决</h3>
                <div className="max-h-44 space-y-1 overflow-auto text-xs">
                  {selected.policy_decisions.map(item => <div key={item.rule_id} className="flex justify-between gap-2 border-b py-1">
                    <span>{item.rule_id}</span><span>{item.decision}{item.reason_codes.length ? ` · ${item.reason_codes.join(', ')}` : ''}</span>
                  </div>)}
                </div>
              </div>
              <div>
                <h3 className="mb-2 text-sm font-medium">实际执行</h3>
                {selected.executions.length ? selected.executions.map(item => <p key={item.execution_id} className="border-b py-2 text-xs">
                  {item.actual_action} {item.actual_qty} 股 · {item.result} · {item.reconcile_status}
                </p>) : <p className="text-xs text-muted-foreground">尚无人工执行记录。</p>}
              </div>
              <div className="space-y-2 border-t pt-4">
                <h3 className="text-sm font-medium">记录已发生的人工操作</h3>
                <p className="text-xs text-muted-foreground">这里只登记你已执行的交易，不会向券商下单。超出政策的操作会标记为计划外。</p>
                <select value={actualAction} onChange={event => setActualAction(event.target.value)} className="h-9 w-full rounded-md border bg-background px-2 text-sm">
                  {actions.map(action => <option key={action} value={action}>{action}</option>)}
                </select>
                <div className="grid grid-cols-2 gap-2">
                  <Input type="number" min="1" step="1" value={quantity} onChange={event => setQuantity(event.target.value)} placeholder="实际股数" />
                  <Input type="number" min="0" step="0.01" value={price} onChange={event => setPrice(event.target.value)} placeholder="实际价格（可选）" />
                </div>
                <Input value={notes} onChange={event => setNotes(event.target.value)} placeholder="备注（可选）" />
                <div className="flex gap-2"><Button disabled={recording} onClick={() => void recordExecution()}>{recording ? '登记中…' : '登记人工操作'}</Button><Button variant="outline" onClick={() => void ignore()}>记录忽略</Button></div>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
