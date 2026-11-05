# Developer notes

For whoever comes back to this project after a while. Users read [README.md](README.md), admins read [INSTALLATION.md](INSTALLATION.md), CLI users read [CLI.md](CLI.md). This file is the way in for the person who changes the code.

## Layout

| Path | What it is |
|---|---|
| `looking_glass_server.py` | The whole server. Python 3.8+, standard library only. |
| `config.json`, `target_defs.json` | **Samples.** `config.json` has an empty `listen_host`, port 0 and an empty `auth_token`, so the server refuses to start until they are set. `target_defs.json` uses neutral names and documentation IPs. Never commit real values; keep them elsewhere. |
| `test_looking_glass.py` | 81 server tests (`unittest`, standard library only). |
| `pyproject.toml` | Only the formatter setting: `black`, 120 columns. |
| `cli/main.go` | The whole CLI. Standard library plus `golang.org/x/term` (hidden token prompt). |
| `cli/*_test.go` | CLI tests: `main_test.go`, `version_test.go` (version flags, banner, linker flag), `token_test.go` (token prompt, no overall timeout). |
| `cli/build.sh`, `cli/release.sh` | Build the six binaries; make a release. |

## Python (server)

Python 3.8 is the minimum and the tests run on it, so keep a 3.8 venv at `.venv/` (`python3.8 -m venv .venv`). A newer system `python3` runs the server too.

```
.venv/bin/python -I test_looking_glass.py        # all server tests (about 30 seconds)
.venv/bin/python -I test_looking_glass.py -v     # verbose; add a test name to run one
LG_PATH=/path/to/other_copy.py .venv/bin/python -I test_looking_glass.py   # test another copy of the server
black -l 120 looking_glass_server.py test_looking_glass.py                 # formatting (pyproject.toml)

python3 looking_glass_server.py -c               # self check; does not start the server. Exit 0 = ok, 1 = fix something
python3 looking_glass_server.py                  # run it (reads config.json and target_defs.json next to the script)
python3 looking_glass_server.py -d               # dev mode, see below
```

- **`-d` (dev mode):** a thread polls the modification time of `looking_glass_server.py` every 0.3 seconds. When the file changes, the process restarts itself with `os.execv` and the same arguments. It watches only that one file. `config.json` needs a manual restart; `target_defs.json` is re-read on every request anyway.
- `-c` and `-d` are the only flags. There is **no `-h`**: any other argument is ignored and the server starts (and fails with "Address already in use" if one is already running).
- The tests start the real request handler on a loopback port with stub tools. `TestRealTools` is the exception: it runs the real `ping`, `mtr` and `traceroute` from 127.0.0.1 to 127.0.0.1, so those need to be installed and allowed (ICMP sockets) on the machine, and they fail loudly on a Mac or on a host that is not ready. That is deliberate.
- The tests are run with `-I` (isolated mode: no script folder or environment variables on the import path), so they cannot pick up a stray module by accident.

## Go (CLI)

Run these from `cli/`. `go.mod` says `go 1.26.10`; a newer Go than that is fine, and an older Go downloads 1.26.10 by itself the first time (needs network once).

```
go test ./...                    # all CLI tests
go test -short ./...             # skip the one test that builds the program
go test -run TestName -v ./...   # one test
go vet ./...
gofmt -l .                       # lists badly formatted files; gofmt -w . fixes them
go mod tidy                      # after changing imports or modules

go install golang.org/x/vuln/cmd/govulncheck@latest    # once
govulncheck ./...                # known vulnerabilities in what the code really uses. Needs ./... (bare "govulncheck" scans nothing)
GOOS=windows GOARCH=amd64 govulncheck ./...            # the same for another target

go get golang.org/x/sys@latest golang.org/x/term@latest && go mod tidy   # update the two modules
```

Try the CLI against a local server: run the server on 127.0.0.1 with sample files, then `go run . init` (URL `http://127.0.0.1:8080`, token from `config.json`) and `go run . list targets`.

## Build

```
cd cli
./build.sh          # six targets, a development build
./build.sh clean    # remove build/
```

`build/looking-glass-<os>-<arch>/` is made for darwin and linux (amd64, arm64) and windows (amd64, arm64). Each folder holds `looking-glass` (`looking-glass.exe` on Windows) and its own `looking-glass.sha256`. Binaries are static (`CGO_ENABLED=0`), stripped, built with `-trimpath`.

The version is one variable, `main.version`, set at build time: `-ldflags "-X main.version=$VERSION"`. `build.sh` takes `$VERSION` from the environment and defaults to `development`. A development build prints `RUNNING A DEVELOPMENT VERSION OF CLIENT looking-glass` on **stderr** (stdout stays clean) and `looking-glass --version` says `development`. Only `release.sh` sets a real version.

## Release

### One time: create the repository

1. Create an empty repository on GitHub. It must be public if users download from the Releases page.
2. On the build machine (Linux, with `git`, `gh`, `go`, `tar`):

```
gh auth login
git init -b main
git status                       # look first: nothing private, no leftovers
git add -A && git commit -m "chore: initial import."
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

`.gitignore` keeps out `.venv/`, `build/`, `*.log` and the usual Python and editor files. Check `git status` anyway before the first commit.

### Every release

1. Commit everything. `release.sh` refuses to run with uncommitted changes.
2. From `cli/`: `./release.sh minor` (or `patch`, or `major`). The next version comes from the latest git tag, so the first run with no tags gives `v0.1.0` for `minor` and `v0.0.1` for `patch`.

What it does, in order, and stops at the first failure:

1. Checks that `gh` exists and the working tree is clean.
2. `git fetch origin`; refuses if the local branch is behind `origin`.
3. `go test ./...`.
4. Works out the next version from the latest tag (`git fetch --tags`, `git describe`).
5. `./build.sh clean`, then `./build.sh build` with `VERSION` set.
6. Packs every `build/looking-glass-<os>-<arch>/` folder into `<same name>.tar.gz` (Windows too), owner 0:0.
7. `git push origin <branch>`. It does **not** push a tag.
8. `gh release create <version> build/*.tar.gz --target <this commit> --generate-notes`: GitHub creates the tag on that exact commit together with the release and the six archives. If this step fails, no tag is public.
9. `git fetch --tags` to bring the new tag home.

It uses GNU tar flags (`--owner=0 --group=0`), so run it on Linux. The stock `tar` on macOS may not accept them.

Check the result:

```
gh release view v0.1.0
# download one archive, then:
tar -xzf looking-glass-linux-amd64.tar.gz && cd looking-glass-linux-amd64
./looking-glass --version        # must print v0.1.0
sha256sum -c looking-glass.sha256
```

Undo a bad release (the version number is then free again):

```
gh release delete v0.1.0 --cleanup-tag --yes
git tag -d v0.1.0
```

There is no code signing. Windows users may see SmartScreen or antivirus warnings; [CLI.md](CLI.md) says what to do. The checksum file is inside the archive, so it catches damage, not tampering.

## Design notes (the decisions behind the code)

- **Raw output only.** No verdicts, no interpretation. The commands are fixed and a client cannot add arguments: `ping -c 3 -I <src> <dest>`, `mtr -n -r -c 3 -a <src> <dest>`, `traceroute -I -n -q 3 -s <src> <dest>`.
- **Names.** `remote` and `link` match `^[a-z][a-z0-9._-]{0,15}$`, trimmed and case-insensitive. A bad name is ignored with everything under it, and `-c` reports it.
- **Addresses.** `src` and `dest` are IPv4 only. No hostnames, so the result never depends on DNS. IPv6 is out only because it was never verified.
- **Route gate.** Before every probe the server runs `ip route get <dest> from <src>`. If the host cannot route, the answer says so and contains NOT A TEST RESULT.
- **Timeouts and limits.** Each probe has its own timeout from `config.json`; on expiry the whole process group is killed (so `mtr-packet` dies too). At most `max_probes` run at once; the next gets HTTP 429.
- **Reload.** `target_defs.json` is read on every request. `config.json` is read at start-up.
- **Status codes.** 401 wrong token, 400 unknown probe, remote or link (with the usage text), 429 busy, 500 a problem with the probe host or the files.
- **No TLS in the app.** The admin chooses http inside a private network or https through a proxy.
- **CLI.** A thin client: every command except `init` maps straight onto a server URL, and the server's own errors (usage, busy) go to stderr. Exit codes: 0 ok, 1 the server or network failed, 2 wrong usage. There is no overall timeout; the server ends every probe, and Ctrl-C stops the client. Connecting is limited to 10 seconds. The config is saved in the user's config folder (folder 0700, file 0600); `init` checks the URL and token against the server before saving anything.

## Gotchas

- **No `-h`** on the server (see Python above).
- **`-d` watches one file** only.
- **Samples in the repo.** `config.json` does not start (empty `listen_host`, `listen_port` 0, empty `auth_token`) and `target_defs.json` has documentation addresses. Keep live copies outside the repo and copy them over to run for real. A test fails if the sample `config.json` ever becomes startable.
- **`app.log` is never rotated.** It is git-ignored; the admin rotates it.
- **Several unrelated remotes failing at once** means the probe host, not the remotes.
- **`govulncheck`** with no arguments scans nothing and says "No vulnerabilities found". Use `./...`.

## Not done / left on purpose

- **Route check code.** `_route_check` runs `ip route get` with its own `subprocess.run` and does not share `_run_cmd` with the probes. Merging them was considered and rejected: the two have different failure meanings. The comments in the server are also left as written.
- **Maybe later:** a DNS check, an API check, and checks on systems downstream of the probe host. None exist; the scope is raw probe output only.
- **IPv6.** Out only because it was never verified. The fixed commands were checked for IPv4 only.
- **Code signing.** No certificate. Windows users may see SmartScreen or antivirus warnings; the admin allows the file by hash, or the user runs `Unblock-File`. Buy an OV certificate only if the CLI leaves a controlled group of users. Azure Artifact Signing is not available in every country.
- **One shared token, no TLS in the app, no log rotation.** By design: the admin puts a proxy or VPN in front, and rotates `app.log`.
