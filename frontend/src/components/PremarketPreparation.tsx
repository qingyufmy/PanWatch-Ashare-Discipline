export type Preparation = {
  origin?: string
  created_at: string
  previous_cn_session: string | null
  market_summary: string
  global_summary: string
  breadth_summary: string
  execution_boundary: string
  checkpoints: string[]
  exposure: {
    current_weight: number | null
    suggested_min: number | null
    suggested_max: number | null
    basis: string
    rule: string
  }
  positions: Array<{
    symbol: string
    name: string
    stance: string
    current_weight: number | null
    conditional_target_weight: number | null
    reduce_condition: string
    add_condition: string
    invalidation: string
  }>
}

const percent = (value: number | null) => value == null ? '待核查' : `${(value * 100).toFixed(1)}%`

export function PremarketPreparation({ plan }: { plan: Preparation }) {
  return <div className="space-y-3">
    <div className="rounded-lg border p-3 space-y-2 text-sm">
      <p className="font-medium">{plan.market_summary}</p>
      <p>收盘基准：{plan.previous_cn_session || '待核实'} · 当前估算仓位 {percent(plan.exposure.current_weight)}</p>
      <p className="font-medium">条件成立后的建议区间：{percent(plan.exposure.suggested_min)}—{percent(plan.exposure.suggested_max)}</p>
      <p className="text-xs text-muted-foreground">{plan.exposure.basis}。{plan.exposure.rule}</p>
      <p>{plan.global_summary}</p>
      <p className="text-xs text-muted-foreground">{plan.breadth_summary}</p>
      {plan.origin === 'DERIVED_VIEW_OF_FROZEN_PLAN' && <p className="text-xs text-muted-foreground">根据原计划冻结证据生成的显示修订，生成于 {new Date(plan.created_at).toLocaleString('zh-CN')}；原模型输出保留。</p>}
    </div>
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
      {plan.positions.map(row => <div key={row.symbol} className="rounded-lg border p-3 space-y-2 text-sm">
        <div className="font-medium">{row.name}（{row.symbol}）</div>
        <p>{row.stance}</p>
        <p>当前 {percent(row.current_weight)} → 条件目标 {percent(row.conditional_target_weight)}</p>
        <p className="text-xs">减仓观察：{row.reduce_condition}</p>
        <p className="text-xs">加仓观察：{row.add_condition}</p>
        <p className="text-xs text-muted-foreground">{row.invalidation}</p>
      </div>)}
    </div>
    <div className="space-y-1 text-xs">{plan.checkpoints.map(item => <p key={item}>{item}</p>)}</div>
    <p className="text-xs text-muted-foreground">{plan.execution_boundary}</p>
  </div>
}
