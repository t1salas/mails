"""El analista: junta todo el análisis de la lista de activos en un solo lugar.

Lo usan igual el bot en vivo y el backtest, así lo que se prueba es exactamente lo que opera.

Para cada activo y fecha calcula:
- la tendencia (puntaje técnico);
- la probabilidad de ganar con DOS modelos independientes:
    1. "casos parecidos": qué pasó en esta misma situación en la historia del activo;
    2. regresión logística entrenada con la historia de todos los activos;
  si los dos se contradicen mucho, el bot no confía y no entra;
- la CALIBRACIÓN de esa probabilidad: el promedio de los dos modelos se corrige con su propio
  historial de aciertos (fuera de muestra). Si históricamente "60 %" resultó ser 50 %, ahora dice
  50 %. Si los modelos no predicen nada, la probabilidad calibrada se queda en el promedio y
  el bot no se engaña creyendo que tiene ventaja;
- el régimen del mercado (alcista, neutral, defensivo o crisis) según el activo de referencia;
- la volatilidad anual y las correlaciones entre activos, para dimensionar la cartera.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import replace
from datetime import date
from statistics import median

from bot_trader.analysis import indicators as ind
from bot_trader.analysis.features import feature_rows
from bot_trader.analysis.model import Dataset, LogisticModel, logit
from bot_trader.analysis.probability import ProbabilityModel
from bot_trader.analysis.trend import Features, compute_features
from bot_trader.config import Settings
from bot_trader.models import Candle, NewsView, Position, Probability, Regime, Signal
from bot_trader.strategy import evaluate

CORR_WINDOW = 120
WALK_FORWARD_STEP = 63  # para la historia de calibración, el modelo se reentrena cada ~3 meses
BETA_WINDOW = 250
CRISIS_VOL = 1.5  # volatilidad de corto plazo 1.5 veces la normal = estrés


class Analyst:
    def __init__(self, data: dict[str, list[Candle]], settings: Settings, reference: list[Candle] | None = None):
        self.s = settings
        sp, cost = settings.strategy, settings.cost_per_side
        self.sides = [True, False] if settings.risk.allow_short else [True]
        self.feats: dict[str, Features] = {}
        self.dates: dict[str, list[date]] = {}
        self.index: dict[str, dict[date, int]] = {}
        self.returns: dict[str, dict[date, float]] = {}
        for symbol, candles in data.items():
            if not candles:
                continue
            f = compute_features(candles, sp)
            self.feats[symbol] = f
            ds = [c.date.date() for c in candles]
            self.dates[symbol] = ds
            self.index[symbol] = {d: i for i, d in enumerate(ds)}
            self.returns[symbol] = dict(zip(ds, ind.log_returns(f.close)))

        # Mercado de referencia
        self.ref_symbol = settings.reference.upper() if settings.reference else ""
        if reference is None and self.ref_symbol in data:
            reference = data[self.ref_symbol]
        self.ref: Features | None = None
        if reference and len(reference) > sp.slow_sma + 30:
            self.ref = compute_features(reference, sp)
            self.ref_dates = [c.date.date() for c in reference]
            self.ref_vol20 = ind.rolling_std(ind.log_returns(self.ref.close), 20)
            self.ref_returns = dict(zip(self.ref_dates, ind.log_returns(self.ref.close)))
        self._regime_cache: dict[date, Regime] = {}
        self._beta_cache: dict[tuple[str, int, int], bool] = {}

        # Modelos
        self.rows: dict[str, list[list[float] | None]] = {}
        self.hist: dict[str, dict[bool, ProbabilityModel]] = {}
        self.datasets = {side: Dataset() for side in self.sides}
        for symbol, f in self.feats.items():
            market = [self._ref_score(d) for d in self.dates[symbol]] if self.ref else None
            rows = feature_rows(f, market)
            self.rows[symbol] = rows
            self.hist[symbol] = {side: ProbabilityModel(f, sp, cost, side) for side in self.sides}
            for side in self.sides:
                outcomes = self.hist[symbol][side].outcomes
                for i, row in enumerate(rows):
                    out = outcomes[i]
                    if row is not None and out is not None:
                        self.datasets[side].add(self.dates[symbol][out.resolved], row, out.r > 0)
        self.models: dict[bool, LogisticModel | None] = {side: None for side in self.sides}
        self.calibrators: dict[bool, LogisticModel | None] = {side: None for side in self.sides}
        self.trained_on: date | None = None
        self.calibration = {side: self._calibration_history(side) for side in self.sides}

    def _calibration_history(self, side: bool) -> Dataset:
        """Predicciones fuera de muestra de toda la historia, con su resultado real.

        Para cada vela pasada se calcula lo que el bot HABRÍA predicho en ese momento (el modelo
        estadístico entrenado solo con lo anterior) y se anota si la operación ganó. Con eso se
        aprende cuánto creerle a cada probabilidad.
        """
        out = Dataset()
        all_dates = sorted({d for ds in self.dates.values() for d in ds})
        boundaries = all_dates[::WALK_FORWARD_STEP]
        vintages = [(b, self.datasets[side].fit_until(b)) for b in boundaries]
        for symbol, rows in self.rows.items():
            hist = self.hist[symbol][side]
            dates = self.dates[symbol]
            for i, row in enumerate(rows):
                out_i = hist.outcomes[i]
                if row is None or out_i is None:
                    continue
                h = hist.at(i)
                if h is None:
                    continue
                k = bisect_right(boundaries, dates[i]) - 1
                model = vintages[k][1] if k >= 0 else None
                raw = h.p_hist if model is None else (h.p_hist + model.predict(row)) / 2
                out.add(dates[out_i.resolved], [logit(raw)], out_i.r > 0)
        return out

    # --- entrenamiento -------------------------------------------------------------------

    def train(self, as_of: date) -> None:
        for side in self.sides:
            self.models[side] = self.datasets[side].fit_until(as_of)
            self.calibrators[side] = self.calibration[side].fit_until(as_of)
        self.trained_on = as_of

    def maybe_train(self, as_of: date, every_days: int = 30) -> None:
        if self.trained_on is None or (as_of - self.trained_on).days >= every_days:
            self.train(as_of)

    # --- mercado de referencia -----------------------------------------------------------

    def _ref_index(self, d: date) -> int | None:
        if self.ref is None:
            return None
        k = bisect_right(self.ref_dates, d) - 1
        return k if k >= 0 else None

    def _ref_score(self, d: date) -> float | None:
        k = self._ref_index(d)
        return self.ref.score[k] if k is not None else None

    def market_regime(self, d: date) -> Regime:
        if d in self._regime_cache:
            return self._regime_cache[d]
        k = self._ref_index(d)
        if k is None or self.ref.score[k] is None or self.ref.sma_slow[k] is None:
            regime = Regime()
        else:
            score = self.ref.score[k]
            below = self.ref.close[k] < self.ref.sma_slow[k]
            recent = [v for v in self.ref_vol20[max(0, k - 750) : k + 1] if v is not None]
            stressed = bool(recent) and self.ref_vol20[k] is not None and self.ref_vol20[k] > CRISIS_VOL * median(recent)
            if below and stressed:
                label, mult = "crisis", 0.0
            elif below or score <= -0.2:
                label, mult = "defensivo", self.s.risk.defensive_mult
            elif score >= 0.2:
                label, mult = "alcista", 1.0
            else:
                label, mult = "neutral", 1.0
            detail = (
                f"{self.ref_symbol} {'bajo' if below else 'sobre'} su media de 200, tendencia {score:+.2f}"
                + (", volatilidad de estrés" if stressed else "")
            )
            regime = Regime(label=label, score=score, risk_mult=mult, detail=detail)
        self._regime_cache[d] = regime
        return regime

    def regime_for(self, symbol: str, d: date) -> Regime:
        """El régimen solo frena a los activos que se mueven con el mercado (oro o bonos no)."""
        regime = self.market_regime(d)
        if self.ref is None:
            return regime
        key = (symbol, d.year, d.month)  # la relación con el mercado cambia lento: se recalcula cada mes
        if key not in self._beta_cache:
            if symbol == self.ref_symbol:
                moves = True
            else:
                c = self._corr(self.returns.get(symbol, {}), self.dates.get(symbol, []), self.ref_returns, d, BETA_WINDOW)
                moves = c is not None and c >= 0.5
            self._beta_cache[key] = moves
        return replace(regime, applies=self._beta_cache[key])

    # --- riesgo de cartera ---------------------------------------------------------------

    @staticmethod
    def _corr(ra: dict[date, float], dates_a: list[date], rb: dict[date, float], d: date, window: int) -> float | None:
        k = bisect_right(dates_a, d) - 1
        xs, ys = [], []
        while k > 0 and len(xs) < window:
            day = dates_a[k]
            if day in rb:
                xs.append(ra[day])
                ys.append(rb[day])
            k -= 1
        if len(xs) < 30:
            return None
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        if sxx <= 0 or syy <= 0:
            return None
        return sxy / math.sqrt(sxx * syy)

    def correlation(self, a: str, b: str, d: date, window: int = CORR_WINDOW) -> float | None:
        if a not in self.returns or b not in self.returns:
            return None
        return self._corr(self.returns[a], self.dates[a], self.returns[b], d, window)

    def annual_vol(self, symbol: str, t: int) -> float | None:
        v = self.feats[symbol].vol[t]
        return v * math.sqrt(252) if v is not None else None

    # --- predicción ----------------------------------------------------------------------

    def probability(self, symbol: str, t: int, is_buy: bool) -> Probability | None:
        if is_buy not in self.hist.get(symbol, {}):
            return None
        hist = self.hist[symbol][is_buy].at(t)
        if hist is None:
            return None
        model = self.models.get(is_buy)
        row = self.rows[symbol][t]
        p_model = model.predict(row) if model is not None and row is not None else None
        raw = hist.p_hist if p_model is None else (hist.p_hist + p_model) / 2
        calibrator = self.calibrators.get(is_buy)
        p = calibrator.predict([logit(raw)]) if calibrator is not None else raw
        return replace(
            hist,
            p_win=p,
            p_model=p_model,
            calibrated=calibrator is not None,
            expectancy_r=p * hist.avg_win_r - (1 - p) * hist.avg_loss_r,
        )

    def evaluate(self, symbol: str, t: int, position: Position | None, news: NewsView | None) -> Signal:
        d = self.dates[symbol][t]
        return evaluate(
            symbol,
            self.feats[symbol],
            t,
            self.probability(symbol, t, True),
            self.probability(symbol, t, False) if self.s.risk.allow_short else None,
            news,
            position,
            self.s.strategy,
            self.s.risk,
            regime=self.regime_for(symbol, d),
        )
