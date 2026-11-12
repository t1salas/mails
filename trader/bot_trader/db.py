"""Bitácora del bot en SQLite: señales, órdenes, posiciones propias, capital y estado de riesgo.

Llevar diario es lo que separa a un trader de un apostador: cada decisión queda registrada con
su porqué, para revisarla después.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from bot_trader.models import Position, Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS instrumentos (
    simbolo TEXT PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    actualizado TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS senales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cuenta TEXT NOT NULL,
    simbolo TEXT NOT NULL,
    accion TEXT NOT NULL,
    es_compra INTEGER NOT NULL,
    precio REAL NOT NULL,
    stop REAL,
    objetivo REAL,
    monto REAL,
    probabilidad REAL,
    expectativa REAL,
    muestras INTEGER,
    puntaje REAL,
    noticias REAL,
    razones TEXT NOT NULL,
    estado TEXT NOT NULL,
    posicion_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_senales_estado ON senales(estado, simbolo);
CREATE TABLE IF NOT EXISTS ordenes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cuenta TEXT NOT NULL,
    senal_id INTEGER,
    simbolo TEXT NOT NULL,
    tipo TEXT NOT NULL,
    monto REAL,
    estado TEXT NOT NULL,
    referencia TEXT,
    orden_broker TEXT,
    posicion_id TEXT,
    detalle TEXT
);
CREATE TABLE IF NOT EXISTS posiciones (
    id TEXT NOT NULL,
    cuenta TEXT NOT NULL,
    simbolo TEXT NOT NULL,
    instrument_id INTEGER,
    es_compra INTEGER NOT NULL,
    monto REAL NOT NULL,
    unidades REAL NOT NULL,
    apalancamiento INTEGER NOT NULL DEFAULT 1,
    precio_entrada REAL NOT NULL,
    stop REAL,
    objetivo REAL,
    abierta TEXT NOT NULL,
    cerrada TEXT,
    precio_salida REAL,
    pnl REAL,
    motivo_cierre TEXT,
    seguimiento INTEGER NOT NULL DEFAULT 0,
    distancia_stop REAL,
    stop_inicial REAL,
    PRIMARY KEY (id, cuenta)
);
CREATE TABLE IF NOT EXISTS capital (
    ts TEXT NOT NULL,
    cuenta TEXT NOT NULL,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS estado (
    cuenta TEXT NOT NULL,
    clave TEXT NOT NULL,
    valor TEXT NOT NULL,
    PRIMARY KEY (cuenta, clave)
);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Journal:
    """Acceso a la base de datos. Cada cuenta (papel/demo/real) tiene sus datos separados."""

    def __init__(self, path: str | Path, account: str):
        self.path = str(path)
        self.account = account
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Agrega columnas nuevas a bitácoras creadas con versiones anteriores."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(posiciones)")}
        for name, ddl in (
            ("seguimiento", "INTEGER NOT NULL DEFAULT 0"),
            ("distancia_stop", "REAL"),
            ("stop_inicial", "REAL"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE posiciones ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- estado clave/valor ---------------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT valor FROM estado WHERE cuenta=? AND clave=?", (self.account, key)
        ).fetchone()
        return row["valor"] if row else default

    def set(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO estado(cuenta, clave, valor) VALUES (?,?,?) "
                "ON CONFLICT(cuenta, clave) DO UPDATE SET valor=excluded.valor",
                (self.account, key, value),
            )

    # --- instrumentos ---------------------------------------------------------------------

    def instrument_id(self, symbol: str) -> int | None:
        row = self.conn.execute("SELECT instrument_id FROM instrumentos WHERE simbolo=?", (symbol,)).fetchone()
        return int(row["instrument_id"]) if row else None

    def save_instrument(self, symbol: str, instrument_id: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO instrumentos(simbolo, instrument_id, actualizado) VALUES (?,?,?) "
                "ON CONFLICT(simbolo) DO UPDATE SET instrument_id=excluded.instrument_id, "
                "actualizado=excluded.actualizado",
                (symbol, int(instrument_id), iso(now_utc())),
            )

    # --- señales --------------------------------------------------------------------------

    def save_signal(self, signal: Signal, amount: float | None, state: str) -> int:
        p = signal.probability
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO senales(ts, cuenta, simbolo, accion, es_compra, precio, stop, objetivo, monto, "
                "probabilidad, expectativa, muestras, puntaje, noticias, razones, estado) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    iso(now_utc()), self.account, signal.symbol, signal.action, int(signal.is_buy),
                    signal.price, signal.stop, signal.target, amount,
                    p.p_win if p else None, p.expectancy_r if p else None, p.n if p else None,
                    signal.combined, signal.news_score, json.dumps(signal.reasons, ensure_ascii=False), state,
                ),
            )
            return int(cur.lastrowid)

    def signal(self, signal_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM senales WHERE id=? AND cuenta=?", (signal_id, self.account)
        ).fetchone()

    def pending_suggestion(self, symbol: str, action: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM senales WHERE cuenta=? AND simbolo=? AND accion=? AND estado='sugerida' "
            "ORDER BY id DESC LIMIT 1",
            (self.account, symbol, action),
        ).fetchone()

    def set_signal_state(self, signal_id: int, state: str, position_id: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE senales SET estado=?, posicion_id=COALESCE(?, posicion_id) WHERE id=?",
                (state, position_id, signal_id),
            )

    def expire_suggestions(self, older_than: datetime) -> int:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE senales SET estado='vencida' WHERE cuenta=? AND estado='sugerida' AND ts < ?",
                (self.account, iso(older_than)),
            )
            return cur.rowcount

    def recent_signals(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM senales WHERE cuenta=? ORDER BY id DESC LIMIT ?", (self.account, limit)
            )
        )

    # --- órdenes --------------------------------------------------------------------------

    def save_order(
        self,
        kind: str,
        symbol: str,
        amount: float | None,
        state: str,
        signal_id: int | None = None,
        reference: str | None = None,
        broker_order: str | None = None,
        position_id: str | None = None,
        detail: dict | None = None,
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO ordenes(ts, cuenta, senal_id, simbolo, tipo, monto, estado, referencia, "
                "orden_broker, posicion_id, detalle) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    iso(now_utc()), self.account, signal_id, symbol, kind, amount, state, reference,
                    broker_order, position_id, json.dumps(detail or {}, ensure_ascii=False, default=str),
                ),
            )
            return int(cur.lastrowid)

    def pending_orders(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM ordenes WHERE cuenta=? AND estado='pendiente' AND referencia IS NOT NULL",
                (self.account,),
            )
        )

    def update_order(self, order_id: int, state: str, position_id: str | None, detail: dict | None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE ordenes SET estado=?, posicion_id=COALESCE(?, posicion_id), detalle=? WHERE id=?",
                (state, position_id, json.dumps(detail or {}, ensure_ascii=False, default=str), order_id),
            )

    def recent_orders(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM ordenes WHERE cuenta=? ORDER BY id DESC LIMIT ?", (self.account, limit)
            )
        )

    # --- posiciones propias del bot -------------------------------------------------------

    def add_position(self, p: Position) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO posiciones(id, cuenta, simbolo, instrument_id, es_compra, monto, unidades, "
                "apalancamiento, precio_entrada, stop, objetivo, abierta, seguimiento, distancia_stop, stop_inicial) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p.id, self.account, p.symbol, p.instrument_id, int(p.is_buy), p.amount, p.units,
                    p.leverage, p.entry_price, p.stop, p.target, iso(p.opened_at), int(p.trailing),
                    p.trail_distance, p.stop,
                ),
            )

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            id=row["id"],
            symbol=row["simbolo"],
            instrument_id=row["instrument_id"],
            is_buy=bool(row["es_compra"]),
            amount=row["monto"],
            units=row["unidades"],
            leverage=row["apalancamiento"],
            entry_price=row["precio_entrada"],
            stop=row["stop"],
            target=row["objetivo"],
            opened_at=from_iso(row["abierta"]),
            trailing=bool(row["seguimiento"]),
            trail_distance=row["distancia_stop"],
        )

    def initial_stop(self, position_id: str) -> float | None:
        row = self.conn.execute(
            "SELECT stop_inicial, stop FROM posiciones WHERE id=? AND cuenta=?", (position_id, self.account)
        ).fetchone()
        if row is None:
            return None
        return row["stop_inicial"] if row["stop_inicial"] is not None else row["stop"]

    def update_stop(self, position_id: str, stop: float) -> None:
        with self.tx() as c:
            c.execute("UPDATE posiciones SET stop=? WHERE id=? AND cuenta=?", (stop, position_id, self.account))

    def closed_trades_with_probability(self, limit: int = 100) -> list[sqlite3.Row]:
        """Operaciones cerradas del bot junto con la probabilidad que se estimó al abrirlas."""
        return list(
            self.conn.execute(
                "SELECT p.simbolo, p.pnl, s.probabilidad FROM posiciones p "
                "JOIN senales s ON s.posicion_id = p.id AND s.cuenta = p.cuenta "
                "WHERE p.cuenta=? AND p.cerrada IS NOT NULL AND p.pnl IS NOT NULL AND s.probabilidad IS NOT NULL "
                "ORDER BY p.cerrada DESC LIMIT ?",
                (self.account, limit),
            )
        )

    def open_positions(self) -> list[Position]:
        rows = self.conn.execute(
            "SELECT * FROM posiciones WHERE cuenta=? AND cerrada IS NULL ORDER BY abierta", (self.account,)
        )
        return [self._row_to_position(r) for r in rows]

    def close_position(self, position_id: str, exit_price: float | None, pnl: float | None, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE posiciones SET cerrada=?, precio_salida=?, pnl=?, motivo_cierre=? "
                "WHERE id=? AND cuenta=? AND cerrada IS NULL",
                (iso(now_utc()), exit_price, pnl, reason, position_id, self.account),
            )

    def closed_positions(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM posiciones WHERE cuenta=? AND cerrada IS NOT NULL ORDER BY cerrada DESC LIMIT ?",
                (self.account, limit),
            )
        )

    def last_close_time(self, symbol: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT MAX(cerrada) AS c FROM posiciones WHERE cuenta=? AND simbolo=? AND cerrada IS NOT NULL",
            (self.account, symbol),
        ).fetchone()
        return from_iso(row["c"]) if row and row["c"] else None

    def realized_pnl(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS s FROM posiciones WHERE cuenta=? AND cerrada IS NOT NULL",
            (self.account,),
        ).fetchone()
        return float(row["s"])

    # --- capital ---------------------------------------------------------------------------

    def record_equity(self, equity: float) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO capital(ts, cuenta, equity) VALUES (?,?,?)", (iso(now_utc()), self.account, equity))
