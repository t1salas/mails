# Arquitectura y decisiones

## Objetivo del piloto

El piloto cubre lo siguiente para una barbería:

- Agendar, reprogramar y cancelar citas por WhatsApp.
- Elegir barbero y servicio.
- Recibir un recordatorio antes de la cita.
- Ver la agenda en un panel simple.

La restricción principal es **gastar lo mínimo**. Además, el diseño no debe cerrar el paso a venderlo como suscripción a otras barberías, peluquerías, salones de uñas y spas.

## Stack

| Capa | Elección | Por qué |
|---|---|---|
| Lenguaje | Python 3.11+ | Pedido explícito |
| Web | FastAPI + Uvicorn | Webhook, panel y recordatorios en un solo proceso |
| Base de datos | SQLite (con SQLAlchemy) | Costo cero, es un archivo. Pasar a PostgreSQL es cambiar `DATABASE_URL` |
| Panel | HTML con Jinja2 + CSS propio | Sin Node, sin build y sin JS de terceros. Funciona en el celular |
| WhatsApp | Meta Cloud API directa (httpx) | Sin intermediario que cobre recargo por mensaje |
| Recordatorios | Ciclo asíncrono dentro del proceso | Sin Redis, colas ni cron externo |
| IA | Ninguna en el piloto | Botones y listas cubren el flujo sin costo por mensaje |

Dependencias de Python: fastapi, uvicorn, sqlalchemy, httpx, jinja2, python-multipart, itsdangerous y tzdata. Las contraseñas usan `hashlib.scrypt` de la biblioteca estándar.

## Componentes

```
Cliente (WhatsApp)
      │
Meta Cloud API ── webhook ─► app/whatsapp/webhook.py   firma HMAC, deduplicación, 200 inmediato
      ▲                            │
      │                     app/bot/engine.py           máquina de estados (botones/listas)
      │                            │
      │                     app/domain/booking.py       ÚNICA puerta para crear/mover/cancelar citas
      │                            │
      └── app/whatsapp/client.py ◄─┤ (envío después del commit)
                                   │
Panel web ─────────────► app/panel/routes.py ──► app/domain/booking.py
                                   │
Cada minuto ───────────► app/reminders.py ──► client.py
                                   │
                                SQLite
```

### Principios

1. **Varios negocios desde el día 1.** Todo cuelga de `businesses`. El `phone_number_id` de cada número de WhatsApp enruta los mensajes a su negocio.
2. **Las reglas viven en un solo lugar.** El bot y el panel llaman a `app/domain/booking.py`. Para agregar otro canal (Instagram, widget web, IA) basta con otro "frontend" de esas mismas funciones.
3. **Nunca hay doble reserva.** En SQLite cada transacción abre con `BEGIN IMMEDIATE`, así que dos reservas no se pueden intercalar. En PostgreSQL la reserva bloquea la fila del profesional (`SELECT … FOR UPDATE`) antes de verificar choques. Hay una prueba con 6 reservas simultáneas del mismo horario y solo una gana.
4. **Primero se guarda, después se avisa.** El bot devuelve los mensajes, se hace commit y recién entonces se envían. Nunca se confirma algo que no quedó guardado.
5. **Vocabulario genérico.** Internamente se habla de "staff" y "service". El cliente ve `staff_label` ("barbero", "estilista"…), configurable por negocio.

## Flujo del bot

```
menú ─┬─ Agendar ─► servicio ─► barbero* ─► día ─► franja** ─► hora ─► confirmar
      ├─ Mis citas ─► cita ─┬─ Reprogramar ─► día ─► franja** ─► hora ─► confirmar
      │                     └─ Cancelar ─► ¿seguro?
      └─ Ubicación y precios
```

\* El paso de barbero se salta si solo uno hace ese servicio. Incluye la opción "El primero disponible", que reparte la carga.

\** Si el día tiene más de 10 horarios se pregunta primero mañana, tarde o noche. Las listas de WhatsApp admiten como máximo 10 filas, así que el resto se pagina con "Ver más…".

Detalles:

- **Ids de los botones.** Cada botón lleva su acción (`svc:3`, `day:2026-11-12`, `rem:ok:15`). Las acciones de un paso solo valen si la conversación está en ese paso. Un botón viejo recibe la respuesta "esa opción ya no está disponible".
- **Texto libre.** Se reconocen palabras clave: "cancelar", "cambiar", "mis citas", "cita", "precio", "hola"… Si el cliente escribe algo en medio de un flujo, el bot repite el paso actual.
- **Audios e imágenes.** El bot responde con amabilidad que por ahora solo entiende texto y botones, y muestra el menú.
- **Expiración.** Un flujo a medias expira a las 2 h.

## Datos

| Tabla | Para qué |
|---|---|
| `businesses` | Negocio: zona horaria, número de WhatsApp y reglas (intervalo, anticipación, días, límite de cambios, recordatorio) |
| `staff`, `services`, `staff_services` | Quién hace qué |
| `working_hours` | Tramos semanales; puede haber varios por día (comida) |
| `time_off` | Bloqueos: vacaciones, salidas |
| `customers` | Teléfono (wa_id), nombre y último mensaje recibido (ventana de 24 h) |
| `appointments` | Citas, en UTC; estado, origen y control del recordatorio |
| `conversations` | Estado del flujo por cliente |
| `message_log` | Bitácora; `wa_message_id` único evita procesar dos veces los reintentos de Meta |
| `users` | Panel: `owner` ve todo; `staff` solo su agenda |

## Costos del piloto

| Concepto | Costo |
|---|---|
| Servidor | US$0 en una VM gratuita (Oracle Cloud o Google Cloud) o unos US$4–6 al mes en un VPS |
| Dominio | US$0 con un subdominio de duckdns.org, o unos US$10 al año uno propio |
| Base de datos | US$0 (SQLite en el mismo servidor) |
| IA | US$0 (no se usa) |
| Mensajes que responden al cliente (agendar, cambiar, cancelar) | US$0: Meta no cobra las respuestas dentro de las 24 h siguientes a un mensaje del cliente |
| Recordatorio si el cliente escribió en las últimas ~23.5 h | US$0: se manda como mensaje normal con botones |
| Recordatorio fuera de esa ventana | Plantilla "utility" que Meta cobra por mensaje según el país. Revisa la tarifa vigente de tu país en la página de precios de WhatsApp Business |
| Avisos al cliente cuando la barbería cancela desde el panel | US$0. Dentro de la ventana se envían solos; fuera de ella el panel da un link `wa.me` para escribirle desde el teléfono de la barbería |

## Límites conocidos (decididos para el piloto)

- **Un solo proceso.** Los recordatorios viven dentro del proceso, así que se corre con un solo worker. Sobra para cientos de citas al día.
- **Configuración por archivo.** Servicios, barberos y horarios se editan en un TOML y se cargan con `python -m app.cli cargar-config`. Todavía no hay pantallas para eso.
- **Sin migraciones automáticas.** Las tablas se crean al arrancar. Antes de cambiar el esquema con datos reales hay que agregar Alembic.
- **Un solo recordatorio por cita** (por defecto 24 h antes). Si la cita se agenda con menos anticipación, no se manda.
- **Sin bandeja de conversaciones.** Las preguntas que el bot no resuelve se dirigen al teléfono del negocio.

## Camino a SaaS (después del piloto)

1. Pantallas de configuración: servicios, barberos, horarios y textos.
2. Alta autoservicio de negocios con *Embedded Signup* de Meta, como Tech Provider. Así cada barbería conecta su propio número, incluso el que ya usa, mediante "coexistencia". El token pasa a guardarse por negocio.
3. PostgreSQL y Alembic. Restricción de exclusión `tstzrange` como segunda red contra choques.
4. Cobro de la suscripción, métricas (no-shows, ocupación) y lista de espera.
5. IA opcional para entender texto libre ("mañana a las 5 con Juan"), siempre pasando por `booking.py`.
