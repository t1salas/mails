# Agente trader para eToro

Agente que opera solo y busca **ganancias sostenibles en el tiempo**, no las más rápidas. Sobre una lista diversificada de activos:

- **Analiza la tendencia** con medias móviles, momentum de 1 a 12 meses, MACD, RSI y volatilidad.
- **Predice la probabilidad de ganar** con dos modelos independientes. Esa probabilidad se **calibra** con el historial real de aciertos del propio agente, y se mide qué tan buena es.
- **Lee noticias** (Google News y Yahoo Finance) con Claude o, sin IA, con palabras clave.
- **Administra el riesgo como un gestor de capital:**
  - diversifica y evita correlaciones;
  - limita cada posición según su volatilidad;
  - usa Kelly fraccional para no arriesgar de más;
  - arriesga menos cuando la cuenta cae o el mercado se pone bajista.
- **Opera solo** en eToro (demo o real): decide una vez al día y vigila entre decisiones. Si deja de acertar lo que prometía, **se pausa solo**.
- **Se ve en eToro y se maneja desde Telegram:** sus operaciones aparecen en tu cuenta, y por Telegram te avisa y recibe tus órdenes (pausar, cerrar, aprobar).
- **Puede ganar también a la baja** con ventas en corto. Es opcional y viene apagado; más abajo se explica por qué y cómo decidir.

> **Aviso.** Esto es una herramienta, no asesoría financiera. Ningún sistema gana siempre: los mercados cambian y el rendimiento pasado no garantiza el futuro. Empieza en papel, sigue en demo y pasa a dinero real solo con montos que puedas perder.

## La filosofía: sostenible antes que rápido

La ganancia más rápida casi siempre viene con caídas que te sacan del juego. Una caída de 50 % necesita una subida de 100 % solo para volver al punto de partida. Por eso el agente optimiza **rendimiento por unidad de riesgo** (Sharpe, Calmar) y **consistencia**, no la ganancia máxima.

| Principio | Cómo lo aplica el agente |
|---|---|
| **Sobrevivir primero** | Arriesga ~0.75 % del capital por operación. Con −3 % en el día no abre más; con −15 % desde el máximo se pausa. |
| **Cortar pérdidas, dejar correr ganancias** | Usa un stop móvil a 3 ATR que sube detrás del precio. Gana menos veces (~35–45 %), pero las ganancias grandes pagan muchas pérdidas chicas. |
| **Diversificar de verdad** | La lista incluye acciones de EE. UU. y del mundo, oro, bonos, energía y cripto. No compra algo que se mueve casi igual que lo que ya tiene. |
| **Cada posición aporta un riesgo parecido** | Lo más volátil (cripto, por ejemplo) recibe montos más chicos que lo estable (bonos, índices). |
| **No apostar de más aunque haya ventaja** | Nunca arriesga más de 1/4 de lo que justificaría el criterio de Kelly. |
| **Protegerse en mal mercado** | Si el S&P 500 está bajista, arriesga la mitad en lo que se mueve con el mercado. En crisis no compra eso (el oro o los bonos sí pueden seguir). |
| **Operar poco** | Decide una vez al día, después del cierre de Wall Street. Menos ruido, menos costos, menos errores. |
| **Medirse y corregirse** | Calibra sus probabilidades con su historial real y se pausa si sus operaciones ganan mucho menos de lo prometido. |

## Cómo predice (y cómo sabe si predice bien)

Para cada activo y cada día, el agente responde: *"si entro hoy con este stop móvil, ¿qué probabilidad hay de terminar ganando y cuánto gano o pierdo en promedio?"*. Para eso usa:

1. **Casos parecidos.** Simula la misma operación en cada día pasado del activo y cuenta qué pasó en las situaciones parecidas a la de hoy (misma fuerza de tendencia y RSI similar). Con pocos casos, la estimación se acerca al promedio del activo.
2. **Modelo estadístico.** Una regresión logística entrenada con la historia de **todos** los activos vigilados, lo que le da miles de ejemplos. Usa 10 variables medidas contra la volatilidad de cada activo:
   - momentum de 1, 3, 6 y 12 meses;
   - distancia al máximo de 52 semanas;
   - régimen de volatilidad;
   - estado del mercado general, y otras.

   Se eligió un modelo simple y regularizado a propósito: con datos de mercado, los modelos complejos memorizan el pasado y fallan en vivo.
3. **Si los dos modelos no coinciden** (difieren más de 15 puntos), no hay confianza y no entra.
4. **Calibración.** El promedio de los dos modelos se corrige con su historial de aciertos *fuera de muestra*. Si cuando decía "60 %" en realidad ganaba el 50 %, ahora dice 50 %. Si los modelos no saben nada, la probabilidad se queda en el promedio y el agente no se engaña creyendo tener ventaja.

**Regla de oro: nunca mira el futuro.** En cada fecha solo usa operaciones que ya habían terminado, y el modelo se reentrena "hacia adelante".

El backtest muestra la **calidad de las predicciones** con el puntaje de Brier contra la tasa base. Positivo significa que predice mejor que suponer siempre el promedio; en mercados, +1 % a +5 % ya es ventaja real. También muestra una tabla de **calibración** con lo que predijo y lo que pasó.

### Cómo leer `p=38% E=+0.26R n=165`

| Número | Qué significa |
|---|---|
| **n=165** | Hubo 165 situaciones parecidas en la historia del activo. |
| **p=38%** | Probabilidad calibrada de que la operación termine en ganancia, ya con costos. |
| **E=+0.26R** | Ganancia esperada: si arriesgas 100 USD por operación, son +26 USD en promedio por operación, *si el futuro se parece al pasado*. Sale de `p × (lo que gana cuando gana) − (1 − p) × (lo que pierde cuando pierde)`. |

Con stop móvil es normal ganar el 35–45 % de las veces y aun así tener ventaja: cuando gana, gana 2–3 veces lo arriesgado.

## Probarlo en tu computadora (cuenta de papel)

```bash
cd trader
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # o ".[ia]" si solo quieres usarlo con Claude

cp config/estrategia.example.toml config/estrategia.toml   # ajusta activos y riesgo
cp .env.example .env                                         # llaves (opcionales para papel)

python -m bot_trader backtest --robustez   # ¿es sostenible? ¿se rompe si cambian los parámetros?
python -m bot_trader analizar              # qué ve hoy en cada activo y por qué (no opera)
python -m bot_trader ciclo                 # decide ahora (en papel)
python -m bot_trader estado                # capital, posiciones, stops móviles y frenos
python -m bot_trader diario                # bitácora de señales, órdenes y cierres
```

- **Sin llaves de eToro:** los precios salen de Yahoo Finance y las órdenes se simulan en la cuenta de papel.
- **Sin `ANTHROPIC_API_KEY`:** las noticias se leen con palabras clave.

Pruebas automáticas: `python -m pytest`.

### Qué mirar en el backtest

- **Calmar** (rendimiento anual ÷ peor caída) y **Sharpe**, comparados con comprar y mantener. Es la medida de sostenibilidad.
- **Meses positivos**, **años positivos** y el **mayor tiempo bajo el máximo**. Un sistema sostenible no pasa años sin recuperarse.
- **Calidad de las predicciones:** mejora sobre la tasa base y tabla de calibración.
- **`--robustez`:** repite todo con stops, umbrales, medias y costos distintos. Si la mayoría de variantes pierde, la estrategia está sobreajustada al pasado: no la uses.

## Ganar también a la baja (ventas en corto)

En eToro no solo se gana cuando el precio sube: también se puede **vender en corto** y ganar cuando baja. El agente sabe hacerlo, pero viene **apagado** (`permitir_cortos = false`), porque en las pruebas no mejoró el resultado.

**Qué hace el agente cuando el mercado cae, sin cortos:**

- deja de comprar lo que se mueve con el mercado y se queda en efectivo;
- puede comprar lo que suele subir cuando las acciones caen (bonos, oro), que ya están en la lista.

**Qué agrega con los cortos encendidos.** Vende en corto un activo cuando su tendencia es bajista y la historia de *ese* activo muestra que esa operación tuvo ventaja. Usa los mismos dos modelos, con probabilidades calculadas y calibradas aparte para el lado corto. Reglas propias:

| Regla | Por qué |
|---|---|
| Nunca va en corto contra un mercado alcista (si el activo se mueve con el mercado) | Ir contra la corriente es la forma más común de perder en corto. |
| Un corto arriesga la mitad que una compra (`multiplicador_cortos`) | La pérdida en corto no tiene techo: el precio puede subir sin límite. |
| Máximo 30 % del capital en cortos (`max_exposicion_cortos`) | Limita el daño de un rebote general del mercado. |
| Stop móvil siempre, por encima del precio | Corta la pérdida si el precio rebota y asegura ganancia si sigue cayendo. |
| Descuenta el costo por noche (`costo_anual_cfd`) | Un corto en eToro es un CFD y cobra por cada noche abierta. |
| Una noticia buena con evento de riesgo (oferta de compra, resultados) veta el corto | Son los saltos al alza que más daño hacen. |
| Si eToro no permite el corto en ese activo, no lo intenta | Depende del activo y del país de tu cuenta. |

Además, vender en corto algo parecido a lo que ya tienes comprado se trata como **cobertura**, no como apuesta duplicada.

**Por qué es más difícil ganar a la baja que al alza:**

- Los mercados pasan más tiempo subiendo que bajando, y las caídas vienen con rebotes violentos.
- Cada noche abierta cuesta dinero, así que el tiempo juega en contra.
- El stop no protege de un hueco: si el precio abre muy arriba, se pierde más de lo previsto.
- El agente solo va en corto cuando ya vio caídas parecidas en la historia del activo. Con 4–5 años de datos mayormente alcistas, tomará pocos cortos.

**Resultado en las pruebas** (datos inventados; solo muestran el comportamiento):

| Escenario | Solo compras | Con ventas en corto |
|---|---|---|
| Dos caídas largas del mercado | +1.2 % anual, peor caída 2.8 %, Sharpe 0.56 | +0.5 % anual, peor caída 3.9 %, Sharpe 0.21 |
| Mercado mixto | +4.3 % anual, peor caída 4.2 %, Sharpe 1.04 | igual: no encontró ningún corto con ventaja |

En el escenario de caídas tomó 16 cortos, ganó el 19 % y perdieron 183 USD en total: entró tarde y los rebotes lo sacaron. Con tus datos reales puede ser distinto, y por eso se mide en vez de suponerse.

**Cómo decidir con tus datos:**

1. Corre `python -m bot_trader backtest --robustez` con los cortos apagados. La fila **"con ventas en corto"** muestra qué pasaría al encenderlos.
2. Si mejora el Calmar y el Sharpe, pon `permitir_cortos = true`. El reporte separa **Compras** y **Ventas en corto**, y el veredicto dice sin rodeos si los cortos sumaron o restaron.
3. Corre `python -m bot_trader verificar-etoro`. Por cada activo dice si eToro permite venderlo en corto y qué costos cobra; ajusta `costo_anual_cfd` con eso.
4. Pruébalo en demo antes que en real, como todo lo demás.

## Pasar a la cuenta DEMO de eToro (dinero virtual)

1. En eToro entra a **Configuración → API** (o a api-portal.etoro.com) y crea una llave:
   - entorno **Virtual/Demo**;
   - permisos de **lectura y escritura**.

   Copia la *API key* y la *user key* al `.env`.
2. Verifica la conexión. Este comando solo lee: revisa las llaves, que cada símbolo exista en eToro, si permite stop móvil y venta en corto, y la cartera.
   ```bash
   python -m bot_trader verificar-etoro
   python -m bot_trader buscar GLD          # si un símbolo no aparece, busca cómo se llama en eToro
   ```
   Si el símbolo es distinto, usa `simbolo_etoro = "..."` en ese `[[activos]]`.
3. En `config/estrategia.toml` pon `cuenta = "demo"` y deja al agente operar varias semanas. Compara con lo que prometía el backtest.

El agente **solo administra las posiciones que él abrió**. No toca lo que abras a mano ni tus copias de otros traders. Con `capital_maximo_bot` puedes limitar cuánto del capital usa.

## Dejar el agente corriendo 24/7

```bash
python -m bot_trader correr --cada-minutos 30
```

- **Cada 30 minutos vigila:** detecta stops ejecutados por eToro, revisa los frenos y avisa si pasó algo.
- **Una vez al día** (desde `hora_decision_utc`, por defecto 21:00 UTC, después del cierre de Wall Street) **decide**: cierra lo que rompió la tendencia, abre lo que tiene ventaja y manda un resumen.
- Los stops móviles quedan puestos **en eToro**, que los va moviendo aunque el agente esté apagado.

En un servidor (el mismo de US$5 del bot de citas alcanza), como servicio que se reinicia solo:

```bash
sudo useradd -r -m -d /opt/trader trader
sudo cp -r . /opt/trader && sudo chown -R trader: /opt/trader
sudo -u trader bash -c 'cd /opt/trader && python3 -m venv .venv && .venv/bin/pip install -e ".[ia]"'
sudo cp deploy/bot-trader.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now bot-trader
journalctl -u bot-trader -f                         # ver qué hace
sudo crontab -u trader deploy/respaldo.cron         # respaldo diario (requiere: sudo apt install sqlite3)
```

**Pausas automáticas** (se reanudan solo a mano, después de revisar):

- **Drawdown:** la cuenta cayó más de 15 % desde su máximo.
- **Desgaste del modelo:** en sus últimas operaciones ganó muchas menos veces de las que sus probabilidades prometían. La diferencia tiene que ser estadísticamente fuerte (z < −2, con al menos 15 operaciones). Señal de que el mercado cambió.

## Verlo en eToro y manejarlo desde Telegram

Una vez instalado, el día a día es así:

- **El agente vive en el servidor**, encendido las 24 horas. Tu computadora puede estar apagada.
- **Sus operaciones aparecen en tu cuenta de eToro** (app o web) como cualquier otra posición, con su stop puesto. Las abre en tu cuenta con tus llaves de API. También puedes cerrar una a mano desde eToro: el agente lo detecta en su siguiente ronda y lo anota.
- **Telegram es tu control remoto:** ahí te avisa y ahí le das órdenes.

**Configurar Telegram (5 minutos):**

1. En Telegram, háblale a @BotFather, envía `/newbot` y copia el token.
2. Ábrele chat a tu bot nuevo y envíale `/start` (un bot no puede escribirte primero).
3. Háblale a @userinfobot: el número "Id" que te contesta es tu chat id.
4. Pon `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en el `.env`.
5. Corre `python -m bot_trader probar-telegram`: debe llegarte un mensaje.

**Qué te avisa solo:**

- un resumen después de cada decisión diaria;
- cuando abre o cierra una posición, falla algo o se pausa;
- cuando eToro ejecuta un stop: lo detecta en su siguiente ronda de vigilancia (cada 30 minutos);
- "Agente en línea" cada vez que arranca.

**Qué le puedes pedir** (mientras corre `correr`; contesta en segundos):

| Comando | Qué hace |
|---|---|
| `/estado` | Capital, posiciones con su stop y frenos de riesgo. |
| `/analisis` | Qué ve hoy en cada activo. Solo consulta, no opera. |
| `/diario` | Últimas señales, órdenes y cierres. |
| `/pausar` | Deja de abrir posiciones nuevas. Los stops siguen puestos. |
| `/reanudar` | Vuelve a operar. |
| `/cerrar SPY` | Cierra esa posición. Pide confirmación. |
| `/cerrar_todo` | Cierra todas las posiciones del bot y lo pausa. Pide confirmación. |
| `/aprobar 12` | Ejecuta la sugerencia #12 (con `modo = "sugerir"`). |
| `/ayuda` | Lista de comandos. |

**Seguridad del control remoto:**

- Solo obedece al chat de `TELEGRAM_CHAT_ID`. Mensajes de cualquier otra persona se ignoran.
- Cerrar posiciones pide un código de un solo uso que vence en 2 minutos (`/confirmar 4821`).
- Desde Telegram **no** se puede cambiar la configuración: ni los límites de riesgo, ni pasar de demo a real, ni la lista de activos. Eso solo se cambia en el servidor. Si alguien entrara a tu Telegram, lo más que podría hacer es pausar el agente o cerrar posiciones.
- Un comando que llegó mientras el agente estaba apagado (más de 10 minutos) no se ejecuta; te avisa para que lo repitas.
- Activa la verificación en dos pasos de Telegram: tu cuenta de Telegram pasa a ser una llave del agente.

Los mismos controles existen en el servidor, por si Telegram falla:

| Comando | Qué hace |
|---|---|
| `python -m bot_trader pausar` | Deja de abrir posiciones nuevas. |
| `python -m bot_trader cerrar-todo --si` | Cierra todas las posiciones del bot y lo pausa. |
| `python -m bot_trader reanudar` | Vuelve a operar. |

Si prefieres aprobar cada operación, usa `modo = "sugerir"`: el agente te manda la sugerencia con su número y la ejecutas con `/aprobar <número>`. Antes de ejecutar, vuelve a validar la señal con datos frescos.

## Pasar a dinero REAL (solo después de semanas en demo)

Lista de control:

- [ ] `backtest --robustez` sale robusto o aceptable, con Calmar/Sharpe mejor que comprar y mantener.
- [ ] La calidad de las predicciones es positiva y la tabla de calibración es razonable.
- [ ] Corrió 4–8 semanas en demo sin pausas por desgaste y con resultados parecidos al backtest.
- [ ] Entiendes por qué hizo cada operación (`diario`).

Para activarlo:

1. Crea una llave con entorno **Real**.
2. Agrega `ETORO_CONFIRMO_DINERO_REAL=si` al `.env`. Sin esto el agente se niega a usar la cuenta real.
3. Pon `cuenta = "real"`.
4. Empieza con `capital_maximo_bot` chico. El tope por orden es `monto_maximo_real` (250 USD por defecto).

Antes de empezar, toma en cuenta:

- **Costos de eToro.** Revisa la tabla de comisiones de eToro para tu país y ajusta `costo_por_lado`. Ese valor es lo que el agente descuenta en cada entrada y salida, en backtest y en papel. La cripto suele costar bastante más que las acciones (~1 % por lado). Las ventas en corto y el apalancamiento son CFDs con costo por noche (`costo_anual_cfd`): vienen apagados.
- **Impuestos.** Las ganancias pueden causar impuestos en tu país; la bitácora te sirve de registro.

## Costos de operar el agente

| Pieza | Costo |
|---|---|
| Precios de eToro o Yahoo, noticias RSS | gratis |
| Noticias con Claude | una llamada para todos los activos, reusada durante `refrescar_horas` (4 h), con esfuerzo bajo. Del orden de unos dólares al mes. El modelo se cambia en `[noticias] modelo` |
| Servidor | US$5/mes o tu computadora |

## Limitaciones (para no engañarse)

- **El control por Telegram tampoco se probó contra Telegram real**, solo con un servidor falso. `probar-telegram` es la primera comprobación.
- **Las rutas de eToro no se probaron en vivo.** Se tomaron de la documentación pública de eToro y de un cliente de código abierto que las verificó contra la API real; eso incluye el stop móvil (`stopLossType: "trailing"`). Las pruebas automáticas usan un servidor falso. Corre `verificar-etoro` y prueba en demo antes que nada. Si eToro cambia algo, todas las rutas están en `bot_trader/etoro.py`.
- **La orden de venta en corto tampoco se probó en vivo.** Se envía como CFD con `sellShort`, según el mismo cliente de código abierto. Si eToro la rechaza, el agente lo reporta como orden fallida y no insiste.
- **Los cortos no incluyen dividendos.** Quien está en corto paga el dividendo; el backtest no lo descuenta.
- **El desarrollo se validó con datos sintéticos.** No había acceso a datos reales de mercado desde el entorno de desarrollo. La evaluación que cuenta es la que corras tú con `backtest --robustez` sobre datos reales.
- **Historia limitada:** eToro entrega como máximo 1000 velas (~4 años en diario); Yahoo da 5 años. Más historia hace las estadísticas más confiables.
- **El backtest no incluye noticias:** no hay titulares históricos confiables.
- **El stop móvil en el backtest es una aproximación.** Se mueve con el máximo de cada vela diaria; eToro lo mueve en tiempo real, así que en vivo puede salir un poco antes o después.
- **Yahoo Finance no es una API oficial.** Para backtests reproducibles usa `fuente_precios = "csv"`.
- **No optimices parámetros hasta que el backtest se vea perfecto.** Eso casi siempre falla en vivo. `--robustez` existe para detectarlo.

## Estructura

```
bot_trader/
  analysis/indicators.py   medias, RSI, ATR, MACD, volatilidad (sin mirar el futuro)
  analysis/trend.py        puntaje de tendencia de -1 a +1
  analysis/features.py     10 variables medidas contra la volatilidad para el modelo
  analysis/probability.py  simulación de operaciones con stop móvil y "casos parecidos"
  analysis/model.py        regresión logística regularizada (entrenamiento hacia adelante)
  analysis/analyst.py      combina modelos, calibra, régimen de mercado y correlaciones
  analysis/sentiment.py    noticias con Claude (salida JSON estructurada) o palabras clave
  strategy.py              decide COMPRAR / VENDER EN CORTO / CERRAR / MANTENER / ESPERAR y explica por qué
  risk.py                  tamaño con volatilidad, correlación, Kelly, drawdown y régimen; frenos
  brokers.py               cuenta de papel y eToro (demo/real), con stop móvil
  etoro.py                 cliente de la API pública de eToro
  sources.py               precios (eToro, Yahoo, CSV) y noticias (RSS)
  engine.py                el agente: vigilar, decidir una vez al día, desgaste del modelo
  remote.py                control remoto por Telegram (comandos con confirmación)
  notify.py                avisos por Telegram
  backtest.py              backtest con métricas de sostenibilidad, calidad de predicción y robustez
  db.py                    bitácora SQLite
  cli.py                   comandos
config/estrategia.example.toml   perfil sostenible con una lista diversificada
deploy/                          servicio systemd y respaldo diario
tests/
```
