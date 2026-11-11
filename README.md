# Citas por WhatsApp — piloto

Bot de citas por WhatsApp para barberías (y después peluquerías, uñas y spa), con un panel web para que el negocio vea su agenda.

**Qué hace el piloto**

- El cliente **agenda** por WhatsApp eligiendo servicio, barbero (o "el primero disponible"), día y hora con botones y listas.
- El cliente puede **reprogramar** y **cancelar** desde "Mis citas", con un límite de anticipación configurable.
- **Recordatorio** antes de la cita, con botones *Confirmo*, *Reprogramar* y *Cancelar*.
- **Panel web** pensado para el celular, con:
  - agenda del día por barbero y vista de la semana;
  - citas manuales para quien llega sin cita o llama por teléfono;
  - bloqueos de horario;
  - marcar una cita como atendida o como "no vino".
- La base de datos garantiza que **no haya dobles reservas**, aunque dos clientes confirmen al mismo tiempo.
- Puede atender **varios negocios** desde el día 1: cada número de WhatsApp apunta a su negocio.

Está diseñado para gastar lo mínimo:

- No usa IA.
- La base de datos es SQLite, un archivo, sin servidor de base de datos.
- Todo corre en un solo proceso que cabe en un servidor gratuito.
- Solo se paga a Meta por los recordatorios que salen fuera de la ventana gratuita de 24 h.

Detalles en [docs/arquitectura.md](docs/arquitectura.md).

## Probarlo en tu computadora (5 minutos)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp config/negocio.example.toml config/negocio.toml     # ajusta servicios, barberos y horarios
python -m app.cli cargar-config config/negocio.toml

# Conversar con el bot en la terminal (sin WhatsApp):
python -m app.cli simular --negocio barberia-piloto
#   escribe "hola" y responde con el número de cada opción; /recordatorio simula el aviso; /salir

# Panel web:
python -m app.cli crear-usuario --negocio barberia-piloto --usuario dueno
uvicorn app.main:app --reload        # abre http://localhost:8000
```

Pruebas automáticas: `python -m pytest`.

## Ponerlo en marcha con WhatsApp real

Sigue [docs/puesta-en-marcha.md](docs/puesta-en-marcha.md). Cubre:

- la configuración en Meta (número de prueba, webhook, token y plantilla del recordatorio);
- el servidor gratuito o de unos US$5 con HTTPS;
- los respaldos.

## Estructura

```
app/
  domain/         reglas de agenda: disponibilidad, reservar, reprogramar, cancelar
  bot/            conversación de WhatsApp (máquina de estados con botones y listas)
  whatsapp/       webhook de Meta, mensajes y envío por la Cloud API
  panel/          panel web (HTML renderizado en el servidor, sin build de frontend)
  reminders.py    recordatorios (corren dentro del mismo proceso, cada minuto)
  cli.py          comandos: cargar-config, crear-usuario, simular, recordatorios, respaldo
config/           configuración de ejemplo de un negocio (TOML)
deploy/           systemd, Caddy (HTTPS) y cron de respaldos
tests/            pruebas (agenda, bot, webhook, recordatorios, panel)
```
