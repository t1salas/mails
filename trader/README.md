# Bot trader para eToro

Bot que vigila una lista de activos y hace cuatro cosas:

- **Analiza la tendencia** de cada activo con medias móviles, pendiente, momentum, MACD y RSI.
- **Calcula probabilidades**: revisa la historia del activo y responde "las otras veces que estuvo así, ¿cuántas veces ganó esta operación y cuánto en promedio?".
- **Lee noticias** recientes (Google News y Yahoo Finance) con Claude o, sin IA, con palabras clave.
- **Sugiere compras y ventas** con monto, stop y objetivo, explicando por qué. Si lo activas, también **ejecuta las operaciones solo** en eToro, primero en la cuenta demo.

> **Aviso.** Esto es una herramienta, no asesoría financiera. Ningún bot gana siempre; los mercados cambian y el rendimiento pasado no garantiza el futuro. Empieza en papel, sigue en demo y pasa a dinero real solo con montos que puedas perder.

## Cómo piensa

Piensa como un trader disciplinado, no como un adivino:

1. **Primero el riesgo y después la ganancia.** Cada operación arriesga una fracción fija del capital, 1 % por defecto. El tamaño de la posición se calcula a partir de la distancia al stop. Diez pérdidas seguidas dejan la cuenta en ~90 %, no en cero.
2. **Solo entra con ventaja medible.** Debe cumplirse todo esto a la vez:
   - la tendencia acompaña;
   - en casos históricos parecidos la operación ganó más de lo que perdió, ya con costos;
   - hay suficientes casos para creerle a la estadística;
   - el precio no está en sobrecompra.
3. **El stop y el objetivo se ponen al abrir.** Por defecto el stop va a 2 ATR y el objetivo a 4 ATR, así que cada operación busca ganar el doble de lo que arriesga. En eToro quedan puestos en la orden, aunque el bot se apague.
4. **Las noticias pueden vetar, pero nunca mandan solas.** Pesan como máximo 25 %:
   - una noticia buena no abre una operación si la tendencia y la probabilidad no acompañan;
   - un evento de riesgo con noticias malas (demanda, investigación, resultados flojos) bloquea compras y puede cerrar una posición.
5. **Frenos automáticos:**
   - con −3 % en el día no abre nada más hasta mañana;
   - con −15 % desde el máximo se **pausa** hasta que tú lo revises y lo reanudes;
   - hay límites de posiciones, de exposición por activo y de exposición total.
6. **Todo queda en una bitácora** (SQLite): cada señal con sus razones, cada orden y cada cierre con su P&L.

### Qué significan los números

`p=58% E=+0.35R n=112` quiere decir:

- **n=112**: en la historia del activo hubo 112 situaciones parecidas a la actual (misma fuerza de tendencia y RSI similar).
- **p=58%**: en esas situaciones, entrar con el mismo stop y objetivo terminó en ganancia el 58 % de las veces.
- **E=+0.35R**: la ganancia promedio fue 0.35 veces lo arriesgado, ya con costos. Si arriesgas 100 USD por operación, eso es +35 USD en promedio por operación, *si el futuro se parece al pasado*.

Con pocos casos, la estimación se acerca al promedio general del activo para no creerle a una racha de suerte. Además, el cálculo solo usa operaciones que ya habían terminado en esa fecha, así que nunca mira el futuro.

## Probarlo en tu computadora (cuenta de papel, 5 minutos)

```bash
cd trader
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # o ".[ia]" si solo quieres usarlo con Claude

cp config/estrategia.example.toml config/estrategia.toml   # ajusta activos y riesgo
cp .env.example .env                                         # llaves (opcionales para papel)

python -m bot_trader analizar      # qué ve y qué haría con cada activo (no opera)
python -m bot_trader backtest      # cómo le habría ido con datos históricos
python -m bot_trader ciclo         # sugiere operaciones (modo "sugerir")
python -m bot_trader aprobar 3     # ejecuta la sugerencia #3 en la cuenta de papel
python -m bot_trader estado        # capital, posiciones y frenos
python -m bot_trader diario        # bitácora
```

- **Sin llaves de eToro:** los precios salen de Yahoo Finance y las órdenes se simulan en la cuenta de papel.
- **Sin `ANTHROPIC_API_KEY`:** las noticias se leen con palabras clave.

Pruebas automáticas: `python -m pytest`.

## Pasar a la cuenta DEMO de eToro (dinero virtual)

1. En eToro entra a **Configuración → API** (o a api-portal.etoro.com) y crea una llave:
   - entorno **Virtual/Demo**;
   - permisos de **lectura y escritura**.

   Copia la *API key* y la *user key* al `.env`:
   ```
   ETORO_API_KEY=...
   ETORO_USER_KEY=...
   ```
2. Verifica la conexión. Este comando solo lee: revisa las llaves, que cada símbolo exista en eToro y la cartera.
   ```bash
   python -m bot_trader verificar-etoro
   python -m bot_trader buscar BTC          # si un símbolo no aparece, busca cómo se llama en eToro
   ```
   Si el símbolo en eToro es distinto al tuyo, usa `simbolo_etoro = "..."` en ese `[[activos]]`.
3. En `config/estrategia.toml` pon `cuenta = "demo"`. Deja `modo = "sugerir"` unos días y aprueba a mano lo que te convenza.
4. Cuando confíes en lo que propone, pon `modo = "autonomo"`. El bot abre y cierra solo en la demo.

El bot **solo administra las posiciones que él abrió**. No toca lo que abras a mano ni tus copias de otros traders. Con `capital_maximo_bot` puedes limitar cuánto del capital usa.

## Dejarlo corriendo

```bash
python -m bot_trader correr --cada-minutos 60
```

Corre un ciclo cada hora. Con velas diarias no hace falta más seguido: el stop y el objetivo ya están puestos en eToro. También puedes usar cron:

```
# lunes a viernes, cada hora de 9 a 16 (hora del servidor)
0 9-16 * * 1-5  cd /ruta/trader && .venv/bin/python -m bot_trader ciclo >> bot.log 2>&1
```

**Avisos por Telegram:**

1. Crea un bot con @BotFather.
2. Pon `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en el `.env`.

Te avisa cuando sugiere, ejecuta, cierra o se activa un freno.

**Botones de pánico:**

- `python -m bot_trader pausar`: no abre nada nuevo.
- `python -m bot_trader cerrar-todo --si`: cierra todas las posiciones del bot y lo pausa.
- `python -m bot_trader reanudar`: vuelve a operar.

## Pasar a dinero REAL (solo después de semanas en demo)

Lista de control:

- [ ] El backtest de tus activos tiene ventaja (no solo "gana": que gane con caídas aceptables).
- [ ] Corrió al menos 4–8 semanas en demo y los resultados se parecen al backtest.
- [ ] Entiendes por qué hizo cada operación (`diario`).

Para activarlo:

1. Crea una llave nueva con entorno **Real** y ponla en el `.env`.
2. Agrega `ETORO_CONFIRMO_DINERO_REAL=si` al `.env`. Sin esto el bot se niega a usar la cuenta real.
3. Pon `cuenta = "real"` y empieza con `modo = "sugerir"`.
4. Revisa el tope por orden `monto_maximo_real` (250 USD por defecto) y considera `capital_maximo_bot`.

Antes de empezar, toma en cuenta:

- **Costos de eToro.** Revisa la tabla de comisiones de eToro para tu país y ajusta `costo_por_lado`. Ese valor es lo que el bot descuenta en cada entrada y salida, en backtest y en papel. La cripto suele costar bastante más que las acciones (~1 % por lado). Las ventas en corto y el apalancamiento son CFDs con costo nocturno.
- **Impuestos.** Las ganancias pueden causar impuestos en tu país; la bitácora te sirve de registro.

## Costos de operar el bot

| Pieza | Costo |
|---|---|
| Precios de eToro o Yahoo, noticias RSS | gratis |
| Noticias con Claude | una llamada para todos los activos, reusada durante `refrescar_horas` (4 h), con esfuerzo bajo. Son unas pocas llamadas al día: del orden de unos dólares al mes con 6 activos. El modelo se cambia en `[noticias] modelo` |
| Servidor | el mismo de US$5 que el bot de citas alcanza (o tu computadora) |

## Limitaciones (para no engañarse)

- **Las rutas de eToro no se probaron en vivo.** Se tomaron de la documentación pública de eToro y de un cliente de código abierto que las verificó contra la API real. Las pruebas automáticas usan un servidor falso. Corre `verificar-etoro` y prueba en demo antes que nada. Si eToro cambia una ruta, todas están juntas en `bot_trader/etoro.py` (`PATHS`).
- **Historia limitada:** eToro entrega como máximo 1000 velas (~4 años en diario). Yahoo da 5 años.
- **El backtest no incluye noticias**, porque no hay titulares históricos confiables: mide solo la parte técnica.
- **Yahoo Finance no es una API oficial**, así que puede fallar o cambiar. Para backtests reproducibles, usa `fuente_precios = "csv"`.
- **El modelo de probabilidad es simple a propósito** (tendencia × RSI). Más complejo suele significar sobreajustado. No optimices parámetros hasta que el backtest se vea perfecto: eso casi siempre falla en vivo.

## Estructura

```
bot_trader/
  analysis/indicators.py   medias, RSI, ATR, MACD, volatilidad (sin mirar el futuro)
  analysis/trend.py        puntaje de tendencia de -1 a +1
  analysis/probability.py  probabilidad y ganancia esperada de casos parecidos
  analysis/sentiment.py    noticias con Claude (salida JSON estructurada) o palabras clave
  strategy.py              decide COMPRAR / CERRAR / MANTENER / ESPERAR y explica por qué
  risk.py                  tamaño de posición y frenos de la cuenta
  brokers.py               cuenta de papel y eToro (demo/real)
  etoro.py                 cliente de la API pública de eToro
  sources.py               precios (eToro, Yahoo, CSV) y noticias (RSS)
  engine.py                el ciclo completo, aprobar sugerencias, cerrar todo
  backtest.py              misma estrategia y riesgo sobre datos históricos
  db.py                    bitácora SQLite
  cli.py                   comandos
config/estrategia.example.toml
tests/
```
