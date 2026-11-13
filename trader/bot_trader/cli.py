"""Comandos del bot.  Uso:  python -m bot_trader <comando> [opciones]

  analizar           analiza la lista de activos y explica qué haría (no guarda ni opera)
  ciclo              decide ahora: sugiere u opera según `modo`
  correr             el agente: vigila cada N minutos y decide una vez al día
  aprobar ID         ejecuta una sugerencia
  estado             capital, posiciones del bot y frenos de riesgo
  diario             últimas señales, órdenes y operaciones cerradas
  backtest           prueba la estrategia con datos históricos
  verificar-etoro    revisa llaves y conexión con eToro (solo lectura)
  buscar TEXTO       busca instrumentos en eToro
  pausar / reanudar  freno de mano del bot
  cerrar-todo --si   cierra todas las posiciones del bot
  probar-telegram    envía un mensaje de prueba y registra el menú de comandos

Con Telegram configurado, mientras corre `correr` también obedece comandos desde el chat
(/estado, /pausar, /reanudar, /cerrar, /cerrar_todo, /aprobar...).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from bot_trader.backtest import compare_variants, robustness, run_backtest
from bot_trader.config import ConfigError, Settings, load_settings
from bot_trader.engine import build_bot
from bot_trader.etoro import EtoroClient, EtoroError, ci_get, short_allowed
from bot_trader.notify import send_telegram
from bot_trader.report import (
    ACCOUNT_LABEL,
    analysis_detail,
    analysis_line,
    backtest_report,
    cycle_report,
    journal_text,
    robustness_report,
    status_text,
    variants_report,
)
from bot_trader.remote import make_remote

DEFAULT_CONFIG = "config/estrategia.toml"


def _settings(args) -> Settings:
    path = args.config or os.environ.get("BOT_CONFIG") or DEFAULT_CONFIG
    settings = load_settings(path)
    return settings


def cmd_analizar(args) -> int:
    settings = _settings(args)
    bot = build_bot(settings)
    candles, errors, reference = bot.fetch_market()
    prices = {s: b[-1].close for s, b in candles.items()}
    account = bot.broker.account(prices)
    analyst = bot.build_analyst(candles, reference)
    news = bot.read_news([a for a in settings.assets if a.symbol in candles])
    analyses = bot.analyze(analyst, errors, account.positions, news)
    print(f"== Análisis ({len(analyses)} activos, precios de {bot.prices.name}) ==")
    if analyst.ref is not None:
        regime = analyst.market_regime(analyst.ref_dates[-1])
        print(f"Mercado: {regime.label} ({regime.detail})")
    print(bot.model_summary(analyst))
    for a in analyses:
        print("  " + analysis_line(a))
    print()
    for a in analyses:
        for line in analysis_detail(a):
            print(line)
    print("\nNo se sugirió ni se ejecutó nada. Para sugerir u operar usa: python -m bot_trader ciclo")
    return 0


def _run_once(settings: Settings, quiet_unless_noteworthy: bool = False, force_decision: bool = False) -> int:
    bot = build_bot(settings)
    result = bot.run_cycle(force_decision=force_decision)
    text = cycle_report(result)
    print(text)
    if result.noteworthy or not quiet_unless_noteworthy:
        if send_telegram(settings.telegram_token, settings.telegram_chat_id, cycle_report(result, detailed=False)):
            print("(aviso enviado por Telegram)")
    bot.journal.close()
    return 0


def cmd_ciclo(args) -> int:
    # Corrido a mano, decide en ese momento (si el mercado está abierto, con la vela del día en curso).
    return _run_once(_settings(args), force_decision=True)


def cmd_correr(args) -> int:
    settings = _settings(args)
    minutes = max(args.cada_minutos, 1)
    hour = settings.decision_hour_utc
    when = "en cada ciclo" if hour < 0 else f"una vez al día desde las {hour:02d}:00 UTC"
    print(f"Agente corriendo: vigila cada {minutes} min y decide {when}. Ctrl+C para detener.")
    remote = make_remote(lambda: _settings(args))
    if remote is not None:
        print("Control por Telegram activo: escríbele /ayuda a tu bot.")
        try:
            remote.channel.register_commands()
        except Exception as exc:
            logging.warning("No pude registrar el menú de comandos en Telegram: %s", type(exc).__name__)
        remote.channel.send(
            f"Agente en línea. Cuenta {ACCOUNT_LABEL[settings.account]}, modo {settings.mode}. "
            f"Vigilo cada {minutes} min y decido {when}. /ayuda para ver los comandos."
        )
    while True:
        try:
            _run_once(_settings(args), quiet_unless_noteworthy=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # que un error de red no apague el bot
            logging.exception("Falló el ciclo: %s", exc)
            send_telegram(settings.telegram_token, settings.telegram_chat_id, f"Bot trader: falló un ciclo ({exc})")
        if remote is not None:
            remote.serve(minutes * 60)  # entre ciclos atiende los comandos de Telegram
        else:
            time.sleep(minutes * 60)


def cmd_probar_telegram(args) -> int:
    settings = _settings(args)
    remote = make_remote(lambda: settings)
    if remote is None:
        print("Faltan TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el .env.")
        return 1
    if not remote.channel.send("Prueba del bot trader: si lees esto, los avisos funcionan. /ayuda para los comandos."):
        print("No pude enviar el mensaje: revisa el token, el chat id, y que le hayas escrito /start a tu bot.")
        return 1
    try:
        remote.channel.register_commands()
    except Exception as exc:
        print(f"El mensaje salió, pero no pude registrar el menú de comandos ({type(exc).__name__}).")
        return 1
    print("Mensaje enviado y menú de comandos registrado. Los comandos se atienden mientras corre `correr`.")
    return 0


def cmd_aprobar(args) -> int:
    settings = _settings(args)
    if settings.account == "real":
        print("ATENCIÓN: cuenta REAL. Esta orden usa dinero real.")
    bot = build_bot(settings)
    print(bot.approve(args.id))
    return 0


def cmd_estado(args) -> int:
    print(status_text(build_bot(_settings(args))))
    return 0


def cmd_diario(args) -> int:
    print(journal_text(build_bot(_settings(args)), args.n))
    return 0


def cmd_backtest(args) -> int:
    settings = _settings(args)
    if args.simbolos:
        wanted = {s.strip().upper() for s in args.simbolos.split(",")}
        settings.assets = [a for a in settings.assets if a.symbol in wanted]
    settings.news.enabled = False
    bot = build_bot(settings)
    candles, errors, reference = bot.fetch_market()
    for sym, err in errors.items():
        print(f"{sym}: {err}")
    if args.variantes:
        print("Comparando 5 versiones de la estrategia (tarda un par de minutos)...", flush=True)
        print(variants_report(compare_variants(settings, candles, reference), settings.cash_rate))
        return 0
    res = run_backtest(settings, candles, initial=args.capital, reference=reference)
    print(backtest_report(res, sorted(candles), settings.cash_rate))
    if args.robustez:
        print("\nCalculando la robustez (8 backtests más, tarda un par de minutos)...", flush=True)
        print(robustness_report(robustness(settings, candles, reference), settings.cash_rate))
    return 0


def _client(settings: Settings) -> EtoroClient:
    if not settings.has_etoro_keys:
        raise ConfigError("faltan ETORO_API_KEY y ETORO_USER_KEY en el .env")
    return EtoroClient(settings.etoro_api_key, settings.etoro_user_key, demo=settings.account != "real")


def cmd_verificar(args) -> int:
    settings = _settings(args)
    client = _client(settings)
    ok = True
    try:
        me = client.me()
        print(f"Llaves válidas. Identidad: {ci_get(me, 'username', 'userName', 'gcid', default='(sin nombre)')}")
    except EtoroError as exc:
        print(f"Las llaves no funcionan: {exc}")
        return 1
    for asset in settings.assets:
        try:
            inst = client.resolve(asset.etoro_symbol)
            iid = int(ci_get(inst, "instrumentId"))
            bars = client.candles(iid, settings.interval, 5)
            print(f"  {asset.symbol:<8} -> id {iid}  {ci_get(inst, 'displayname', default='')}  último {bars[-1].close}")
        except (EtoroError, LookupError) as exc:
            ok = False
            print(f"  {asset.symbol:<8} PROBLEMA: {exc}")
            continue
        print("           " + _eligibility_line(client, iid, settings))
    try:
        portfolio = client.portfolio()
        positions = ci_get(portfolio, "positions", default=[]) or []
        print(f"Cartera {client.env}: crédito {ci_get(portfolio, 'credit', default=0)} USD, {len(positions)} posiciones abiertas")
    except EtoroError as exc:
        ok = False
        print(f"No pude leer la cartera {client.env} (¿la llave tiene permiso de lectura de trading?): {exc}")
    print("Todo en orden." if ok else "Hay problemas: revisa los símbolos (simbolo_etoro) o los permisos de la llave.")
    return 0 if ok else 1


def _eligibility_line(client: EtoroClient, iid: int, settings: Settings) -> str:
    """Qué permite eToro en este activo: venta en corto, stop móvil y lo que cobraría un corto."""
    try:
        elig = client.eligibility(iid)
    except (EtoroError, LookupError) as exc:
        return f"no pude consultar qué permite eToro aquí ({exc})"
    can_short = short_allowed(elig, settings.risk.leverage)
    trailing = ci_get(elig, "allowTrailingStopLoss")
    yes_no = {True: "sí", False: "NO", None: "no informado"}
    parts = [f"venta en corto: {yes_no[can_short]}", f"stop móvil: {yes_no[trailing]}"]
    if can_short and settings.risk.allow_short:
        try:
            costs = client.cost_estimate(iid, False, 1000, settings.risk.leverage)
            detail = ", ".join(
                f"{ci_get(c, 'costType')} {ci_get(c, 'amount')} {ci_get(c, 'currency', default='')}".strip()
                for c in costs
            )
            parts.append(f"costos de un corto de 1000 USD según eToro: {detail or 'sin datos'}")
        except (EtoroError, LookupError) as exc:
            parts.append(f"no pude estimar los costos del corto ({exc})")
    return "  |  ".join(parts)


def cmd_buscar(args) -> int:
    client = _client(_settings(args))
    for item in client.search(args.texto)[:25]:
        print(f"  {ci_get(item, 'internalSymbolFull'):<14} id {ci_get(item, 'instrumentId'):<8} "
              f"{ci_get(item, 'displayname', default='')}  operable={ci_get(item, 'isCurrentlyTradable')}")
    return 0


def cmd_pausar(args) -> int:
    bot = build_bot(_settings(args))
    bot.risk.pause(args.motivo or "pausa manual")
    print("Bot en pausa: no abrirá posiciones nuevas (sí puede cerrar). Usa `reanudar` para seguir.")
    return 0


def cmd_reanudar(args) -> int:
    settings = _settings(args)
    bot = build_bot(settings)
    candles, _ = bot.fetch_candles(settings.assets)
    account = bot.broker.account({s: b[-1].close for s, b in candles.items()})
    equity, _ = bot.bot_equity(account)
    bot.risk.resume(equity)
    print(f"Bot reanudado. El máximo para medir el drawdown se reinicia en {equity:,.2f} USD.")
    return 0


def cmd_cerrar_todo(args) -> int:
    if not args.si:
        print("Esto cierra TODAS las posiciones del bot. Repite con --si para confirmar.")
        return 1
    bot = build_bot(_settings(args))
    for line in bot.close_all():
        print(line)
    bot.risk.pause("cerrar-todo")
    print("Bot en pausa tras cerrar todo. Usa `reanudar` para seguir.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot_trader", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help=f"archivo TOML (por defecto $BOT_CONFIG o {DEFAULT_CONFIG})")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("analizar").set_defaults(fn=cmd_analizar)
    sub.add_parser("ciclo").set_defaults(fn=cmd_ciclo)
    p = sub.add_parser("correr")
    p.add_argument("--cada-minutos", type=int, default=30)
    p.set_defaults(fn=cmd_correr)
    p = sub.add_parser("aprobar")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_aprobar)
    sub.add_parser("estado").set_defaults(fn=cmd_estado)
    p = sub.add_parser("diario")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_diario)
    p = sub.add_parser("backtest")
    p.add_argument("--simbolos", help="solo estos símbolos, separados por coma")
    p.add_argument("--capital", type=float, default=None)
    p.add_argument("--robustez", action="store_true", help="repite con variaciones de parámetros")
    p.add_argument("--variantes", action="store_true", help="compara las versiones de la estrategia")
    p.set_defaults(fn=cmd_backtest)
    sub.add_parser("verificar-etoro").set_defaults(fn=cmd_verificar)
    sub.add_parser("probar-telegram").set_defaults(fn=cmd_probar_telegram)
    p = sub.add_parser("buscar")
    p.add_argument("texto")
    p.set_defaults(fn=cmd_buscar)
    p = sub.add_parser("pausar")
    p.add_argument("motivo", nargs="?")
    p.set_defaults(fn=cmd_pausar)
    sub.add_parser("reanudar").set_defaults(fn=cmd_reanudar)
    p = sub.add_parser("cerrar-todo")
    p.add_argument("--si", action="store_true")
    p.set_defaults(fn=cmd_cerrar_todo)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # que el log del servicio salga al momento
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nDetenido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
