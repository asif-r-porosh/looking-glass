# looking_glass_server.py
"""
looking_glass_server.py -- run ping / mtr / traceroute from a fixed source IP to a fixed destination IP
over HTTP, and stream the raw terminal output back. For users who may not log in to production VMs.

HOW IT WORKS (the 30-second version)
  1. A client sends  GET /<probe>/<remote>/<link>  with the header  Authorization: Bearer <token>.
     probe = ping | mtr | traceroute,  remote = a remote endpoint,  link = one connection to it (primary, backup-1...).
  2. We look up <remote>/<link> in target_defs.json to get its  src  (source IP) and  dest  (destination IP).
  3. We check this host can route src -> dest. If not, we say so (that is a HOST problem, not a test result).
  4. We run the real tool with fixed arguments and send its output to the client line by line, as it appears.
  5. A probe that runs too long is killed (config.json "timeouts"); too many at once get HTTP 429 ("max_probes").

FILES (next to this script):  config.json, target_defs.json, app.log (created).
COMMAND LINE:  python3 looking_glass_server.py        start the server
               python3 looking_glass_server.py -c     check tools, both files and every entry, then exit
               python3 looking_glass_server.py -d     start, and restart itself when this script changes (dev)
Python 3.8+, standard library only. Written to keep every probe's output exactly as the real tool printed it.
"""

import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------------------------
APP_NAME = "LOOKING_GLASS"  # shown in logs and in every error message
BASE_DIR = Path(__file__).resolve().parent  # the folder this script lives in
LOG_PATH = BASE_DIR / "app.log"
CONFIG_PATH = BASE_DIR / "config.json"
TARGET_DEF_PATH = BASE_DIR / "target_defs.json"

PROBES = ["ping", "mtr", "traceroute"]  # the only tools we ever run
LIST_TOPICS = ["probes", "remote", "link", "targets"]  # what `list <topic>` can show
NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,15}$")  # applied to a name AFTER _norm (trimmed, lower-case)
NAME_RULE = "a name starts with a letter, then letters, digits, . _ - only, 16 characters at most"
LOG_SEP = "::::"  # separates the parts of one log line
# Ends every message about OUR problems.
NOT_A_RESULT = (
    "NOT A TEST RESULT. This is a problem with the probe host or tool, not the network under test. Contact admin."
)

# What a client sees when it sends something we do not understand (HTTP 400).
help_text = (
    "\n\nUsage\n\n"
    + "\n".join(["list probes", "list remote", "list link", "list targets [remote|link]"])
    + "\n\n"
    + "\n".join([x + " remote link" for x in PROBES])
    + "\n\n"
)


class ConfigError(Exception):
    """config.json or target_defs.json is wrong. The message is worded so a user and an admin can both act on it."""


class _Obj(dict):
    """A dict that remembers keys that appeared twice in the same JSON object (`json` normally hides this)."""

    def __init__(self):
        super().__init__()
        self.dups = []


def _keep_duplicates(pairs):
    """json hook: build each JSON object as an _Obj and note repeated keys; the later value wins, as usual."""
    obj = _Obj()
    for key, value in pairs:
        if key in obj:
            obj.dups.append(key)
        obj[key] = value
    return obj


# ---------------------------------------------------------------------------------------------------------------
# Logging: one JSON object per line, to the terminal and to app.log
# ---------------------------------------------------------------------------------------------------------------
class LogJSONL(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info:  # logger.exception(...) attaches the Python traceback
            entry["traceback"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(entry)


def _setup_logger(name, log_file, log_level=logging.DEBUG):
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    logger.propagate = False  # do not also print through Python's root logger
    for handler in (logging.StreamHandler(), logging.FileHandler(log_file)):
        handler.setFormatter(LogJSONL())
        logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------------------------------------------
# Sending the HTTP response
# ---------------------------------------------------------------------------------------------------------------
def _response_header(req, code=200):
    """Send the status line and headers. After this a second status line must never be sent (see headers_sent)."""
    req.send_response(code)
    req.send_header("Content-Type", "text/plain; charset=utf-8")
    req.send_header("Cache-Control", "no-cache")
    req.end_headers()
    req.headers_sent = True


def _response_content(req, content):
    """Write text (or bytes) to the client and flush at once, so probe output appears live."""
    req.wfile.write(content.encode("utf-8") if isinstance(content, str) else content)
    req.wfile.flush()


def _response(req, content, code=200):
    _response_header(req, code)
    _response_content(req, content)


def _table(headers, rows):
    """Draw a plain-text table with a header row. Column widths follow the longest cell, so nothing is cut off."""
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]

    def line(cells):
        return " " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()

    rule = "-" * (sum(widths) + 3 * (len(widths) - 1) + 2)
    return "\n".join(["", rule, line(headers), rule] + [line(r) for r in rows] + [rule, ""])


# ---------------------------------------------------------------------------------------------------------------
# Reading and checking the two JSON files
# ---------------------------------------------------------------------------------------------------------------
def _read_json_file(file_path):
    """Read a JSON file. Any failure becomes a ConfigError with a reason a human can act on."""
    name = Path(file_path).name
    try:
        with open(file_path) as handle:
            return json.load(handle, object_pairs_hook=_keep_duplicates)
    except OSError as e:
        raise ConfigError(f"cannot read {name}: {e.strerror or e}")
    except ValueError as e:  # includes json.JSONDecodeError
        raise ConfigError(f"{name} is not valid JSON ({e})")


def _is_ipv4(text):
    """True for a plain IPv4 address such as 192.0.2.10. (IPv6 is not supported: the fixed commands are IPv4 only.)"""
    try:
        return isinstance(ipaddress.ip_address(text.strip()), ipaddress.IPv4Address)
    except ValueError:
        return False


def _norm(text):
    """The one canonical form of a name: trimmed and lower-case. Names are case-insensitive."""
    return text.strip().lower()


def _load_defs(defs_file, issues=None):
    """Read target_defs.json and check its shape:  { remote: { link: { "src": "...", "dest": "..." } } }.
    Names are normalised (see _norm). A name that breaks NAME_RULE is DISCARDED, together with everything under it,
    as if it were not in the file. Mistakes in the shape of the file raise ConfigError.
    `issues` (optional list): gets a (label, text) pair for each thing -c should print; the server passes nothing.
    It is read on EVERY request, so a fixed file works without restarting the server."""
    name = Path(defs_file).name
    raw = _read_json_file(defs_file)
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a JSON object of remote -> link -> src/dest")
    issues = [] if issues is None else issues
    issues += [
        ("duplicate", f"{d} appears more than once in {name}; only the last one is used") for d in _duplicates(raw)
    ]

    def good_names(obj, prefix):
        """{normalised name: value} for one JSON object; bad names are reported and left out."""
        kept = {}
        for key, value in obj.items():
            norm = _norm(key)
            if not NAME_RE.match(norm):
                issues.append(("bad name", f'"{prefix}{key}" is ignored: {NAME_RULE}'))
                continue
            if norm != key:
                issues.append(("note", f'"{prefix}{key}" is read as {prefix}{norm}'))
            if norm in kept:  # e.g. "Acme" and "acme": the same name once case is ignored
                issues.append(
                    ("duplicate", f"{prefix}{norm} appears more than once (case is ignored); only the last is used")
                )
            kept[norm] = value
        return kept

    defs = {}
    for remote, links in good_names(raw, "").items():
        if not isinstance(links, dict):
            raise ConfigError(f'{name}: "{remote}" must be an object of links')
        kept = {}
        for link, entry in good_names(links, f"{remote}/").items():
            if not isinstance(entry, dict):
                raise ConfigError(f'{name}: "{remote}/{link}" must be an object with "src" and "dest"')
            for key in ("src", "dest"):
                if not isinstance(entry.get(key), str) or not entry[key].strip():
                    raise ConfigError(f'{name}: "{remote}/{link}" needs a non-empty "{key}"')
            # src and dest must be plain IPv4 addresses: `ip route get`, `mtr -a` and `ping -I` reject names and URLs,
            # and an address can never be mistaken for an option such as "-f". A bad one drops just this entry.
            bad = [k for k in ("src", "dest") if not _is_ipv4(entry[k])]
            if bad:
                issues.append(
                    ("bad ip", f'"{remote}/{link}" is ignored: {bad[0]} "{entry[bad[0]]}" is not an IPv4 address')
                )
                continue
            kept[link] = {"src": entry["src"].strip(), "dest": entry["dest"].strip()}
        if kept:  # a remote with no usable link is not listed at all
            defs[remote] = kept
    return defs


def _entries(defs):
    """Flatten the defs into rows [remote, link, src, dest], in file order."""
    return [[remote, link, e["src"], e["dest"]] for remote, links in defs.items() for link, e in links.items()]


def _duplicates(obj, prefix=""):
    """Names used twice in the same JSON object, e.g. ['globex', 'acme/primary'].
    The later one silently replaces the earlier."""
    found = [prefix + key for key in obj.dups]
    for key, value in obj.items():
        if isinstance(value, _Obj):
            found += _duplicates(value, f"{prefix}{key}/")
    return found


def _log_path(config):
    """Where the log file goes: config "log_file_path", or app.log next to this script when it is empty or absent.
    A relative path is read from the script's folder."""
    value = config.get("log_file_path", "").strip()
    return BASE_DIR / Path(value).expanduser() if value else LOG_PATH  # BASE_DIR / absolute path = that path


def _log_path_problem(path):
    """Why we could not write the log file there, or "" when we can. Never creates the file (-c must not log)."""
    folder = path.parent
    if path.is_dir():
        return f"{path} is a folder, not a file"
    if path.exists():
        return "" if os.access(path, os.W_OK) else f"no permission to write {path}"
    if not folder.is_dir():
        return f"folder {folder} does not exist"
    return "" if os.access(folder, os.W_OK | os.X_OK) else f"no permission to create a file in {folder}"


def _load_config(config_file):
    """Read config.json and check every key we use. Anything wrong stops startup with a clear message."""
    name = Path(config_file).name
    config = _read_json_file(config_file)
    if not isinstance(config, dict):
        raise ConfigError(f"{name} must be a JSON object")
    missing = [key for key in ("listen_host", "listen_port", "auth_token") if key not in config]
    if missing:
        raise ConfigError(f"{name} is missing required key(s): {', '.join(missing)}")
    port, host, token = config["listen_port"], config["listen_host"], config["auth_token"]
    if not isinstance(host, str) or not isinstance(token, str):
        raise ConfigError(f'{name}: "listen_host" and "auth_token" must be text')
    if not host.strip():
        raise ConfigError(f'{name}: "listen_host" must not be empty')
    if not token or token != token.strip() or any(c.isspace() for c in token):
        raise ConfigError(f'{name}: "auth_token" must not be empty or contain spaces')
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f'{name}: "listen_port" must be a number from 1 to 65535')
    timeouts = config.get("timeouts")
    if not isinstance(timeouts, dict):
        raise ConfigError(f'{name}: "timeouts" must be an object, e.g. {{"ping": 30, "mtr": 60, "traceroute": 90}}')
    bad = [
        k
        for k in PROBES
        if isinstance(timeouts.get(k), bool) or not isinstance(timeouts.get(k), (int, float)) or timeouts[k] <= 0
    ]
    if bad:
        raise ConfigError(f'{name}: "timeouts" needs a number of seconds above 0 for: {", ".join(bad)}')
    max_probes = config.get("max_probes", 8)  # optional
    if not isinstance(max_probes, int) or isinstance(max_probes, bool) or max_probes < 1:
        raise ConfigError(f'{name}: "max_probes" must be a whole number, 1 or more (default 8)')
    if not isinstance(config.get("log_file_path", ""), str):  # optional
        raise ConfigError(f'{name}: "log_file_path" must be text, or "" for the default (app.log next to the script)')
    return config


# ---------------------------------------------------------------------------------------------------------------
# Understanding the request
# ---------------------------------------------------------------------------------------------------------------
def _is_authorized(req, auth_token):
    """True only for the header  Authorization: Bearer <the token from config.json>."""
    header = req.headers.get("Authorization", "")
    return header.startswith("Bearer ") and header.split(" ")[1] == auth_token


def _parse_request(url_path, defs):
    """Turn a URL path into a list of words, or None if it is not one we serve. Valid forms:
    list probes | list remote | list link | list targets | list targets <remote or link>
    <ping|mtr|traceroute> <remote> <link>        (the remote/link pair must exist in target_defs.json)"""
    args = [_norm(urllib.parse.unquote(a)) for a in urllib.parse.urlparse(url_path).path.strip("/").split("/")]
    if args[0] == "list":
        names = set(defs) | {link for links in defs.values() for link in links}
        if args[1:] in [[topic] for topic in LIST_TOPICS] or (
            len(args) == 3 and args[1] == "targets" and args[2] in names
        ):
            return args
    elif len(args) == 3 and args[0] in PROBES and args[2] in defs.get(args[1], {}):
        return args
    return None


def _list_text(topic, entries, only=None):
    """The table for a `list` request. `only` (targets only) keeps rows whose remote or link equals it."""
    if topic == "probes":
        return _table(["probe"], [[p] for p in PROBES])
    if topic == "targets":
        return _table(["remote", "link", "src", "dest"], [e for e in entries if only in (None, e[0], e[1])])
    # "remote": one row per remote listing its links;  "link": one row per link listing the remotes that have it.
    key, other = (0, 1) if topic == "remote" else (1, 0)
    groups = {}
    for entry in entries:
        groups.setdefault(entry[key], []).append(entry[other])
    return _table([topic, "links" if topic == "remote" else "remotes"], [[k, " + ".join(v)] for k, v in groups.items()])


def _get_client(req):
    """Best-effort client (ip, port) for the log. Forwarded headers are believed only when the TCP peer is a
    local/private address (a reverse proxy such as nginx); any other peer could forge them. A host inside the
    private network that talks to this server directly can still forge them. The client's own port is not
    knowable behind a proxy."""
    peer_ip, peer_port = req.client_address[:2]
    try:
        behind_proxy = ipaddress.ip_address(peer_ip).is_private  # includes loopback
    except ValueError:
        behind_proxy = False
    if behind_proxy:
        # X-Real-IP is set by the proxy itself; else take the LAST X-Forwarded-For entry, the one the proxy appended.
        candidates = [req.headers.get("X-Real-IP", "")] + req.headers.get("X-Forwarded-For", "").split(",")[-1:]
        for candidate in candidates:
            try:
                return str(ipaddress.ip_address(candidate.strip())), "Unknown"
            except ValueError:
                continue
    return peer_ip, peer_port


# ---------------------------------------------------------------------------------------------------------------
# Running a probe
# ---------------------------------------------------------------------------------------------------------------
def _route_check(src, dest, timeout=5):
    """Can this host route src -> dest? Runs `ip route get`. Returns (exit code, its output). Exit 0 = yes."""
    r = subprocess.run(
        ["ip", "route", "get", dest, "from", src],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        timeout=timeout,
    )
    return r.returncode, r.stdout.strip()


def _run_cmd(cmd):
    """Start the tool. stderr is merged into stdout so the client sees exactly what a terminal would show."""
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line by line
        start_new_session=True,  # own process group, so the whole group can be killed
    )


def _kill_group(p):
    """Kill the tool AND its helpers (mtr starts mtr-packet, which would otherwise keep the output open)."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:  # already gone
        pass


def _start_timeout(p, seconds):
    """Kill the probe after `seconds`. Returns (timer, fired); `fired` is set only if the kill really happened."""
    fired = threading.Event()

    def _fire():
        if p.poll() is None:  # still running
            fired.set()
            _kill_group(p)

    timer = threading.Timer(seconds, _fire)
    timer.daemon = True
    timer.start()
    return timer, fired


def _end_probe(req, timer, fired, probe, seconds, code, resp):
    """Stop the timer. If the probe was cut off or the tool itself failed, tell the client; returns the text sent."""
    timer.cancel()
    if fired.is_set():
        note = f"probe stopped after {seconds}s (timeout), output above is incomplete."
    elif (code >= 2 if probe == "ping" else code != 0) or (probe == "mtr" and "|--" not in resp):
        note = f"probe tool failed (exit {code}), output above is not a test result."
    else:
        return ""  # ping exits 1 when nothing answers: that is a real result, not a tool failure
    text = f"\n{APP_NAME}: {note} {NOT_A_RESULT}\n"
    _response_content(req, text)
    return text


# ---------------------------------------------------------------------------------------------------------------
# The HTTP handler
# ---------------------------------------------------------------------------------------------------------------
def probe_api_server_handler(defs_file, auth_token, logger, timeouts, max_probes):
    """Build the request-handler class (it needs the settings above, so it is made on the fly)."""
    slots = threading.BoundedSemaphore(max_probes)  # probes running at once; more get HTTP 429

    class Handler(BaseHTTPRequestHandler):
        headers_sent = False  # set by _response_header; tells the error path whether a status line is still allowed

        def do_GET(self):
            ip, port = _get_client(self)
            who = f"Client {ip}:{port}"

            def log(code, level=logger.info, *extra):
                """One log line: who asked, what we answered, plus any details."""
                level(f" {LOG_SEP} ".join([who, f"Response: {code}", *extra]))

            def reply(text, code=200, level=logger.info, detail=""):
                """Send a short, complete answer and log it."""
                _response(self, text, code)
                _response_content(self, b"\n\n")
                log(code, level, *([detail] if detail else []))

            # --- 1. Who is asking, and is the request understandable? (all quick; no probe yet) ---
            if not _is_authorized(self, auth_token):
                return reply("Unauthorized", 401)
            try:
                defs = _load_defs(defs_file)
            except ConfigError as e:
                return reply(f"{APP_NAME} ERROR: {e}. {NOT_A_RESULT}", 500, logger.error, f"Config error: {e}")
            args = _parse_request(self.path, defs)
            if args is None:
                return reply(help_text, 400)
            if args[0] == "list":
                text = _list_text(args[1], _entries(defs), *args[2:])
                return reply(text, detail=f"Cmd: {' '.join(args)} {LOG_SEP} Cmd Response: {text}")

            # --- 2. A probe. These are set before `try` so the error handler can always log them. ---
            probe, remote, link = args
            src, dest = defs[remote][link]["src"], defs[remote][link]["dest"]
            shell_cmd, resp, _return_code = "", "", None
            p = timer = None  # the tool's process and its timeout timer
            held = False  # do we hold one of the max_probes slots?

            def cmd_log():
                return f"Cmd: {shell_cmd}", f"Cmd Response: {resp}", f"Cmd Return: {_return_code}"

            try:
                # Gate: can this host route src -> dest at all? If not, a probe would only show OUR problem.
                code, out = _route_check(src, dest, timeouts.get("ip", 5))
                if code:
                    shell_cmd, resp, _return_code = f"ip route get {dest} from {src}", out, code
                    text = f"{APP_NAME} ERROR: probe host cannot route {src} to {dest} ({out}). {NOT_A_RESULT}"
                    return reply(text, 500, logger.error, " :::: ".join(cmd_log()))
                if not slots.acquire(blocking=False):  # all slots busy: refuse at once, do not queue
                    return reply(f"{APP_NAME}: busy, {max_probes} probes already running. Retry in a few seconds.", 429)
                held = True

                # The commands are fixed on purpose: nobody can pass their own arguments.
                cmd = {
                    "ping": ["ping", "-c", "3", "-I", src, dest],
                    "mtr": ["mtr", "-n", "-r", "-c", "3", "-a", src, dest],
                    "traceroute": ["traceroute", "-I", "-n", "-q", "3", "-s", src, dest],
                }[probe]
                p = _run_cmd(cmd)
                _response_header(self)  # from here on the answer is streamed
                shell_cmd = "\n\n$ " + shlex.join(cmd) + "\n\n"  # first line the client sees: the exact command
                _response_content(self, shell_cmd)
                timer, fired = _start_timeout(p, timeouts[probe])
                for line in p.stdout:  # blocks until the tool prints a line; ends when the tool exits
                    _response_content(self, line)
                    resp += line
                _return_code = p.wait()
                resp += _end_probe(self, timer, fired, probe, timeouts[probe], _return_code, resp)
                _response_content(self, b"\n\n")
                log(200, logger.info, *cmd_log())
            except ConnectionError:
                # The client went away mid-stream (broken pipe / reset): nothing to send back, just record it.
                log("Disconnected", logger.info, *cmd_log()[:2])
            except Exception as e:
                status = 200 if self.headers_sent else 500
                log(status, logger.exception, f"An Exception occurred: {e}", *cmd_log())
                error_text = f"{APP_NAME} ERROR: {type(e).__name__}: {e}. {NOT_A_RESULT}"
                if self.headers_sent:
                    # Output already started streaming: a second HTTP status line here would corrupt it.
                    try:
                        _response_content(self, f"\n\n{error_text}\n\n")
                    except ConnectionError:
                        pass
                else:
                    _response(self, f"Internal Server Error\n{error_text}", 500)
                    _response_content(self, b"\n\n")
            finally:
                # Always runs, however we left: give the slot back, stop the timer, make sure the tool is dead.
                if held:
                    slots.release()
                if timer is not None:
                    timer.cancel()
                if p is not None:
                    if p.poll() is None:
                        _kill_group(p)
                    p.wait()
                    p.stdout.close()

    return Handler


# ---------------------------------------------------------------------------------------------------------------
# -c : check everything before go-live
# ---------------------------------------------------------------------------------------------------------------
def _check():
    """Print what is wrong (or "all good") for the tools, both files and every remote/link entry, run from THIS host.
    Returns the exit code: 0 = no real problem, 1 = something must be fixed. Starts no server, writes no log."""
    problems = 0
    missing = [t for t in PROBES + ["ip"] if not shutil.which(t)]
    print("tools         " + (f"MISSING: {', '.join(missing)}" if missing else "OK"))
    problems += bool(missing)
    try:
        config = _load_config(CONFIG_PATH)
        print("config.json   OK")
        why = _log_path_problem(_log_path(config))
        print(f"log file      {'ERROR: ' + why if why else 'OK (' + str(_log_path(config)) + ')'}")
        problems += bool(why)
        if len(config["auth_token"]) < 16:  # a warning only: it does not fail the check
            print("config.json   WARN: auth_token is shorter than 16 characters, use a long random one")
    except ConfigError as e:
        print(f"config.json   ERROR: {e}")
        problems += 1
    try:
        issues = []
        defs = _load_defs(TARGET_DEF_PATH, issues)
    except ConfigError as e:
        print(f"target_defs   ERROR: {e}")
        return 1
    entries = _entries(defs)
    print(f"target_defs   OK ({len(entries)} entries)")

    # Bad and repeated names are only REPORTED, never fixed. Both are real problems: an entry is lost.
    # A "note" (a name that only needed trimming or lower-casing) is information, not a problem.
    for label, text in issues:
        print(f"{label:<13} {text}")
        problems += label != "note"
    same = {}
    for remote, link, src, dest in entries:
        same.setdefault((src, dest), []).append(f"{remote}/{link}")
    for (src, dest), names in same.items():  # only a warning: it may be on purpose
        if len(names) > 1:
            print(f"same src/dest {' and '.join(names)} have the same src -> dest ({src} -> {dest})")

    # Every entry: can this host route it, and does the destination answer one ping?
    for remote, link, src, dest in entries if "ip" not in missing else []:
        try:
            code, out = _route_check(src, dest)
            if code:
                status, problems = f"ROUTE FAIL ({out})", problems + 1  # a host problem: must be fixed
            else:
                ping = subprocess.run(
                    ["ping", "-c", "1", "-W", "2", "-I", src, dest], stdout=subprocess.DEVNULL, timeout=10
                ).returncode
                if ping == 0:
                    status = "OK"
                elif ping == 1:  # route is fine, the destination just did not answer: a warning only
                    status = "NO REPLY (route ok; destination down or not answering ICMP)"
                else:
                    status, problems = f"PING ERROR (exit {ping})", problems + 1
        except Exception as e:
            status, problems = f"ERROR {type(e).__name__}: {e}", problems + 1
        print(f"{remote + '/' + link:<13} {src} -> {dest}  {status}")
    print(f"{problems} problem(s)" if problems else "all good")
    return 1 if problems else 0


# ---------------------------------------------------------------------------------------------------------------
# Starting the server
# ---------------------------------------------------------------------------------------------------------------
def _watch_file():
    """-d only: poll this script and restart the whole process when it changes (handy while developing)."""
    script_path = os.path.abspath(__file__)
    last_mtime = os.path.getmtime(script_path)
    while True:
        time.sleep(0.3)
        try:
            if os.path.getmtime(script_path) != last_mtime:
                print(f"\n[!] Change detected in {os.path.basename(script_path)}. Restarting...\n")
                os.execv(sys.executable, [sys.executable] + sys.argv)  # replace this process with a fresh one
        except Exception:
            pass


def run(config, logger):
    if "-d" in sys.argv:
        threading.Thread(target=_watch_file, daemon=True).start()
    handler = probe_api_server_handler(
        TARGET_DEF_PATH, config["auth_token"], logger, config["timeouts"], config.get("max_probes", 8)
    )
    # ThreadingHTTPServer: one thread per request, so a slow probe never blocks other clients.
    server = ThreadingHTTPServer((config["listen_host"], config["listen_port"]), handler)
    sign = f"{APP_NAME} at {config['listen_host']}:{config['listen_port']}"
    try:
        logger.info(f"Starting {sign}")
        server.serve_forever()
    except KeyboardInterrupt:  # Ctrl-C
        logger.info(f"Stopping: {sign}")
    finally:
        server.server_close()
        logger.info(f"Stopped: {sign}")


if __name__ == "__main__":
    if "-c" in sys.argv:
        sys.exit(_check())
    try:
        config = _load_config(CONFIG_PATH)
    except ConfigError as e:
        # config.json is unusable, so we do not know the wanted log path: write this one error to the default log
        _setup_logger(APP_NAME, LOG_PATH).error(f"Config error {LOG_SEP} {e}")
        sys.exit(f"{APP_NAME} CONFIG ERROR: {e}. Fix the file and start again.")
    why = _log_path_problem(_log_path(config))
    if why:
        sys.exit(f'{APP_NAME} CONFIG ERROR: cannot use "log_file_path": {why}. Fix it and start again.')
    run(config, _setup_logger(APP_NAME, _log_path(config)))
