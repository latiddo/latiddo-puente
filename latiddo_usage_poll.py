#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Rafael – Latiddo (https://latiddo.com)
"""
Latiddo Pulse - /usage poller.

Runs the official Claude Code command `claude -p /usage`, reads the session
and weekly limits from its text output and POSTs them to
http://<ip>/api/update on a Latiddo Pulse device on your local network, and/or
to http://<robot>/api/claude on a Latiddo robot (the same JSON). Each
destination is optional; both are sent to at once, so one that is off never
delays the other.
Limits are account-wide (claude.ai chat, mobile and Claude Code), so this
works even if you only use the chat. Run it periodically (Task Scheduler,
cron or launchd): each run does one query and exits.

It only runs your own `claude` binary and reads its output. It does NOT read
credentials or tokens, does NOT call any Anthropic API, and only uses the
Python standard library (Windows, macOS, Linux). It needs
latiddo_statusline.py in the same folder (shared config, log and send).

Usage:
  python latiddo_usage_poll.py                 (one silent query + send)
  python latiddo_usage_poll.py --setup [--ip IP] [--robot ADDRESS] [--devices pulse|latiddo|both]
                                       [--minutes 2] [--yes] [--dry-run] [--with-statusline] [--with-hooks]
  python latiddo_usage_poll.py --remove [--yes]
  python latiddo_usage_poll.py --test [--ip 192.168.1.50] [--robot latiddo-xxxx.local]
  python latiddo_usage_poll.py --settings [--port 8737]   (settings page, only on this computer)
  --setup also adds a shortcut that opens it without a console: «Latiddo · Bridge settings» in the
  Windows Start menu, an app in ~/Applications on macOS, an entry in the Linux applications menu.
  --remove removes it.
  Any mode accepts --lang es|en|auto (saved in latiddo.json; auto = the system language).
  --setup copies both bridge scripts to %LOCALAPPDATA%\\Latiddo (Windows) or
  ~/.local/share/latiddo and schedules the copy there.
  --install-task and --uninstall-task are aliases of --setup --yes and --remove --yes.
  Any mode accepts --claude-path <path to claude>.
  --with-hooks also adds the Claude Code hooks for the live status on the Pulse (working /
  needs you / done); an interactive --setup asks. --remove (1.3.1) removes everything the
  bridge added: the task, its hooks and status line (only ours, a dated copy of settings.json
  first), the settings shortcut, its scripts, ~/.claude/latiddo.json, latiddo_hosts.json,
  latiddo.log and its temporary files.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import latiddo_statusline as common  # noqa: E402  (shared config, log, send, language)

t = common.t

TASK_NAME = "LatiddoUsage"
CLAUDE_TIMEOUT = 25          # seconds for `claude -p /usage`
RESEND_S = 90                # resend unchanged data (heartbeat) after 90 s
ERROR_REPEAT_S = 60          # same error in a row: at most one log line a minute, with a counter
TARGET_ERROR_REPEAT_S = 1800  # a destination that keeps failing (off, other network): one line in 30 min
SLOW_CLAUDE_S = 10           # log how long `claude -p /usage` took when it is slower than this
NO_PERSIST_FLAG = "--no-session-persistence"
CREATE_NO_WINDOW = 0x08000000

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# "Current session: 17% used · resets Nov 9, 12:50am (Europe/Madrid)"
_TAIL = r"\s*:\s*(\d+(?:\.\d+)?)\s*%\s*used\b(?:.*?\bresets?\b\s*(.*?)\s*(?:\(([^()]*)\))?)?\s*$"
SESSION_RE = re.compile(r"^\s*current\s+session" + _TAIL, re.I | re.M)
WEEK_RE = re.compile(r"^\s*current\s+week\s*\(\s*all\s+models\s*\)" + _TAIL, re.I | re.M)
# "Current week (Fable): 0% used · resets Nov 16, 2am (Europe/Madrid)" — optional
FABLE_RE = re.compile(r"^\s*current\s+week\s*\(\s*fable\s*\)" + _TAIL, re.I | re.M)
# "Nov 9, 12:50am", "Nov 9, 2am", "Dec 31, 11:59 pm", or just "3pm"
RESET_RE = re.compile(
    r"^\s*(?:([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+)?"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?\s*$", re.I)
NOT_LOGGED_RE = re.compile(r"not\s+logged\s+in|please\s+log\s*in|/login|authenticat", re.I)
CONTRIB_RE = re.compile(r"^\s*what'?s\s+contributing", re.I)


def state_path():
    return Path(tempfile.gettempdir()) / "latiddo_poll_state.json"


def log(msg):
    common.log("poll " + msg)


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------
def _zone(name):
    """ZoneInfo for an IANA name, or None (then local time is used)."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name.strip())
    except Exception:  # unknown zone, or no tzdata (common on Windows)
        return None


def parse_reset(text, zone=None, now=None):
    """'Nov 9, 12:50am' / 'Nov 9, 2am' / '3pm' in `zone` -> epoch seconds, or None.
    No year: current year, or next year if that is more than 1 day in the past."""
    m = RESET_RE.match(text or "")
    if not m:
        return None
    mon_s, day_s, hour_s, min_s, ampm = m.groups()
    hour, minute = int(hour_s), int(min_s or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if ampm.lower() == "p" else 0)
    tz = _zone(zone)
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now, tz)

    def stamp(y, mo, d):
        try:
            return dt.datetime(y, mo, d, hour, minute, tzinfo=tz).timestamp()
        except ValueError:  # e.g. Feb 30
            return None

    if mon_s is None:  # time only: today, or tomorrow if already past
        t = stamp(today.year, today.month, today.day)
        if t is not None and t < now:
            t += 86400
        return t
    month = MONTHS.get(mon_s[:3].lower())
    if month is None:
        return None
    t = stamp(today.year, month, int(day_s))
    if t is None or t < now - 86400:
        t = stamp(today.year + 1, month, int(day_s))
    return t


def _window(regex, text, now):
    m = regex.search(text)
    if not m:
        return None
    pct, reset_text, zone = m.groups()
    return {"pct": float(pct), "reset_text": (reset_text or "").strip(),
            "zone": (zone or "").strip(), "reset": parse_reset(reset_text, zone, now)}


def parse_usage(text, now=None):
    """Return {'session': {...}, 'week': {...}, 'fable': {...} or None}, or None if the
    format is not recognised. Fable is optional: without it, session and week still work."""
    now = time.time() if now is None else now
    session = _window(SESSION_RE, text or "", now)
    week = _window(WEEK_RE, text or "", now)
    if session is None or week is None:
        return None
    try:
        fable = _window(FABLE_RE, text or "", now)
    except Exception:            # never let the optional line break the other two
        fable = None
    return {"session": session, "week": week, "fable": fable}


def build_payload(parsed, now):
    s, w = parsed["session"], parsed["week"]
    payload = {
        "sesion": int(round(s["pct"])),
        "semanal": int(round(w["pct"])),
        "reset_h": 0,
        "reset_m": 0,
        "reset_label": "",
        "extra_used": 0,
        "extra_limit": 0,
        "extra_label": "",
        "bridge_version": common.BRIDGE_VERSION,
    }
    if s["reset"] is not None:
        secs = max(0, int(s["reset"] - now))
        payload.update(reset_h=secs // 3600, reset_m=(secs % 3600) // 60, session_reset_s=secs)
    if w["reset"] is not None:
        payload["reset_label"] = dt.datetime.fromtimestamp(w["reset"]).strftime("%a %H:%M")[:39]
        payload["weekly_reset_s"] = max(0, int(w["reset"] - now))
    f = parsed.get("fable")
    if f is not None:
        payload["fable"] = max(0, min(100, int(round(f["pct"]))))
    return payload


def fable_text(payload):
    return f" fable={payload['fable']}" if "fable" in payload else ""


def summarize_output(text):
    """Raw output without the 'What's contributing...' block and blank-line runs."""
    out = []
    for line in (text or "").splitlines():
        if CONTRIB_RE.match(line):
            break
        if line.strip() or (out and out[-1].strip()):
            out.append(line.rstrip())
    return "\n".join(out).strip()


# ----------------------------------------------------------------------------
# Running claude
# ----------------------------------------------------------------------------
def find_claude(override=None):
    if override:                     # an explicit --claude-path is used as given
        return str(override) if Path(override).is_file() else None
    saved = common.load_json(common.config_path(), {}).get("claude_path")
    if saved and Path(saved).is_file():  # a saved path that is gone falls through to the search
        return str(saved)
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    candidates = [home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "claude.cmd")
    else:
        candidates += [Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def run_claude(claude):
    """Run `claude -p /usage`. Returns (returncode, stdout+stderr text) or raises."""
    kw = dict(stdin=subprocess.DEVNULL, capture_output=True, timeout=CLAUDE_TIMEOUT,
              cwd=tempfile.gettempdir())
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    # Our own `claude -p /usage` must not report "working / done" to the Pulse via the hooks
    kw["env"] = dict(os.environ, **{common.NO_HOOK_ENV: "1"})

    def once(args):
        r = subprocess.run([claude] + args, **kw)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        return r.returncode, out, err

    # --no-session-persistence keeps each poll from saving a transcript under
    # ~/.claude/projects. Older CLIs without the flag get the plain command.
    rc, out, err = once(["-p", NO_PERSIST_FLAG, "/usage"])
    if rc != 0 and NO_PERSIST_FLAG in (out + err) and re.search(r"unknown|unrecognized", out + err, re.I):
        rc, out, err = once(["-p", "/usage"])
    return rc, (out + ("\n" + err if err.strip() else ""))


def query(claude_path=None, runner=run_claude, now=None):
    """Return (parsed or None, raw_text, reason_code_if_failed). reason_text() gives the text."""
    now = time.time() if now is None else now
    claude = find_claude(claude_path)
    if not claude:
        return None, "", "not_found"
    t0 = time.monotonic()
    try:
        rc, text = runner(claude)
    except subprocess.TimeoutExpired:
        return None, "", "timeout"
    except OSError as e:
        return None, "", "run_error:" + type(e).__name__
    took = time.monotonic() - t0
    if took > SLOW_CLAUDE_S:
        log(t("claude -p /usage took {s} s", s=f"{took:.1f}"))
    parsed = parse_usage(text, now)
    if parsed is not None:
        return parsed, text, None
    if NOT_LOGGED_RE.search(text):
        return None, text, "not_signed_in"
    if rc != 0:
        return None, text, f"exit:{rc}"
    return None, text, "format"


def reason_text(code):
    """The reason of a failed query, in the current language."""
    kind, _, arg = str(code).partition(":")
    if kind == "not_found":
        return t("claude not found")
    if kind == "timeout":
        return t("claude timeout ({s} s)", s=CLAUDE_TIMEOUT)
    if kind == "run_error":
        return t("could not run claude: {name}", name=arg)
    if kind == "not_signed_in":
        return t("claude is not signed in")
    if kind == "exit":
        return t("claude exited with code {rc}", rc=arg)
    if kind == "format":
        return t("format not recognised")
    return str(code)


# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------
def _hhmm(ts):
    return dt.datetime.fromtimestamp(float(ts)).strftime("%H:%M")


def streak_fail(state, key, reason, suffix, now, repeat=ERROR_REPEAT_S):
    """A failure. The first of a run is logged; the same one again run after run gives at most
    one line every `repeat` seconds, with a counter. `key`: "err" for the query, "err_<id>" for
    each destination (so the Pulse and the robot never hide each other's failures)."""
    streak = state.get(key) if isinstance(state.get(key), dict) else {}
    if streak.get("reason") != reason:
        streak_end(state, key, now)
        log(reason + suffix)
        state[key] = {"reason": reason, "suffix": suffix, "since": now,
                      "count": 1, "logged": 1, "logged_at": now, "last": now}
        return
    streak["count"] = int(streak.get("count", 1)) + 1
    if now - float(streak.get("logged_at", 0)) >= repeat:
        log(reason + suffix + " " + t("(x{count} since {since})", count=streak["count"],
                                      since=_hhmm(streak.get("since", now))))
        streak.update(logged=streak["count"], logged_at=now)
    streak["last"] = now
    state[key] = streak


def streak_end(state, key, now):
    """The run of the same error is over: log the repeats that were not logged yet."""
    streak = state.pop(key, None)
    if isinstance(streak, dict) and int(streak.get("count", 0)) > int(streak.get("logged", 0)):
        log(f"{streak.get('reason', '?')}{streak.get('suffix', '')} "
            + t("(x{count} since {since}, last at {last})", count=streak["count"],
                since=_hhmm(streak.get("since", now)), last=_hhmm(streak.get("last", now))))


def poll_once(claude_path=None, runner=run_claude, now=None, sender=None):
    """One query; send to each destination (Pulse, robot) if changed, or as a heartbeat when
    its last good send is >= RESEND_S old. Destinations are sent to at once: one that does not
    answer never delays the other, and is tried again on the next run. Logs sends that change
    values and every failure: the same query error several runs in a row gives at most one line
    a minute, the same destination failure one line in 30 min, each with a counter."""
    now = time.time() if now is None else now
    sender = sender or (lambda targets, payload: common.send_all(targets, payload, timeout=common.POLL_NET_TIMEOUT))
    try:
        # The snapshot of Claude Code's sessions to the robot (a lost hook never leaves it with an
        # old state), first: `claude -p /usage` below can take up to CLAUDE_TIMEOUT
        common.resend_cc_state()
    except Exception:
        pass
    state = common.load_json(state_path(), {})
    for old in ("err_logged", "last_ok", "last_key"):   # keys of older versions
        state.pop(old, None)
    parsed, _, reason = query(claude_path, runner, now)
    not_sent = t("; nothing sent")

    if parsed is None:
        streak_fail(state, "err", reason_text(reason), not_sent, now)
    else:
        payload = build_payload(parsed, now)
        targets = common.load_targets()
        key = [payload["sesion"], payload["semanal"], payload.get("fable"),
               int(parsed["session"]["reset"] or 0) // 60, int(parsed["week"]["reset"] or 0) // 60]
        if not targets:
            streak_fail(state, "err", t("no device configured (latiddo.json, LATIDDO_IP or LATIDDO_ROBOT)"),
                        not_sent, now)
        else:
            streak_end(state, "err", now)
            sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
            due = [d for d in targets
                   if key != (sent.get(d["id"]) or {}).get("last_key")
                   or now - float((sent.get(d["id"]) or {}).get("last_ok", 0)) >= RESEND_S]
            if due:
                results = sender(due, payload)
                common.record_sends(due, results, now, "poll")
                for d in due:
                    ok, result = results[d["id"]][:2]
                    who = f"{d['log']}{d['address']}"
                    if not ok:
                        streak_fail(state, "err_" + d["id"], f"{who} {result}", "", now,
                                    repeat=TARGET_ERROR_REPEAT_S)
                        continue
                    streak_end(state, "err_" + d["id"], now)
                    if key != (sent.get(d["id"]) or {}).get("last_key"):
                        log(f"{who} {result} sesion={payload['sesion']} semanal={payload['semanal']}"
                            f"{fable_text(payload)} "
                            f"reset={payload['reset_h']}h{payload['reset_m']:02d}m label='{payload['reset_label']}'")
                    sent[d["id"]] = {"last_ok": now, "last_key": key}
            state["sent"] = sent
    try:
        common.save_json_atomic(state_path(), state)
    except Exception:
        pass


def cmd_test(ip_arg, claude_path, robot_arg=None):
    now = time.time()
    claude = find_claude(claude_path)
    print("claude:", claude or t("(not found)"))
    parsed, raw, reason = query(claude_path, now=now)
    if raw:
        print(t("--- output of claude -p /usage (summary) ---"))
        print(summarize_output(raw))
        print("---")
    if parsed is None:
        print(t("FAILED:"), reason_text(reason))
        log(f"{reason_text(reason)} (test)")
        return 1
    names = {"session": t("session"), "week": t("week"), "fable": "Fable"}
    for name in ("session", "week", "fable"):
        w = parsed.get(name)
        if w is None:
            print(t("{name}: not in the output", name=names[name]))
            continue
        when = dt.datetime.fromtimestamp(w["reset"]).strftime("%Y-%m-%d %H:%M") \
            if w["reset"] is not None else t("not parsed")
        print(t("{name}: {pct}% used, resets '{text}' ({zone}) -> {when}", name=names[name],
                pct=f"{w['pct']:g}", text=w["reset_text"], zone=w["zone"] or t("no zone"), when=when))
    payload = build_payload(parsed, now)
    print("payload:", json.dumps(payload))
    targets = test_targets(ip_arg, robot_arg)
    if not targets:
        print(t("No device. Use --ip <Pulse IP>, --robot <robot address>, LATIDDO_IP or {path}",
                path=common.config_path()))
        return 1
    results = common.send_all(targets, payload)
    common.record_sends(targets, results, now, "test")
    all_ok = True
    for d in targets:
        ok, result, took = results[d["id"]]
        all_ok = all_ok and ok
        log(f"{d['log']}{d['address']} {result} sesion={payload['sesion']} semanal={payload['semanal']}"
            f"{fable_text(payload)} (test)")
        print(f"POST http://{d['address']}{d['path']} -> {result} ({took:.2f} s)")
        if d["id"] == "pulse":
            status = common.fetch_status(d["address"])
            if common.is_device(status):
                print(f"  Pulse: {status.get('device')} {status.get('model', '')} "
                      f"{status.get('hostname', '')} (firmware {status.get('fw', '?')})".replace("  ", " "))
            else:
                print("  " + t("Note: {address} did not identify itself as a Latiddo Pulse in /api/status.",
                               address=d["address"]))
        else:
            got = common.fetch_json(d["address"], common.ROBOT_PATH)
            data = got.get("data") if isinstance(got, dict) else None
            if isinstance(data, dict):
                print("  " + t("Robot: session {s}%, weekly {w}%, {live} (GET {path})", s=data.get("session"),
                               w=data.get("week"), live=t("live") if data.get("live") else t("not live"),
                               path=common.ROBOT_PATH))
            else:
                print("  " + t("Note: {address} did not answer GET {path} like a Latiddo robot.",
                               address=d["address"], path=common.ROBOT_PATH))
    return 0 if all_ok else 1


def test_targets(ip_arg, robot_arg):
    """--test: the destinations given (--ip, --robot), else the ones switched on in the config."""
    if not ip_arg and not robot_arg:
        return common.load_targets()
    out = []
    if ip_arg:
        out.append({"id": "pulse", "label": "Pulse", "address": common.normalize_ip(ip_arg),
                    "path": common.PULSE_PATH, "password": common.load_config()[1], "log": "ip="})
    if robot_arg:
        out.append({"id": "latiddo", "label": "Latiddo", "address": common.normalize_ip(robot_arg),
                    "path": common.ROBOT_PATH, "password": "", "log": "latiddo="})
    return out


def save_config(ip=None, claude_path=None, hostname=None, robot=None, pulse_on=None, robot_on=None):
    """Merge ip / claude_path / hostname / robot and the on / off of each destination into the
    config. Never writes passwords. pulse_on / robot_on None: left as they are."""
    cfg = common.load_json(common.config_path(), {})
    if ip:
        cfg["ip"] = ip
    if claude_path:
        cfg["claude_path"] = claude_path
    if hostname:
        cfg["hostname"] = hostname
    if robot:
        cfg["robot"] = robot
    if pulse_on is not None:
        if pulse_on:
            cfg.pop("pulse_enabled", None)   # on is the default: a Pulse-only config stays as before
        else:
            cfg["pulse_enabled"] = False
    if robot_on is not None:
        cfg["robot_enabled"] = bool(robot_on)
    common.save_json_atomic(common.config_path(), cfg, indent=2)


# ----------------------------------------------------------------------------
# --setup / --remove: find the device, check claude, install, schedule
# ----------------------------------------------------------------------------
DEVICE_IDS = common.DEVICE_IDS   # ("latiddo",): "device" in /api/status
# Finding the devices: mDNS first (the Pulse and the robot announce _http._tcp), and at the same
# time a scan of the local /24 in two steps: who has port 80 open, then /api/status on those. Both
# are ESP32s: they usually answer in 0.1 s, but sometimes take seconds (WiFi power save, or the
# Pulse busy drawing or downloading), so every step waits generously.
SCAN_CONNECT_TIMEOUT = 3.0
STATUS_TIMEOUT = 6.0
MDNS_WAIT = 2.5
UNIT_NAME = "latiddo-usage"            # systemd user service + timer
LAUNCHD_LABEL = "com.latiddo.usage"    # macOS LaunchAgent
CRON_MARK = "# latiddo-usage"
SETUP_UA = "latiddo-setup/1.0"
DEFAULT_MINUTES = 2
BRIDGE_FILES = ("latiddo_usage_poll.py", "latiddo_statusline.py")

CLAUDE_MISSING = """Claude Code (the `claude` command) was not found on this computer.
Install it from https://claude.com/claude-code
  Windows (PowerShell):  irm https://claude.ai/install.ps1 | iex
  macOS / Linux:         curl -fsSL https://claude.ai/install.sh | bash
If it is already installed somewhere else, add --claude-path <path to claude>.
Then close this window, open a new terminal window and run the command again
(it will offer to sign in to Claude Code)."""
SIGNIN_HELP = "Claude Code will open: sign in with your claude.ai account, then type /exit to come back here."


class SetupError(Exception):
    pass


def install_dir():
    """Fixed folder the scheduled task runs from, so the repository can change freely."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Latiddo"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "latiddo"


def installed_script():
    return install_dir() / "latiddo_usage_poll.py"


def copy_bridge():
    """Copy both bridge scripts next to this one into install_dir() (no-op if already there)."""
    src_dir = Path(__file__).resolve().parent
    dest = install_dir()
    if dest.exists() and src_dir == dest.resolve():
        return False
    dest.mkdir(parents=True, exist_ok=True)
    for name in BRIDGE_FILES:
        src = src_dir / name
        if not src.is_file():
            raise SetupError(t("Missing {path}: keep both bridge scripts in the same folder.", path=src))
        tmp = dest / (name + ".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest / name)
    return True


def is_interactive():
    """True only when stdin is a real terminal (on Windows, NUL also claims isatty)."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import ctypes
            import msvcrt
            mode = ctypes.c_uint()
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        return True
    except Exception:
        return False


def ask(question):
    try:
        return input(question).strip()
    except EOFError:
        return ""


def probe(ip, timeout=STATUS_TIMEOUT):
    """GET http://<ip>/api/status. Returns ('ok', data) for a Latiddo Pulse,
    ('other', None) for something else, ('down', None) if nothing answers. `ip` may be a name."""
    data = common.fetch_json(ip, "/api/status", timeout)
    if data is None:
        return "down", None
    if data.get("device") in DEVICE_IDS:
        return "ok", data
    return "other", None


resolve_host = common.resolve_host   # IPv4 of latiddo-a1b2.local, or None (bounded wait)


def local_ipv4():
    """This computer's LAN address: the one of its route to the internet (a UDP connect sends
    no packet), else the one towards a private network."""
    import socket
    for target in ("192.0.2.1", "10.255.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith(("127.", "0.")):
                return ip
        except Exception:
            pass
        finally:
            s.close()
    return None


def local_ipv4s():
    """Every private IPv4 of this computer, the main one first (no loopback, no 169.254)."""
    import ipaddress
    import socket
    out = []
    first = local_ipv4()
    if first:
        out.append(first)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            a = ipaddress.ip_address(ip)
            if a.is_private and not a.is_loopback and not a.is_link_local and ip not in out:
                out.append(ip)
    except Exception:
        pass
    return out


_scan_cache = {}


def mdns_browse(local_ips, wait=MDNS_WAIT):
    """The IPs that answer an mDNS question for _http._tcp.local (the Pulse and the robot
    announce it). Asked from an ordinary port, so the devices answer straight to this computer
    (no need for port 5353, which the system's own mDNS may hold)."""
    import socket
    import struct
    name = b"".join(bytes([len(part)]) + part for part in (b"_http", b"_tcp", b"local")) + b"\x00"
    query = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + name + struct.pack(">HH", 12, 0x8001)  # PTR, QU
    socks = []
    for ip in local_ips or [""]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            if ip:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.bind((ip, 0))
            s.setblocking(False)
            socks.append(s)
        except OSError:
            pass
    found = set()
    end = time.monotonic() + wait
    asked = 0
    import select
    while socks and time.monotonic() < end:
        if asked < 2 and time.monotonic() >= end - wait + asked * 0.8:   # ask twice (lost packets)
            for s in socks:
                try:
                    s.sendto(query, ("224.0.0.251", 5353))
                except OSError:
                    pass
            asked += 1
        ready, _, _ = select.select(socks, [], [], 0.1)
        for s in ready:
            try:
                _, addr = s.recvfrom(9000)
                found.add(addr[0])
            except OSError:
                pass
    for s in socks:
        s.close()
    return found


def scan_open_80(my_ip):
    """The addresses of the local /24 that accept a connection on port 80."""
    import socket
    from concurrent.futures import ThreadPoolExecutor
    prefix = my_ip.rsplit(".", 1)[0]
    hosts = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != my_ip]

    def open80(h):
        try:
            with socket.create_connection((h, 80), timeout=SCAN_CONNECT_TIMEOUT):
                return True
        except OSError:
            return False
    with ThreadPoolExecutor(max_workers=128) as ex:
        return [h for h, ok in zip(hosts, ex.map(open80, hosts)) if ok]


_scan_cache = {}


def scan_all(my_ip):
    """Find the Latiddo devices (once per run): mDNS and the scan of the local /24 at the same
    time, then /api/status of every address found. ([(ip, status) of each Latiddo Pulse],
    [(ip, status) of each Latiddo robot])."""
    if my_ip in _scan_cache:
        return _scan_cache[my_ip]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        by_mdns = ex.submit(mdns_browse, local_ipv4s())
        by_scan = ex.submit(scan_open_80, my_ip)
        hosts = sorted(set(by_mdns.result()) | set(by_scan.result()),
                       key=lambda h: tuple(int(x) for x in h.split(".")))
    with ThreadPoolExecutor(max_workers=32) as ex:
        results = list(ex.map(lambda h: common.fetch_json(h, "/api/status", STATUS_TIMEOUT), hosts))
    pulses = [(h, d) for h, d in zip(hosts, results) if common.is_device(d)]
    robots = [(h, d) for h, d in zip(hosts, results) if common.is_robot(d)]
    _scan_cache[my_ip] = (pulses, robots)
    return pulses, robots


def scan_subnet(my_ip):
    return scan_all(my_ip)[0]


def device_label(d):
    host = d.get("hostname")
    return (f"{host}.local, " if host else "") + f"firmware {d.get('fw', '?')}"


def find_device(ip_arg, interactive):
    """Return (ip, status): --ip, else the saved IP, else the saved hostname (.local), else the
    devices found on the network (mDNS and a scan of the local /24), else ask. The mDNS name
    includes part of the MAC, so it cannot be guessed before the device has been found once."""
    if ip_arg:
        ip = common.normalize_ip(ip_arg)
        st, d = probe(ip)
        if st == "ok":
            return ip, d
        if st == "other":
            raise SetupError(t("{ip} answered, but it is not a Latiddo Pulse, or its firmware is too old. "
                               "Update the device firmware and try again.", ip=ip))
        raise SetupError(t("Could not reach a Latiddo Pulse at {ip}. Check the IP shown on the device "
                           "and that this computer is on the same network.", ip=ip))

    print(t("Looking for your Latiddo Pulse..."))
    cfg = common.load_json(common.config_path(), {})
    saved = common.load_config()[0]
    if saved:
        st, d = probe(saved)
        if st == "ok":
            print("  " + t("found it at the saved address {ip}", ip=saved))
            return saved, d
    host = str(cfg.get("hostname") or "").strip()
    if host:
        name = host if host.endswith(".local") else host + ".local"
        ip = resolve_host(name, common.RESOLVE_TIMEOUT)
        if ip:
            st, d = probe(ip)
            if st == "ok":
                print("  " + t("found {name} at {ip}", name=name, ip=ip))
                return ip, d
    me = local_ipv4()
    if me:
        print("  " + t("searching the network (mDNS and {net}.1-254, about 10 s)...", net=me.rsplit(".", 1)[0]))
        found = scan_all(me)[0]
        if len(found) == 1:
            print("  " + t("found it at {ip}", ip=found[0][0]))
            return found[0]
        if len(found) > 1:
            print("  " + t("found more than one:"))
            for i, (h, d) in enumerate(found, 1):
                print(f"    {i}. {h} ({device_label(d)})")
            if not interactive:
                raise SetupError(t("More than one device found. Run again with --ip <ip>."))
            pick = ask(t("Which one? Type its number: "))
            if pick.isdigit() and 1 <= int(pick) <= len(found):
                return found[int(pick) - 1]
            raise SetupError(t("Cancelled. Nothing was changed."))

    if not interactive:
        raise SetupError(t("No Latiddo Pulse found on this network. Run again with "
                           "--ip <the IP shown on the device screen at startup>."))
    print("  " + t("not found automatically."))
    while True:
        ip = common.normalize_ip(ask(t("Type the IP shown on the device (or press Enter to cancel): ")))
        if not ip:
            raise SetupError(t("Cancelled. Nothing was changed."))
        st, d = probe(ip)
        if st == "ok":
            return ip, d
        print("  " + t("{ip} is not a Latiddo Pulse or did not answer. Try again.", ip=ip))


# --- The Latiddo robot ---------------------------------------------------------------
def probe_robot(address, timeout=STATUS_TIMEOUT):
    """('ok', status) for a Latiddo robot that takes Claude data (GET /api/claude answers),
    ('old', status) for a robot whose firmware has no /api/claude yet, ('other', None) for
    something else, ('down', None) if nothing answers. `address` may be a name."""
    status = common.fetch_json(address, "/api/status", timeout)
    if status is None:
        return "down", None
    if not common.is_robot(status):
        return "other", None
    claude = common.fetch_json(address, common.ROBOT_PATH, timeout)
    if not isinstance(claude, dict) or "data" not in claude:
        return "old", status
    return "ok", status


def robot_label(d):
    return f"{d.get('host', '?')}, {d.get('board', '?')}, Latiddo {d.get('latiddo_version') or '?'}"


def robot_address_to_save(used, status):
    """Keep what the user typed; for an address found by the setup (an IP, or the shared
    latiddo.local), the robot's own name when it resolves to it here: it survives a new IP."""
    host = str((status or {}).get("host") or "").strip()
    if not host or used.lower() == host.lower():
        return used
    ip = common.resolve_host(host, common.RESOLVE_TIMEOUT)
    want = common.resolve_host(used, common.RESOLVE_TIMEOUT) if not common.is_ip(used) else used
    if ip and ip == want:
        common.resolve(host)   # keeps its IP for the next runs
        return host
    return used


def find_robot_by_name(name):
    """1.3.2: `name` (latiddo-xxxx.local) does not resolve on this computer (its mDNS can be slow or
    off): the robot whose /api/status says that name, on the local network; its IP is kept for
    the name (hosts_path()) and the status returned. None if it is not there."""
    me = local_ipv4()
    if not me:
        return None
    def bare(h):   # without ".local" (Python 3.8: no str.removesuffix)
        h = str(h or "").lower()
        return h[:-len(".local")] if h.endswith(".local") else h
    want = bare(common.split_host(name)[0])
    print("  " + t("{name} does not answer by its name here: looking for it on the network (mDNS and "
                   "{net}.1-254, about 10 s)...", name=name, net=me.rsplit(".", 1)[0]))
    for h, d in scan_all(me)[1]:
        host = bare((d or {}).get("host"))
        if host == want and probe_robot(h)[0] == "ok":
            common.keep_host_ip(name, h)
            print("  " + t("found {name} at {ip}; its IP is kept for the next runs", name=name, ip=h))
            return probe_robot(name)[1]
    return None


def find_robot(robot_arg, interactive):
    """Return (address, status): --robot, else the saved address, else latiddo.local, else the
    devices found on the network (mDNS and a scan of the local /24), else ask. The address may
    be a .local name or an IP."""
    def check(address, typed):
        st, d = probe_robot(address)
        if st == "ok":
            return d
        if st == "old":
            raise SetupError(t("{address} is a Latiddo robot ({label}), but its firmware cannot take Claude "
                               "data yet (no /api/claude). Update the robot and try again.",
                               address=address, label=robot_label(d)))
        if typed:
            what = t("answered, but it is not a Latiddo robot") if st == "other" else t("did not answer")
            raise SetupError(t("{address} {what}. Use the address shown in the robot's portal (Ajustes: "
                               "latiddo-xxxx.local, or its IP) and check that this computer is on the "
                               "same network.", address=address, what=what))
        return None

    if robot_arg:
        address = common.normalize_ip(robot_arg)
        if not common.is_ip(common.split_host(address)[0]) and probe_robot(address)[0] == "down":
            found = find_robot_by_name(address)
            if found:
                return address, found
        return address, check(address, True)

    print(t("Looking for your Latiddo robot..."))
    cfg = common.load_json(common.config_path(), {})
    saved = common.normalize_ip(cfg.get("robot") or "")
    if saved:
        d = check(saved, False)
        if d:
            print("  " + t("found it at the saved address {ip}", ip=saved))
            return saved, d
    d = check("latiddo.local", False)
    if d:
        address = robot_address_to_save("latiddo.local", d)
        print("  " + t("found {name}", name=address))
        return address, d
    me = local_ipv4()
    if me:
        print("  " + t("searching the network (mDNS and {net}.1-254, about 10 s)...", net=me.rsplit(".", 1)[0]))
        found = []
        for h, _ in scan_all(me)[1]:
            st, d = probe_robot(h)
            if st == "ok":
                found.append((h, d))
        if len(found) == 1:
            address = robot_address_to_save(found[0][0], found[0][1])
            print("  " + t("found it at {ip}", ip=address))
            return address, found[0][1]
        if len(found) > 1:
            print("  " + t("found more than one:"))
            for i, (h, d) in enumerate(found, 1):
                print(f"    {i}. {h} ({robot_label(d)})")
            if not interactive:
                raise SetupError(t("More than one robot found. Run again with --robot <address>."))
            pick = ask(t("Which one? Type its number: "))
            if pick.isdigit() and 1 <= int(pick) <= len(found):
                h, d = found[int(pick) - 1]
                return robot_address_to_save(h, d), d
            raise SetupError(t("Cancelled. Nothing was changed."))

    if not interactive:
        raise SetupError(t("No Latiddo robot found on this network. Run again with --robot <its address: "
                           "latiddo-xxxx.local or its IP, shown in its portal under Ajustes>."))
    print("  " + t("not found automatically."))
    while True:
        address = common.normalize_ip(ask(t("Type the robot's address, latiddo-xxxx.local or its IP (in its "
                                            "portal, under Ajustes), or press Enter to cancel: ")))
        if not address:
            raise SetupError(t("Cancelled. Nothing was changed."))
        try:
            return address, check(address, True)
        except SetupError as e:
            print("  " + str(e).replace("\n", "\n  "))


def send_test_robot(address, parsed):
    payload = build_payload(parsed, time.time())
    ok, result = common.send(address, "", payload, common.ROBOT_PATH, timeout=common.POLL_NET_TIMEOUT)
    if not ok:
        print(t("Test send to {url} failed: {result}.", url=f"http://{address}{common.ROBOT_PATH}", result=result))
        return False
    time.sleep(0.3)
    got = common.fetch_json(address, common.ROBOT_PATH, STATUS_TIMEOUT)
    data = got.get("data") if isinstance(got, dict) else None
    if isinstance(data, dict) and data.get("live") and data.get("session") == payload["sesion"]:
        print(t("The robot received the test: session {s}%, weekly {w}% (its Claude screen and portal "
                "show it as live).", s=payload["sesion"], w=payload["semanal"]))
        return True
    print(t("The test was sent, but the robot does not show it as live. Check its portal (Hoy → Claude)."))
    return False


DEVICE_CHOICES = {"1": {"pulse"}, "2": {"latiddo"}, "3": {"pulse", "latiddo"}}
DEVICES_ARG = {"pulse": {"pulse"}, "latiddo": {"latiddo"}, "robot": {"latiddo"}, "both": {"pulse", "latiddo"}}


def configured_devices():
    """What the config sends to now (a config of earlier versions: the Pulse)."""
    cfg = common.load_json(common.config_path(), {})
    out = set()
    if common.pulse_enabled(cfg) and (cfg.get("ip") or not common.robot_enabled(cfg)):
        out.add("pulse")
    if common.robot_enabled(cfg):
        out.add("latiddo")
    return out or {"pulse"}


def choose_devices(ip_arg, robot_arg, devices_arg, can_ask):
    """Which devices get the data. --devices says it. Else --ip / --robot add their device to
    what the config already has (re-running the Pulse's own command never drops the robot);
    an interactive setup asks, with that as the default (Enter)."""
    if devices_arg:
        return set(DEVICES_ARG[devices_arg])
    default = configured_devices()
    if ip_arg:
        default.add("pulse")
    if robot_arg:
        default.add("latiddo")
        if not ip_arg and not common.load_config()[0]:
            default.discard("pulse")
    if not can_ask:
        return default
    key = next(k for k, v in DEVICE_CHOICES.items() if v == default)
    print("\n" + t("Which devices should show your Claude usage?") + "\n  1. Latiddo Pulse\n  2. "
          + t("Latiddo robot") + "\n  3. " + t("Both"))
    while True:
        answer = ask(t("Type 1, 2 or 3 [{key}]: ", key=key)) or key
        if answer in DEVICE_CHOICES:
            return set(DEVICE_CHOICES[answer])
        print("  " + t("Please type 1, 2 or 3."))


def sign_in(claude):
    """Open Claude Code in this terminal so the user can sign in. True if it ran."""
    print("\n" + t(SIGNIN_HELP) + "\n")
    try:
        subprocess.call([claude], cwd=tempfile.gettempdir())
    except OSError as e:
        print(t("Could not open Claude Code ({name}).", name=type(e).__name__))
        return False
    except KeyboardInterrupt:
        pass
    print()
    return True


def check_claude(claude_path, offer_signin=False):
    """offer_signin: when Claude Code is not signed in, ask to open it to sign in, then check again."""
    claude = find_claude(claude_path)
    if not claude:
        raise SetupError(t(CLAUDE_MISSING))
    print(t("Checking Claude Code ({path}) ...", path=claude))
    parsed, raw, reason = query(claude)
    if parsed is not None:
        return claude, parsed
    if (reason == "not_signed_in" and offer_signin
            and common.is_yes(ask(t("Claude Code is not signed in. Sign in to Claude Code now? [y/N] ")))):
        if sign_in(claude):
            print(t("Checking Claude Code again ({path}) ...", path=claude))
            parsed, raw, reason = query(claude)
            if parsed is not None:
                return claude, parsed
    if reason == "timeout":
        msg = t("Claude Code did not answer in time. Run `claude` once to finish any first-run steps, "
                "then run the command again.")
    elif reason == "not_signed_in":
        msg = t("Claude Code is not signed in. Run `claude`, sign in with your claude.ai account, "
                "type /exit, then run the command again.")
    elif reason == "format":
        msg = t("Claude Code did not show subscription usage limits. Make sure it is signed in with your "
                "claude.ai subscription (run `claude`, then type /login), then run the command again.")
    else:
        msg = t("Claude Code could not be run ({reason}).", reason=reason_text(reason))
    summary = summarize_output(raw)
    if summary:
        msg += "\n\n" + t("Output of claude -p /usage:") + "\n  " + summary.replace("\n", "\n  ")
    raise SetupError(msg)


def python_for_console():
    """python.exe (not pythonw.exe) for commands shown to the user."""
    return str(Path(sys.executable).resolve())


def python_for_task():
    """Python executable for the scheduled task (pythonw.exe on Windows: no window)."""
    py = Path(sys.executable).resolve()
    if os.name == "nt":
        pyw = py.with_name("pythonw.exe")
        if pyw.is_file():
            py = pyw
    return str(py)


def task_command():
    """(Python executable, installed script) that the scheduled task runs."""
    return python_for_task(), str(installed_script())


def run_cmd(args, input_text=None):
    r = subprocess.run(args, input=input_text, capture_output=True, text=True, errors="replace")
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


# --- Windows: Task Scheduler via PowerShell -------------------------------------
def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def windows_task_script(minutes):
    """Register the task (replacing ours), then read it back and fail unless it
    repeats every `minutes` min with no end."""
    py, script = task_command()
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(py)} -Argument {_ps_quote(chr(34) + script + chr(34))}"
        f" -WorkingDirectory {_ps_quote(str(Path(script).parent))}",
        f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes {minutes})",
        "$t.Repetition.Duration = ''   # empty = repeat indefinitely",
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable"
        " -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)",
        "$p = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
        " -LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}"
        " -Description 'Latiddo Pulse: sends your Claude usage limits to the device'"
        " -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null",
        f"$r = (Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}).Triggers[0].Repetition",
        "if (-not $r.Interval -or [Xml.XmlConvert]::ToTimeSpan($r.Interval).TotalMinutes -ne "
        f"{minutes}) {{ throw \"the task does not repeat every {minutes} min (Interval='$($r.Interval)')\" }}",
        "if ($r.Duration -and [Xml.XmlConvert]::ToTimeSpan($r.Duration).TotalDays -lt 3650) "
        "{ throw \"the task repetition stops after $($r.Duration)\" }",
        "\"verified: every $($r.Interval), duration '$($r.Duration)' (empty = no end)\"",
    ])


def windows_remove_script():
    name = _ps_quote(TASK_NAME)
    return ("$ProgressPreference = 'SilentlyContinue'\n"
            f"if (Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue) {{\n"
            f"  Unregister-ScheduledTask -TaskName {name} -Confirm:$false; \"removed {TASK_NAME}\" }}")


def run_powershell(script):
    """Run a script; an error comes back as a plain 'ERROR: <message>' line (not CLIXML)."""
    import base64
    wrapped = ("try {\n" + script + "\n} catch {\n"
               "  Write-Output ('ERROR: ' + $_.Exception.Message); exit 1\n}")
    enc = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
    return run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-EncodedCommand", enc])


# --- Linux: systemd user timer, or cron ------------------------------------------
def systemd_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _sd_quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_units(minutes):
    py, script = task_command()
    service = ("[Unit]\n"
               "Description=Latiddo Pulse: send Claude usage limits to the device\n\n"
               "[Service]\n"
               "Type=oneshot\n"
               f"ExecStart={_sd_quote(py)} {_sd_quote(script)}\n"
               "TimeoutStartSec=120\n")
    timer = ("[Unit]\n"
             f"Description=Latiddo Pulse: send Claude usage every {minutes} min\n\n"
             "[Timer]\n"
             "OnBootSec=1min\n"
             f"OnUnitActiveSec={minutes}min\n"
             "AccuracySec=15s\n"
             f"Unit={UNIT_NAME}.service\n\n"
             "[Install]\n"
             "WantedBy=timers.target\n")
    return service, timer


def has_systemd_user():
    if not shutil.which("systemctl"):
        return False
    try:
        return run_cmd(["systemctl", "--user", "show-environment"])[0] == 0
    except Exception:
        return False


def remove_systemd_units(names):
    """Disable and delete the given user units. Returns the names removed."""
    d = systemd_dir()
    removed = []
    for name in names:
        files = [d / f"{name}.service", d / f"{name}.timer"]
        if not any(f.exists() for f in files):
            continue
        if shutil.which("systemctl"):
            run_cmd(["systemctl", "--user", "disable", "--now", f"{name}.timer"])
        for f in files:
            if f.exists():
                f.unlink()
        removed.append(name)
    if removed and shutil.which("systemctl"):
        run_cmd(["systemctl", "--user", "daemon-reload"])
    return removed


def cron_line(minutes):
    py, script = task_command()
    when = f"*/{minutes} * * * *" if minutes < 60 else "0 * * * *"
    return f'{when} "{py}" "{script}" >/dev/null 2>&1 {CRON_MARK}'


def cron_merge(existing, line=None):
    """Crontab text without our line, plus `line` if given."""
    kept = [l for l in (existing or "").splitlines() if CRON_MARK not in l]
    if line:
        kept.append(line)
    return "\n".join(kept) + "\n" if kept else ""


def cron_has_ours(text):
    return CRON_MARK in (text or "")


def cron_read():
    """Current crontab text, '' if empty, None if crontab is not available."""
    if not shutil.which("crontab"):
        return None
    rc, out = run_cmd(["crontab", "-l"])
    return out + "\n" if rc == 0 else ""


def cron_write(text):
    rc, out = run_cmd(["crontab", "-"], input_text=text)
    if rc != 0:
        raise SetupError(t("crontab failed: {out}", out=out))


# --- macOS: LaunchAgent -------------------------------------------------------------
def launchd_plist_path(label=LAUNCHD_LABEL):
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def launchd_plist(minutes):
    import plistlib
    py, script = task_command()
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [py, script],
        "StartInterval": minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    })


def launchd_unload(plist):
    domain = f"gui/{os.getuid()}"
    if run_cmd(["launchctl", "bootout", domain, str(plist)])[0] != 0:
        run_cmd(["launchctl", "unload", str(plist)])


def remove_launchd(labels):
    removed = []
    for label in labels:
        plist = launchd_plist_path(label)
        if plist.exists():
            launchd_unload(plist)
            plist.unlink()
            removed.append(str(plist))
    return removed


# --- Plans: (lines describing it, function that does it) --------------------------
def schedule_plan(minutes):
    if os.name == "nt":
        def apply():
            rc, out = run_powershell(windows_task_script(minutes))
            if rc != 0:
                raise SetupError(t("Could not create the scheduled task:") + "\n" + out)
            # the script throws (rc != 0) unless the task repeats every N min with no end
            print(t("Scheduled task '{name}' runs every {n} min (checked with Get-ScheduledTask).",
                    name=TASK_NAME, n=minutes))
        py, script = task_command()
        return [t("Create or replace the Windows scheduled task '{name}': every {n} min with no end, hidden, "
                  "also on battery, as the current user ({cmd})", name=TASK_NAME, n=minutes,
                  cmd=f'{Path(py).name} "{script}"')], apply

    if sys.platform == "darwin":
        plist = launchd_plist_path()

        def apply():
            plist.parent.mkdir(parents=True, exist_ok=True)
            launchd_unload(plist)
            plist.write_bytes(launchd_plist(minutes))
            domain = f"gui/{os.getuid()}"
            rc, out = run_cmd(["launchctl", "bootstrap", domain, str(plist)])
            if rc != 0:
                rc, out = run_cmd(["launchctl", "load", "-w", str(plist)])
            if rc != 0:
                raise SetupError(t("Could not load {path}:", path=plist) + "\n" + out)
            print(t("LaunchAgent {name} runs every {n} min.", name=LAUNCHD_LABEL, n=minutes))
        return [t("Create or replace {path} (runs every {n} min) and load it", path=plist, n=minutes)], apply

    if has_systemd_user():
        d = systemd_dir()

        def apply():
            service, timer = systemd_units(minutes)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{UNIT_NAME}.service").write_text(service, encoding="utf-8")
            (d / f"{UNIT_NAME}.timer").write_text(timer, encoding="utf-8")
            for args in (["systemctl", "--user", "daemon-reload"],
                         ["systemctl", "--user", "enable", "--now", f"{UNIT_NAME}.timer"],
                         ["systemctl", "--user", "restart", f"{UNIT_NAME}.timer"]):
                rc, out = run_cmd(args)
                if rc != 0:
                    raise SetupError(t("{cmd} failed:", cmd=" ".join(args)) + "\n" + out)
            old = cron_read()
            if cron_has_ours(old):
                cron_write(cron_merge(old))   # switched from cron to systemd: no duplicates
            print(t("systemd user timer {name} runs every {n} min.", name=f"{UNIT_NAME}.timer", n=minutes))
            print(t("Note: user timers run while you are logged in. To keep it running after logout: "
                    "loginctl enable-linger $USER"))
        return [t("Create or replace the systemd user timer {path} (every {n} min) and enable it; remove our "
                  "crontab line if present", path=d / (UNIT_NAME + ".timer"), n=minutes)], apply

    def apply():
        old = cron_read()
        if old is None:
            raise SetupError(t("Neither systemd (user) nor crontab is available. Run this script every {n} min "
                               "with your own scheduler:", n=minutes) + f"\n  {cron_line(minutes)}")
        cron_write(cron_merge(old, cron_line(minutes)))
        print(t("cron runs it every {n} min.", n=minutes))
    return [t("Add or replace this line in your crontab: {line}", line=cron_line(minutes))], apply


def remove_plan():
    if os.name == "nt":
        def apply():
            rc, out = run_powershell(windows_remove_script())
            if rc != 0:
                raise SetupError(t("Could not remove the scheduled task:") + "\n" + out)
            removed = [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("removed ")]
            for n in removed:
                print(t("Removed the scheduled task '{name}'.", name=n))
            if not removed:
                print(t("No scheduled task was found."))
        return [t("The Windows scheduled task '{name}'", name=TASK_NAME)], apply

    if sys.platform == "darwin":
        def apply():
            removed = remove_launchd((LAUNCHD_LABEL,))
            for r in removed:
                print(t("Removed {path}.", path=r))
            if not removed:
                print(t("No LaunchAgent was found."))
        return [t("The LaunchAgent {path}", path=launchd_plist_path())], apply

    def apply():
        removed = remove_systemd_units((UNIT_NAME,))
        for r in removed:
            print(t("Removed the systemd user timer {name}.", name=r))
        old = cron_read()
        if cron_has_ours(old):
            cron_write(cron_merge(old))
            print(t("Removed the crontab line."))
        elif not removed:
            print(t("No timer or crontab line was found."))
    return [t("The systemd user timer {name} and the crontab line '{mark}', if present",
              name=UNIT_NAME, mark=CRON_MARK)], apply


# --- The settings shortcut: Start menu (Windows), ~/Applications (macOS), app menu (Linux) ----
# It opens the settings page (--settings) without a console window. Named in the language of the
# setup; --remove removes it under either name.
SHORTCUT_NAMES = ("Latiddo · Bridge settings", "Latiddo · Ajustes del puente")   # en, es
DESKTOP_FILE = "latiddo-bridge-settings.desktop"   # Linux: one file, its name in each language inside


def shortcut_name():
    return t("Latiddo · Bridge settings")


def shortcut_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if sys.platform == "darwin":
        return Path.home() / "Applications"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "applications"


def shortcut_path(name=None):
    if os.name == "nt":
        return shortcut_dir() / ((name or shortcut_name()) + ".lnk")
    if sys.platform == "darwin":
        return shortcut_dir() / ((name or shortcut_name()) + ".app")
    return shortcut_dir() / DESKTOP_FILE


def shortcut_paths():
    """Every path the shortcut may have (one per language; one on Linux)."""
    return list(dict.fromkeys(shortcut_path(n) for n in SHORTCUT_NAMES))


def windows_shortcut_script(path, py, script):
    """A .lnk to pythonw.exe "<installed script>" --settings, then read back."""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"New-Item -ItemType Directory -Force -Path {_ps_quote(Path(path).parent)} | Out-Null",
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(path)})",
        f"$s.TargetPath = {_ps_quote(py)}",
        f"$s.Arguments = {_ps_quote(chr(34) + str(script) + chr(34) + ' --settings')}",
        f"$s.WorkingDirectory = {_ps_quote(Path(script).parent)}",
        f"$s.Description = {_ps_quote(t('Opens the settings page of the Latiddo bridge'))}",
        "$s.Save()",
        f"$c = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(path)})",
        f"if ($c.TargetPath -ne {_ps_quote(py)}) {{ throw \"the shortcut was not saved\" }}",
        "'saved'",
    ])


def _desktop_arg(s):
    """One argument of Exec= in a .desktop file (quoted when needed, % doubled)."""
    s = str(s).replace("%", "%%")
    if not any(c in s for c in " \t\n\"'\\><~|&;$*?#()`"):
        return s
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in s).replace("\\", "\\\\") + '"'


def desktop_entry(py, script):
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=" + SHORTCUT_NAMES[0],
        "Name[es]=" + SHORTCUT_NAMES[1],
        "Comment=Opens the settings page of the Latiddo bridge",
        "Comment[es]=" + common.I18N_ES["Opens the settings page of the Latiddo bridge"],
        "Exec=" + " ".join([_desktop_arg(py), _desktop_arg(script), "--settings"]),
        "Terminal=false",
        "Categories=Utility;",
        "",
    ])


def _as_string(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def mac_applet_source(py, script):
    """AppleScript of a small app that starts the settings page in the background (no Terminal)."""
    return ('do shell script "nohup " & quoted form of ' + _as_string(py) + ' & " " & quoted form of '
            + _as_string(script) + ' & " --settings >/dev/null 2>&1 &"')


def remove_shortcut():
    """Remove our shortcut under any of its names -> the paths removed."""
    removed = []
    for p in shortcut_paths():
        try:
            if p.is_dir():   # macOS: only an app made by osacompile
                if (p / "Contents" / "Resources" / "Scripts" / "main.scpt").is_file():
                    shutil.rmtree(p)
                    removed.append(p)
            elif p.is_file():
                p.unlink()
                removed.append(p)
        except OSError:
            pass
    return removed


def shortcut_plan():
    """(line of the plan, function that adds it). A failure never stops the setup."""
    path = shortcut_path()
    py, script = python_for_task(), str(installed_script())

    def apply():
        try:
            remove_shortcut()   # also the one named in the other language
            if os.name == "nt":
                rc, out = run_powershell(windows_shortcut_script(path, py, script))
                if rc != 0:
                    raise SetupError(out)
            elif sys.platform == "darwin":
                path.parent.mkdir(parents=True, exist_ok=True)
                rc, out = run_cmd(["osacompile", "-o", str(path), "-e", mac_applet_source(py, script)])
                if rc != 0:
                    raise SetupError(out)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(desktop_entry(py, script), encoding="utf-8")
                os.replace(tmp, path)
            print(t("Added the shortcut «{name}» ({path}).", name=shortcut_name(), path=path))
            return True
        except (SetupError, OSError) as e:
            print(t("The shortcut was not added ({error}). The settings page still opens with: {cmd}",
                    error=str(e).strip() or type(e).__name__, cmd=settings_command()))
            return False
    if os.name == "nt":
        line = t("Add «{name}» to the Start menu: it opens the settings page, without a console window ({path})",
                 name=shortcut_name(), path=path)
    elif sys.platform == "darwin":
        line = t("Add the app «{name}» to {dir}: it opens the settings page, without Terminal",
                 name=shortcut_name(), dir=path.parent)
    else:
        line = t("Add «{name}» to the applications menu: it opens the settings page, without a terminal ({path})",
                 name=shortcut_name(), path=path)
    return line, apply


def settings_command():
    return f'"{python_for_console()}" "{installed_script()}" --settings'


def shortcut_where():
    """Where to find the shortcut, for the last lines of the setup."""
    if os.name == "nt":
        return t("Start menu, «{name}»", name=shortcut_name())
    if sys.platform == "darwin":
        return t("the app «{name}» in {dir} (also with Spotlight)", name=shortcut_name(), dir=shortcut_dir())
    return t("the applications menu, «{name}»", name=shortcut_name())


def send_test(ip, parsed):
    payload = build_payload(parsed, time.time())
    password = common.load_config()[1]
    ok, result = common.send(ip, password, payload, timeout=common.POLL_NET_TIMEOUT)
    if not ok:
        print(t("Test send to {url} failed: {result}.", url=f"http://{ip}/api/update", result=result))
        return False
    time.sleep(0.3)
    st, d = probe(ip)
    if st == "ok" and d.get("data_state") == "active" and d.get("session_pct") == payload["sesion"]:
        print(t("The Pulse received the test: session {s}%, weekly {w}%.", s=payload["sesion"], w=payload["semanal"]))
        return True
    print(t("The test was sent, but the Pulse does not show it as active. Check the device screen."))
    return False


HOOKS_QUESTION = "Also show Claude Code status (working / needs you / done) on your devices? [Y/n] "


def cmd_setup(ip_arg, minutes, claude_path, yes=False, dry_run=False, with_statusline=False,
              with_hooks=False, robot_arg=None, devices_arg=None):
    if not 1 <= minutes <= 1439:
        print(t("--minutes must be between 1 and 1439."))
        return 1
    interactive = is_interactive()
    try:
        devices = choose_devices(ip_arg, robot_arg, devices_arg, interactive and not yes and not dry_run)
        want_pulse, want_robot = "pulse" in devices, "latiddo" in devices
        ip, hostname, robot = None, "", None
        if want_pulse:
            ip, status = find_device(ip_arg, interactive)
            hostname = str(status.get("hostname") or "").strip()
            print(t("Latiddo Pulse found at {ip} ({label}).", ip=ip, label=device_label(status)))
        if want_robot:
            robot, rstatus = find_robot(robot_arg, interactive)
            print(t("Latiddo robot found at {ip} ({label}).", ip=robot, label=robot_label(rstatus)))
        # Signing in is offered only when someone is there to answer (never with --yes or --dry-run)
        claude, parsed = check_claude(claude_path, offer_signin=interactive and not yes and not dry_run)
        print(t("Claude Code is ready: session {s}%, weekly {w}%.", s=f"{parsed['session']['pct']:g}",
                w=f"{parsed['week']['pct']:g}"))

        # Live Claude Code status (the Pulse and the robot show it): asked only when someone is there
        if not with_hooks and interactive and not yes and not dry_run:
            with_hooks = ask("\n" + t(HOOKS_QUESTION)).lower() not in ("n", "no")

        dest = install_dir()
        sched_lines, sched_apply = schedule_plan(minutes)
        saved = []
        if want_pulse:
            saved.append(t("Pulse ip {ip}", ip=ip) + (t(", hostname {name}", name=hostname) if hostname else ""))
        else:
            saved.append(t("the Pulse switched off (its IP is kept, if any)"))
        if want_robot:
            saved.append(t("robot {address}", address=robot))
        elif common.load_json(common.config_path(), {}).get("robot"):
            saved.append(t("the robot switched off (its address is kept)"))
        plan = [t("Copy {a} and {b} to {dest} (the task runs from there, so moving or switching the "
                  "repository does not break it)", a=BRIDGE_FILES[0], b=BRIDGE_FILES[1], dest=dest),
                t("Save {path} with {what} and claude_path {claude} (no passwords)", path=common.config_path(),
                  what=", ".join(saved), claude=claude)]
        if with_statusline:
            plan.append(t("Add the Claude Code status line to {path} (only if no other status line is set)",
                          path=common.settings_path()))
        if with_hooks:
            plan.append(t("Add Claude Code hooks to {path} for the live status (working / needs you / done). "
                          "Your other hooks and settings are kept; a backup is made first",
                          path=common.settings_path()))
        plan += sched_lines
        short_line, short_apply = shortcut_plan()
        plan.append(short_line)
        plan.append(t("Send a test update to the Pulse and the robot and check that it arrived")
                    if want_pulse and want_robot else
                    t("Send a test update to the Pulse and check that it arrived") if want_pulse else
                    t("Send a test update to the robot and check that it arrived"))
        print("\n" + t("Plan:"))
        for line in plan:
            print("  - " + line)
        if dry_run:
            if os.name == "nt":
                print("\n" + t("PowerShell that would run:") + "\n  "
                      + windows_task_script(minutes).replace("\n", "\n  "))
            print("\n" + t("Dry run: nothing was changed."))
            return 0
        if not yes:
            if not interactive:
                raise SetupError(t("No terminal to ask for confirmation. Run again with --yes."))
            if not common.is_yes(ask("\n" + t("Continue? [y/N] "))):
                print(t("Cancelled. Nothing was changed."))
                return 1

        if copy_bridge():
            print(t("Copied the bridge scripts to {dest}.", dest=dest))
        save_config(ip, claude, hostname, robot, pulse_on=want_pulse,
                    robot_on=want_robot if (want_robot or common.load_json(common.config_path(), {}).get("robot")) else None)
        if want_pulse:
            # its firmware, already read: the snapshot to the Pulse starts with the first run
            common.remember_pulse_fw(common.normalize_ip(ip), status.get("fw"))
        print(t("Saved {path}.", path=common.config_path()))
        if with_statusline:
            # Run the installed copy, so the status line points to the fixed folder too (it keeps
            # the keys saved above)
            print()
            rc, out = run_cmd([sys.executable, str(dest / "latiddo_statusline.py"), "--install"]
                              + (["--ip", ip] if want_pulse else []))
            print(out)
            if rc != 0:
                print(t("The status line was not added (see above). The rest of the setup continues."))
            print()
        if with_hooks:
            ok, msg = common.install_hooks(python_for_task(), dest / "latiddo_statusline.py")
            print(msg if ok else msg + "\n" + t("The hooks were not added. The rest of the setup continues."))
        sched_apply()
        has_shortcut = short_apply()
        ok_pulse = send_test(ip, parsed) if want_pulse else True
        ok_robot = send_test_robot(robot, parsed) if want_robot else True
        settings_cmd = settings_command()
        if has_shortcut:
            settings_cmd = t("{where}, or {cmd}", where=shortcut_where(), cmd=settings_cmd)
        if ok_pulse and ok_robot:
            print("\n" + (t("Done. The Pulse and the robot will be updated every {n} min.", n=minutes)
                          if want_pulse and want_robot else
                          t("Done. The Pulse will be updated every {n} min.", n=minutes) if want_pulse else
                          t("Done. The robot will be updated every {n} min.", n=minutes)))
            print(t("Settings page (switch each device on or off, change its address, language): {cmd}",
                    cmd=settings_cmd))
            return 0
        print("\n" + (t("The task is installed (every {n} min), but the test reached neither the Pulse nor the robot.",
                          n=minutes) if not ok_pulse and not ok_robot else
                        t("The task is installed (every {n} min), but the test did not reach the Pulse.", n=minutes)
                        if not ok_pulse else
                        t("The task is installed (every {n} min), but the test did not reach the robot.", n=minutes)))
        print(t("Settings page: {cmd}", cmd=settings_cmd))
        return 1
    except SetupError as e:
        print("\n" + str(e))
        return 1
    except KeyboardInterrupt:
        print("\n" + t("Cancelled."))
        return 1


def bridge_files():
    """What the bridge leaves on this computer besides settings.json and the scheduled task: its
    files in ~/.claude and its temporary files (only those that exist)."""
    own = [common.config_path(), common.hosts_path(), common.log_path()]
    temp = [common.state_path(), common.dump_path(), common.sends_path(), common.cc_dir(),
            common.pulse_fw_path(), state_path()]
    return [p for p in own if p.exists()], [p for p in temp if p.exists()] + leftover_files()


def leftover_files():
    """1.3.2: what interrupted writes of earlier versions left (<our file>.<pid>.tmp next to each
    of our files, also next to settings.json) and the hosts file a 1.2 development version kept
    in the temp folder."""
    pid_tmp = re.compile(r"\.\d+\.tmp$")
    ours = [common.config_path(), common.hosts_path(), common.state_path(), common.dump_path(),
            common.sends_path(), common.pulse_fw_path(), state_path(), common.settings_path()]
    out = []
    for p in ours:
        if p.parent.is_dir():
            out += [q for q in sorted(p.parent.glob(p.name + ".*.tmp")) if pid_tmp.search(q.name)]
    legacy = Path(tempfile.gettempdir()) / "latiddo_hosts.json"
    if legacy.is_file():
        out.append(legacy)
    return out


def delete_path(p):
    """One file or folder of the bridge; True if it is gone."""
    try:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        return True
    except OSError as e:
        print(t("Could not delete {path}: {error}", path=p, error=e))
        return False


def delete_scripts(dest):
    """The bridge scripts in the install folder (and __pycache__, leftovers .tmp); the folder too
    when nothing else is left in it. The number of paths deleted."""
    n = 0
    if not dest.is_dir():
        return 0
    for p in list(dest.iterdir()):
        if p.name in BRIDGE_FILES or p.name == "__pycache__" or p.name.endswith(".tmp"):
            n += 1 if delete_path(p) else 0
    try:
        dest.rmdir()
    except OSError:
        print(t("Kept {dest}: it has other files.", dest=dest))
    return n


def cmd_remove(yes=False):
    """1.3.1: everything the bridge added: the scheduled task, its hooks and status line in
    settings.json (a dated copy first; nothing else there is touched), the settings shortcut,
    its scripts, its files in ~/.claude and its temporary files."""
    interactive = is_interactive()
    lines, apply_task = remove_plan()
    has_hooks = common.hooks_present()
    settings, _ = common.read_settings()
    has_line = isinstance(settings, dict) and common.is_ours(settings.get("statusLine"))
    dest = install_dir()
    own, temp = bridge_files()
    if has_hooks:
        lines = lines + [t("The Latiddo hooks in {path} (other hooks are kept)", path=common.settings_path())]
    if has_line:
        lines = lines + [t("The Latiddo status line in {path} (a dated copy of the file first)",
                           path=common.settings_path())]
    shortcuts = [p for p in shortcut_paths() if p.exists()]
    if shortcuts:
        lines = lines + [t("The shortcut {path}", path=p) for p in shortcuts]
    if dest.is_dir():
        lines = lines + [t("The bridge scripts in {dest}", dest=dest)]
    if own:
        lines = lines + [t("Its files: {paths}", paths=", ".join(str(p) for p in own))]
    if temp:
        lines = lines + [t("Its temporary files: {paths}", paths=", ".join(str(p) for p in temp))]

    def apply():
        apply_task()
        if has_hooks:
            print(common.remove_hooks()[1])
        if has_line:
            print(common.remove_statusline()[1])
        for p in remove_shortcut():
            print(t("Removed the shortcut {path}.", path=p))
        n = delete_scripts(dest)
        for p in own + temp:
            n += 1 if delete_path(p) else 0
        print(t("Deleted {n} files of the bridge.", n=n))
        left = ([p for p in bridge_files()[0] + bridge_files()[1]] + ([dest] if dest.exists() else [])
                + [p for p in shortcut_paths() if p.exists()])
        if not left:
            print(t("Nothing of the bridge is left on this computer."))
    print(t("This removes:"))
    for line in lines:
        print("  - " + line)
    try:
        if not yes:
            if not interactive:
                raise SetupError(t("No terminal to ask for confirmation. Run again with --yes."))
            if not common.is_yes(ask(t("Continue? [y/N] "))):
                print(t("Cancelled. Nothing was changed."))
                return 1
        apply()
    except SetupError as e:
        print(str(e))
        return 1
    return 0


# ----------------------------------------------------------------------------
# --settings: a page on this computer only (http://127.0.0.1:<port>)
# ----------------------------------------------------------------------------
SETTINGS_PORT = 8737
SETTINGS_IDLE_S = 15 * 60          # the server stops 15 min after the page was last open
ADDRESS_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?$")
SOURCES = {"poll": "scheduled task", "statusline": "status line", "test": "test"}

# The page: the same language system as the Pulse's portal (English in the code, I18N_ES from
# latiddo_statusline.py, t() with {variables}, EN / ES selector). Texts set by the code go through
# t(); the server puts LANG, LANG_SAVED and the dictionary in the page.
SETTINGS_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Latiddo bridge</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--text:#1d1c1a;--muted:#6b6862;--line:#e3e0da;--accent:#c76a00;
--ok:#1f8a4c;--bad:#c0392b;--field:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--card:#1c1c20;--text:#ecebe8;--muted:#9a978f;
--line:#2e2e34;--accent:#ffb000;--ok:#3ecf7a;--bad:#ff6b5e;--field:#141417}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:28px 16px 40px}
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
h1{font-size:22px;margin:0 0 4px}h1 span{color:var(--accent)}
.sub{color:var(--muted);margin:0 0 22px}
.lang-sel{display:flex;flex:none;gap:2px;padding:3px;border:1px solid var(--line);border-radius:999px;background:var(--card)}
.lang-sel button{min-width:34px;height:28px;padding:0 8px;border:0;border-radius:999px;background:transparent;
color:var(--muted);font-size:12px;font-weight:760;letter-spacing:.04em;cursor:pointer}
.lang-sel button[aria-pressed=true]{background:linear-gradient(180deg,#FFC233,#FFB000);color:#241800}
.lang-sel button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.head h2{font-size:17px;margin:0}
.switch{position:relative;display:inline-flex;align-items:center;gap:8px;cursor:pointer;color:var(--muted);font-size:14px}
.switch input{position:absolute;opacity:0;width:1px;height:1px}
.sw{width:44px;height:26px;border-radius:99px;background:var(--line);position:relative;transition:background .2s}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:transform .2s}
.switch input:checked+.sw{background:var(--accent)}.switch input:checked+.sw::after{transform:translateX(18px)}
.switch input:focus-visible+.sw{outline:2px solid var(--accent);outline-offset:2px}
label.addr{display:block;margin:14px 0 4px;font-size:13px;color:var(--muted)}
.row{display:flex;gap:8px;flex-wrap:wrap}
input[type=text]{flex:1 1 220px;min-width:0;padding:9px 11px;border-radius:9px;border:1px solid var(--line);
background:var(--field);color:var(--text);font:inherit}
button{padding:9px 14px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--text);
font:inherit;cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent);color:#1d1c1a;font-weight:600}
button:disabled{opacity:.5;cursor:default}
.hint{font-size:13px;color:var(--muted);margin-top:6px}
.last{margin-top:14px;padding-top:12px;border-top:1px solid var(--line);font-size:14px}
.last b{font-weight:600}.ok{color:var(--ok)}.bad{color:var(--bad)}
.msg{font-size:13px;margin-top:8px}
.msg:empty{display:none}
.bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0 18px}
footer{color:var(--muted);font-size:12.5px;margin-top:20px;word-break:break-all}
</style></head><body><main>
<div class="top"><div><h1 id="h1"></h1><p class="sub" id="sub"></p></div>
<div class="lang-sel" role="group" aria-label="Language"><button type="button" data-lang="en" lang="en">EN</button><button type="button" data-lang="es" lang="es">ES</button></div></div>
<div id="cards"></div>
<div class="bar"><button class="primary" id="save"></button><span class="msg" id="saved"></span></div>
<footer id="foot"></footer>
</main>
<script>
const T="__TOKEN__",$=id=>document.getElementById(id);
const I18N_ES=__I18N__;
let LANG="__LANG__",langSaved="__LANG_SAVED__";
function t(s,v){let r=(LANG==="es"&&I18N_ES[s])||s;if(v)r=r.replace(/\{(\w+)\}/g,(m,k)=>(k in v?String(v[k]):m));return r}
const DEV=[["pulse","Latiddo Pulse","Its IP, shown on the Pulse at startup (for example 192.168.1.50)."],
  ["latiddo","Latiddo robot","Its name latiddo-xxxx.local or its IP, shown in the robot's portal under Ajustes."]];
const SRC=__SOURCES__;
let st=null,dirty=false,form=null;
async function call(path,body){
  const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json","X-Latiddo-Token":T},body:JSON.stringify(body)}
                        :{headers:{"X-Latiddo-Token":T}});
  return r.json();
}
const two=n=>String(n).padStart(2,"0");
function when(ts){const d=new Date(ts*1000),a=Math.max(0,Math.round(Date.now()/1000-ts));
  const ago=a<60?t("{n} s ago",{n:a}):a<3600?t("{n} min ago",{n:Math.round(a/60)}):t("{n} h ago",{n:Math.round(a/3600)});
  return two(d.getHours())+":"+two(d.getMinutes())+":"+two(d.getSeconds())+" ("+ago+")"}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"})[c])}
function lastHtml(l){
  if(!l)return '<div class="last">'+t("Last send:")+' <b>'+t("none yet")+'</b></div>';
  const head=l.ok?'<span class="ok">✅ OK</span>':'<span class="bad">❌ '+esc(l.result)+'</span>';
  let h='<div class="last">'+t("Last send:")+' <b>'+head+'</b> '+t("at {time}",{time:when(l.at)})+' · '+esc(t(SRC[l.source]||l.source))+' · '+esc(l.address);
  if(!l.ok)h+='<br><span class="hint">'+t("Last successful send:")+' '+(l.last_ok?when(l.last_ok):t("never"))+'</span>';
  return h+'</div>';
}
// The last Claude Code event (hooks) sent to the device, if any
const CC={working:"working",needs_you:"needs you",done:"done",error:"error",end:"end",idle:"idle"};
function hookHtml(k){
  if(!k)return "";
  return '<div class="hint">'+t("Last Claude Code event:")+' '+(k.ok?'<span class="ok">✅</span>':'<span class="bad">❌</span>')+' '+
    t("at {time}",{time:when(k.at)})+(k.state?' · '+esc(t(CC[k.state]||k.state)):"")+'</div>';
}
// the form keeps what is being typed when the language changes or the page refreshes
function readForm(){if(!$("e-pulse"))return null;const f={};DEV.forEach(([id])=>{f[id]={enabled:$("e-"+id).checked,address:$("a-"+id).value}});return f}
function render(){
  const f=dirty?readForm():null;
  document.documentElement.lang=LANG;document.title=t("Latiddo bridge");
  const w=t("Latiddo bridge").split(" ");$("h1").innerHTML=esc(w[0])+" <span>"+esc(w.slice(1).join(" "))+"</span>";$("sub").textContent=t("Where your Claude usage goes. The scheduled task reads these settings on its next run.");
  $("save").textContent=t("Save");
  document.querySelectorAll(".lang-sel button").forEach(b=>b.setAttribute("aria-pressed",b.dataset.lang===LANG?"true":"false"));
  $("cards").innerHTML=DEV.map(([id,name,hint])=>{const d=f?f[id]:st[id];return '<section class="card" id="c-'+id+'">'+
    '<div class="head"><h2>'+t(name)+'</h2><label class="switch"><span id="t-'+id+'">'+(d.enabled?t("On"):t("Off"))+'</span>'+
    '<input type="checkbox" id="e-'+id+'"'+(d.enabled?" checked":"")+'><span class="sw"></span></label></div>'+
    '<label class="addr" for="a-'+id+'">'+t("Address")+'</label><div class="row"><input type="text" id="a-'+id+'" value="'+esc(d.address)+
    '" spellcheck="false" autocomplete="off"><button id="k-'+id+'">'+t("Check")+'</button></div>'+
    '<div class="hint">'+t(hint)+(st[id].env?' <b>'+t("An environment variable overrides it.")+'</b>':'')+'</div>'+
    '<div class="msg" id="m-'+id+'"></div><div id="l-'+id+'"></div></section>'}).join("");
  DEV.forEach(([id])=>{
    $("e-"+id).onchange=e=>{$("t-"+id).textContent=e.target.checked?t("On"):t("Off");dirty=true;$("saved").textContent=t("Not saved yet.")};
    $("a-"+id).oninput=()=>{dirty=true;$("saved").textContent=t("Not saved yet.")};
    $("k-"+id).onclick=()=>check(id);
    $("l-"+id).innerHTML=lastHtml(st[id].last)+hookHtml(st[id].hook);
  });
  $("foot").innerHTML=esc(t("Bridge {v} · settings in {path}",{v:st.version,path:st.config}))+
    "<br>"+esc(t("This page only works on this computer. It closes by itself 15 min after you close it."));
}
function refreshLast(){DEV.forEach(([id])=>{const el=$("l-"+id);if(el)el.innerHTML=lastHtml(st[id].last)+hookHtml(st[id].hook)})}
async function load(first){try{st=await call("/api/state");if(first||!dirty)render();else refreshLast()}
  catch(e){$("saved").textContent=t("The bridge settings server is not running.")}}
async function check(id){
  $("m-"+id).textContent=t("Checking…");
  const j=await call("/api/check",{id,address:$("a-"+id).value});
  $("m-"+id).innerHTML=(j.ok?'<span class="ok">✅ ':'<span class="bad">❌ ')+esc(j.message||j.error)+'</span>';
}
$("save").onclick=async()=>{
  $("save").disabled=true;const j=await call("/api/config",readForm());$("save").disabled=false;
  $("saved").innerHTML=j.ok?'<span class="ok">✅ '+esc(j.message)+'</span>':'<span class="bad">❌ '+esc(j.error)+'</span>';
  if(j.ok){dirty=false;load(true)}
};
// EN / ES: translated at once (no reload) and saved in latiddo.json, for the console and the log too
document.querySelectorAll(".lang-sel button").forEach(b=>b.onclick=async()=>{
  const l=b.dataset.lang;if(l===LANG&&l===langSaved)return;
  LANG=l;langSaved=l;const keep=$("saved").textContent;render();$("saved").textContent=keep;
  await call("/api/lang",{lang:l});
});
load(true);setInterval(()=>{if(!document.hidden)load(false)},10000);
</script></body></html>
"""

# Texts of the page that only the JavaScript uses: listed here so the translation check finds them
PAGE_TEXTS = ("Latiddo bridge", "Where your Claude usage goes. The scheduled task reads these settings on its next run.",
              "Save", "Latiddo robot", "On", "Off", "Address", "Check",
              "Its IP, shown on the Pulse at startup (for example 192.168.1.50).",
              "Its name latiddo-xxxx.local or its IP, shown in the robot's portal under Ajustes.",
              "An environment variable overrides it.", "Not saved yet.", "Last send:", "none yet", "at {time}",
              "Last successful send:", "never", "{n} s ago", "{n} min ago", "{n} h ago",
              "Bridge {v} · settings in {path}",
              "This page only works on this computer. It closes by itself 15 min after you close it.",
              "The bridge settings server is not running.", "Checking…",
              "scheduled task", "status line", "test",
              "Last Claude Code event:", "working", "needs you", "done", "error", "end", "idle")


def settings_state():
    cfg = common.load_json(common.config_path(), {})
    sends = common.load_json(common.sends_path(), {})

    def dev(id_, address, enabled, env):
        last = sends.get(id_) if isinstance(sends.get(id_), dict) else None
        hook = sends.get("hook_" + id_) if isinstance(sends.get("hook_" + id_), dict) else None
        return {"address": address, "enabled": enabled, "env": env, "last": last, "hook": hook}
    pulse_ip = common.normalize_ip(cfg.get("ip") or "")
    robot = common.normalize_ip(cfg.get("robot") or "")
    return {"version": common.BRIDGE_VERSION, "config": str(common.config_path()),
            "lang": common.lang(), "lang_saved": common.saved_lang(),
            "pulse": dev("pulse", pulse_ip, bool(pulse_ip) and common.pulse_enabled(cfg),
                         bool(os.environ.get("LATIDDO_IP"))),
            "latiddo": dev("latiddo", robot, common.robot_enabled(cfg),
                           bool(os.environ.get("LATIDDO_ROBOT")))}


def settings_save(body):
    """Body {"pulse": {"enabled", "address"}, "latiddo": {...}} -> (ok, message). Keeps every
    other key of the config (password, claude_path, hostname, lang)."""
    cfg = common.load_json(common.config_path(), {})
    names = {"pulse": "Latiddo Pulse", "latiddo": t("Latiddo robot")}
    for id_, key in (("pulse", "ip"), ("latiddo", "robot")):
        d = body.get(id_)
        if not isinstance(d, dict):
            continue
        address = common.normalize_ip(d.get("address") or "")
        enabled = d.get("enabled") is True
        if enabled and not address:
            return False, t("{name}: type its address to switch it on.", name=names[id_])
        if address and not ADDRESS_RE.match(address):
            return False, t("{name}: «{address}» is not an IP or a device name.", name=names[id_], address=address)
        if address:
            cfg[key] = address
        else:
            cfg.pop(key, None)
        if id_ == "pulse":
            if enabled:
                cfg.pop("pulse_enabled", None)
            else:
                cfg["pulse_enabled"] = False
        else:
            cfg["robot_enabled"] = enabled
    common.save_json_atomic(common.config_path(), cfg, indent=2)
    on = [names[i] for i in ("pulse", "latiddo")
          if (common.pulse_enabled(cfg) and cfg.get("ip") if i == "pulse" else common.robot_enabled(cfg))]
    if not on:
        return True, t("Saved. Nothing is sent now.")
    return True, t("Saved. Sending to: {devices}.", devices=(" " + t("and") + " ").join(on))


def settings_check(id_, address):
    """Is there a Pulse / robot at `address`? -> (ok, message)."""
    address = common.normalize_ip(address or "")
    if not address or not ADDRESS_RE.match(address):
        return False, t("Type an IP or a device name first.")
    if id_ == "pulse":
        st, d = probe(address)
        if st == "ok":
            return True, t("Latiddo Pulse found ({label}).", label=device_label(d))
        return False, (t("No answer: check the address and that this computer is on the same network.")
                       if st == "down" else t("It answered, but it is not a Latiddo Pulse."))
    st, d = probe_robot(address)
    if st == "ok":
        return True, t("Latiddo robot found ({label}).", label=robot_label(d))
    if st == "old":
        return False, t("It is a Latiddo robot, but its firmware cannot take Claude data yet: update it.")
    if st == "other":
        return False, t("It answered, but it is not a Latiddo robot.")
    return False, t("No answer: check the address and that this computer is on the same network.")


def make_settings_handler(port, token):
    from http.server import BaseHTTPRequestHandler
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    def page():
        # the dictionary as JSON in a <script>: "</" can never close it
        i18n = json.dumps(common.I18N_ES, ensure_ascii=False).replace("</", "<\\/")
        return (SETTINGS_PAGE.replace("__TOKEN__", token).replace("__I18N__", i18n)
                .replace("__LANG_SAVED__", common.saved_lang()).replace("__LANG__", common.lang())
                .replace("__SOURCES__", json.dumps(SOURCES))).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "latiddo-bridge"
        sys_version = ""

        def log_message(self, *args):
            pass

        def reply(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, write):
            """Only this computer, only by its own address (no DNS rebinding), and writes (and
            the data) only from this page: its token travels in a header that another web page
            cannot send without a preflight this server never answers."""
            self.server.last_seen = time.monotonic()
            if self.client_address[0] != "127.0.0.1" or self.headers.get("Host", "") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in origins:
                return False
            if write and not self.headers.get("Content-Type", "").startswith("application/json"):
                return False
            return True

        def token_ok(self):
            import hmac
            return hmac.compare_digest(self.headers.get("X-Latiddo-Token", ""), token)

        def do_GET(self):
            if not self.allowed(False):
                return self.reply(403, {"ok": False, "error": "Forbidden"})
            if self.path == "/":
                return self.reply(200, page(), "text/html; charset=utf-8")
            if self.path == "/api/state" and self.token_ok():
                return self.reply(200, settings_state())
            return self.reply(404, {"ok": False, "error": "Not found"})

        def do_POST(self):
            # The body is read first, also for a request that is refused: answering before it
            # is read makes some systems reset the connection under the client
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= 4096:
                self.close_connection = True
                return self.reply(413, {"ok": False, "error": "Too large"})
            raw = self.rfile.read(n) if n else b""
            if not self.allowed(True) or not self.token_ok():
                return self.reply(403, {"ok": False, "error": "Forbidden"})
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                body = None
            if not isinstance(body, dict):
                return self.reply(400, {"ok": False, "error": "Bad request"})
            if self.path == "/api/config":
                ok, msg = settings_save(body)
            elif self.path == "/api/check" and body.get("id") in ("pulse", "latiddo"):
                ok, msg = settings_check(body["id"], body.get("address"))
            elif self.path == "/api/lang" and body.get("lang") in ("en", "es", ""):
                common.set_lang(body["lang"])
                ok, msg = True, "OK"
            else:
                return self.reply(404, {"ok": False, "error": "Not found"})
            return self.reply(200, {"ok": True, "message": msg} if ok else {"ok": False, "error": msg})

    return Handler


def start_settings_server(port):
    """(server, port, token) on 127.0.0.1, trying port..port+9; (None, None, None) if all busy."""
    import secrets
    from http.server import ThreadingHTTPServer
    token = secrets.token_urlsafe(24)
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_settings_handler(p, token))
        except OSError:
            continue
        server.daemon_threads = True
        server.last_seen = time.monotonic()
        return server, p, token
    return None, None, None


def settings_running(port):
    """Is our settings page already served on 127.0.0.1:port?"""
    import http.client
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status == 200 and (r.getheader("Server") or "").startswith("latiddo-bridge")
    except (OSError, http.client.HTTPException):
        return False


def open_url(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def cmd_settings(port, open_browser=True):
    import threading
    if settings_running(port):
        # the shortcut clicked again, or the page left open: the same page, not a second one
        url = f"http://127.0.0.1:{port}/"
        print(t("The settings page is already open: {url}", url=url))
        if open_browser:
            open_url(url)
        return 0
    server, p, _ = start_settings_server(port)
    if server is None:
        print(t("Ports {a}-{b} are busy. Run again with --port <another port>.", a=port, b=port + 9))
        return 1
    url = f"http://127.0.0.1:{p}/"
    print(t("Latiddo bridge settings: {url}", url=url))
    print(t("Only this computer can open it. It stops 15 min after you close the page, or with Ctrl+C."))

    def watchdog():
        while True:
            time.sleep(15)
            if time.monotonic() - server.last_seen > SETTINGS_IDLE_S:
                server.shutdown()
                return
    threading.Thread(target=watchdog, daemon=True).start()
    if open_browser:
        open_url(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(t("Settings page closed."))
    return 0


def main():
    common.lang_from_argv(sys.argv[1:])
    parser = argparse.ArgumentParser(description=t("Latiddo bridge - poll `claude -p /usage` for the Pulse and the robot"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--setup", action="store_true",
                       help=t("find the Pulse and/or the Latiddo robot, check Claude Code and schedule this script"))
    group.add_argument("--remove", action="store_true", help=t("remove the scheduled task"))
    group.add_argument("--test", action="store_true", help=t("query, show the result and send it"))
    group.add_argument("--install-task", action="store_true", help=t("same as --setup --yes"))
    group.add_argument("--uninstall-task", action="store_true", help=t("same as --remove --yes"))
    group.add_argument("--settings", action="store_true",
                       help=t("open the settings page (only on this computer): devices on/off, addresses, last sends"))
    parser.add_argument("--ip", help=t("Latiddo Pulse IP, for example 192.168.1.50"))
    parser.add_argument("--robot", help=t("Latiddo robot address: latiddo-xxxx.local or its IP"))
    parser.add_argument("--devices", choices=sorted(DEVICES_ARG),
                        help=t("--setup: which devices get the data (default: ask, or what is configured)"))
    parser.add_argument("--lang", choices=["es", "en", "auto"],
                        help=t("language of the bridge (saved; auto = the system language)"))
    parser.add_argument("--port", type=int, default=SETTINGS_PORT,
                        help=t("--settings: port on 127.0.0.1 (default {port})", port=SETTINGS_PORT))
    parser.add_argument("--no-browser", action="store_true", help=t("--settings: do not open the browser"))
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES,
                        help=t("how often to run (default {n})", n=DEFAULT_MINUTES))
    parser.add_argument("--claude-path", help=t("path to the claude executable"))
    parser.add_argument("--yes", action="store_true", help=t("do not ask for confirmation"))
    parser.add_argument("--dry-run", action="store_true", help=t("show what --setup would do"))
    parser.add_argument("--with-statusline", action="store_true",
                        help=t("--setup also adds the Claude Code status line"))
    parser.add_argument("--with-hooks", action="store_true",
                        help=t("--setup also adds the Claude Code hooks (live status on the Pulse)"))
    opts = parser.parse_args()
    try:  # never crash printing claude's output on a cp1252/cp850 console
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    if opts.settings:
        return cmd_settings(opts.port, open_browser=not opts.no_browser)
    if opts.setup or opts.install_task:
        return cmd_setup(opts.ip, opts.minutes, opts.claude_path, yes=opts.yes or opts.install_task,
                         dry_run=opts.dry_run, with_statusline=opts.with_statusline,
                         with_hooks=opts.with_hooks, robot_arg=opts.robot, devices_arg=opts.devices)
    if opts.remove or opts.uninstall_task:
        return cmd_remove(yes=opts.yes or opts.uninstall_task)
    if opts.test:
        return cmd_test(opts.ip, opts.claude_path, opts.robot)

    # Scheduled mode: silent, never fails, always exit code 0.
    try:
        sys.stderr = open(os.devnull, "w")
    except Exception:
        pass
    try:
        poll_once(opts.claude_path)
    except BaseException:
        pass
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
