"""Textos para la terminal y para Telegram."""

from __future__ import annotations

from bot_trader.backtest import BacktestResult
from bot_trader.db import now_utc
from bot_trader.engine import Analysis, Bot, CycleResult, describe_age
from bot_trader.risk import check_guards

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


def status_text(bot: Bot) -> str:
    """Capital, frenos y posiciones del bot, con precios de este momento."""
    s = bot.s
    candles, _ = bot.fetch_candles(s.assets)
    account = bot.broker.account({sym: bars[-1].close for sym, bars in candles.items()})
    equity, cash = bot.bot_equity(account)
    peak = max(float(bot.journal.get("pico", f"{equity}") or equity), equity)
    day_start = float(bot.journal.get("inicio_dia", f"{equity}") or equity)
    guard = check_guards(equity, peak, day_start, bot.risk.paused, s.risk)
    out = [
        f"Cuenta {ACCOUNT_LABEL[s.account]} | modo {s.mode}",
        f"Capital del bot {equity:,.2f} USD | efectivo {cash:,.2f} | máximo histórico {peak:,.2f}",
    ]
    if s.account != "papel":
        out.append(f"Capital total de la cuenta eToro {account.equity:,.2f} USD")
    out.append("Frenos: " + ("OK, puede abrir posiciones" if guard.can_open else guard.reason))
    if bot.risk.paused and bot.risk.pause_reason:
        out.append(f"Motivo de la pausa: {bot.risk.pause_reason}")
    out.append(f"Posiciones del bot ({len(account.positions)}):")
    for p in account.positions:
        stop = f"{p.stop:,.2f}" if p.stop is not None else "-"
        exits = f"stop móvil {stop}" if p.trailing else f"stop {stop}  objetivo {p.target if p.target is not None else '-'}"
        out.append(
            f"  {p.symbol:<8} {'compra' if p.is_buy else 'corto':<6} {p.amount:>10,.2f} USD  P&L {p.pnl:+,.2f}  "
            f"{exits}  desde {p.opened_at:%Y-%m-%d}"
        )
    out.append(f"P&L realizado acumulado: {bot.journal.realized_pnl():+,.2f} USD")
    return "\n".join(out)


def journal_text(bot: Bot, n: int = 20) -> str:
    """Últimas señales, órdenes y operaciones cerradas."""
    now = now_utc()
    out = ["Señales recientes:"]
    for r in bot.journal.recent_signals(n):
        prob = f" p={r['probabilidad']:.0%} E={r['expectativa']:+.2f}R" if r["probabilidad"] is not None else ""
        monto = f" {r['monto']:,.2f} USD" if r["monto"] else ""
        out.append(
            f"  #{r['id']:<4} {describe_age(r['ts'], now):<12} {r['accion']:<12} {r['simbolo']:<8} "
            f"{r['precio']:>10,.2f}{monto}{prob}  [{r['estado']}]"
        )
    out.append("Órdenes:")
    for r in bot.journal.recent_orders(n):
        out.append(
            f"  {describe_age(r['ts'], now):<12} {r['tipo']:<7} {r['simbolo']:<8} {r['estado']:<10} {r['posicion_id'] or ''}"
        )
    out.append("Operaciones cerradas:")
    for r in bot.journal.closed_positions(n):
        pnl = f"{r['pnl']:+,.2f}" if r["pnl"] is not None else "?"
        out.append(f"  {r['simbolo']:<8} {r['abierta'][:10]} -> {r['cerrada'][:10]}  P&L {pnl}  ({r['motivo_cierre']})")
    return "\n".join(out)


def cycle_report(r: CycleResult, detailed: bool = True) -> str:
    out = [
        f"== Bot trader | cuenta {ACCOUNT_LABEL.get(r.account, r.account)} | modo {r.mode} ==",
        f"Capital: {_money(r.equity)} USD   Efectivo: {_money(r.cash)} USD   Posiciones del bot: {len(r.positions)}",
    ]
    if r.guard and not r.guard.can_open:
        out.append(f"FRENO DE RIESGO: {r.guard.reason}")
    if r.regime is not None and r.regime.label != "sin referencia":
        out.append(f"Mercado: {r.regime.label} ({r.regime.detail})")
    if r.model_info:
        out.append(r.model_info.capitalize())
    out += r.notes
    if r.positions:
        out.append("")
        out.append("Posiciones abiertas:")
        for p in r.positions:
            side = "compra" if p.is_buy else "corto"
            exit_text = f"stop móvil {_money(p.stop)}" if p.trailing else f"stop {_money(p.stop)}  objetivo {_money(p.target)}"
            out.append(
                f"  {p.symbol:<8} {side:<6} invertido {_money(p.amount)}  entrada {p.entry_price:,.4f}  "
                f"actual {_money(p.current_price)}  P&L {p.pnl:+,.2f}  {exit_text}"
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
    if not r.analyses:
        return "\n".join(out)
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
    b = res.benchmark_stats
    lines = [
        f"== Backtest {', '.join(symbols)} | {res.start} a {res.end} ({res.years:.1f} años) ==",
        f"Capital inicial {res.initial:,.2f} -> final {res.final:,.2f}  ({res.total_return:+.1%})",
        "",
        "                      Bot        Comprar y mantener",
        f"  Rendimiento anual  {res.cagr:>+7.1%}     {b['cagr']:>+7.1%}",
        f"  Volatilidad anual  {res.annual_vol:>7.1%}     {b['vol']:>7.1%}",
        f"  Peor caída         {res.max_dd:>7.1%}     {b['max_dd']:>7.1%}",
        f"  Sharpe             {res.sharpe:>7.2f}     {b['sharpe']:>7.2f}",
        f"  Sortino            {res.sortino:>7.2f}     {b['sortino']:>7.2f}",
        f"  Calmar (anual/caída) {res.calmar:>5.2f}     {b.get('calmar', 0):>7.2f}",
        "",
        "Sostenibilidad:",
        f"  Meses positivos {res.positive_months:.0%}   Mayor tiempo bajo el máximo {res.longest_underwater_days} días   "
        f"Tiempo invertido {res.exposure:.0%}",
        "  Por año: " + "   ".join(f"{y} {r:+.1%}" for y, r in res.yearly),
        "",
        "Operaciones:",
        f"  {len(res.trades)} operaciones   ganadoras {res.win_rate:.0%}   factor de ganancia {pf}   "
        f"promedio {res.avg_r:+.2f}R   duración media {res.avg_days:.0f} días",
    ]
    for label, side in (("Compras", True), ("Ventas en corto", False)):
        st = res.side_stats(side)
        if st and res.side_stats(False):  # el desglose solo aporta si hubo cortos
            lines.append(
                f"  {label + ':':<17}{st['n']:>3} operaciones  ganadoras {st['win_rate']:.0%}  "
                f"P&L {st['pnl']:+,.2f}  promedio {st['avg_r']:+.2f}R"
            )
    if res.would_pause_on:
        lines.append(f"  Ojo: el {res.would_pause_on} el bot en vivo se habría pausado por drawdown.")
    by_symbol: dict[str, list] = {}
    for t in res.trades:
        by_symbol.setdefault(t.symbol, []).append(t)
    for sym, trades in sorted(by_symbol.items()):
        wins = sum(1 for t in trades if t.pnl > 0)
        lines.append(
            f"  {sym:<8} {len(trades):>3} operaciones  ganadoras {wins / len(trades):.0%}  "
            f"P&L {sum(t.pnl for t in trades):+,.2f}  promedio {sum(t.r for t in trades) / len(trades):+.2f}R"
        )
    lines += ["", *prediction_lines(res), "", _verdict(res)]
    return "\n".join(lines)


def prediction_lines(res: BacktestResult) -> list[str]:
    q = res.prediction_quality()
    if not q["n"] or q["base"] is None:
        return ["Calidad de las predicciones: sin datos suficientes."]

    def skill(key: str) -> str:
        v = q.get(f"{key}_skill")
        return "-" if v is None else f"{v:+.1%}"

    lines = [
        f"Calidad de las predicciones ({q['n']:,} días evaluados, siempre sin mirar el futuro):",
        f"  Mejora sobre la tasa base: combinada {skill('final')}  |  casos parecidos {skill('hist')}  |  "
        f"modelo estadístico {skill('model')}",
        "  (positivo = predice mejor que suponer siempre el promedio; en mercados +1 % a +5 % ya es ventaja)",
    ]
    if q.get("cortos_n"):
        lines.append(f"  Por lado: compras {skill('largos')}  |  ventas en corto {skill('cortos')}")
    if q["calibration"]:
        lines.append("  Calibración (lo que predijo vs. lo que pasó):")
        for lo, hi, n, pred, real in q["calibration"]:
            hi_txt = "100%" if hi > 1 else f"{hi:.0%}"
            lines.append(f"    predijo {lo:.0%}-{hi_txt}: {n:>5} casos, promedio predicho {pred:.0%}, ganó realmente {real:.0%}")
    return lines


def robustness_report(results: list) -> str:
    lines = ["== Robustez: ¿sigue funcionando si cambian un poco los parámetros? ==",
             f"  {'variante':<26}{'anual':>8}{'caída':>8}{'Calmar':>8}{'Sharpe':>8}{'operac.':>9}"]
    calmars = []
    for label, r in results:
        if r.start is None:
            lines.append(f"  {label:<26} sin datos")
            continue
        calmars.append(r.calmar)
        lines.append(f"  {label:<26}{r.cagr:>+8.1%}{r.max_dd:>8.1%}{r.calmar:>8.2f}{r.sharpe:>8.2f}{len(r.trades):>9}")
    if calmars:
        bad = sum(1 for c in calmars if c <= 0)
        lines.append("")
        if bad == 0:
            lines.append("Veredicto: robusta. Todas las variantes ganan; la ventaja no depende de números exactos.")
        elif bad <= len(calmars) // 3:
            lines.append(f"Veredicto: aceptable. {bad} de {len(calmars)} variantes pierden; revisa cuáles.")
        else:
            lines.append(f"Veredicto: FRÁGIL. {bad} de {len(calmars)} variantes pierden: probablemente está "
                         "sobreajustada al pasado. No la uses con dinero real así.")
    return "\n".join(lines)


def _verdict(res: BacktestResult) -> str:
    return _main_verdict(res) + _shorts_note(res)


def _shorts_note(res: BacktestResult) -> str:
    """Si hubo ventas en corto, dice sin rodeos si sumaron o restaron."""
    shorts = res.side_stats(False)
    if not shorts:
        return ""
    if shorts["pnl"] < 0:
        return (f"\nOjo: las ventas en corto RESTARON en este periodo ({shorts['n']} operaciones, P&L "
                f"{shorts['pnl']:+,.2f}). Compara con `permitir_cortos = false` antes de dejarlas encendidas.")
    if shorts["n"] < 20:
        return (f"\nLas ventas en corto sumaron ({shorts['pnl']:+,.2f}), pero con solo {shorts['n']} operaciones "
                "no alcanza para concluir que tienen ventaja.")
    return f"\nLas ventas en corto sumaron: {shorts['n']} operaciones, P&L {shorts['pnl']:+,.2f}."


def _main_verdict(res: BacktestResult) -> str:
    b = res.benchmark_stats
    if len(res.trades) < 20:
        return ("Veredicto: muy pocas operaciones para sacar conclusiones. Agrega activos o más historia "
                "antes de confiar en estos números.")
    if res.avg_r <= 0 or res.profit_factor < 1:
        return "Veredicto: la estrategia PIERDE en este periodo. No la uses con dinero real con estos parámetros."
    losing_years = sum(1 for _, r in res.yearly if r < 0)
    consistency = f"{len(res.yearly) - losing_years} de {len(res.yearly)} años positivos"
    better_risk_adjusted = res.calmar >= b.get("calmar", 0) and res.sharpe >= b["sharpe"]
    consistent = losing_years * 3 <= len(res.yearly)  # a lo más 1 de cada 3 años en negativo
    if better_risk_adjusted and consistent:
        return (f"Veredicto: SOSTENIBLE en este periodo: mejor rendimiento por unidad de riesgo que comprar y "
                f"mantener (Calmar {res.calmar:.2f} vs {b.get('calmar', 0):.2f}), {consistency}. Siguiente paso: "
                "`--robustez`, luego semanas en DEMO comparando con estos números.")
    if better_risk_adjusted:
        return (f"Veredicto: rinde más por unidad de riesgo que comprar y mantener (Calmar {res.calmar:.2f} vs "
                f"{b.get('calmar', 0):.2f}), pero de forma irregular: solo {consistency}. Todavía no es una "
                "ganancia constante; mira `--robustez` y más historia antes de confiar.")
    if res.max_dd < b["max_dd"]:
        return (f"Veredicto: gana ({res.cagr:+.1%} anual) con caídas más chicas ({res.max_dd:.1%} vs "
                f"{b['max_dd']:.1%}), pero rinde menos por unidad de riesgo que comprar y mantener. {consistency}. "
                "Decide si ese intercambio te conviene.")
    return ("Veredicto: gana, pero comprar y mantener fue mejor y con caídas parecidas o menores. "
            "Para estos activos, invertir y esperar fue la opción más sostenible.")
