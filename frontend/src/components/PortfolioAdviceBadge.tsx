import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'

export interface PortfolioAdvice {
  symbol: string
  action: string
  decision_id: number | null
  decision_status: string
  created_at: string
  expires_at: string
  source: string
  reason?: string
}

interface DecisionHistory {
  decision_id: number
  trade_date: string
  symbol: string
  revision: number
  action: string
  decision_status: string
  created_at: string
  expires_at: string
  rationale: string | null
  target_weight: number | null
  qty_hint: number | null
  source_agent: string | null
  current: boolean
}

interface LegacySuggestion {
  id: number
  action_label: string
  action: string
  reason: string
  agent_label: string
  created_at: string
  is_expired: boolean
}

const LABELS: Record<string, string> = {
  HOLD: '持有', ADD: '加仓', REDUCE: '减仓', EXIT: '退出', RISK_REVIEW: '风险复核',
}

function formatTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

export function PortfolioAdviceBadge({ advice, stockName, symbol, unavailable = false }: {
  advice: PortfolioAdvice | null
  stockName: string
  symbol: string
  unavailable?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [history, setHistory] = useState<DecisionHistory[]>([])
  const [legacyHistory, setLegacyHistory] = useState<LegacySuggestion[]>([])
  const [historyError, setHistoryError] = useState(false)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    let active = true
    setLoading(true)
    setHistoryError(false)
    Promise.all([
      fetchAPI<DecisionHistory[]>(`/portfolio-workflow/decisions?symbol=${encodeURIComponent(symbol)}&current_only=false&limit=30`),
      fetchAPI<LegacySuggestion[]>(`/suggestions/${encodeURIComponent(symbol)}?market=CN&include_expired=true&limit=10`),
    ])
      .then(([rows, legacy]) => { if (active) { setHistory(rows); setLegacyHistory(legacy) } })
      .catch(() => { if (active) setHistoryError(true) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [open, symbol, advice?.decision_id])

  const active = advice && new Date(advice.expires_at).getTime() > Date.now() ? advice : null
  const label = unavailable ? '建议不可用' : active ? (LABELS[active.action] || '待更新') : '待更新'
  const tone = active?.action === 'RISK_REVIEW' || active?.action === 'EXIT' ? 'text-rose-700 border-rose-300 bg-rose-50'
    : active?.action === 'REDUCE' ? 'text-amber-700 border-amber-300 bg-amber-50'
    : active?.action === 'ADD' ? 'text-emerald-700 border-emerald-300 bg-emerald-50'
    : 'text-muted-foreground border-border bg-background'

  return <>
    <button type="button" title="查看当前建议与历史" onClick={event => { event.stopPropagation(); setOpen(true) }}
      className={`rounded-md border px-2 py-0.5 text-[11px] font-medium ${tone}`}>
      {active && active.action !== 'RISK_REVIEW' ? `建议${label}` : label}
    </button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-lg max-h-[80vh] overflow-y-auto" onClick={event => event.stopPropagation()}>
        <DialogHeader><DialogTitle>{stockName}（{symbol}）· 持仓建议</DialogTitle></DialogHeader>
        <div className="space-y-3 text-sm">
          <div className="rounded-md border border-border p-3">
            <div className="font-medium">当前：{active ? label : unavailable ? '建议不可用' : '待更新'}</div>
            {active ? <>
              <div className="mt-1 text-xs text-muted-foreground">{formatTime(active.created_at)} 更新 · {active.source === 'hard_risk' ? '分钟风险监测' : '持仓工作流'} · {active.decision_status === 'APPROVED' ? '策略门禁通过' : '待人工确认'}</div>
              <div className="mt-2 text-xs">{active.reason || (active.action === 'RISK_REVIEW' ? '请人工复核' : '仅为建议，实际交易由你确认执行')}</div>
            </> : <div className="mt-1 text-xs text-muted-foreground">当日没有有效建议；过期建议仅在下方历史中查看。</div>}
          </div>
          <div className="font-medium">历史建议</div>
          {loading && <div className="text-xs text-muted-foreground">加载中…</div>}
          {historyError && <div className="text-xs text-rose-600">历史记录加载失败，请稍后重试。</div>}
          {!loading && !historyError && history.length === 0 && legacyHistory.length === 0 && <div className="text-xs text-muted-foreground">暂无建议记录。</div>}
          {!loading && !historyError && history.map(row => <div key={row.decision_id} className="rounded-md border border-border p-3 text-xs">
            <div className="font-medium">{LABELS[row.action] || row.action} · {formatTime(row.created_at)} · 第 {row.revision} 版</div>
            <div className="mt-1 text-muted-foreground">{row.source_agent || '持仓工作流'} · {row.decision_status === 'APPROVED' ? '策略门禁通过' : '待人工确认'} · {new Date(row.expires_at).getTime() <= Date.now() ? '已过期' : row.current ? '当前版本' : '已被替代'}</div>
            {row.rationale && <div className="mt-2 whitespace-pre-wrap">{row.rationale}</div>}
            {row.target_weight != null && <div className="mt-1 text-muted-foreground">目标仓位：{(row.target_weight * 100).toFixed(1)}%</div>}
          </div>)}
          {!loading && !historyError && legacyHistory.length > 0 && <div className="text-xs font-medium text-muted-foreground">旧 Agent 建议（仅供追溯）</div>}
          {!loading && !historyError && legacyHistory.map(row => <div key={`legacy-${row.id}`} className="rounded-md border border-border p-3 text-xs">
            <div className="font-medium">{row.action_label || row.action} · {formatTime(row.created_at)}</div>
            <div className="mt-1 text-muted-foreground">{row.agent_label || '旧 Agent'} · {row.is_expired ? '已过期' : '不作为当前持仓决议'}</div>
            {row.reason && <div className="mt-2 whitespace-pre-wrap">{row.reason}</div>}
          </div>)}
        </div>
      </DialogContent>
    </Dialog>
  </>
}
