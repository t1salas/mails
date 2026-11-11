# Puesta en marcha

La puesta en marcha tiene cuatro partes:

1. Meta, donde puedes empezar hoy con el número de prueba.
2. El servidor.
3. La plantilla del recordatorio.
4. Pasar al número real de la barbería.

Meta cambia seguido los nombres de sus menús. Si algo no coincide, busca la opción equivalente.

## 1. Meta (WhatsApp Cloud API)

1. Entra a <https://developers.facebook.com>, crea una app y agrégale el producto **WhatsApp**. Elige el caso de uso para conectar con clientes por WhatsApp. Meta crea un portafolio de negocio si no tienes uno.
2. En **WhatsApp → Configuración de la API** (API Setup) vas a encontrar:
   - un **número de prueba**, con el que se puede escribir hasta a 5 números que registres como destinatarios. Agrega tu celular;
   - el **Phone number ID**. Ponlo en `wa_phone_number_id` de `config/negocio.toml` y vuelve a cargar la configuración;
   - un **token temporal**, que dura 24 h. Sirve para probar. Para producción, ver el paso 5.
3. En **Configuración de la app → Básica**, copia el **App secret** a `WA_APP_SECRET`. Con él se verifica que los webhooks vienen de Meta.
4. En **WhatsApp → Configuración**, en la sección del webhook:
   - **URL de devolución de llamada**: `https://TU-DOMINIO/webhook/whatsapp`.
   - **Token de verificación**: el mismo valor que pusiste en `WA_VERIFY_TOKEN`.
   - Suscríbete al campo **messages**.
5. **Token permanente.** Ve a business.facebook.com, luego a Configuración del negocio, luego a Usuarios del sistema. Crea un usuario de sistema, asígnale la app y la cuenta de WhatsApp, y genera un token con los permisos `whatsapp_business_messaging` y `whatsapp_business_management`. Ponlo en `WA_ACCESS_TOKEN`.
6. Conviene iniciar ya la **verificación del negocio** en el portafolio de Meta. Tarda días y sube los límites de envío.

Mientras no haya `WA_ACCESS_TOKEN`, la app no envía nada y solo escribe en el log lo que habría mandado (modo de prueba).

## 2. Servidor (mínimo presupuesto)

Meta exige una URL con **HTTPS**, y los recordatorios necesitan que el proceso esté siempre encendido. Por eso **no conviene un plan gratuito que se "duerme"**: los recordatorios no saldrían y el primer mensaje tardaría casi un minuto en responderse.

Hay dos opciones:

- **US$0**: una VM gratuita permanente. Oracle Cloud "Always Free", o Google Cloud con la e2-micro del nivel gratuito en us-west1, us-central1 o us-east1. Las dos piden tarjeta para registrarse.
- **US$4–6 al mes**: un VPS chico (Hetzner, DigitalOcean, Vultr, etc.). Es más simple y más estable.

Para el dominio sirve uno gratuito de <https://www.duckdns.org> apuntando a la IP del servidor. También puedes comprar uno propio por unos US$10 al año.

En el servidor (Ubuntu/Debian), copia y ejecuta:

```bash
sudo apt update && sudo apt install -y python3-venv git caddy
sudo useradd --system --create-home --home-dir /opt/citas citas
sudo -u citas git clone <URL-del-repo> /opt/citas
cd /opt/citas
sudo -u citas python3 -m venv .venv
sudo -u citas .venv/bin/pip install .
sudo -u citas cp .env.example .env      # y llénalo
sudo -u citas mkdir -p backups

# Datos del negocio y usuario del panel
sudo -u citas cp config/negocio.example.toml config/negocio.toml   # ajústalo
sudo -u citas .venv/bin/python -m app.cli cargar-config config/negocio.toml
sudo -u citas .venv/bin/python -m app.cli crear-usuario --negocio barberia-piloto --usuario dueno

# Servicio, HTTPS y respaldos
sudo cp deploy/citas.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now citas
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # cambia el dominio
sudo systemctl reload caddy
sudo crontab -u citas deploy/respaldo.cron
```

En `.env` de producción hay tres valores que no pueden faltar:

- `SESSION_SECRET` largo y aleatorio;
- `SECURE_COOKIES=1`;
- `WA_APP_SECRET` (sin él, el webhook rechaza todo).

Para revisar que todo funciona:

- `https://TU-DOMINIO/salud` responde `{"ok": true}`.
- Para ver el log: `journalctl -u citas -f`.
- Los respaldos diarios quedan en `/opt/citas/backups` (14 copias). Conviene bajarlos de vez en cuando a otro lado.

**Actualizar:** `cd /opt/citas && sudo -u citas git pull && sudo -u citas .venv/bin/pip install . && sudo systemctl restart citas`.

## 3. Plantilla del recordatorio

Fuera de la ventana de 24 h, WhatsApp solo deja escribirle al cliente con una **plantilla aprobada**. Créala en **WhatsApp Manager → Plantillas de mensajes**:

- **Nombre:** `recordatorio_cita`. Debe coincidir con `reminder_template` en el TOML.
- **Categoría:** Utilidad (Utility).
- **Idioma:** Español (MEX). Debe coincidir con `template_lang = "es_MX"`. Si eliges otro, ajústalo.
- **Cuerpo:**

  ```
  Hola {{1}}, te recordamos tu cita en {{2}}: {{3}} con {{4}} el {{5}}. ¿Nos confirmas tu asistencia?
  ```

  Ejemplos que pide Meta: `Ana`, `Barbería El Piloto`, `Corte clásico`, `Juan`, `jueves 12 de noviembre a las 10:30`.
- **Botones:** tres de respuesta rápida, en este orden: `Confirmo`, `Reprogramar`, `Cancelar`.

El orden de las variables y de los botones importa: la app los manda en ese orden.

Mientras la plantilla no esté aprobada, los recordatorios igual salen gratis a quienes escribieron en las últimas ~23.5 h. Los demás se registran en el log como "no se puede enviar recordatorio".

## 4. Número real de la barbería

Hay dos caminos:

- **Número nuevo solo para citas.** Es lo más rápido. Agrégalo en WhatsApp Manager, verifícalo por SMS o llamada, y espera la aprobación del nombre visible. Ese número no puede estar activo en la app de WhatsApp.
- **El número que ya usa la barbería.** Meta tiene una modalidad de "coexistencia" que permite usar el mismo número en la app de WhatsApp Business y en la API. Su alta va por *Embedded Signup* (como Tech Provider o mediante un proveedor), así que es más trámite. Queda para la etapa SaaS.

Después de cambiar de número, actualiza `wa_phone_number_id` en el TOML, vuelve a cargarlo y vuelve a suscribir el webhook si hace falta. Si Meta lo pide, pasa la app a modo **Live**; para eso solicita una URL de política de privacidad.

## Lista antes de abrir al público

- [ ] Servicios, duraciones, precios y horarios revisados con el dueño.
- [ ] Plantilla `recordatorio_cita` aprobada.
- [ ] Token permanente (no el temporal de 24 h).
- [ ] `SESSION_SECRET`, `SECURE_COOKIES=1` y `WA_APP_SECRET` configurados.
- [ ] Prueba completa desde un celular: agendar, reprogramar, cancelar y recordatorio (`python -m app.cli recordatorios` lo fuerza).
- [ ] Usuarios del panel creados (dueño y, si se quiere, uno por barbero con `--rol staff --profesional "Juan"`).
- [ ] Respaldo diario funcionando (revisa `/opt/citas/backups`).
- [ ] Aviso de privacidad según la ley de datos personales del país.
