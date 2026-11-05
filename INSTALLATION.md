# Installing looking_glass_server

## Prerequisites

1. Linux host (recommended Ubuntu 20+).
2. The host must **own every `src` IP** and route from it to the matching `dest`, for every target that is to be probed.
3. Python 3.8+.
4. `ping`, `mtr`, `traceroute`, `ip` (iproute2) installed.
5. The user running `looking_glass_server` can run `ping`, `mtr`, `traceroute` and `ip`.
6. `traceroute -I` needs ICMP sockets for that user (`sysctl net.ipv4.ping_group_range`) or the raw-socket capability.
7. Use a separate user for `looking_glass_server`.

## Setup

1. Copy `looking_glass_server.py`, `config.json` and `target_defs.json` to an accessible directory, for example `~/looking_glass_server/`.
2. Set up `config.json` with values as per the deployment plan (it needs a restart after a change). The sample in the repository leaves `listen_host`, `listen_port` and `auth_token` empty on purpose: the server refuses to start until you set all three. A filled-in example:

```json
{
  "listen_host": "0.0.0.0",
  "listen_port": 8080,
  "auth_token": "a_long_random_key",
  "timeouts": { "ping": 30, "mtr": 60, "traceroute": 90 },
  "max_probes": 8,
  "log_file_path": ""
}
```

- `listen_host` and `listen_port`: where `looking_glass_server` listens for probe requests. The port is 1-65535.
- `auth_token`: no spaces. `-c` warns when it is shorter than 16 characters.
- `timeouts`: seconds per probe. A longer probe is killed and the client is told the output is incomplete.
- `max_probes`: optional (default 8). Probes running at once. When all are busy the next probe request is answered at once with `429` (see Endpoints). `list` requests are never blocked.
- `log_file_path`: optional. Keep it an empty string `""` (or leave it out) to use the default path, `app.log` in the same directory as `looking_glass_server.py`. A relative path is read from that directory. The folder must exist and be writable. `-c` checks it.

3. Set up `target_defs.json` with some values to do the initial tests (read on every request, so edits are live, no restart). Format:

```json
{
  "unique_remote_name": {
    "unique_link_name": { "src": "source_ip", "dest": "destination_ip" }
  }
}
```

Example:

```json
{
  "acme": {
    "primary":   { "src": "192.0.2.10", "dest": "198.51.100.10" },
    "secondary": { "src": "192.0.2.20", "dest": "198.51.100.20" }
  },
  "globex": {
    "primary":   { "src": "192.0.2.10", "dest": "198.51.100.10" },
    "secondary": { "src": "192.0.2.20", "dest": "198.51.100.20" }
  },
  "initech": {
    "backup-1":  { "src": "192.0.2.10", "dest": "198.51.100.10" },
    "backup-2":  { "src": "192.0.2.10", "dest": "198.51.100.11" },
    "secondary": { "src": "192.0.2.20", "dest": "198.51.100.20" }
  }
}
```

- `src`, `dest`: IPv4 addresses `x.y.p.q` (no hostnames, URLs or IPv6).
- `unique_remote_name` (the remote endpoint, a `remote`): `^[a-z][a-z0-9._-]{0,15}$`. Case insensitive; letters, digits, `-`, `.` and `_`; must start with a letter; 16 characters at most; unique.
- `unique_link_name` (a connection to that remote endpoint, a `link`): same rule as above; unique inside its `unique_remote_name`.

**Note:**

- A name that breaks the rule is **ignored**, with everything under it: it is not listed and cannot be probed.
- A `remote` left with no valid link disappears too.
- An entry with a bad address is **ignored** like a bad name.
- Keep each name once per level: JSON silently keeps only the last of two equal keys, and names that differ only by case clash the same way.

4. Run `python3 looking_glass_server.py -c`. It checks the tools, `config.json`, the log file path, `target_defs.json`, and every entry with `ip route get` plus one ping. It does not start the server. Exit code 0 = no real problem, 1 = fix something. How to read its output is in the next section.
5. `-c` only pings, so run one `traceroute` and one `mtr` by hand as the service user once:

```
traceroute -I -n -q 3 -s <src> <dest>
mtr -n -r -c 3 -a <src> <dest>
```

6. Run `python3 looking_glass_server.py` as a persistent service.
7. Forward traffic to `listen_host:listen_port`, for example `0.0.0.0:8080`.

## Reading the `-c` output

| Output | Meaning |
|---|---|
| `ROUTE FAIL` | host problem, must be fixed |
| `NO REPLY` | route fine, destination did not answer: warning |
| `log file  ERROR` | the log path cannot be written: must be fixed |
| `bad ip  acme/primary ...` | `src` or `dest` is not an IPv4 address, entry ignored: must be fixed |
| `bad name  acme/na me ...` | name breaks the rule, entry ignored: must be fixed |
| `duplicate  acme/primary ...` | a name appears twice in the same object (case ignored): only the last is used. Reported, never auto-fixed |
| `note  " ACME " is read as acme` | only trimmed/lower-cased: information, not a problem |
| `same src/dest  acme/primary and acme/secondary ...` | two names, same `src -> dest`: warning, may be on purpose |

## Notes for the admin

1. **Source addresses:** the host must own every `src` IP and have a route from it to the matching `dest`. A probe whose route check fails is refused. `-c` checks each entry with `ip route get` plus one ping.
2. **traceroute as the service user:** `traceroute -I` needs ICMP sockets for that user (see Prerequisites). `traceroute --version` should say "Modern traceroute for Linux, 2.1.x". `-c` only pings, so run `traceroute` and `mtr` by hand once (Setup, step 5).
3. **Anything between the users and the server** (a proxy, a VPN gateway) must let a reply last as long as the longest probe timeout (traceroute: 90 seconds by default, see `timeouts`) and must not buffer it. With buffering, users see nothing until the probe ends. Set the proxy's read timeout above the longest probe timeout and turn its response buffering off.
4. **NOT A TEST RESULT:** a reply containing this text means the probe host or a tool failed (for example no route, or a missing tool). It says nothing about the network under test. Users are told to contact you, so fix the host first and never pass that reply on as a network result.
5. **Several remotes failing at the same moment** (unrelated ones) points at the probe host, not at the remotes. Check the host before telling anyone that a link is down.

## Endpoints and authorization

Every request needs `Authorization: Bearer <auth_token>` (the `auth_token` set in `config.json`), else `401`.
Anything the server does not know (a wrong remote, link or probe name) gets `400` with the usage text.
When all `max_probes` slots are busy, a probe request gets `429` at once with "busy, N probes already running. Retry in a few seconds."

In the URLs below, `<unique_remote_name>` is the `remote` and `<unique_link_name>` is the `link`.

```
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/list/probes
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/list/remote
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/list/link
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/list/targets
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/list/targets/<unique_remote_name>
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/list/targets/<unique_link_name>
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/ping/<unique_remote_name>/<unique_link_name>
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/mtr/<unique_remote_name>/<unique_link_name>
curl -N -H "Authorization: Bearer $TOKEN" http://HOST:PORT/traceroute/<unique_remote_name>/<unique_link_name>
```
