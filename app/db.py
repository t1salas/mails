"""Conexión a la base de datos.

Piloto: SQLite (un archivo, costo cero). El código usa SQLAlchemy, así que pasar a
PostgreSQL es cambiar DATABASE_URL.

Regla importante: no abrir una segunda sesión mientras otra sigue abierta en el mismo
hilo (con SQLite la segunda esperaría el bloqueo de escritura de la primera). Por eso
los mensajes de WhatsApp se envían DESPUÉS de cerrar la transacción.

Garantía contra doble reserva:
- En SQLite cada transacción empieza con BEGIN IMMEDIATE, así que las escrituras
  se serializan: la verificación de choque y la inserción no se pueden intercalar
  con otra reserva.
- En PostgreSQL la reserva bloquea la fila del profesional (SELECT ... FOR UPDATE)
  antes de verificar choques (ver app/domain/booking.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, types
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class UTCDateTime(types.TypeDecorator):
    """Guarda siempre en UTC y devuelve datetimes con zona horaria UTC.

    SQLite no guarda zona horaria; con este tipo el código nunca maneja fechas "naive".
    """

    impl = types.DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Se esperaba un datetime con zona horaria")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})

        @event.listens_for(engine, "connect")
        def _sqlite_on_connect(dbapi_conn, _record):  # pragma: no cover - trivial
            # Desactiva el manejo automático de transacciones de pysqlite;
            # emitimos nuestro propio BEGIN IMMEDIATE en el evento "begin".
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_on_begin(conn):  # pragma: no cover - trivial
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return create_engine(url, pool_pre_ping=True)


def init_db(url: str, create_tables: bool = True) -> Engine:
    """Inicializa el motor global. Se llama al arrancar la app, la CLI o los tests."""
    global _engine, _SessionLocal
    from app import models  # noqa: F401  (registra las tablas)

    if _engine is not None:
        _engine.dispose()
    _engine = make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    if create_tables:
        Base.metadata.create_all(_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Base de datos no inicializada: llama a init_db() primero")
    return _engine


def new_session() -> Session:
    if _SessionLocal is None:
        raise RuntimeError("Base de datos no inicializada: llama a init_db() primero")
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Una transacción: commit al salir bien, rollback si hay excepción."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
