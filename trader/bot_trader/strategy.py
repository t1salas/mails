"""Estrategia: junta tendencia, probabilidad, régimen de mercado y noticias en una decisión explicada.

Cómo piensa (lado comprador). La venta en corto (`permitir_cortos`) es el espejo: entra cuando
la tendencia es bajista, y nunca va en corto contra un mercado alcista si el activo se mueve
con el mercado.

ENTRAR solo si TODO esto se cumple:
  1. Puntaje combinado (técnico + noticias) por encima de `umbral_entrada`.
  2. La probabilidad estimada de ganar supera `probabilidad_minima`.
  3. La ganancia esperada (en R, ya con costos) supera `expectativa_minima`.
  4. Hay suficientes casos parecidos (`muestras_minimas`) para creerle a la estadística.
  5. Los dos modelos (casos parecidos y regresión logística) no se contradicen.
  6. El RSI no está en sobrecompra (no perseguir el precio).
  7. El mercado no está en crisis (si el activo se mueve con el mercado).
  8. No hay un evento de riesgo con noticias negativas (las noticias pueden vetar).

SALIR:
  - por el stop, que en el estilo `seguimiento` sube detrás del precio y deja correr las ganancias;
  - si el puntaje combinado cae bajo `umbral_salida` (la tendencia se rompió);
  - si aparece un evento de riesgo con noticias claramente negativas.
"""

from __future__ import annotations

from bot_trader.analysis.probability import trade_levels
from bot_trader.analysis.trend import Features, describe, trend_label
from bot_trader.config import RiskParams, StrategyParams
from bot_trader.models import NewsView, Position, Probability, Regime, Signal

BAD_NEWS_EXIT = -0.5


def _probability_reasons(prob: Probability, trailing: bool) -> list[str]:
    parts = [f"en {prob.n} casos parecidos ganó el {prob.p_hist:.0%}"]
    if prob.p_model is not None:
        parts.append(f"el modelo estadístico dice {prob.p_model:.0%}")
    if prob.calibrated:
        parts.append(f"ajustado por su historial real de aciertos: probabilidad {prob.p_win:.0%}")
    else:
        parts.append(f"probabilidad {prob.p_win:.0%}")
    out = ["; ".join(parts)]
    style = "con stop móvil" if trailing else "con stop y objetivo"
    out.append(
        f"{style}: cuando gana, gana {prob.avg_win_r:.2f}R en promedio; cuando pierde, pierde {prob.avg_loss_r:.2f}R "
        f"-> ganancia esperada {prob.expectancy_r:+.2f}R por operación, ya con costos"
    )
    return out


def evaluate(
    symbol: str,
    feats: Features,
    t: int,
    prob_long: Probability | None,
    prob_short: Probability | None,
    news: NewsView | None,
    position: Position | None,
    strategy: StrategyParams,
    risk: RiskParams,
    regime: Regime | None = None,
) -> Signal:
    if strategy.kind == "nucleo":
        return evaluate_core(symbol, feats, t, news, position, strategy)
    price = feats.close[t]
    tech = feats.score[t]
    atr = feats.atr[t]
    rsi = feats.rsi[t]
    trailing = strategy.exit_style == "seguimiento"
    if tech is None or atr is None or rsi is None or atr <= 0:
        return Signal(
            symbol=symbol,
            action="MANTENER" if position else "ESPERAR",
            price=price,
            is_buy=position.is_buy if position else True,
            reasons=[f"datos insuficientes: hacen falta al menos ~{strategy.slow_sma + 30} velas"],
            news=news,
            regime=regime,
        )

    news_score = news.score if news else 0.0
    w = strategy.news_weight if news and news.method != "sin_noticias" else 0.0
    combined = (1 - w) * tech + w * news_score
    sig = Signal(
        symbol=symbol,
        action="ESPERAR",
        price=price,
        tech_score=round(tech, 3),
        news_score=round(news_score, 3),
        combined=round(combined, 3),
        trend=trend_label(tech),
        rsi=round(rsi, 1),
        atr=atr,
        news=news,
        regime=regime,
        trailing=trailing,
    )
    context = describe(feats, t)
    if regime is not None and regime.label != "sin referencia":
        context.append(
            f"mercado {regime.label} ({regime.detail})" + ("" if regime.applies else "; este activo va por su lado")
        )
    bad_event = bool(news and news.risk_event and news.score < 0)

    # --- Hay posición abierta: ¿mantener o cerrar? ---------------------------------------
    if position is not None:
        sig.is_buy = position.is_buy
        sig.stop, sig.target = position.stop, position.target
        sig.trailing = position.trailing
        direction = 1 if position.is_buy else -1
        aligned = combined * direction
        reasons = []
        if aligned <= strategy.exit_score:
            reasons.append(
                f"la tendencia se volteó en contra (puntaje {combined:+.2f}, umbral de salida {strategy.exit_score:+.2f})"
            )
        if news and news.risk_event and news.score * direction <= BAD_NEWS_EXIT:
            reasons.append(f"evento de riesgo con noticias en contra ({news.score:+.2f}): {news.summary}")
        if reasons:
            sig.action = "CERRAR"
            sig.reasons = reasons + context
        else:
            sig.action = "MANTENER"
            sig.reasons = [f"la tesis sigue en pie (puntaje {combined:+.2f})"] + context
        return sig

    # --- Sin posición: ¿hay una entrada con ventaja? -------------------------------------
    candidates = [(True, prob_long)]
    if risk.allow_short:
        candidates.append((False, prob_short))
    for is_buy, prob in candidates:
        direction = 1 if is_buy else -1
        if combined * direction < strategy.entry_score:
            continue
        sig.is_buy = is_buy
        sig.probability = prob
        sig.stop, sig.target = trade_levels(price, atr, is_buy, strategy)
        blockers = []
        if not risk.use_probability:
            pass  # modo "sin filtro de probabilidad": manda la tendencia; la probabilidad solo se informa
        elif prob is None:
            blockers.append("sin historia suficiente para estimar probabilidades")
        else:
            if prob.p_win < risk.min_probability:
                blockers.append(f"probabilidad {prob.p_win:.0%} < mínimo {risk.min_probability:.0%}")
            if prob.expectancy_r < risk.min_expectancy_r:
                blockers.append(f"ganancia esperada {prob.expectancy_r:+.2f}R < mínimo {risk.min_expectancy_r:+.2f}R")
            if prob.n < risk.min_samples:
                blockers.append(f"solo {prob.n} casos parecidos (< {risk.min_samples})")
            if prob.disagreement > strategy.max_disagreement:
                blockers.append(
                    f"los modelos no coinciden (casos parecidos {prob.p_hist:.0%} vs. estadístico {prob.p_model:.0%}): "
                    "sin confianza no se entra"
                )
        if is_buy and rsi >= strategy.rsi_overbought:
            blockers.append(f"RSI {rsi:.0f} en sobrecompra: mejor esperar un retroceso")
        if not is_buy and rsi <= strategy.rsi_oversold:
            blockers.append(f"RSI {rsi:.0f} en sobreventa: mejor esperar un rebote")
        if is_buy and regime is not None and regime.applies and regime.label == "crisis":
            blockers.append(f"mercado en crisis ({regime.detail}): se protege el capital")
        if not is_buy and regime is not None and regime.applies and regime.label == "alcista":
            blockers.append(f"mercado alcista ({regime.detail}): no se vende en corto contra el mercado")
        if (is_buy and bad_event) or (not is_buy and news and news.risk_event and news.score > 0):
            blockers.append(f"evento de riesgo en las noticias: {news.summary}")
        side = "compra" if is_buy else "venta en corto"
        if blockers:
            sig.action = "ESPERAR"
            sig.reasons = [f"hay señal de {side} (puntaje {combined:+.2f}) pero:"] + blockers + context
            sig.stop = sig.target = None
            return sig
        sig.action = "COMPRAR" if is_buy else "VENDER_CORTO"
        exit_text = (
            f"stop móvil a {strategy.stop_atr:g} ATR (empieza en {sig.stop:.2f} y sigue al precio)"
            if trailing
            else f"stop {sig.stop:.2f} ({strategy.stop_atr:g} ATR) y objetivo {sig.target:.2f} ({strategy.target_atr:g} ATR)"
        )
        sig.reasons = (
            [f"tendencia {sig.trend} (técnico {tech:+.2f}, noticias {news_score:+.2f}, combinado {combined:+.2f})"]
            + (_probability_reasons(prob, trailing) if prob is not None else [])
            + [exit_text]
            + context
        )
        if news and news.summary and news.method != "sin_noticias":
            sig.reasons.append(f"noticias: {news.summary}")
        return sig

    sig.reasons = [f"sin ventaja clara: puntaje {combined:+.2f} (entrada desde {strategy.entry_score:+.2f})"] + context
    return sig


def evaluate_core(
    symbol: str,
    feats: Features,
    t: int,
    news: NewsView | None,
    position: Position | None,
    strategy: StrategyParams,
) -> Signal:
    """Núcleo: estar dentro mientras el precio esté sobre su media de 200 días.

    Es el filtro de tendencia más estudiado: no busca adivinar, solo evita quedarse dentro
    durante las caídas largas. Sale con un margen bajo la media para no entrar y salir por ruido.
    Las noticias solo pueden vetar una entrada (evento de riesgo negativo), nunca forzarla.
    """
    price = feats.close[t]
    sma = feats.sma_slow[t]
    if sma is None:
        return Signal(
            symbol=symbol,
            action="MANTENER" if position else "ESPERAR",
            price=price,
            reasons=[f"datos insuficientes: hacen falta {strategy.slow_sma} velas para la media"],
            news=news,
        )
    gap = price / sma - 1
    above = f"precio {gap:+.1%} respecto a su media de {strategy.slow_sma} días ({sma:,.2f})"
    sig = Signal(symbol=symbol, action="ESPERAR", price=price, trend="alcista" if gap > 0 else "bajista",
                 tech_score=round(gap, 3), combined=round(gap, 3), news=news, atr=feats.atr[t], rsi=feats.rsi[t])
    if position is not None:
        sig.is_buy, sig.stop, sig.target = True, position.stop, position.target
        if price < sma * (1 - strategy.core_exit_margin):
            sig.action = "CERRAR"
            sig.reasons = [f"el precio cayó bajo su media de {strategy.slow_sma} días: {above}"]
        else:
            sig.action = "MANTENER"
            sig.reasons = [f"sigue en tendencia: {above}"]
        return sig
    if gap <= 0:
        sig.reasons = [f"fuera de tendencia: {above}"]
        return sig
    if news and news.risk_event and news.score < 0:
        sig.reasons = [f"en tendencia, pero hay un evento de riesgo en las noticias: {news.summary}"]
        return sig
    sig.action = "COMPRAR"
    sig.stop = price * (1 - strategy.core_stop)
    sig.reasons = [
        f"en tendencia: {above}",
        f"se mantiene mientras siga sobre la media; stop de protección a {strategy.core_stop:.0%} ({sig.stop:,.2f})",
    ]
    return sig
