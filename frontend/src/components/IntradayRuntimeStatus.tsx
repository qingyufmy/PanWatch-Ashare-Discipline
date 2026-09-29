import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'

interface Runtime {
  performance_notice?: string | null
  batch: { status: string; reason?: string; coverage: number; blocked_proposal_count?: number; started_at: string; finished_at?: string } | null
  paper_scan: { finished_at: string; status: string; no_signal_reason?: string; skipped_reasons?: Record<string, number> } | null
  paper_fills_today: number
}
const reasons: Record<string, string> = {
  PLAN_CHANGED_OR_MISSING: '持仓计划已变更或缺失，等待新分析', OBSERVATION_TRIGGER_NO_LONGER_VALID: '最新价格已不满足原观察价触发条件',
  PROPOSAL_FACT_OR_SCOPE_INVALID: '模型价格事实或账户范围未通过，禁止成交',
  NO_CURRENT_PORTFOLIO_SIGNAL: '暂无有效的组合操作信号', LATEST_ACTION_HAS_NO_TRADE: '最新建议不要求交易',
  ALREADY_FILLED: '该信号已经模拟成交', QUOTE_STALE_OR_NO_VOLUME: '行情过期或缺少成交量',
  PRICE_OR_QUANTITY_MISSING: '缺少有效价格或明确数量', MODEL_OR_CONFIDENCE_UNVERIFIED: '模型依据或置信度未通过',
  AUCTION_EVIDENCE_MISSING: '缺少当日竞价依据', POLICY_BLOCKED_OR_INCOMPLETE: '政策阻断或检查不完整',
  ADD_MARKET_CASH_WEIGHT_OR_RISK_LIMIT: '加仓条件、资金或仓位限制未满足', SELL_QUANTITY_LOT_OR_T_PLUS_ONE: '卖出数量、整手或T+1限制',
  LATEST_SIGNAL_NOT_ELIGIBLE: '最新信号不具备模拟资格', NO_OPEN_PAPER_POSITION: '模拟盘没有该持仓',
  LATEST_SIGNAL_EXPIRED: '最新信号已过期', outside_cn_continuous_session: '非连续交易时段',
  calendar_unverified_or_closed: '休市或交易日历未核实', quote_error: '行情读取失败', scan_error: '扫描发生异常',
}
const clock = (s: string) => new Date(s).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
export default function IntradayRuntimeStatus() {
  const [data, setData] = useState<Runtime | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let active = true
    const load = () => fetchAPI<Runtime>('/portfolio-workflow/runtime').then(d => { if (active) { setData(d); setFailed(false) } }).catch(() => { if (active) setFailed(true) })
    load(); const timer = window.setInterval(load, 30000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])
  if (!data) return failed ? <div className="card p-3 text-sm">盘中运行状态暂不可用</div> : null
  const bad = data.batch?.reason?.includes('FAILED') || data.batch?.reason?.includes('DISCARDED') || data.batch?.status === 'FAILED' || (data.batch?.status !== 'RUNNING' && data.batch?.coverage === 0)
  return <div className="card p-3 text-xs space-y-2">
    {data.performance_notice && <div className="text-amber-700">{data.performance_notice}</div>}
    <div className="font-medium">盘中批量分析与模拟执行{failed ? '（连接中断，以下为缓存状态）' : ''}</div>
    <div className={bad ? 'text-rose-600' : ''}>最近批次：{data.batch ? `${clock(data.batch.started_at)} · ${data.batch.status === 'RUNNING' ? '分析中' : bad ? '分析失败，本批次未生成有效操作信号' : `已完成，覆盖 ${data.batch.coverage} 只${data.batch.blocked_proposal_count ? `；${data.batch.blocked_proposal_count} 条提案已拦截` : ''}`}` : '尚未运行'}</div>
    <div>模拟盘今日成交 {data.paper_fills_today} 笔；最近扫描：{data.paper_scan ? `${clock(data.paper_scan.finished_at)} · ${data.paper_scan.status === 'ok' ? '已完成' : reasons[data.paper_scan.status] || data.paper_scan.status}` : '等待首次扫描记录'}。模拟成交与实盘独立。</div>
    {data.paper_scan?.no_signal_reason && <div>{reasons[data.paper_scan.no_signal_reason] || data.paper_scan.no_signal_reason}</div>}
    {Object.entries(data.paper_scan?.skipped_reasons || {}).map(([key, count]) => <span key={key} className="inline-block mr-3 text-muted-foreground">{reasons[key] || key}：{count}</span>)}
  </div>
}
