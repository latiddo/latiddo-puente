# Puente de Latiddo para Claude Code

Lleva tu uso de Claude y el estado de Claude Code a tu robot Latiddo (o a tu Latiddo Pulse) por la red de tu casa. Versión 1.3.3, la misma que se descarga desde releases.latiddo.com.

## Qué hace

- Cada 2 minutos (tarea programada) ejecuta tu propio `claude -p /usage` y lee de su texto el uso de la sesión (5 h) y el semanal, y cuándo se reinician.
- Si lo eliges al instalar, añade hooks a Claude Code: el robot se entera de cuándo Claude trabaja, te necesita o ha terminado.
- También puede hacer de barra de estado de Claude Code (`--with-statusline`), opcional.
- Busca el robot solo al instalarlo, en tu red local (mDNS y el puerto 80 de tu red /24), y guarda su dirección.
- Página de ajustes (`latiddo_usage_poll.py --settings`): se abre en tu navegador en `http://127.0.0.1:8737`, solo en tu ordenador y con una clave de un solo uso.
- Al instalarlo añade el acceso directo «Latiddo · Ajustes del puente», que abre esa página: en el menú Inicio (Windows), en `~/Applications` (macOS) o en el menú de aplicaciones (Linux).

## Qué lee

- La salida de texto de `claude -p /usage`: porcentajes y horas de reinicio.
- De cada hook de Claude Code: el nombre del evento y el id de la sesión. El id lo convierte en un resumen corto y nunca lo envía tal cual.
- De `UserPromptSubmit`, solo si el mensaje empieza por `<task-notification>`. Así sabe si el turno lo empezó Claude Code solo, por ejemplo con el aviso de una tarea en segundo plano. El mensaje nunca se guarda, se apunta ni se envía.
- Si usas la barra de estado: los límites de uso que Claude Code le pasa.
- Su configuración: `~/.claude/latiddo.json`.

## A dónde lo envía

- Solo a la dirección de tu robot (o de tu Pulse) que eliges al instalar, en tu red local, por HTTP. Siempre directo, sin pasar por un proxy.
- Al robot le llegan:
  - los porcentajes y las horas de reinicio;
  - el nombre del evento, el estado, el resumen de la sesión y la hora;
  - en el «Listo» de un turno que no empezaste tú, `"quiet": true`. Es solo información (y para el registro): el robot muestra «Listo» y pita como siempre. Al Pulse no le llega;
  - la versión del puente;
  - un resumen de 8 caracteres de tu ordenador, nunca su nombre.
- Nunca tus prompts, tus archivos, las rutas ni lo que hacen las herramientas.

## Registro

`~/.claude/latiddo.log` apunta cada aviso de Claude Code: el resumen de la sesión, el evento, el estado y si el turno lo empezaste tú. Por ejemplo:

```
aviso sesión=77cb47b8 Stop -> done, turno: automático, «Listo» sin pitido
```

En la 1.3.3, «Listo» sin pitido solo quiere decir que ese aviso llevaba `quiet`: el robot pita igual. Ese texto cambiará en la 1.4.

Los `PostToolUse` repetidos de una sesión que ya está trabajando no se apuntan. Nunca apunta tus mensajes.

## Qué no hace

- No envía nada a internet. Solo habla con tu robot o tu Pulse.
- No guarda contraseñas: el instalador nunca escribe ninguna.
- Si tu Pulse tiene contraseña, puedes escribirla tú a mano en `latiddo.json` (`"password"`). Viaja sin cifrar por tu red local, solo al Pulse, nunca al robot.
- No lee tus credenciales ni tus tokens de Claude, y no llama a ninguna API de Anthropic.
- Solo usa la biblioteca estándar de Python. Sin telemetría.

Ten en cuenta:

- `claude -p /usage` es tu Claude Code de siempre: él sí habla con Anthropic, con tu cuenta, como cuando lo usas tú.
- El instalador de una línea descarga los dos scripts de releases.latiddo.com y comprueba su SHA-256.
- Si te falta Python o Claude Code, el instalador ofrece instalarlos (Python con winget en Windows, Claude Code con su instalador oficial) y solo lo hace si respondes que sí.

## Instalar

Necesitas Python 3 y Claude Code con tu sesión iniciada. Pon la dirección de tu robot, la que sale en su portal.

- Windows (PowerShell):
  ```powershell
  & ([scriptblock]::Create((irm https://releases.latiddo.com/pulse/bridge/install.ps1))) --robot latiddo-xxxx.local
  ```
- macOS y Linux:
  ```sh
  curl -fsSL https://releases.latiddo.com/pulse/bridge/install.sh | sh -s -- --robot latiddo-xxxx.local
  ```
- Desde esta copia, sin descargar nada:
  - Windows: `.\install.ps1 -LocalPath . --robot latiddo-xxxx.local`
  - macOS y Linux: `sh install.sh --local . --robot latiddo-xxxx.local`
- Sin argumentos, el instalador pregunta: robot, Pulse o los dos.
- Si tu ordenador no encuentra el nombre `latiddo-xxxx.local`, el instalador busca en tu red el robot con ese nombre y guarda su IP.
- Copia los scripts en `%LOCALAPPDATA%\Latiddo` (Windows) o `~/.local/share/latiddo` (macOS y Linux) crea la tarea que se ejecuta cada 2 minutos y añade el acceso directo a los ajustes.

## Desinstalar

- Windows: `python "$env:LOCALAPPDATA\Latiddo\latiddo_usage_poll.py" --remove`
- macOS y Linux: `python3 ~/.local/share/latiddo/latiddo_usage_poll.py --remove`
- Te enseña lo que va a quitar y te pide confirmación (`--yes` no pregunta):
  - la tarea programada;
  - los hooks y la barra de estado del puente en `~/.claude/settings.json`, con una copia con fecha del archivo antes (tus otros ajustes y hooks no se tocan);
  - el acceso directo «Latiddo · Ajustes del puente»;
  - los scripts y su carpeta;
  - `latiddo.json`, `latiddo_hosts.json` y `latiddo.log` de `~/.claude`;
  - sus archivos temporales, también los restos de escrituras que no terminaron.
- Al terminar dice «No queda nada del puente en este ordenador». Solo quedan las copias de `settings.json`.

## Licencia

[PolyForm Noncommercial 1.0.0](LICENSE): puedes usarlo y modificarlo sin fines comerciales.
