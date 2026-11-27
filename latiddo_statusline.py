#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Rafael – Latiddo (https://latiddo.com)
"""
Latiddo Pulse - Claude Code status line bridge.

Claude Code runs this script as its status line command and sends session
data as JSON on stdin. The script reads rate_limits.five_hour and
rate_limits.seven_day from that JSON and POSTs them to
http://<ip>/api/update on a Latiddo Pulse device on your local network, and/or
to http://<robot>/api/claude on a Latiddo robot (same JSON). Each destination is
optional (~/.claude/latiddo.json: "ip" and "pulse_enabled" for the Pulse, "robot"
and "robot_enabled" for the robot); both are sent to at once, so one that does
not answer never delays the other.

It only uses the official status line input. It does NOT read credentials or
tokens, does NOT call any Anthropic API, and only uses the Python standard
library (Windows, macOS, Linux).

It is also the Claude Code hook for the live status on the Pulse and the robot (working /
needs you / done): `--hook` reads the hook JSON on stdin and POSTs only the event name, the
state, a short hash of the session id, the time and the bridge version to
http://<ip>/claude-state (Pulse) and http://<robot>/api/claude-state (robot), at the same time.
Never the prompt, tool input or output, paths or any other content. It is quick (0.6 s to the
Pulse; to the robot 3.5 s and one retry at once, within 8 s, as its Wi-Fi sleeps in standby; the
robot's kept IP), prints nothing and always exits with code 0. The last state of each session is
kept in the temp folder (latiddo_cc), and every run (2 min) the poller sends the robot, and a Pulse
with firmware 3.18.0 or newer, a snapshot of all of them, so each ends what is no longer live.
Every hook to the robot and to the Pulse also carries src, 8 hex of a hash of this PC (never its
name), so a snapshot only ends this PC's sessions (a Pulse before 3.18.0 ignores it).

Usage:
  (status line)  python latiddo_statusline.py           < JSON on stdin
  (diagnostics)  python latiddo_statusline.py --dump    < JSON on stdin
  (hook)         python latiddo_statusline.py --hook    < hook JSON on stdin
  python latiddo_statusline.py --install --ip 192.168.1.50
  python latiddo_statusline.py --uninstall
  python latiddo_statusline.py --test [--ip 192.168.1.50]
  python latiddo_statusline.py --test-hook needs_you|working|done|end   (the notices, without Claude Code)
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Version of the bridge (both scripts and the installers): the ONLY place where it is set.
# Sent in every update so the Pulse can say when a newer one is published (it never updates
# anything by itself); tools/make-release.py copies it to latest.json.
BRIDGE_VERSION = "1.3.3"
SCRIPT_NAME = "latiddo_statusline.py"
DEVICE_IDS = ("latiddo",)  # "device" in /api/status
NET_TIMEOUT = 1.5          # seconds (status line, tests)
POLL_NET_TIMEOUT = 3.0     # seconds for the scheduled poller (in the background; a sleepy ESP32
                           # sometimes takes a couple of seconds to answer)
# A name such as latiddo-xxxx.local is looked up once (Windows can take more than 2 s the first
# time) and its IP is kept in hosts_path(): later runs use it at once, and look the name up
# again only when that IP does not answer
RESOLVE_TIMEOUT = 6.0      # seconds for a lookup (poller, setup, tests)
STATUSLINE_RESOLVE_TIMEOUT = 2.0  # the status line runs in Claude Code: never wait long there
PULSE_PATH = "/api/update"  # Latiddo Pulse
ROBOT_PATH = "/api/claude"  # Latiddo robot: the same JSON as the Pulse
FAIL_LOG_REPEAT_S = 600    # status line: the same failure of a destination, one log line in 10 min
BACKOFF_S = 60             # no retry for 60 s after a failed send
MIN_INTERVAL_S = 60        # resend unchanged data at most every 60 s
LOG_MAX = 50 * 1024        # trim the log above ~50 KB
LOG_KEEP = 35 * 1024       # ...keeping the last ~35 KB
USER_AGENT = "latiddo-statusline/" + BRIDGE_VERSION
PRIVATE_KEYS = {"transcript_path", "cwd", "session_id"}
KEEP_STRING_KEYS = {"version"}


# ----------------------------------------------------------------------------
# Paths (computed on each call so HOME / USERPROFILE / TMP are honoured)
# ----------------------------------------------------------------------------
def claude_dir():
    return Path.home() / ".claude"


def config_path():
    return claude_dir() / "latiddo.json"


def log_path():
    return claude_dir() / "latiddo.log"


def settings_path():
    return claude_dir() / "settings.json"


def state_path():
    return Path(tempfile.gettempdir()) / "latiddo_state.json"


def dump_path():
    return Path(tempfile.gettempdir()) / "latiddo_last_input.json"


def hosts_path():
    """The IP of each name (latiddo-xxxx.local), kept between runs."""
    return claude_dir() / "latiddo_hosts.json"


def sends_path():
    """Last send to each destination (poller and status line), for the settings page."""
    return Path(tempfile.gettempdir()) / "latiddo_sends.json"


# ----------------------------------------------------------------------------
# Language: English or Spanish (Spain), the same way as the Pulse's portal
# ----------------------------------------------------------------------------
# I18N_ES is the ONLY place with the translations: the key is the English text as written, so a
# missing one shows the English (never empty). t() takes {variables}. The language is "lang" in
# latiddo.json ("en" or "es": --lang, or the EN / ES selector of the settings page); without it,
# the language of the system (Windows display language, macOS AppleLanguages, LANG on Linux).
# Not translated: names (Latiddo, Pulse, Claude, Claude Code), commands, paths, field names.
LANGS = ("en", "es")
# BEGIN I18N_ES
I18N_ES = {
    "Added the Claude Code hooks to {path}": "Hooks de Claude Code añadidos a {path}",
    "Removed the Latiddo hooks from {path} (other hooks were kept)": "Hooks de Latiddo quitados de {path} (los demás hooks se mantienen)",
    "name not found: {host}": "no se encuentra {host}",
    " (backup: {path})": " (copia de seguridad: {path})",
    "; backup: {path}": "; copia de seguridad: {path}",
    "Installed.": "Instalado.",
    "Next: run with --test to check the device, then restart Claude Code.": "Siguiente: ejecútalo con --test para comprobar el aparato y luego reinicia Claude Code.",
    "Removed the Latiddo statusLine from {path}": "statusLine de Latiddo quitada de {path}",
    "Config and log were kept: {config} and {log}": "Se mantienen la configuración y el log: {config} y {log}",
    "Sent:": "Enviado:",
    "no address": "sin dirección",
    "Latiddo: no data": "Latiddo: sin datos",
    "No device. Use --install --ip <ip>, --test --ip <ip>, LATIDDO_IP or LATIDDO_ROBOT.": "No hay ningún aparato. Usa --install --ip <ip>, --test --ip <ip>, LATIDDO_IP o LATIDDO_ROBOT.",
    "\"hooks\" in {path} is not a JSON object. Nothing was changed.": "\"hooks\" en {path} no es un objeto JSON. No se ha cambiado nada.",
    "No Latiddo hooks were found.": "No se han encontrado hooks de Latiddo.",
    "{path} does not contain a JSON object. Nothing was changed.": "{path} no contiene un objeto JSON. No se ha cambiado nada.",
    "Missing --ip (for example: --install --ip 192.168.1.50).": "Falta --ip (por ejemplo: --install --ip 192.168.1.50).",
    "A statusLine is already configured in {path}": "Ya hay una statusLine configurada en {path}",
    "It was NOT changed. To use both, call this script from your existing status line script, passing it the same JSON and discarding its output.": "NO se ha cambiado. Para usar las dos, llama a este script desde tu script de status line, pasándole el mismo JSON y descartando su salida.",
    "In a bash script that already did  input=$(cat):": "En un script de bash que ya hizo  input=$(cat):",
    "In a Python status line script, with the raw stdin text in `raw`:": "En un script de status line en Python, con el texto de stdin en `raw`:",
    "Then save the device IP with LATIDDO_IP or in {path}": "Después guarda la IP del aparato con LATIDDO_IP o en {path}",
    "No statusLine configured. Nothing to do.": "No hay ninguna statusLine configurada. No hay nada que hacer.",
    "The configured statusLine is not this script; it was left unchanged:": "La statusLine configurada no es este script; se ha dejado como estaba:",
    "Latiddo - Claude Code status line bridge": "Latiddo: puente de la status line de Claude Code",
    "save the IP and add the statusLine to Claude Code": "guarda la IP y añade la statusLine a Claude Code",
    "remove this script's statusLine": "quita la statusLine de este script",
    "send sample data to the devices": "envía datos de ejemplo a los aparatos",
    "status line mode that also saves the sanitized input": "modo status line que además guarda la entrada sin datos privados",
    "send a Claude Code event to the devices (the notices, without Claude Code)": "envía un evento de Claude Code a los aparatos (los avisos, sin Claude Code)",
    "Latiddo Pulse IP, for example 192.168.1.50": "IP del Latiddo Pulse, por ejemplo 192.168.1.50",
    "language of the bridge (saved; auto = the system language)": "idioma del puente (se guarda; auto = el del sistema)",
    "error: {name}": "error: {name}",
    "timeout": "sin respuesta",
    "(none)": "(ninguna)",
    "\"hooks.{event}\" in {path} is not a list. Nothing was changed.": "\"hooks.{event}\" en {path} no es una lista. No se ha cambiado nada.",
    "{path} is not valid JSON ({error}). Nothing was changed.": "{path} no es un JSON válido ({error}). No se ha cambiado nada.",
    "rate_limits missing; keys received: {keys}": "falta rate_limits; claves recibidas: {keys}",
    "no device configured (latiddo.json, LATIDDO_IP or LATIDDO_ROBOT); nothing sent": "ningún aparato configurado (latiddo.json, LATIDDO_IP o LATIDDO_ROBOT); no se envía",
    "(empty)": "(vacío)",
    "{who} does not take the Claude Code hooks ({state})": "{who} no recibe los avisos de Claude Code ({state})",
    "hook session={sid} {event} -> {state}, turn: {turn}{extra}": "aviso sesión={sid} {event} -> {state}, turno: {turn}{extra}",
    "yours": "tuyo",
    "automatic": "automático",
    ", «Listo» without a beep": ", «Listo» sin pitido",
    "no answer": "sin respuesta",
    "scheduled task": "tarea programada",
    "status line": "status line",
    "test": "prueba",
    "Latiddo bridge": "Puente Latiddo",
    "Where your Claude usage goes. The scheduled task reads these settings on its next run.": "Adónde va tu uso de Claude. La tarea programada lee estos ajustes en su siguiente ejecución.",
    "Save": "Guardar",
    "Latiddo robot": "Robot Latiddo",
    "On": "Activado",
    "Off": "Desactivado",
    "Address": "Dirección",
    "Check": "Comprobar",
    "Its IP, shown on the Pulse at startup (for example 192.168.1.50).": "Su IP, la que muestra el Pulse al arrancar (por ejemplo 192.168.1.50).",
    "Its name latiddo-xxxx.local or its IP, shown in the robot's portal under Ajustes.": "Su nombre latiddo-xxxx.local o su IP, en el portal del robot, en Ajustes.",
    "An environment variable overrides it.": "Una variable de entorno manda sobre este valor.",
    "Not saved yet.": "Sin guardar todavía.",
    "Last send:": "Último envío:",
    "none yet": "ninguno todavía",
    "at {time}": "a las {time}",
    "Last successful send:": "Último envío correcto:",
    "never": "nunca",
    "{n} s ago": "hace {n} s",
    "{n} min ago": "hace {n} min",
    "{n} h ago": "hace {n} h",
    "Bridge {v} · settings in {path}": "Puente {v} · ajustes en {path}",
    "This page only works on this computer. It closes by itself 15 min after you close it.": "Esta página solo funciona en este ordenador. Se cierra sola 15 min después de que la cierres.",
    "The bridge settings server is not running.": "El servidor de ajustes del puente no está en marcha.",
    "Checking…": "Comprobando…",
    "Last Claude Code event:": "Último aviso de Claude Code:",
    "working": "trabajando",
    "needs you": "te necesita",
    "done": "listo",
    "error": "error",
    "end": "fin",
    "idle": "en reposo",
    "; nothing sent": "; no se envía",
    "claude not found": "claude no encontrado",
    "claude timeout ({s} s)": "claude no respondió a tiempo ({s} s)",
    "could not run claude: {name}": "no se pudo ejecutar claude: {name}",
    "claude is not signed in": "claude sin sesión iniciada",
    "claude exited with code {rc}": "claude salió con código {rc}",
    "format not recognised": "formato no reconocido",
    "session": "sesión",
    "week": "semana",
    "Looking for your Latiddo Pulse...": "Buscando tu Latiddo Pulse...",
    "Looking for your Latiddo robot...": "Buscando tu robot Latiddo...",
    "The test was sent, but the robot does not show it as live. Check its portal (Hoy → Claude).": "La prueba se ha enviado, pero el robot no la muestra en vivo. Mira su portal (Hoy → Claude).",
    "Checking Claude Code ({path}) ...": "Comprobando Claude Code ({path})...",
    "Claude Code did not answer in time. Run `claude` once to finish any first-run steps, then run the command again.": "Claude Code no ha respondido a tiempo. Ejecuta `claude` una vez para terminar los pasos de la primera vez y vuelve a ejecutar el comando.",
    "The test was sent, but the Pulse does not show it as active. Check the device screen.": "La prueba se ha enviado, pero el Pulse no la muestra como activa. Mira la pantalla del aparato.",
    "This removes:": "Esto quita:",
    "The scripts in {dest} were kept; delete the folder if you no longer need them.": "Se mantienen los scripts de {dest}; borra la carpeta si ya no los necesitas.",
    "Saved. Sending to: {devices}.": "Guardado. Se envía a: {devices}.",
    "No answer: check the address and that this computer is on the same network.": "Sin respuesta: revisa la dirección y que este ordenador está en la misma red.",
    "Latiddo bridge settings: {url}": "Ajustes del puente Latiddo: {url}",
    "Only this computer can open it. It stops 15 min after you close the page, or with Ctrl+C.": "Solo se puede abrir desde este ordenador. Se detiene 15 min después de cerrar la página, o con Ctrl+C.",
    "Settings page closed.": "Página de ajustes cerrada.",
    "claude -p /usage took {s} s": "claude -p /usage tardó {s} s",
    "(not found)": "(no encontrado)",
    "--- output of claude -p /usage (summary) ---": "--- salida de claude -p /usage (resumen) ---",
    "FAILED:": "FALLO:",
    "not parsed": "no se ha podido leer",
    "{name}: {pct}% used, resets '{text}' ({zone}) -> {when}": "{name}: {pct} % usado, se reinicia '{text}' ({zone}) -> {when}",
    "No device. Use --ip <Pulse IP>, --robot <robot address>, LATIDDO_IP or {path}": "No hay ningún aparato. Usa --ip <IP del Pulse>, --robot <dirección del robot>, LATIDDO_IP o {path}",
    "Could not reach a Latiddo Pulse at {ip}. Check the IP shown on the device and that this computer is on the same network.": "No se ha podido conectar con un Latiddo Pulse en {ip}. Revisa la IP que muestra el aparato y que este ordenador está en la misma red.",
    "No Latiddo Pulse found on this network. Run again with --ip <the IP shown on the device screen at startup>.": "No se ha encontrado ningún Latiddo Pulse en esta red. Vuelve a ejecutarlo con --ip <la IP que muestra la pantalla del aparato al arrancar>.",
    "not found automatically.": "no se ha encontrado automáticamente.",
    "No Latiddo robot found on this network. Run again with --robot <its address: latiddo-xxxx.local or its IP, shown in its portal under Ajustes>.": "No se ha encontrado ningún robot Latiddo en esta red. Vuelve a ejecutarlo con --robot <su dirección: latiddo-xxxx.local o su IP, en su portal, en Ajustes>.",
    "Test send to {url} failed: {result}.": "La prueba a {url} ha fallado: {result}.",
    "The robot received the test: session {s}%, weekly {w}% (its Claude screen and portal show it as live).": "El robot ha recibido la prueba: sesión {s} %, semanal {w} % (su pantalla de Claude y su portal lo muestran en vivo).",
    "Both": "Los dos",
    "Claude Code (the `claude` command) was not found on this computer.\nInstall it from https://claude.com/claude-code\n  Windows (PowerShell):  irm https://claude.ai/install.ps1 | iex\n  macOS / Linux:         curl -fsSL https://claude.ai/install.sh | bash\nIf it is already installed somewhere else, add --claude-path <path to claude>.\nThen close this window, open a new terminal window and run the command again\n(it will offer to sign in to Claude Code).": "No se ha encontrado Claude Code (el comando `claude`) en este ordenador.\nInstálalo desde https://claude.com/claude-code\n  Windows (PowerShell):  irm https://claude.ai/install.ps1 | iex\n  macOS / Linux:         curl -fsSL https://claude.ai/install.sh | bash\nSi ya está instalado en otro sitio, añade --claude-path <ruta de claude>.\nDespués cierra esta ventana, abre una ventana de terminal nueva y vuelve a ejecutar el comando\n(te ofrecerá iniciar sesión en Claude Code).",
    "Claude Code is not signed in. Run `claude`, sign in with your claude.ai account, type /exit, then run the command again.": "Claude Code no tiene la sesión iniciada. Ejecuta `claude`, inicia sesión con tu cuenta de claude.ai, escribe /exit y vuelve a ejecutar el comando.",
    "crontab failed: {out}": "crontab ha fallado: {out}",
    "cron runs it every {n} min.": "cron lo ejecuta cada {n} min.",
    "Add or replace this line in your crontab: {line}": "Añadir o sustituir esta línea en tu crontab: {line}",
    "The systemd user timer {name} and the crontab line '{mark}', if present": "El temporizador de usuario de systemd {name} y la línea del crontab '{mark}', si están",
    "The Pulse received the test: session {s}%, weekly {w}%.": "El Pulse ha recibido la prueba: sesión {s} %, semanal {w} %.",
    "--minutes must be between 1 and 1439.": "--minutes debe estar entre 1 y 1439.",
    "Claude Code is ready: session {s}%, weekly {w}%.": "Claude Code está listo: sesión {s} %, semanal {w} %.",
    "Copy {a} and {b} to {dest} (the task runs from there, so moving or switching the repository does not break it)": "Copiar {a} y {b} a {dest} (la tarea se ejecuta desde ahí, así que mover el repositorio o cambiar de rama no la rompe)",
    "Save {path} with {what} and claude_path {claude} (no passwords)": "Guardar {path} con {what} y claude_path {claude} (sin contraseñas)",
    "Saved {path}.": "Guardado {path}.",
    "Settings page: {cmd}": "Página de ajustes: {cmd}",
    " The status line still uses it.": " La status line todavía lo usa.",
    "Saved. Nothing is sent now.": "Guardado. Ahora no se envía nada.",
    "Type an IP or a device name first.": "Escribe primero una IP o un nombre de aparato.",
    "Latiddo robot found ({label}).": "Robot Latiddo encontrado ({label}).",
    "It is a Latiddo robot, but its firmware cannot take Claude data yet: update it.": "Es un robot Latiddo, pero su firmware aún no admite los datos de Claude: actualízalo.",
    "It answered, but it is not a Latiddo robot.": "Ha respondido, pero no es un robot Latiddo.",
    "Ports {a}-{b} are busy. Run again with --port <another port>.": "Los puertos {a}-{b} están ocupados. Vuelve a ejecutarlo con --port <otro puerto>.",
    "Latiddo bridge - poll `claude -p /usage` for the Pulse and the robot": "Puente Latiddo: consulta `claude -p /usage` para el Pulse y el robot",
    "find the Pulse and/or the Latiddo robot, check Claude Code and schedule this script": "busca el Pulse y/o el robot Latiddo, comprueba Claude Code y programa este script",
    "remove the scheduled task": "quita la tarea programada",
    "query, show the result and send it": "consulta, muestra el resultado y lo envía",
    "same as --setup --yes": "igual que --setup --yes",
    "same as --remove --yes": "igual que --remove --yes",
    "open the settings page (only on this computer): devices on/off, addresses, last sends": "abre la página de ajustes (solo en este ordenador): aparatos activados, direcciones, últimos envíos",
    "Latiddo robot address: latiddo-xxxx.local or its IP": "dirección del robot Latiddo: latiddo-xxxx.local o su IP",
    "--setup: which devices get the data (default: ask, or what is configured)": "--setup: qué aparatos reciben los datos (por defecto: pregunta, o lo configurado)",
    "--settings: port on 127.0.0.1 (default {port})": "--settings: puerto en 127.0.0.1 (por defecto {port})",
    "--settings: do not open the browser": "--settings: no abrir el navegador",
    "how often to run (default {n})": "cada cuántos minutos se ejecuta (por defecto {n})",
    "path to the claude executable": "ruta del ejecutable de claude",
    "do not ask for confirmation": "no pedir confirmación",
    "show what --setup would do": "muestra lo que haría --setup",
    "--setup also adds the Claude Code status line": "--setup añade también la status line de Claude Code",
    "--setup also adds the Claude Code hooks (live status on the Pulse)": "--setup añade también los hooks de Claude Code (estado en vivo en el Pulse)",
    "(x{count} since {since})": "(x{count} desde las {since})",
    "(x{count} since {since}, last at {last})": "(x{count} desde las {since}, la última a las {last})",
    "no device configured (latiddo.json, LATIDDO_IP or LATIDDO_ROBOT)": "ningún aparato configurado (latiddo.json, LATIDDO_IP o LATIDDO_ROBOT)",
    "{name}: not in the output": "{name}: no aparece en la salida",
    "Missing {path}: keep both bridge scripts in the same folder.": "Falta {path}: deja los dos scripts del puente en la misma carpeta.",
    "{ip} answered, but it is not a Latiddo Pulse, or its firmware is too old. Update the device firmware and try again.": "{ip} ha respondido, pero no es un Latiddo Pulse o su firmware es demasiado antiguo. Actualiza el firmware del aparato y vuelve a intentarlo.",
    "searching the network (mDNS and {net}.1-254, about 10 s)...": "buscando en la red (mDNS y {net}.1-254, unos 10 s)...",
    "Which one? Type its number: ": "¿Cuál? Escribe su número: ",
    "Cancelled. Nothing was changed.": "Cancelado. No se ha cambiado nada.",
    "Type the IP shown on the device (or press Enter to cancel): ": "Escribe la IP que muestra el aparato (o pulsa Enter para cancelar): ",
    "{ip} is not a Latiddo Pulse or did not answer. Try again.": "{ip} no es un Latiddo Pulse o no ha respondido. Vuelve a intentarlo.",
    "{address} is a Latiddo robot ({label}), but its firmware cannot take Claude data yet (no /api/claude). Update the robot and try again.": "{address} es un robot Latiddo ({label}), pero su firmware aún no admite los datos de Claude (no tiene /api/claude). Actualiza el robot y vuelve a intentarlo.",
    "answered, but it is not a Latiddo robot": "ha respondido, pero no es un robot Latiddo",
    "did not answer": "no ha respondido",
    "{address} {what}. Use the address shown in the robot's portal (Ajustes: latiddo-xxxx.local, or its IP) and check that this computer is on the same network.": "{address} {what}. Usa la dirección que muestra el portal del robot (Ajustes: latiddo-xxxx.local, o su IP) y comprueba que este ordenador está en la misma red.",
    "found {name}": "encontrado {name}",
    "Type the robot's address, latiddo-xxxx.local or its IP (in its portal, under Ajustes), or press Enter to cancel: ": "Escribe la dirección del robot, latiddo-xxxx.local o su IP (en su portal, en Ajustes), o pulsa Enter para cancelar: ",
    "Type 1, 2 or 3 [{key}]: ": "Escribe 1, 2 o 3 [{key}]: ",
    "Please type 1, 2 or 3.": "Escribe 1, 2 o 3, por favor.",
    "Claude Code will open: sign in with your claude.ai account, then type /exit to come back here.": "Se abrirá Claude Code: inicia sesión con tu cuenta de claude.ai y después escribe /exit para volver aquí.",
    "Could not open Claude Code ({name}).": "No se ha podido abrir Claude Code ({name}).",
    "Claude Code is not signed in. Sign in to Claude Code now? [y/N] ": "Claude Code no tiene la sesión iniciada. ¿Iniciar sesión en Claude Code ahora? [s/N] ",
    "Checking Claude Code again ({path}) ...": "Comprobando Claude Code otra vez ({path})...",
    "Claude Code did not show subscription usage limits. Make sure it is signed in with your claude.ai subscription (run `claude`, then type /login), then run the command again.": "Claude Code no ha mostrado los límites de uso de la suscripción. Comprueba que ha iniciado sesión con tu suscripción de claude.ai (ejecuta `claude` y escribe /login) y vuelve a ejecutar el comando.",
    "Claude Code could not be run ({reason}).": "No se ha podido ejecutar Claude Code ({reason}).",
    "Scheduled task '{name}' runs every {n} min (checked with Get-ScheduledTask).": "La tarea programada '{name}' se ejecuta cada {n} min (comprobado con Get-ScheduledTask).",
    "Create or replace the Windows scheduled task '{name}': every {n} min with no end, hidden, also on battery, as the current user ({cmd})": "Crear o sustituir la tarea programada de Windows '{name}': cada {n} min sin fin, oculta, también con batería, como el usuario actual ({cmd})",
    "LaunchAgent {name} runs every {n} min.": "El LaunchAgent {name} se ejecuta cada {n} min.",
    "Create or replace {path} (runs every {n} min) and load it": "Crear o sustituir {path} (se ejecuta cada {n} min) y cargarlo",
    "systemd user timer {name} runs every {n} min.": "El temporizador de usuario de systemd {name} se ejecuta cada {n} min.",
    "Note: user timers run while you are logged in. To keep it running after logout: loginctl enable-linger $USER": "Ojo: los temporizadores de usuario funcionan mientras tienes la sesión abierta. Para que siga al cerrarla: loginctl enable-linger $USER",
    "Create or replace the systemd user timer {path} (every {n} min) and enable it; remove our crontab line if present": "Crear o sustituir el temporizador de usuario de systemd {path} (cada {n} min) y activarlo; quitar nuestra línea del crontab si está",
    "The Windows scheduled task '{name}'": "La tarea programada de Windows '{name}'",
    "The LaunchAgent {path}": "El LaunchAgent {path}",
    "Removed the systemd user timer {name}.": "Temporizador de usuario de systemd {name} quitado.",
    "Removed the crontab line.": "Línea del crontab quitada.",
    "Latiddo Pulse found at {ip} ({label}).": "Latiddo Pulse encontrado en {ip} ({label}).",
    "Latiddo robot found at {ip} ({label}).": "Robot Latiddo encontrado en {ip} ({label}).",
    "the Pulse switched off (its IP is kept, if any)": "el Pulse desactivado (se conserva su IP, si la hay)",
    "robot {address}": "robot {address}",
    "Add the Claude Code status line to {path} (only if no other status line is set)": "Añadir la status line de Claude Code a {path} (solo si no hay otra status line)",
    "Add Claude Code hooks to {path} for the live status (working / needs you / done). Your other hooks and settings are kept; a backup is made first": "Añadir los hooks de Claude Code a {path} para el estado en vivo (trabajando / te necesita / hecho). Tus otros hooks y ajustes se mantienen; antes se hace una copia de seguridad",
    "Send a test update to the Pulse and the robot and check that it arrived": "Enviar una prueba al Pulse y al robot y comprobar que llega",
    "Plan:": "Plan:",
    "Copied the bridge scripts to {dest}.": "Scripts del puente copiados a {dest}.",
    "Settings page (switch each device on or off, change its address, language): {cmd}": "Página de ajustes (activar o desactivar cada aparato, cambiar su dirección, idioma): {cmd}",
    "The Latiddo hooks in {path} (other hooks are kept)": "Los hooks de Latiddo en {path} (los demás hooks se mantienen)",
    "{name}: type its address to switch it on.": "{name}: escribe su dirección para activarlo.",
    "{name}: «{address}» is not an IP or a device name.": "{name}: «{address}» no es una IP ni un nombre de aparato.",
    "Latiddo Pulse found ({label}).": "Latiddo Pulse encontrado ({label}).",
    "It answered, but it is not a Latiddo Pulse.": "Ha respondido, pero no es un Latiddo Pulse.",
    "found it at the saved address {ip}": "encontrado en la dirección guardada {ip}",
    "found it at {ip}": "encontrado en {ip}",
    "found more than one:": "hay más de uno:",
    "More than one device found. Run again with --ip <ip>.": "Hay más de un aparato. Vuelve a ejecutarlo con --ip <ip>.",
    "More than one robot found. Run again with --robot <address>.": "Hay más de un robot. Vuelve a ejecutarlo con --robot <dirección>.",
    "Output of claude -p /usage:": "Salida de claude -p /usage:",
    "Neither systemd (user) nor crontab is available. Run this script every {n} min with your own scheduler:": "No hay systemd (de usuario) ni crontab. Ejecuta este script cada {n} min con tu propio programador:",
    "Removed the scheduled task '{name}'.": "Tarea programada '{name}' quitada.",
    "No scheduled task was found.": "No se ha encontrado ninguna tarea programada.",
    "Removed {path}.": "Quitado {path}.",
    "No LaunchAgent was found.": "No se ha encontrado ningún LaunchAgent.",
    "No timer or crontab line was found.": "No se ha encontrado ningún temporizador ni línea del crontab.",
    "Pulse ip {ip}": "IP del Pulse {ip}",
    "the robot switched off (its address is kept)": "el robot desactivado (se conserva su dirección)",
    "Send a test update to the Pulse and check that it arrived": "Enviar una prueba al Pulse y comprobar que llega",
    "Send a test update to the robot and check that it arrived": "Enviar una prueba al robot y comprobar que llega",
    "Dry run: nothing was changed.": "Simulación: no se ha cambiado nada.",
    "No terminal to ask for confirmation. Run again with --yes.": "No hay terminal para pedir confirmación. Vuelve a ejecutarlo con --yes.",
    "The status line was not added (see above). The rest of the setup continues.": "No se ha añadido la status line (mira arriba). El resto de la instalación sigue.",
    "The task is installed (every {n} min), but the test reached neither the Pulse nor the robot.": "La tarea está instalada (cada {n} min), pero la prueba no ha llegado ni al Pulse ni al robot.",
    "Cancelled.": "Cancelado.",
    "Delete it yourself if you no longer need it.": "Bórralo tú si ya no lo necesitas.",
    "Deleted {path}.": "Borrado {path}.",
    "Kept {path}.": "Se mantiene {path}.",
    "no zone": "sin zona",
    "Note: {address} did not identify itself as a Latiddo Pulse in /api/status.": "Ojo: {address} no se ha identificado como Latiddo Pulse en /api/status.",
    "Robot: session {s}%, weekly {w}%, {live} (GET {path})": "Robot: sesión {s} %, semanal {w} %, {live} (GET {path})",
    "Note: {address} did not answer GET {path} like a Latiddo robot.": "Ojo: {address} no ha respondido a GET {path} como un robot Latiddo.",
    "found {name} at {ip}": "encontrado {name} en {ip}",
    ", hostname {name}": ", nombre {name}",
    "The hooks were not added. The rest of the setup continues.": "No se han añadido los hooks. El resto de la instalación sigue.",
    "Done. The Pulse and the robot will be updated every {n} min.": "Hecho. El Pulse y el robot se actualizarán cada {n} min.",
    "The task is installed (every {n} min), but the test did not reach the Pulse.": "La tarea está instalada (cada {n} min), pero la prueba no ha llegado al Pulse.",
    "The task is installed (every {n} min), but the test did not reach the robot.": "La tarea está instalada (cada {n} min), pero la prueba no ha llegado al robot.",
    "Continue? [y/N] ": "¿Continuar? [s/N] ",
    "[y/N] ": "[s/N] ",
    "Which devices should show your Claude usage?": "¿Qué aparatos deben mostrar tu uso de Claude?",
    "Could not create the scheduled task:": "No se ha podido crear la tarea programada:",
    "Could not load {path}:": "No se ha podido cargar {path}:",
    "Could not remove the scheduled task:": "No se ha podido quitar la tarea programada:",
    "Done. The Pulse will be updated every {n} min.": "Hecho. El Pulse se actualizará cada {n} min.",
    "Done. The robot will be updated every {n} min.": "Hecho. El robot se actualizará cada {n} min.",
    "{cmd} failed:": "{cmd} ha fallado:",
    "Also show Claude Code status (working / needs you / done) on your devices? [Y/n] ": "¿Mostrar también el estado de Claude Code (trabajando / te necesita / listo) en tus aparatos? [S/n] ",
    "PowerShell that would run:": "PowerShell que se ejecutaría:",
    "Also delete {path}?": "¿Borrar también {path}?",
    "{name} does not answer by its name here: looking for it on the network (mDNS and {net}.1-254, about 10 s)...": "{name} no responde por su nombre en este ordenador: lo busco en la red (mDNS y {net}.1-254, unos 10 s)...",
    "found {name} at {ip}; its IP is kept for the next runs": "encontrado {name} en {ip}; se guarda su IP para las siguientes veces",
    "No Latiddo status line was found.": "No se ha encontrado la status line de Latiddo.",
    "The Latiddo status line in {path} (a dated copy of the file first)": "La status line de Latiddo en {path} (antes, una copia con fecha del archivo)",
    "The bridge scripts in {dest}": "Los scripts del puente en {dest}",
    "Its files: {paths}": "Sus archivos: {paths}",
    "Its temporary files: {paths}": "Sus archivos temporales: {paths}",
    "Deleted {n} files of the bridge.": "Borrados {n} archivos del puente.",
    "Could not delete {path}: {error}": "No se ha podido borrar {path}: {error}",
    "Kept {dest}: it has other files.": "Se mantiene {dest}: tiene otros archivos.",
    "Nothing of the bridge is left on this computer.": "No queda nada del puente en este ordenador.",
    "and": "y",
    "live": "en vivo",
    "not live": "no en vivo",
    "Latiddo · Bridge settings": "Latiddo · Ajustes del puente",
    "Opens the settings page of the Latiddo bridge": "Abre la página de ajustes del puente de Latiddo",
    "Added the shortcut «{name}» ({path}).": "Añadido el acceso directo «{name}» ({path}).",
    "The shortcut was not added ({error}). The settings page still opens with: {cmd}": "No se ha añadido el acceso directo ({error}). La página de ajustes se sigue abriendo con: {cmd}",
    "Add «{name}» to the Start menu: it opens the settings page, without a console window ({path})": "Añadir «{name}» al menú Inicio: abre la página de ajustes, sin ventana de consola ({path})",
    "Add the app «{name}» to {dir}: it opens the settings page, without Terminal": "Añadir la app «{name}» a {dir}: abre la página de ajustes, sin Terminal",
    "Add «{name}» to the applications menu: it opens the settings page, without a terminal ({path})": "Añadir «{name}» al menú de aplicaciones: abre la página de ajustes, sin terminal ({path})",
    "Start menu, «{name}»": "menú Inicio, «{name}»",
    "the app «{name}» in {dir} (also with Spotlight)": "la app «{name}» en {dir} (también con Spotlight)",
    "the applications menu, «{name}»": "el menú de aplicaciones, «{name}»",
    "{where}, or {cmd}": "{where}, o {cmd}",
    "The shortcut {path}": "El acceso directo {path}",
    "Removed the shortcut {path}.": "Quitado el acceso directo {path}.",
    "The settings page is already open: {url}": "La página de ajustes ya está abierta: {url}",
}
# END I18N_ES
_lang = None


def system_lang():
    """"es" when the system language is Spanish, else "en"."""
    try:
        if os.name == "nt":
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "es" if (lid & 0x3FF) == 0x0A else "en"
        for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
            v = os.environ.get(var) or ""
            if v and v.split(".")[0] not in ("C", "POSIX"):
                return "es" if v.lower().startswith("es") else "en"
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True,
                                 text=True, timeout=3).stdout
            first = out.replace("(", " ").replace('"', " ").replace(",", " ").split()
            return "es" if first and first[0].lower().startswith("es") else "en"
    except Exception:
        pass
    return "en"


def saved_lang():
    """"en" or "es" when chosen (latiddo.json), "" = automatic."""
    v = str(load_json(config_path(), {}).get("lang") or "")
    return v if v in LANGS else ""


def lang():
    global _lang
    if _lang is None:
        _lang = saved_lang() or system_lang()
    return _lang


def set_lang(value, save=True):
    """"en", "es" or "" (automatic: the system language). Saved in latiddo.json."""
    global _lang
    value = value if value in LANGS else ""
    if save:
        cfg = load_json(config_path(), {})
        if value:
            cfg["lang"] = value
        else:
            cfg.pop("lang", None)
        save_json_atomic(config_path(), cfg, indent=2)
    _lang = value or system_lang()


def t(text, /, **v):
    """The text in the current language, with {variables} filled in."""
    r = I18N_ES.get(text, text) if lang() == "es" else text
    if v:
        import re
        r = re.sub(r"\{(\w+)\}", lambda m: str(v[m.group(1)]) if m.group(1) in v else m.group(0), r)
    return r


def is_yes(answer):
    """y / yes, and in Spanish s / si / sí."""
    return str(answer or "").strip().lower() in ("y", "yes", "s", "si", "sí")


def lang_from_argv(argv):
    """--lang es|en|auto before argparse, so its help is translated too. Saved in latiddo.json."""
    for i, a in enumerate(argv):
        value = argv[i + 1] if a == "--lang" and i + 1 < len(argv) else (a[7:] if a.startswith("--lang=") else None)
        if value in ("es", "en", "auto"):
            set_lang("" if value == "auto" else value)


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def log(msg):
    """Append one line to ~/.claude/latiddo.log and keep it under ~50 KB."""
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {msg}\n")
        if p.stat().st_size > LOG_MAX:
            data = p.read_bytes()[-LOG_KEEP:]
            nl = data.find(b"\n")
            p.write_bytes(data[nl + 1:] if nl >= 0 else data)
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default


def save_json_atomic(path, obj, indent=None):
    """Write `path` through a <name>.<pid>.tmp next to it. 1.3.2: on Windows the replace fails
    while another run of the bridge (a hook) has the file open for a moment: a few short retries,
    and the .tmp never stays behind, whatever happens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, indent=indent, ensure_ascii=False)
            if indent:
                f.write("\n")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def keep_host_ip(name, ip):
    """Keep `ip` as the address of the name `name` (hosts_path()), as a lookup would (1.3.2: the
    setup found the robot on the network by its name, which did not resolve here)."""
    host, _ = split_host(name)
    cache = load_json(hosts_path(), {})
    cache[host.lower()] = ip
    save_json_atomic(hosts_path(), cache)


def normalize_ip(value):
    """Accept '192.168.1.50', '192.168.1.50:8080' or 'http://192.168.1.50/'."""
    s = str(value or "").strip()
    for prefix in ("http://", "https://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    return s.split("/")[0].strip()


def pulse_enabled(cfg):
    """The Pulse is on unless "pulse_enabled" is false (configs without it: on, as before)."""
    return cfg.get("pulse_enabled", True) is not False


def robot_enabled(cfg):
    return bool(str(cfg.get("robot") or "").strip()) and cfg.get("robot_enabled", True) is not False


def load_config():
    """(Pulse address, Pulse password). The address is "" when the Pulse is switched off in
    the config; LATIDDO_IP always wins."""
    cfg = load_json(config_path(), {})
    ip = os.environ.get("LATIDDO_IP") or (cfg.get("ip") if pulse_enabled(cfg) else "") or ""
    password = cfg.get("password") or ""
    return normalize_ip(ip), str(password)


def load_robot():
    """The Latiddo robot's address ("" when there is none or it is switched off); LATIDDO_ROBOT wins."""
    cfg = load_json(config_path(), {})
    robot = os.environ.get("LATIDDO_ROBOT") or (cfg.get("robot") if robot_enabled(cfg) else "") or ""
    return normalize_ip(robot)


def load_targets():
    """The destinations switched on: dicts with id, label, address, path, password and the log
    prefix. The Pulse keeps the log prefix "ip=" of earlier versions."""
    targets = []
    ip, password = load_config()
    if ip:
        targets.append({"id": "pulse", "label": "Pulse", "address": ip, "path": PULSE_PATH,
                        "password": password, "log": "ip="})
    robot = load_robot()
    if robot:
        # never the Pulse's password: it is the Pulse's, not the robot's
        targets.append({"id": "latiddo", "label": "Latiddo", "address": robot, "path": ROBOT_PATH,
                        "password": "", "log": "latiddo="})
    return targets


def split_host(address):
    """'host', 'host:port' -> ('host', 'port' or '')."""
    host, _, port = str(address or "").partition(":")
    return host.strip(), port.strip()


def is_ip(host):
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve_host(name, timeout=3.0):
    """IPv4 of a host name such as latiddo-a1b2.local, or None (bounded wait: urllib's timeout
    does not cover the name lookup, and a .local name that nobody answers can take long)."""
    import threading
    out = []

    def work():
        try:
            out.append(socket.gethostbyname(name))
        except Exception:
            pass
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return out[0] if out else None


def resolve(address, timeout=RESOLVE_TIMEOUT, fresh=False):
    """'host[:port]' -> ('ip[:port]', None, from_cache), or (None, reason, False). An IP is used
    as it is. A name: its kept IP (hosts_path()) unless fresh=True; else a lookup with a bounded
    wait, whose IP is kept for the next runs; if the lookup fails, the kept IP, if any."""
    host, port = split_host(address)
    if not host:
        return None, t("no address"), False
    if is_ip(host):
        return address, None, False
    suffix = f":{port}" if port else ""
    cache = load_json(hosts_path(), {})
    kept = cache.get(host.lower())
    if kept and not fresh:
        return kept + suffix, None, True
    ip = resolve_host(host, timeout)
    if ip:
        if kept != ip:
            cache[host.lower()] = ip
            try:
                save_json_atomic(hosts_path(), cache)
            except Exception:
                pass
        return ip + suffix, None, False
    if kept:
        return kept + suffix, None, True
    return None, t("name not found: {host}", host=host), False


def parse_reset(value):
    """resets_at as epoch seconds, epoch milliseconds (> 1e12) or ISO 8601 text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        t = float(value)
    elif isinstance(value, str):
        s = value.strip()
        try:
            t = float(s)
        except ValueError:
            try:
                if s[-1:] in ("Z", "z"):
                    s = s[:-1] + "+00:00"
                d = dt.datetime.fromisoformat(s)
            except ValueError:
                return None
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.timestamp()
    else:
        return None
    if t > 1e12:
        t /= 1000.0
    return t if t > 0 else None


def to_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_limits(data):
    """Return {'five_hour': (pct, reset_epoch), 'seven_day': (...)} or None."""
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(rl, dict):
        return None
    out = {}
    for key in ("five_hour", "seven_day"):
        w = rl.get(key)
        if not isinstance(w, dict):
            return None
        pct = to_number(w.get("used_percentage"))
        reset = parse_reset(w.get("resets_at"))
        if pct is None or reset is None:
            return None
        out[key] = (pct, reset)
    return out


def build_payload(limits, now):
    p5, r5 = limits["five_hour"]
    p7, r7 = limits["seven_day"]
    secs = max(0, int(r5 - now))
    label = dt.datetime.fromtimestamp(r7).strftime("%a %H:%M")[:39]
    return {
        "sesion": int(round(p5)),
        "semanal": int(round(p7)),
        "reset_h": secs // 3600,
        "reset_m": (secs % 3600) // 60,
        "reset_label": label,
        "extra_used": 0,
        "extra_limit": 0,
        "extra_label": "",
        "session_reset_s": secs,
        "weekly_reset_s": max(0, int(r7 - now)),
        "bridge_version": BRIDGE_VERSION,
    }


def send(ip, password, payload, path=PULSE_PATH, timeout=NET_TIMEOUT, resolve_timeout=RESOLVE_TIMEOUT):
    """POST payload to http://<ip><path> (the Pulse's /api/update, or the robot's /api/claude).
    `ip` may also be a name (latiddo-xxxx.local): its kept IP is used, and when that one does not
    answer the name is looked up again (bounded wait) and, if its IP changed, sent there.
    Returns (ok, 'OK'|'timeout'|'HTTP nnn'|'error: X'|'name not found: NAME'), translated."""
    target, why, from_cache = resolve(ip, resolve_timeout)
    if target is None:
        return False, why
    ok, result, network = _post(target, password, payload, path, timeout)
    if not ok and network and from_cache:
        again, _, _ = resolve(ip, resolve_timeout, fresh=True)
        if again and again != target:
            ok, result, _ = _post(again, password, payload, path, timeout)
    return ok, result


def _post(target, password, payload, path, timeout):
    """(ok, result, network_failure): network_failure is True when nothing answered there."""
    req = urllib.request.Request(
        f"http://{target}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    if password:
        token = base64.b64encode(f"admin:{password}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", "Basic " + token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # LAN: never via proxy
    try:
        with opener.open(req, timeout=timeout) as r:
            code = r.getcode()
            r.read(256)
        return (200 <= code < 300), ("OK" if 200 <= code < 300 else f"HTTP {code}"), False
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", False
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return False, t("timeout"), True
        return False, t("error: {name}", name=type(e.reason).__name__), True
    except (socket.timeout, TimeoutError):
        return False, t("timeout"), True
    except Exception as e:
        return False, t("error: {name}", name=type(e).__name__), True


def send_all(targets, payload, timeout=NET_TIMEOUT, resolve_timeout=RESOLVE_TIMEOUT):
    """Send to every destination at the same time (one thread each), so one that is off or on
    another network never delays the others: the whole call takes at most about one
    destination's lookup + timeout. Returns {id: (ok, result, seconds)}."""
    import threading
    results = {}

    def work(d):
        t0 = time.monotonic()
        ok, result = send(d["address"], d.get("password", ""), payload, d["path"], timeout, resolve_timeout)
        results[d["id"]] = (ok, result, time.monotonic() - t0)
    threads = [threading.Thread(target=work, args=(d,), daemon=True) for d in targets]
    for th in threads:
        th.start()
    # a kept IP that does not answer: one post, a new lookup and one more post
    limit = time.monotonic() + resolve_timeout + 2 * timeout + 1.0
    for th in threads:
        th.join(max(0.0, limit - time.monotonic()))
    for d in targets:
        results.setdefault(d["id"], (False, t("timeout"), resolve_timeout + 2 * timeout))
    return results


def record_sends(targets, results, now, source):
    """Keep the last send to each destination for the settings page (never fails)."""
    try:
        data = load_json(sends_path(), {})
        for d in targets:
            ok, result = results[d["id"]][:2]
            prev = data.get(d["id"]) if isinstance(data.get(d["id"]), dict) else {}
            data[d["id"]] = {"ok": ok, "result": result, "at": now, "address": d["address"],
                             "source": source, "last_ok": now if ok else prev.get("last_ok")}
        save_json_atomic(sends_path(), data)
    except Exception:
        pass


def fetch_json(ip, path, timeout=NET_TIMEOUT):
    """GET http://<ip><path> -> dict, or None. `ip` may be a name (bounded lookup)."""
    target, _, _ = resolve(ip)
    if target is None:
        return None
    req = urllib.request.Request(f"http://{target}{path}", headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read(65536).decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_status(ip):
    """GET http://<ip>/api/status -> dict, or None. Used to recognise the device."""
    return fetch_json(ip, "/api/status")


ROBOT_DEVICE_ID = "latiddo-robot"   # "device" in the robot's /api/status (Latiddo 0.5.2 and newer)


def is_robot(status):
    """True for a Latiddo robot: "device": "latiddo-robot" in /api/status, or (older firmware)
    "board": "latiddo-..." with its "host"."""
    if not isinstance(status, dict):
        return False
    if status.get("device") == ROBOT_DEVICE_ID:
        return True
    return str(status.get("board") or "").startswith("latiddo-") and bool(status.get("host"))


def is_device(status):
    """True for a Latiddo device (/api/status says "device": "latiddo")."""
    return isinstance(status, dict) and status.get("device") in DEVICE_IDS


def read_stdin():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:
        return ""


def sanitize(obj, top=True):
    """For --dump: keep key names; drop transcript_path, cwd and ids; keep
    rate_limits values; replace other strings with their type."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in PRIVATE_KEYS or kl == "id" or kl.endswith("_id"):
                continue
            if top and k == "rate_limits":
                out[k] = v
            elif isinstance(v, str):
                out[k] = v if kl in KEEP_STRING_KEYS else "<str>"
            else:
                out[k] = sanitize(v, top=False)
        return out
    if isinstance(obj, list):
        return [sanitize(x, top=False) for x in obj[:3]]
    if isinstance(obj, str):
        return "<str>"
    return obj


def write_line(text):
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Status line mode
# ----------------------------------------------------------------------------
def run_statusline(dump=False):
    raw = read_stdin()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    if dump:
        try:
            save_json_atomic(dump_path(), {
                "_dumped_at": dt.datetime.now().isoformat(timespec="seconds"),
                "input": sanitize(data),
            }, indent=2)
        except Exception:
            pass

    now = time.time()
    state = load_json(state_path(), {})
    limits = get_limits(data)

    if limits is None:
        # Log at most once a minute: the status line runs very often.
        if now - float(state.get("last_absent_log", 0)) >= MIN_INTERVAL_S:
            keys = ", ".join(sorted(str(k) for k in data.keys())) or t("(none)")
            rl = data.get("rate_limits")
            extra = ""
            if isinstance(rl, dict):
                extra = "; rate_limits: " + (", ".join(sorted(str(k) for k in rl.keys())) or t("(empty)"))
            log(t("rate_limits missing; keys received: {keys}", keys=keys) + extra)
            state["last_absent_log"] = now
        write_line(t("Latiddo: no data"))
    else:
        payload = build_payload(limits, now)
        targets = load_targets()
        key = [payload["sesion"], payload["semanal"],
               int(limits["five_hour"][1]), int(limits["seven_day"][1])]
        if not targets:
            if now - float(state.get("last_noip_log", 0)) >= MIN_INTERVAL_S:
                log(t("no device configured (latiddo.json, LATIDDO_IP or LATIDDO_ROBOT); nothing sent"))
                state["last_noip_log"] = now
        else:
            per = state.get("targets") if isinstance(state.get("targets"), dict) else {}
            due = []
            for d in targets:
                st = per.get(d["id"]) if isinstance(per.get(d["id"]), dict) else {}
                if now < float(st.get("fail_until", 0)):
                    continue  # did not answer recently: wait for the back-off
                if key != st.get("last_key") or now - float(st.get("last_ok", 0)) >= MIN_INTERVAL_S:
                    due.append(d)
            if due:
                results = send_all(due, payload, resolve_timeout=STATUSLINE_RESOLVE_TIMEOUT)
                record_sends(due, results, now, "statusline")
                for d in due:
                    ok, result = results[d["id"]][:2]
                    st = per.setdefault(d["id"], {})
                    line = (f"{d['log']}{d['address']} {result} sesion={payload['sesion']} "
                            f"semanal={payload['semanal']} reset={payload['reset_h']}h"
                            f"{payload['reset_m']:02d}m label='{payload['reset_label']}' "
                            f"raw5h={limits['five_hour'][0]} raw7d={limits['seven_day'][0]}")
                    if ok:
                        log(line)
                        st.update(last_ok=now, last_key=key, fail_until=0, last_err="")
                    else:
                        # the same failure again: one line in FAIL_LOG_REPEAT_S, not one a minute
                        if (st.get("last_err") != result
                                or now - float(st.get("err_logged", 0)) >= FAIL_LOG_REPEAT_S):
                            log(line)
                            st.update(last_err=result, err_logged=now)
                        st["fail_until"] = now + BACKOFF_S
            state["targets"] = per
        write_line(f"Latiddo 5h {payload['sesion']}% | 7d {payload['semanal']}%")

    try:
        save_json_atomic(state_path(), state)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Claude Code hooks: live status on the Pulse
# ----------------------------------------------------------------------------
HOOK_FLAG = "--hook"
HOOK_TIMEOUT = 0.6          # seconds for the whole request (connect + answer): never slows Claude
# The robot in standby has its Wi-Fi in power save (WIFI_PS_MAX_MODEM, listen interval 10: the
# radio wakes about once a second; measured on a Waveshare in standby: 0.4-2.4 s, usually ~1.4 s,
# for one request), so it gets more time and one retry at once. The async hooks have
# HOOK_ASYNC_TIMEOUT; SessionEnd is not async (Claude Code waits for it when it closes): short.
HOOK_ASYNC_TIMEOUT = 10     # "timeout" of our async hooks in settings.json (they never slow Claude)
ROBOT_HOOK_TIMEOUT = 3.5    # one try
ROBOT_HOOK_BUDGET = 8.0     # the tries together
ROBOT_HOOK_END_BUDGET = 1.0  # SessionEnd: one short try
ROBOT_HOOK_DOWN_BUDGET = 1.0  # the robot did not take the last hook (<= ROBOT_DOWN_S ago): one short
ROBOT_DOWN_S = 60            # try, so a robot that is off does not keep a process 8 s per hook;
                             # the poller's resend (full time) catches up
HOOK_RESOLVE_TIMEOUT = 0.3  # a robot name never looked up before: a short try (the poller keeps its IP)
ROBOT_HOOK_PATH = "/api/claude-state"
HOOK_RECORD_S = 30          # the last hook of each device for the settings page: at most every 30 s
HOOK_LOG_REPEAT_S = 600     # a device that does not take the hooks: one log line in 10 min
NO_HOOK_ENV = "LATIDDO_NO_HOOK"   # set by the /usage poller: its own `claude -p` runs send nothing
# A UserPromptSubmit that Claude Code starts by itself, not a message of the user (1.3.3)
AUTO_PROMPT_PREFIXES = ("<task-notification>",)
TURN_END_EVENTS = ("Stop", "StopFailure", "SessionEnd")
# The last state of each session (one small file per short session id) and the poller's resend to
# the robot: a lost hook never leaves the robot with an old state. The same times as the robot's
# and the Pulse's cc_core.h: a session with no news in 30 min is gone, "working" in 10 min stopped.
CC_EXPIRE_MS = 30 * 60 * 1000
CC_WORK_STALE_MS = 10 * 60 * 1000
SNAPSHOT_EVENT = "Snapshot"
SNAPSHOT_MAX = 8            # sessions in a snapshot (the robot keeps 8)
SNAPSHOT_MAX_BYTES = 1000   # the robot takes 1 KB per request
# What the snapshot says for each kept state. "done" goes as "idle": the same state without the
# robot's "Listo" wink (a snapshot is never news); "end" is left out (the robot ends what is not
# in the snapshot).
RESEND_STATES = {"working": "working", "needs_you": "needs_you", "done": "idle", "idle": "idle",
                 "error": "error", "end": "end"}
# Event -> state. Names, matchers and fields as in https://code.claude.com/docs/en/hooks
HOOK_STATES = {
    "UserPromptSubmit": "working",
    "PostToolUse": "working",
    "PostToolUseFailure": "working",   # the tool failed, Claude goes on
    "PermissionRequest": "needs_you",
    "Stop": "done",
    "StopFailure": "error",
    "SessionEnd": "end",
}
# Notification, by notification_type: a permission prompt needs you; idle_prompt (waiting for
# your next prompt after finishing) is not an alarm, the Pulse just keeps "done"
HOOK_NOTIFY = {"permission_prompt": "needs_you", "idle_prompt": "idle"}
# (event, matcher or None, async). SessionEnd runs when Claude Code closes: an async hook could
# be cut off then, so it runs normally (its default budget is 1.5 s; this script takes less).
HOOK_EVENTS = [
    ("UserPromptSubmit", None, True),
    ("PostToolUse", None, True),
    ("PostToolUseFailure", None, True),
    ("PermissionRequest", None, True),
    ("Notification", "permission_prompt|idle_prompt", True),
    ("Stop", None, True),
    ("StopFailure", None, True),
    ("SessionEnd", None, False),
]


def hook_payload(data, now_ms):
    """Hook input -> what the devices get, or None. Only these five fields, nothing else (send_hooks
    adds src for the robot and the Pulse)."""
    if not isinstance(data, dict):
        return None
    event = str(data.get("hook_event_name") or "")
    if event == "Notification":
        state = HOOK_NOTIFY.get(str(data.get("notification_type") or ""))
    else:
        state = HOOK_STATES.get(event)
    sid = data.get("session_id")
    if not state or not isinstance(sid, str) or not sid:
        return None
    short = hashlib.sha256(("latiddo:" + sid).encode("utf-8")).hexdigest()[:8]
    if short == "00000000":
        short = "00000001"
    return {"event": event, "state": state, "sid": short, "ts": int(now_ms),
            "bridge_version": BRIDGE_VERSION}


def cc_dir():
    return Path(tempfile.gettempdir()) / "latiddo_cc"


def user_prompt(data):
    """A UserPromptSubmit of a message the user wrote (True), or one Claude Code started by itself
    (False): a background task's notice comes as a prompt that begins with <task-notification>
    (seen with a test session, 1.3.3). Only the start of the prompt is looked at; it is never kept,
    logged or sent."""
    prompt = data.get("prompt") if isinstance(data, dict) else None
    return not (isinstance(prompt, str) and prompt.lstrip().startswith(AUTO_PROMPT_PREFIXES))


def keep_cc_state(payload, user=None):
    """The last state of the hook's session, for the poller's resend (a newer one is never
    replaced by an older one: async hooks can finish out of order), and whether its turn was started
    by a message of the user (user_turn, 1.3.3: set by UserPromptSubmit with `user`, cleared by Stop,
    StopFailure and SessionEnd). Returns (the state kept before, the turn's user_turn before this
    hook): (None, False) for a session not seen yet."""
    before = (None, False)
    try:
        sid = str(payload["sid"])
        if not (len(sid) == 8 and all(c in "0123456789abcdef" for c in sid)):
            return before
        path = cc_dir() / (sid + ".json")
        old = load_json(path, {})
        if not isinstance(old, dict):
            old = {}
        before = (old.get("state"), old.get("user_turn") is True)
        if int(old.get("ts", 0) or 0) > int(payload["ts"]):
            return before
        user_turn = before[1]
        if payload["event"] == "UserPromptSubmit":
            user_turn = bool(user)
        elif payload["event"] in TURN_END_EVENTS:
            user_turn = False
        path.parent.mkdir(parents=True, exist_ok=True)
        save_json_atomic(path, {"state": payload["state"], "event": payload["event"], "ts": int(payload["ts"]),
                                "user_turn": user_turn})
    except Exception:
        pass
    return before


def log_hook(payload, before_state, user_turn, quiet):
    """The hook in latiddo.log (1.3.3): the session (its short id), the event, the state and whether
    its turn began with a message of the user. The repeats of a working session (each tool of a
    turn) are left out."""
    if payload["event"] in ("PostToolUse", "PostToolUseFailure") and before_state == "working":
        return
    turn = t("yours") if user_turn else t("automatic")
    extra = t(", «Listo» without a beep") if quiet else ""
    log(t("hook session={sid} {event} -> {state}, turn: {turn}{extra}", sid=payload["sid"], event=payload["event"],
          state=payload["state"], turn=turn, extra=extra))


def bridge_src():
    """This PC's bridge, for the robot: 8 hex of a hash of the computer and user names (never the
    names themselves). The robot's snapshot only ends sessions of the bridge that sent it."""
    h = hashlib.sha256(("latiddo-bridge:" + socket.gethostname() + ":" + str(Path.home())).encode("utf-8"))
    src = h.hexdigest()[:8]
    return "00000001" if src == "00000000" else src


def snapshot_sessions(now_ms=None):
    """The live sessions for the poller's snapshot to the robot: each one's kept state with its
    own time (ts) and age_ms, how old it is (the robot ages the session from it, so a snapshot
    never keeps a session alive longer than on the PC). "working" older than 10 min (stopped) and
    "end" are left out (the robot ends what is not listed). At most the 8 newest (the robot keeps
    8). Removes the expired and broken files."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    out = []
    try:
        files = sorted(cc_dir().glob("*.json"))
    except Exception:
        return out
    for path in files:
        data = load_json(path, {})   # {} also for a broken file: removed below
        try:
            state = RESEND_STATES.get(str(data.get("state")))
            ts = int(data.get("ts"))
        except Exception:
            state, ts = None, 0
        age = max(0, int(now_ms - ts))
        if state is None or ts <= 0 or age >= CC_EXPIRE_MS:
            try:
                path.unlink()
            except Exception:
                pass
            continue
        if state == "end" or (state == "working" and age >= CC_WORK_STALE_MS):
            continue
        out.append({"sid": path.stem, "state": state, "ts": ts, "age_ms": age})
    out.sort(key=lambda p: p["ts"])
    return out[-SNAPSHOT_MAX:]


def snapshot_payload(now_ms=None):
    """{"event": "Snapshot", "src", "sessions": [...], "bridge_version"}, within the robot's 1 KB."""
    sessions = snapshot_sessions(now_ms)
    while True:
        payload = {"event": SNAPSHOT_EVENT, "src": bridge_src(), "sessions": sessions, "bridge_version": BRIDGE_VERSION}
        if len(json.dumps(payload, separators=(",", ":"))) <= SNAPSHOT_MAX_BYTES or not sessions:
            return payload
        sessions = sessions[1:]   # the oldest out


def snapshot_state(sessions):
    """One state for the settings page: needs_you, else working, else the newest one; "end": none."""
    states = [x["state"] for x in sessions]
    for s in ("needs_you", "working"):
        if s in states:
            return s
    return states[-1] if states else "end"


# The Pulse takes the snapshot from firmware 3.18.0 on (src in the hooks goes to every Pulse: an
# older one ignores it). Its firmware is read from its /api/status and kept in the temp folder:
# by --setup when it finds the Pulse, then by the poller again every 6 h, every 30 min while it is
# older (an update shows up soon), and at once when the Pulse's address changes. A Pulse that does
# not answer keeps what was known.
PULSE_SNAPSHOT_FW = (3, 18, 0)
PULSE_FW_RECHECK_OK_S = 6 * 3600
PULSE_FW_RECHECK_OLD_S = 30 * 60


def pulse_fw_path():
    return Path(tempfile.gettempdir()) / "latiddo_pulse_fw.json"


def fw_at_least(fw, minimum):
    """'3.18.0' (also '3.18.0-dev') >= (3, 18, 0); False if it is not a version."""
    try:
        parts = tuple(int(x) for x in str(fw).split("-")[0].split("."))
    except ValueError:
        return False
    return len(parts) == 3 and parts >= tuple(minimum)


def pulse_takes_snapshot(address, check=True, now=None):
    """True when the Pulse at `address` has firmware 3.18.0 or newer. check=False (the hooks):
    only what is kept, never the network."""
    now = time.time() if now is None else now
    kept = load_json(pulse_fw_path(), {})
    same = isinstance(kept, dict) and kept.get("address") == address
    ok = same and kept.get("ok") is True
    if not check:
        return ok
    try:
        age = now - float(kept.get("at", 0)) if same else None
    except (TypeError, ValueError):
        age = None
    if age is not None and 0 <= age < (PULSE_FW_RECHECK_OK_S if ok else PULSE_FW_RECHECK_OLD_S):
        return ok
    status = fetch_json(address, "/api/status", POLL_NET_TIMEOUT)
    if not isinstance(status, dict):
        return ok
    return remember_pulse_fw(address, status.get("fw"), now)


def remember_pulse_fw(address, fw, now=None):
    """Keep the firmware of the Pulse at `address` (from its /api/status). Returns True if it
    takes the snapshot (3.18.0 or newer)."""
    now = time.time() if now is None else now
    fw = str(fw or "")
    ok = fw_at_least(fw, PULSE_SNAPSHOT_FW)
    try:
        save_json_atomic(pulse_fw_path(), {"address": address, "fw": fw, "ok": ok, "at": now})
    except Exception:
        pass
    return ok


def resend_cc_state(now_ms=None):
    """The poller, every run: the snapshot of every live Claude Code session to the robot, even an
    empty one, so the robot ends the sessions this PC no longer has; then the same snapshot to the
    Pulse if its firmware takes it (3.18.0 or newer), never to an older one. Only with our hooks
    installed. Returns True if the robot took it, None if nothing was sent to the robot."""
    if not hooks_present():
        return None
    payload = None
    ok = None
    robot = load_robot()
    if robot:
        payload = snapshot_payload(now_ms)
        target = {"id": "latiddo", "address": robot, "password": "", "path": ROBOT_HOOK_PATH, "log": "latiddo="}
        ok = send_hooks([target], payload, record=False, full=True)["latiddo"]
        record_hooks([target], {"latiddo": ok}, {"state": snapshot_state(payload["sessions"])})
    resend_cc_state_pulse(payload, now_ms)
    return ok


def resend_cc_state_pulse(payload=None, now_ms=None):
    """The snapshot to the Pulse (the same one as the robot's), if its firmware takes it.
    Returns True if the Pulse took it, None if nothing was sent."""
    ip, password = load_config()
    if not ip or not pulse_takes_snapshot(ip):
        return None
    payload = payload or snapshot_payload(now_ms)
    target = {"id": "pulse", "address": ip, "password": password, "path": "/claude-state", "log": "ip="}
    ok = send_hooks([target], payload, record=False, full=True)["pulse"]
    record_hooks([target], {"pulse": ok}, {"state": snapshot_state(payload["sessions"])})
    return ok


def send_hook_robot(ip, payload, budget=ROBOT_HOOK_BUDGET, path=ROBOT_HOOK_PATH):
    """The hook to the robot: ROBOT_HOOK_TIMEOUT per try, a second try at once if the first
    fails, both within budget seconds. True if 2xx."""
    end = time.monotonic() + budget
    for _ in range(2):
        left = end - time.monotonic()
        if left < 0.3:
            break
        if send_hook(ip, "", payload, timeout=min(ROBOT_HOOK_TIMEOUT, left), path=path):
            return True
    return False


def send_hook(ip, password, payload, timeout=HOOK_TIMEOUT, path="/claude-state"):
    """POST to /claude-state (the robot: /api/claude-state) with one deadline for connect +
    answer. No retries. True if 2xx."""
    import http.client
    deadline = time.monotonic() + timeout
    host, _, port = ip.partition(":")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "Connection": "close"}
    if password:
        headers["Authorization"] = "Basic " + base64.b64encode(f"admin:{password}".encode("utf-8")).decode("ascii")
    conn = http.client.HTTPConnection(host, int(port) if port.isdigit() else 80, timeout=timeout)
    try:
        conn.connect()
        conn.sock.settimeout(max(0.05, deadline - time.monotonic()))
        conn.request("POST", path, body=body, headers=headers)
        r = conn.getresponse()
        r.read(256)
        return 200 <= r.status < 300
    except Exception:
        return False
    finally:
        conn.close()


def run_hook():
    now_ms = time.time() * 1000      # when the hook started, before reading its input
    if os.environ.get(NO_HOOK_ENV):
        return
    raw = sys.stdin.buffer.read() if sys.stdin is not None else b""
    data = json.loads(raw.decode("utf-8", "replace") or "{}")
    payload = hook_payload(data, now_ms)
    if payload is None:
        return
    user = user_prompt(data) if payload["event"] == "UserPromptSubmit" else None
    before_state, user_turn = keep_cc_state(payload, user)
    if payload["event"] == "UserPromptSubmit":
        user_turn = user
    # «Listo» with its beep only after a message of the user since the last Stop (1.3.3): the turns
    # Claude Code starts by itself (a background task's notice) end in a quiet «Listo», for the
    # robot only (from 0.6.13; an older robot ignores the field and beeps as before)
    quiet = payload["event"] == "Stop" and not user_turn
    log_hook(payload, before_state, user_turn, quiet)
    if quiet:
        payload["quiet"] = True
    send_hooks(hook_targets(), payload)


def hook_targets():
    """The devices that get the hooks: the Pulse and the robot switched on in the config."""
    out = []
    ip, password = load_config()
    if ip:
        out.append({"id": "pulse", "address": ip, "password": password, "path": "/claude-state", "log": "ip="})
    robot = load_robot()
    if robot:
        out.append({"id": "latiddo", "address": robot, "password": "", "path": ROBOT_HOOK_PATH, "log": "latiddo="})
    return out


def robot_down_recently(now=None):
    """The robot did not take the last hook, at most ROBOT_DOWN_S ago (the settings page's record)."""
    now = time.time() if now is None else now
    entry = load_json(sends_path(), {}).get("hook_latiddo")
    try:
        return isinstance(entry, dict) and entry.get("ok") is False and now - float(entry.get("at", 0)) < ROBOT_DOWN_S
    except Exception:
        return False


def send_hooks(targets, payload, record=True, full=False):
    """The hook to every device at the same time (one thread each): one that is off never delays
    the other; the Pulse has HOOK_TIMEOUT, the robot ROBOT_HOOK_BUDGET (two tries); one short try
    for SessionEnd and, unless full (the poller's resend, --test-hook), when the robot did not take
    the last hook in the last minute. Returns {id: ok}."""
    import threading
    results = {}
    if payload.get("event") == "SessionEnd":
        robot_budget = ROBOT_HOOK_END_BUDGET
    elif not full and any(d["id"] == "latiddo" for d in targets) and robot_down_recently():
        robot_budget = ROBOT_HOOK_DOWN_BUDGET
    else:
        robot_budget = ROBOT_HOOK_BUDGET

    def work(d):
        address = d["address"]
        host, _ = split_host(address)
        if host and not is_ip(host):
            address, _, _ = resolve(address, HOOK_RESOLVE_TIMEOUT)   # the kept IP, at once
        if not address:
            results[d["id"]] = False
        elif d["id"] == "latiddo":
            # the robot also gets which bridge sent it (src): its snapshot ends only this PC's sessions
            robot_payload = payload if "src" in payload else dict(payload, src=bridge_src())
            results[d["id"]] = send_hook_robot(address, robot_payload, robot_budget, d["path"])
        else:
            # the Pulse too, whatever its firmware (one before 3.18.0 ignores src): a session it
            # gets with src is always one that this PC's snapshot can end. «quiet» is the robot's
            # only (the Pulse has no beep)
            pulse_payload = {k: v for k, v in payload.items() if k != "quiet"}
            pulse_payload.setdefault("src", bridge_src())
            results[d["id"]] = send_hook(address, d["password"], pulse_payload, path=d["path"])
    threads = [threading.Thread(target=work, args=(d,), daemon=True) for d in targets]
    for th in threads:
        th.start()
    longest = max([robot_budget if d["id"] == "latiddo" else HOOK_TIMEOUT for d in targets] or [HOOK_TIMEOUT])
    limit = time.monotonic() + HOOK_RESOLVE_TIMEOUT + longest + 0.2
    for th in threads:
        th.join(max(0.0, limit - time.monotonic()))
    for d in targets:
        results.setdefault(d["id"], False)
    if record:
        record_hooks(targets, results, payload)
    return results


def record_hooks(targets, results, payload):
    """The last hook of each device for the settings page (at most every HOOK_RECORD_S, or when
    the result changes) and, in the log, a device that does not take them (once in 10 min)."""
    try:
        now = time.time()
        data = load_json(sends_path(), {})
        changed = False
        for d in targets:
            key = "hook_" + d["id"]
            ok = results[d["id"]]
            prev = data.get(key) if isinstance(data.get(key), dict) else {}
            if prev.get("ok") != ok or now - float(prev.get("at", 0)) >= HOOK_RECORD_S:
                entry = {"ok": ok, "at": now, "state": payload.get("state", ""), "address": d["address"],
                         "logged_at": prev.get("logged_at", 0)}
                if not ok and now - float(prev.get("logged_at", 0)) >= HOOK_LOG_REPEAT_S:
                    log(t("{who} does not take the Claude Code hooks ({state})", who=d["log"] + d["address"],
                          state=payload.get("state", "")))
                    entry["logged_at"] = now
                data[key] = entry
                changed = True
        if changed:
            save_json_atomic(sends_path(), data)
    except Exception:
        pass


def cmd_test_hook(state):
    """A Claude Code event without Claude Code: the notices on the Pulse and the robot."""
    targets = hook_targets()
    if not targets:
        print(t("No device. Use --install --ip <ip>, --test --ip <ip>, LATIDDO_IP or LATIDDO_ROBOT."))
        return 1
    names = {"needs_you": "PermissionRequest", "working": "UserPromptSubmit", "done": "Stop",
             "end": "SessionEnd", "error": "StopFailure", "idle": "Notification"}
    payload = {"event": names.get(state, "test"), "state": state, "sid": "7e57c0de", "ts": int(time.time() * 1000),
               "bridge_version": BRIDGE_VERSION}
    keep_cc_state(payload)   # as a real session: the snapshot every 2 min carries it too
    results = send_hooks(targets, payload, record=False, full=True)
    for d in targets:
        print(f"POST http://{d['address']}{d['path']} {state} -> {'OK' if results[d['id']] else t('no answer')}")
    return 0 if all(results.values()) else 1


def hook_python():
    """Python for the hooks: pythonw.exe on Windows (never a console window)."""
    py = Path(sys.executable).resolve()
    if os.name == "nt" and py.with_name("pythonw.exe").is_file():
        py = py.with_name("pythonw.exe")
    return str(py)


def _is_our_hook(h):
    if not isinstance(h, dict):
        return False
    args = h.get("args")
    if isinstance(args, list):
        a = [str(x).replace("\\", "/") for x in args]
        if HOOK_FLAG in a and any(x.endswith("/" + SCRIPT_NAME) or x == SCRIPT_NAME for x in a):
            return True
    cmd = str(h.get("command", ""))
    return SCRIPT_NAME in cmd and HOOK_FLAG in cmd


def strip_our_hooks(hooks):
    """Remove only our handlers from a settings "hooks" object (in place). Groups and events
    that become empty because of that are removed too; everything else is left as it was.
    Returns how many handlers were removed."""
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups, touched = [], False
        for g in groups:
            hs = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(hs, list):
                kept_groups.append(g)
                continue
            kept = [h for h in hs if not _is_our_hook(h)]
            if len(kept) == len(hs):
                kept_groups.append(g)
                continue
            removed += len(hs) - len(kept)
            touched = True
            if kept:
                g = dict(g)
                g["hooks"] = kept
                kept_groups.append(g)
        if touched:
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
    return removed


def hooks_present(settings=None):
    if settings is None:
        settings, err = read_settings()
        if err:
            return False
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return strip_our_hooks(json.loads(json.dumps(hooks))) > 0


def backup_settings_stamped():
    """Copy settings.json to settings.json.latiddo-YYYYmmdd-HHMMSS.bak (if it exists)."""
    sp = settings_path()
    if not sp.exists():
        return None
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    bak = sp.with_name(f"settings.json.latiddo-{stamp}.bak")
    n = 1
    while bak.exists():               # never overwrite an earlier backup (two runs in one second)
        bak = sp.with_name(f"settings.json.latiddo-{stamp}-{n}.bak")
        n += 1
    shutil.copy2(sp, bak)
    return bak


def install_hooks(py, script):
    """Add our hooks to ~/.claude/settings.json, keeping every other hook and setting (a
    backup is made first; running it again replaces ours, it never duplicates them).
    Returns (ok, message)."""
    settings, err = read_settings()
    if err:
        return False, err
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False, t('"hooks" in {path} is not a JSON object. Nothing was changed.', path=settings_path())
    for event, _, _ in HOOK_EVENTS:
        if event in hooks and not isinstance(hooks[event], list):
            return False, t('"hooks.{event}" in {path} is not a list. Nothing was changed.', event=event,
                            path=settings_path())
    strip_our_hooks(hooks)
    for event, matcher, is_async in HOOK_EVENTS:
        handler = {"type": "command", "command": str(py), "args": [str(script), HOOK_FLAG]}
        if is_async:
            handler["async"] = True
            handler["timeout"] = HOOK_ASYNC_TIMEOUT
        group = {"matcher": matcher, "hooks": [handler]} if matcher else {"hooks": [handler]}
        hooks.setdefault(event, []).append(group)
    settings["hooks"] = hooks
    bak = backup_settings_stamped()
    save_json_atomic(settings_path(), settings, indent=2)
    msg = t("Added the Claude Code hooks to {path}", path=settings_path())
    if bak:
        msg += t(" (backup: {path})", path=bak)
    return True, msg + "."


def remove_hooks():
    """Remove only our hooks from settings.json. Returns (removed_count, message)."""
    settings, err = read_settings()
    if err:
        return 0, err
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0, t("No Latiddo hooks were found.")
    probe = json.loads(json.dumps(hooks))
    n = strip_our_hooks(probe)
    if not n:
        return 0, t("No Latiddo hooks were found.")
    if probe:
        settings["hooks"] = probe
    else:
        del settings["hooks"]
    bak = backup_settings_stamped()
    save_json_atomic(settings_path(), settings, indent=2)
    msg = t("Removed the Latiddo hooks from {path} (other hooks were kept)", path=settings_path())
    if bak:
        msg += t("; backup: {path}", path=bak)
    return n, msg + "."


def remove_statusline():
    """Remove this script's statusLine from settings.json, a dated copy first (1.2.1, --remove).
    Returns (removed, message); another status line is never touched."""
    settings, err = read_settings()
    if err:
        return False, err
    if not is_ours(settings.get("statusLine")):
        return False, t("No Latiddo status line was found.")
    bak = backup_settings_stamped()
    del settings["statusLine"]
    save_json_atomic(settings_path(), settings, indent=2)
    msg = t("Removed the Latiddo statusLine from {path}", path=settings_path())
    if bak:
        msg += t("; backup: {path}", path=bak)
    return True, msg + "."


# ----------------------------------------------------------------------------
# Command line options
# ----------------------------------------------------------------------------
def our_command():
    py = Path(sys.executable).resolve().as_posix()
    script = Path(__file__).resolve().as_posix()
    return f'"{py}" "{script}"'


LEGACY_SCRIPT_NAMES = ("holdtag_pulse_statusline.py",)   # the bridge's earlier name (1.2.1)


def is_ours(status_line):
    """This script's status line, also under the bridge's earlier name (setup replaces it, --remove
    removes it)."""
    if not isinstance(status_line, dict):
        return False
    command = str(status_line.get("command", ""))
    return any(name in command for name in (SCRIPT_NAME,) + LEGACY_SCRIPT_NAMES)


def read_settings():
    """Return (settings_dict, error_message)."""
    sp = settings_path()
    if not sp.exists():
        return {}, None
    try:
        text = sp.read_text(encoding="utf-8")
        settings = json.loads(text) if text.strip() else {}
    except Exception as e:
        return None, t("{path} is not valid JSON ({error}). Nothing was changed.", path=sp, error=e)
    if not isinstance(settings, dict):
        return None, t("{path} does not contain a JSON object. Nothing was changed.", path=sp)
    return settings, None


def backup_settings():
    sp = settings_path()
    if sp.exists():
        bak = sp.with_name("settings.json.bak")
        shutil.copy2(sp, bak)
        return bak
    return None


def cmd_install(ip_arg):
    ip = normalize_ip(ip_arg)
    if not ip and not load_robot():
        print(t("Missing --ip (for example: --install --ip 192.168.1.50)."))
        return 1
    settings, err = read_settings()
    if err:
        print(err)
        return 1

    existing = settings.get("statusLine")
    if existing is not None and not is_ours(existing):
        script = Path(__file__).resolve().as_posix()
        py = Path(sys.executable).resolve().as_posix()
        print(t("A statusLine is already configured in {path}", path=settings_path()))
        print(json.dumps({"statusLine": existing}, indent=2, ensure_ascii=False))
        print()
        print(t("It was NOT changed. To use both, call this script from your existing status line "
                "script, passing it the same JSON and discarding its output."))
        print(t("In a bash script that already did  input=$(cat):"))
        print(f'  printf "%s" "$input" | "{py}" "{script}" > /dev/null')
        print(t("In a Python status line script, with the raw stdin text in `raw`:"))
        print(f'  subprocess.run([r"{py}", r"{script}"], input=raw.encode(), capture_output=True, timeout=5)')
        print(t("Then save the device IP with LATIDDO_IP or in {path}", path=config_path()))
        return 1

    cfg = load_json(config_path(), {})
    if ip:
        cfg["ip"] = ip                   # keep the other keys (password, claude_path, robot)
    cfg.setdefault("password", "")
    bak = backup_settings()
    save_json_atomic(config_path(), cfg, indent=2)
    settings["statusLine"] = {"type": "command", "command": our_command()}
    save_json_atomic(settings_path(), settings, indent=2)

    print(t("Installed."))
    print("  config:   ", config_path(), f"(ip {ip})" if ip else f"(robot {load_robot()})")
    print("  settings: ", settings_path(), "-> statusLine =", our_command())
    if bak:
        print("  backup:   ", bak)
    print(t("Next: run with --test to check the device, then restart Claude Code."))
    return 0


def cmd_uninstall():
    settings, err = read_settings()
    if err:
        print(err)
        return 1
    existing = settings.get("statusLine")
    if existing is None:
        print(t("No statusLine configured. Nothing to do."))
        return 0
    if not is_ours(existing):
        print(t("The configured statusLine is not this script; it was left unchanged:"))
        print(json.dumps({"statusLine": existing}, indent=2, ensure_ascii=False))
        return 0
    bak = backup_settings()
    del settings["statusLine"]
    save_json_atomic(settings_path(), settings, indent=2)
    print(t("Removed the Latiddo statusLine from {path}", path=settings_path()))
    if bak:
        print("  backup:", bak)
    print(t("Config and log were kept: {config} and {log}", config=config_path(), log=log_path()))
    return 0


def cmd_test(ip_arg):
    if ip_arg:
        targets = [{"id": "pulse", "label": "Pulse", "address": normalize_ip(ip_arg),
                    "path": PULSE_PATH, "password": load_config()[1], "log": "ip="}]
    else:
        targets = load_targets()
    if not targets:
        print(t("No device. Use --install --ip <ip>, --test --ip <ip>, LATIDDO_IP or LATIDDO_ROBOT."))
        return 1
    payload = {
        "sesion": 42, "semanal": 17, "reset_h": 2, "reset_m": 15,
        "reset_label": "Sat 10:00", "extra_used": 0, "extra_limit": 0, "extra_label": "",
    }
    results = send_all(targets, payload)
    record_sends(targets, results, time.time(), "test")
    for d in targets:
        ok, result, took = results[d["id"]]
        log(f"{d['log']}{d['address']} {result} (test)")
        print(f"POST http://{d['address']}{d['path']} -> {result} ({took:.2f} s)")
    print(t("Sent:"), json.dumps(payload))
    return 0 if all(results[d["id"]][0] for d in targets) else 1


def main():
    args = sys.argv[1:]
    if args == [HOOK_FLAG]:
        # Claude Code hook: silent (Claude Code reads a hook's stdout), never fails, exit code 0
        try:
            sys.stdout = sys.stderr = open(os.devnull, "w")
        except Exception:
            pass
        try:
            run_hook()
        except BaseException:
            pass
        os._exit(0)
    if not args or args == ["--dump"]:
        # Status line mode: silent, never fails, always exit code 0.
        try:
            sys.stderr = open(os.devnull, "w")
        except Exception:
            pass
        try:
            run_statusline(dump=bool(args))
        except BaseException:
            write_line(t("Latiddo: no data"))
        os._exit(0)

    lang_from_argv(args)
    parser = argparse.ArgumentParser(description=t("Latiddo - Claude Code status line bridge"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true", help=t("save the IP and add the statusLine to Claude Code"))
    group.add_argument("--uninstall", action="store_true", help=t("remove this script's statusLine"))
    group.add_argument("--test", action="store_true", help=t("send sample data to the devices"))
    group.add_argument("--dump", action="store_true", help=t("status line mode that also saves the sanitized input"))
    group.add_argument("--test-hook", choices=["needs_you", "working", "done", "end"],
                       help=t("send a Claude Code event to the devices (the notices, without Claude Code)"))
    parser.add_argument("--ip", help=t("Latiddo Pulse IP, for example 192.168.1.50"))
    parser.add_argument("--lang", choices=["es", "en", "auto"],
                        help=t("language of the bridge (saved; auto = the system language)"))
    opts = parser.parse_args(args)
    if opts.install:
        return cmd_install(opts.ip)
    if opts.uninstall:
        return cmd_uninstall()
    if opts.test:
        return cmd_test(opts.ip)
    if opts.test_hook:
        return cmd_test_hook(opts.test_hook)
    return 0


if __name__ == "__main__":
    sys.exit(main())
