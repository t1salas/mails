"""Textos para la terminal y para Telegram."""

from __future__ import annotations

from bot_trader.backtest import BacktestResult
from bot_trader.engine import Analysis, CycleResult

ACTION_LABEL = {
    "COMPRAR": "COMPRAR",
    "VENDER_CORTO": "VENDER EN CORTO",
    "CERRAR": "CERRAR posición",
    "MANTENER": "mantener",
    "ESPERAR": "esperar",
}
ACCOUNT_LABEL = {"papel": "PAPEL (simulada)", "demo": "eToro DEMO (dinero virtual)", "real": "eToro REAL (dinero real)"}


def _money(v: float | None) -> str:
    return "-" if v is None else f"{v:,.2f}"


def analysis_line(a: Analysis) -> str:
    if a.error:
        return f"{a.asset.symbol:<8} ERROR: {a.error}"
    s = a.signal
    prob = f"p={s.probability.p_win:.0%} E={s.probability.expectancy_r:+.2f}R n={s.probability.n}" if s.probability else ""
    news = f"noticias {s.news_score:+.2f}" if s.news and s.news.method != "sin_noticias" else "sin noticias"
    return (
        f"{s.symbol:<8} {s.price:>11,.2f}  {ACTION_LABEL.get(s.action, s.action):<16} "
        f"tendencia {s.trend:<15} técnico {s.tech_score:+.2f}  {news}  {prob}".rstrip()
    )


def analysis_detail(a: Analysis) -> list[str]:
    if a.error or a.signal is None:
        return []
    s = a.signal
    lines = [f"  {s.symbol}: {ACTION_LABEL.get(s.action, s.action)}"]
    lines += [f"    - {r}" for r in s.reasons]
    if s.news and s.news.headlines and s.news.method != "sin_noticias":
        lines.append(f"    titulares ({s.news.method}):")
        lines += [f"      · {h.title}" + (f" ({h.source})" if h.source else "") for h in s.news.headlines[:3]]
    return lines


def cycle_report(r: CycleResult, detailed: bool = True) -> str:
    out = [
        f"== Bot trader | cuenta {ACCOUNT_LABEL.get(r.account, r.account)} | modo {r.mode} ==",
        f"Capital: {_money(r.equity)} USD   Efectivo: {_money(r.cash)} USD   Posiciones del bot: {len(r.positions)}",
    ]
    if r.guard and not r.guard.can_open:
        out.append(f"FRENO DE RIESGO: {r.guard.reason}")
    out += r.notes
    if r.positions:
        out.append("")
        out.append("Posiciones abiertas:")
        for p in r.positions:
            side = "compra" if p.is_buy else "corto"
            out.append(
                f"  {p.symbol:<8} {side:<6} invertido {_money(p.amount)}  entrada {p.entry_price:,.4f}  "
                f"actual {_money(p.current_price)}  P&L {p.pnl:+,.2f}  stop {_money(p.stop)}  objetivo {_money(p.target)}"
            )
    if r.exits:
        out.append("")
        out.append("Cierres:")
        for e in r.exits:
            pnl = f"{e.pnl:+,.2f} USD" if e.pnl is not None else "P&L por confirmar en eToro"
            out.append(f"  {e.position.symbol}: {e.reason} -> {pnl}")
    if r.actions:
        out.append("")
        out.append("Operaciones:")
        for a in r.actions:
            amount = f" por {a.amount:,.2f} USD" if a.amount else ""
            tag = {"sugerida": "SUGERENCIA", "ejecutada": "EJECUTADA", "pendiente": "PENDIENTE",
                   "bloqueada": "bloqueada", "fallida": "FALLÓ"}[a.kind]
            ident = f" #{a.signal_id}" if a.signal_id and a.kind == "sugerida" else ""
            out.append(f"  [{tag}{ident}] {ACTION_LABEL.get(a.action, a.action)} {a.symbol}{amount}: {a.detail}")
        if any(a.kind == "sugerida" and a.signal_id for a in r.actions):
            out.append("  Para ejecutar una sugerencia:  python -m bot_trader aprobar <número>")
    out.append("")
    out.append("Análisis:")
    out += ["  " + analysis_line(a) for a in r.analyses]
    if detailed:
        interesting = [a for a in r.analyses if a.signal and a.signal.action != "ESPERAR"] or r.analyses
        out.append("")
        out.append("Razones:")
        for a in interesting:
            out += analysis_detail(a)
    return "\n".join(out)


def backtest_report(res: BacktestResult, symbols: list[str]) -> str:
    if res.start is None:
        return "No hay datos suficientes para el backtest (hacen falta ~400 velas por activo)."
    pf = "∞" if res.profit_factor == float("inf") else f"{res.profit_factor:.2f}"
    lines = [
        f"== Backtest {', '.join(symbols)} | {res.start} a {res.end} ({res.years:.1f} años) ==",
        f"Capital inicial {res.initial:,.2f} -> final {res.final:,.2f}",
        f"Rendimiento total {res.total_return:+.1%}   anual (CAGR) {res.cagr:+.1%}",
        f"Peor caída (drawdown) {res.max_dd:.1%}   Sharpe {res.sharpe:.2f}   Tiempo invertido {res.exposure:.0%}",
        f"Operaciones {len(res.trades)}   Ganadoras {res.win_rate:.0%}   Factor de ganancia {pf}   Promedio {res.avg_r:+.2f}R",
        f"Comprar y mantener (mismos activos, partes iguales): {res.benchmark_return:+.1%}, peor caída {res.benchmark_max_dd:.1%}",
    ]
    if res.would_pause_on:
        lines.append(f"Ojo: el {res.would_pause_on} el bot en vivo se habría pausado por drawdown.")
    by_symbol: dict[str, list] = {}
    for t in res.trades:
        by_symbol.setdefault(t.symbol, []).append(t)
    if by_symbol:
        lines.append("")
        lines.append("Por activo:")
        for sym, trades in sorted(by_symbol.items()):
            wins = sum(1 for t in trades if t.pnl > 0)
            lines.append(
                f"  {sym:<8} {len(trades):>3} operaciones  ganadoras {wins / len(trades):.0%}  "
                f"P&L {sum(t.pnl for t in trades):+,.2f}  promedio {sum(t.r for t in trades) / len(trades):+.2f}R"
            )
    lines.append("")
    lines.append(_verdict(res))
    return "\n".join(lines)


def _verdict(res: BacktestResult) -> str:
    if len(res.trades) < 20:
        return ("Veredicto: muy pocas operaciones para sacar conclusiones. Agrega activos o más historia "
                "antes de confiar en estos números.")
    if res.avg_r <= 0 or res.profit_factor < 1:
        return "Veredicto: la estrategia PIERDE en este periodo. No la uses con dinero real con estos parámetros."
    if res.total_return < res.benchmark_return:
        if res.max_dd >= res.benchmark_max_dd:
            return ("Veredicto: gana, pero menos que comprar y mantener y sin caer menos. "
                    "Para estos activos, invertir y esperar fue mejor.")
        return (f"Veredicto: gana ({res.total_return:+.1%}) pero menos que comprar y mantener "
                f"({res.benchmark_return:+.1%}); a cambio cae menos ({res.max_dd:.1%} contra "
                f"{res.benchmark_max_dd:.1%}) y no está invertido todo el tiempo. Decide si ese "
                "intercambio te conviene antes de usarla.")
    return ("Veredicto: le ganó a comprar y mantener en este periodo. Siguiente paso: correrla varias "
            "semanas en DEMO y comparar con estos números antes de pasar a real con montos chicos.")
