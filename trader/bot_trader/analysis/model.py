"""Regresión logística entrenada con la historia de TODOS los activos vigilados.

Por qué una regresión logística y no algo "más potente":
- Con datos de mercado (mucho ruido, poca señal) los modelos complejos memorizan el pasado y
  fallan en vivo. Un modelo lineal con regularización generaliza mejor y se puede explicar.
- Junta la historia de varios activos (con variables medidas contra su volatilidad), así tiene
  miles de ejemplos en vez de cientos.
- Se entrena "hacia adelante": en cada fecha solo ve operaciones que ya habían terminado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

MIN_SAMPLES = 250
L2 = 5.0  # regularización: empuja los pesos hacia cero para no sobreajustar


def logit(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return float(np.log(p / (1 - p)))


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class LogisticModel:
    def __init__(self, l2: float = L2):
        self.l2 = l2
        self.mean = None
        self.std = None
        self.w = None
        self.n = 0

    def fit(self, x: np.ndarray, y: np.ndarray, iters: int = 30) -> "LogisticModel":
        self.mean = x.mean(axis=0)
        self.std = x.std(axis=0)
        self.std[self.std < 1e-9] = 1.0
        z = np.column_stack([np.ones(len(x)), (x - self.mean) / self.std])
        w = np.zeros(z.shape[1])
        penalty = np.eye(z.shape[1]) * self.l2
        penalty[0, 0] = 0.0  # sin castigo al intercepto
        for _ in range(iters):  # Newton-Raphson (IRLS)
            p = _sigmoid(z @ w)
            grad = z.T @ (y - p) - penalty @ w
            hess = (z * (p * (1 - p))[:, None]).T @ z + penalty
            step = np.linalg.solve(hess, grad)
            w += step
            if np.max(np.abs(step)) < 1e-6:
                break
        self.w = w
        self.n = len(x)
        return self

    def predict(self, row) -> float:
        z = np.concatenate([[1.0], (np.asarray(row, dtype=float) - self.mean) / self.std])
        return float(_sigmoid(z @ self.w))

    def predict_many(self, rows) -> np.ndarray:
        x = (np.asarray(rows, dtype=float) - self.mean) / self.std
        return _sigmoid(np.column_stack([np.ones(len(x)), x]) @ self.w)

    def weights(self) -> list[float]:
        """Peso de cada variable (ya estandarizada); el signo dice si empuja a favor o en contra."""
        return list(self.w[1:]) if self.w is not None else []


@dataclass
class Dataset:
    """Ejemplos (variables, ¿ganó?) con la fecha en que se conoció el resultado."""

    resolved: list[date] = field(default_factory=list)
    rows: list[list[float]] = field(default_factory=list)
    labels: list[int] = field(default_factory=list)

    def add(self, resolved: date, row: list[float], won: bool) -> None:
        self.resolved.append(resolved)
        self.rows.append(row)
        self.labels.append(1 if won else 0)

    def fit_until(self, as_of: date) -> LogisticModel | None:
        keep = [k for k, d in enumerate(self.resolved) if d <= as_of]
        if len(keep) < MIN_SAMPLES:
            return None
        y = np.array([self.labels[k] for k in keep], dtype=float)
        if y.min() == y.max():
            return None
        x = np.array([self.rows[k] for k in keep], dtype=float)
        return LogisticModel().fit(x, y)
