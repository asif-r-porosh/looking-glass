# looking_glass

A connectivity troubleshooting tool for support users.

It lets user run `ping`, `mtr` and `traceroute` from a fixed source IP (`src`) to a fixed destination IP (`dest`) over HTTP and see the raw terminal output, without logging in to any production VM.

Python 3.8+, standard library only. One file: `looking_glass_server.py`.

## What it does

- Each remote endpoint (`remote`) has one or more connections (`link`), for example `primary`, `secondary`, `backup-1`. Any number of links, any names.
- A client calls `GET /<probe>/<remote>/<link>`. The server looks up `src` and `dest` in `target_defs.json`, runs the fixed command, and streams the output back line by line.
- Fixed commands, client can't add arguments: `ping -c 3 -I <src> <dest>`, `mtr -n -r -c 3 -a <src> <dest>`, `traceroute -I -n -q 3 -s <src> <dest>`.
- `list probes | remote | link | targets [remote or link]` print tables so a user can find what to ask for.

## Architecture

```
User -> CLI (Go) --http--> [nginx, optional] --> looking_glass_server.py --> ping / mtr / traceroute --> remote link
                                                    |-- config.json        (listen, token, timeouts, max_probes)
                                                    |-- target_defs.json   (remote -> link -> src, dest; re-read per request)
                                                    `-- app.log            (one JSON line per event)
```

One thread per request. A route gate (`ip route get <dest> from <src>`) runs before every probe. A timer kills a probe that runs too long (the whole process group, so `mtr-packet` dies too). A limit of `max_probes` concurrent probes returns HTTP 429.

## Caveats

- The probe host must **own every `src` IP** and be able to route to `dest`. The server cannot create source IPs. If the host cannot route, the answer says so.
- A message containing **NOT A TEST RESULT** means our host or tool has a problem, not the network under test. Do not report it as a result. Contact admin.
- A ping with 100% loss is a real result, not an error.
- If several unrelated `remote` names fail at once, suspect the probe host first.
- Auth is one shared bearer token.
- Client IP in the log comes from `X-Real-IP` / `X-Forwarded-For` only when the connection is from a private address (the proxy).
- `app.log` grows without limit; rotate it.

## More

- [INSTALLATION.md](INSTALLATION.md): server setup, config files, `-c` check.
- [CLI.md](CLI.md): install and use the command line client. Download it from the [latest release](https://github.com/asif-r-porosh/looking-glass/releases/latest).
- [DEV.md](DEV.md): for developers: commands, build and release, design notes.
- [LICENSE](LICENSE): MIT.
