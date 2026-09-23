"""Deterministic, source-backed portfolio text. Model prose never supplies prices."""

from __future__ import annotations

import math

ACTION_LABELS = {"EXIT": "清仓离场", "REDUCE": "减仓", "ADD": "加仓", "HOLD": "持有观察"}
ACTION_ORDER = ("EXIT", "REDUCE", "ADD", "HOLD")


def _price(value) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:.2f}元" if math.isfinite(number) and number > 0 else None


def render_premarket_plan(proposals, signals, positions) -> tuple[str, str]:
    """Show directions, never model-suggested shares or unverified prices."""
    by_symbol = {p["symbol"]: p for p in positions}
    by_signal = {s.symbol: s for s in signals}
    grouped = {action: [] for action in ACTION_ORDER}
    for proposal in proposals:
        if proposal.symbol not in by_symbol or proposal.action == "OPEN":
            continue
        signal = by_signal.get(proposal.symbol)
        if signal is None:
            continue
        status = "已批准，待人工执行" if signal.status == "APPROVED" else "方向待复核，不得按模型数量交易"
        grouped[proposal.action].append(f"{by_symbol[proposal.symbol]['name']}（{proposal.symbol}）｜{status}")
    present = [action for action in ACTION_ORDER if grouped[action]]
    title = f"{'/'.join(ACTION_LABELS[a] for a in present) or '持仓复核'}｜盘前计划"
    lines = ["盘前研究计划；盘前行情与券商持仓尚未核实。",
             "数量：未展示模型提示股数；只有独立Policy批准后才可作为人工计划。",
             "技术价位：未取得已收线且来源可核对的盘前证据，暂不填数字。"]
    for action in present:
        if action == "HOLD":
            lines.append(f"持有观察（{len(grouped[action])}只）：" + "、".join(
                row.split("（")[0] for row in grouped[action]))
        else:
            lines.append(f"{ACTION_LABELS[action]}方向（{len(grouped[action])}只）：")
            lines.extend(grouped[action])
    lines.append("执行状态：未登记执行；请在认证页面核对实时行情、可卖数量和当前计划。")
    return title, "\n".join(lines)


def render_position_decision(*, symbol: str, name: str, action: str,
                             decision_status: str, execution_status: str,
                             level: dict | None, approved_qty: int | None,
                             total_qty: int | None, sellable_qty: int | None,
                             truth_trusted: bool) -> tuple[str, str]:
    if action not in ACTION_LABELS:
        raise ValueError("held_position_action_required")
    if level is not None and level.get("symbol") not in {symbol, f"sh{symbol}", f"sz{symbol}", f"bj{symbol}"}:
        raise ValueError("level_symbol_mismatch")
    title = f"{ACTION_LABELS[action]}｜{name}（{symbol}）"
    authorized = decision_status == "APPROVED" and truth_trusted
    sell_quantity_ok = (authorized and approved_qty is not None and approved_qty > 0
                   and total_qty is not None and sellable_qty is not None
                   and approved_qty <= sellable_qty <= total_qty)
    add_quantity_ok = (authorized and approved_qty is not None and approved_qty > 0
                       and total_qty is not None)
    lines = [f"决策：{'Policy已批准' if authorized else '方向待复核'}；执行：{execution_status}。"]
    if sell_quantity_ok and action in {"REDUCE", "EXIT"}:
        lines.append(f"人工计划：{total_qty}股 → {total_qty - approved_qty}股；本次最多卖{approved_qty}股，可卖{sellable_qty}股。")
    elif add_quantity_ok and action == "ADD":
        lines.append(f"人工计划：{total_qty}股 → {total_qty + approved_qty}股；本次最多加{approved_qty}股，仍待人工确认价格。")
    elif action in {"ADD", "REDUCE", "EXIT"}:
        lines.append("数量：待账户、可卖数量和风险预算核对；当前未授权具体股数。")
    if level is None:
        lines.append("行情与价位：缺少同一冻结快照，数字价位不展示。")
    else:
        current = _price(level.get("current_price"))
        asof = level.get("market_data_asof")
        if current and asof:
            lines.append(f"现价：{current}｜数据时刻：{asof}。")
        facts = []
        broken = _price(level.get("broken_prior_support"))
        if broken:
            facts.append(f"已失守上一版冻结支撑{broken}")
        vwap = _price(level.get("vwap"))
        if vwap and current:
            facts.append(f"当日VWAP {vwap}，现价{'低于' if float(level['current_price']) < float(level['vwap']) else '不低于'}VWAP")
        if facts:
            lines.append("数字事实：" + "；".join(facts[:3]) + "。")
        for key, label in (("S1", "近端支撑"), ("S2", "次级支撑"),
                           ("R1", "近端压力"), ("R2", "次级压力")):
            item = (level.get("levels") or {}).get(key)
            value = _price(item.get("value")) if isinstance(item, dict) and item.get("quality_status") == "CONFIRMED" else None
            if value:
                lines.append(f"{label}：{value}（{item['timeframe']}已收线，{item['source_vendor']}）。")
        hard_exit = _price(level.get("approved_hard_exit"))
        if hard_exit:
            lines.append(f"已批准硬退出线：{hard_exit}；触价仍需人工核对。")
    lines.append("当前操作：人工复核；消息送达不代表已读或成交。")
    return title, "\n".join(lines)


def render_daily_review(*, trade_date: str, decisions, notifications,
                        executions, truth_status: str, next_day_count: int) -> tuple[str, str]:
    current = [d for d in decisions if d.current]
    counts = {action: sum(d.action == action for d in current) for action in ACTION_ORDER}
    present = [action for action in ACTION_ORDER if counts[action]]
    title = f"{'/'.join(ACTION_LABELS[a] for a in present) or '持仓复核'}｜收盘复盘"
    delivery = {status: sum(n.delivery_status == status for n in notifications)
                for status in ("SENT_ACCEPTED", "DELIVERY_UNKNOWN", "FAILED_RETRYABLE", "PENDING")}
    lines = [f"交易日：{trade_date}。持仓真相：{truth_status}。",
             "建议方向：" + "；".join(f"{ACTION_LABELS[a]}{counts[a]}只" for a in ACTION_ORDER) + "。",
             f"决策变更：{len(decisions)}条；应发入队：{len(notifications)}条。",
             f"平台接受：{delivery['SENT_ACCEPTED']}条；投递未知：{delivery['DELIVERY_UNKNOWN']}条；"
             f"失败待处理：{delivery['FAILED_RETRYABLE']}条；待发：{delivery['PENDING']}条。",
             f"用户报告执行：{len(executions)}条；券商核对完成："
             f"{sum(e.reconcile_status == 'RECONCILED' for e in executions)}条。",
             f"次日待复核事项：{next_day_count}条。",
             "平台接受不代表已读；用户登记不代表券商已成交。技术价位请以关联的冻结证据及当时有效性为准。"]
    return title, "\n".join(lines)
