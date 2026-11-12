"""Estrategia: junta tendencia, probabilidad y noticias en una decisión explicada.

Cómo piensa (lado comprador; el corto es el espejo y está apagado por defecto):

ENTRAR solo si TODO esto se cumple:
  1. Puntaje combinado (técnico + noticias) por encima de `umbral_entrada`.
  2. La probabilidad histórica de ganar en situaciones parecidas supera `probabilidad_minima`.
  3. La ganancia esperada (en R, ya con costos) supera `expectativa_minima`.
  4. Hay suficientes casos parecidos (`muestras_minimas`) para creerle a esa estadística.
  5. El RSI no está en sobrecompra (no perseguir el precio).
  6. No hay un evento de riesgo con noticias negativas (las noticias pueden vetar).

SALIR (además del stop y el objetivo, que van puestos desde que se abre):
  - si el puntaje combinado cae bajo `umbral_salida` (la tendencia se rompió), o
  - si aparece un evento de riesgo con noticias claramente negativas.
"""

from __future__ import annotations

from bot_trader.analysis.probability import trade_levels
from bot_trader.analysis.trend import Features, describe, trend_label
from bot_trader.config import RiskParams, StrategyParams
from bot_trader.models import NewsView, Position, Probability, Signal

BAD_NEWS_EXIT = -0.5


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
) -> Signal:
    price = feats.close[t]
    tech = feats.score[t]
    atr = feats.atr[t]
    rsi = feats.rsi[t]
    if tech is None or atr is None or rsi is None or atr <= 0:
        return Signal(
            symbol=symbol,
            action="MANTENER" if position else "ESPERAR",
            price=price,
            is_buy=position.is_buy if position else True,
            reasons=[f"datos insuficientes: hacen falta al menos ~{strategy.slow_sma + 30} velas"],
            news=news,
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
    )
    context = describe(feats, t)
    bad_event = bool(news and news.risk_event and news.score < 0)

    # --- Hay posición abierta: ¿mantener o cerrar? ---------------------------------------
    if position is not None:
        sig.is_buy = position.is_buy
        sig.stop, sig.target = position.stop, position.target
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
        if prob is None:
            blockers.append("sin historia suficiente para estimar probabilidades")
        else:
            if prob.p_win < risk.min_probability:
                blockers.append(f"probabilidad {prob.p_win:.0%} < mínimo {risk.min_probability:.0%}")
            if prob.expectancy_r < risk.min_expectancy_r:
                blockers.append(f"ganancia esperada {prob.expectancy_r:+.2f}R < mínimo {risk.min_expectancy_r:+.2f}R")
            if prob.n < risk.min_samples:
                blockers.append(f"solo {prob.n} casos parecidos (< {risk.min_samples})")
        if is_buy and rsi >= strategy.rsi_overbought:
            blockers.append(f"RSI {rsi:.0f} en sobrecompra: mejor esperar un retroceso")
        if not is_buy and rsi <= strategy.rsi_oversold:
            blockers.append(f"RSI {rsi:.0f} en sobreventa: mejor esperar un rebote")
        if (is_buy and bad_event) or (not is_buy and news and news.risk_event and news.score > 0):
            blockers.append(f"evento de riesgo en las noticias: {news.summary}")
        side = "compra" if is_buy else "venta en corto"
        if blockers:
            sig.action = "ESPERAR"
            sig.reasons = [f"hay señal de {side} (puntaje {combined:+.2f}) pero:"] + blockers + context
            sig.stop = sig.target = None
            return sig
        sig.action = "COMPRAR" if is_buy else "VENDER_CORTO"
        sig.reasons = [
            f"tendencia {sig.trend} (técnico {tech:+.2f}, noticias {news_score:+.2f}, combinado {combined:+.2f})",
            (
                f"en {prob.n} casos parecidos ganó el {prob.p_win:.0%} de las veces, "
                f"ganancia esperada {prob.expectancy_r:+.2f}R con costos"
            ),
            f"stop {sig.stop:.2f} ({strategy.stop_atr:g} ATR) y objetivo {sig.target:.2f} ({strategy.target_atr:g} ATR)",
        ] + context
        if news and news.summary and news.method != "sin_noticias":
            sig.reasons.append(f"noticias: {news.summary}")
        return sig

    sig.reasons = [f"sin ventaja clara: puntaje {combined:+.2f} (entrada desde {strategy.entry_score:+.2f})"] + context
    return sig
