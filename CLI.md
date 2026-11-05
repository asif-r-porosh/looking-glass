# looking-glass CLI

A small command line client for the looking_glass server. One program, nothing to install: Windows, macOS and Linux.

## Install

1. Download the archive for your system from the [latest release](https://github.com/asif-r-porosh/looking-glass/releases/latest). These links always give the newest version:
   - Windows: [`looking-glass-windows-amd64.tar.gz`](https://github.com/asif-r-porosh/looking-glass/releases/latest/download/looking-glass-windows-amd64.tar.gz) (most PCs) or [`looking-glass-windows-arm64.tar.gz`](https://github.com/asif-r-porosh/looking-glass/releases/latest/download/looking-glass-windows-arm64.tar.gz)
   - macOS: [`looking-glass-darwin-arm64.tar.gz`](https://github.com/asif-r-porosh/looking-glass/releases/latest/download/looking-glass-darwin-arm64.tar.gz) (Apple chip) or [`looking-glass-darwin-amd64.tar.gz`](https://github.com/asif-r-porosh/looking-glass/releases/latest/download/looking-glass-darwin-amd64.tar.gz) (Intel)
   - Linux: [`looking-glass-linux-amd64.tar.gz`](https://github.com/asif-r-porosh/looking-glass/releases/latest/download/looking-glass-linux-amd64.tar.gz) or [`looking-glass-linux-arm64.tar.gz`](https://github.com/asif-r-porosh/looking-glass/releases/latest/download/looking-glass-linux-arm64.tar.gz)
2. Extract it. Windows 10 and later, macOS and Linux all have `tar`:
   ```
   tar -xzf looking-glass-windows-amd64.tar.gz
   ```
   You get a folder with the same name as the archive. It holds `looking-glass` (`looking-glass.exe` on Windows) and `looking-glass.sha256`.
3. Optional check, inside that folder: the SHA-256 of the program must match the line in `looking-glass.sha256`.
   - Linux: `sha256sum -c looking-glass.sha256`
   - macOS: `shasum -a 256 -c looking-glass.sha256`
   - Windows: `certutil -hashfile looking-glass.exe SHA256`, then compare the result with the file by eye.
   (The checksum file travels in the same archive, so it catches a damaged download, not a replaced one.)
4. Put the program in a folder you keep, and run it from there, or add that folder to your PATH.
   - **Linux and macOS:** if you get "Permission denied", the program is not marked as executable (this can happen when it was copied or unpacked by another tool). Run `chmod +x looking-glass`.
   - **macOS:** if macOS blocks it as coming from the internet: `xattr -d com.apple.quarantine looking-glass`.
   - **Windows:** if SmartScreen or antivirus warns about an unknown program, choose "More info" then "Run anyway", or ask your admin to allow it. The program is not code-signed.

Check the install with `looking-glass --version`.

## First run

```
looking-glass init
```

It asks for the server URL (for example `http://192.0.2.10:8080`) and the token (typed hidden), checks them against the server, and saves them only if they work. Run it again any time to change them; it asks before overwriting.

Saved in `looking-glass/config.json` inside your user config folder: `~/.config` on Linux, `~/Library/Application Support` on macOS, `%AppData%` on Windows. Folder and file are private to your user (mode 0700 / 0600; Windows uses your per-user folder). The token is stored as plain text there. Delete the file to forget it.

## Use

```
looking-glass list probes
looking-glass list remote
looking-glass list link
looking-glass list targets            # all remote, link, src, dest
looking-glass list targets acme       # only one remote (or one link name)
looking-glass ping acme primary
looking-glass mtr acme primary
looking-glass traceroute acme secondary
looking-glass --version               # also: -V, version
```

The probe output appears as it arrives, and may take up to a minute or two. Names are not case sensitive. If the output contains NOT A TEST RESULT, it is not a result for the network under test: contact admin.

Exit code: 0 ok, 1 the server or network failed (the message is on stderr), 2 wrong usage.

A build that did not come from a release prints `RUNNING A DEVELOPMENT VERSION OF CLIENT looking-glass` on stderr (stdout stays clean), and `--version` says `development`.

## Build and release (developers)

Needs Go (the version in `cli/go.mod` or newer; Go downloads the right one by itself) and `tar`. From `cli/`:

```
go test ./...
./build.sh            # 6 targets: build/looking-glass-<os>-<arch>/looking-glass[.exe] + looking-glass.sha256
./build.sh clean      # remove build/
```

A plain `./build.sh` is a development build. A release is made by `release.sh`, from a clean, pushed `main`, with `gh` installed and logged in (`gh auth login`) and `origin` pointing to GitHub:

```
./release.sh          # patch: v0.0.1 -> v0.0.2   (or: ./release.sh minor | major)
```

It runs the tests, works out the next version from the latest tag, builds with that version, packs each folder as `looking-glass-<os>-<arch>.tar.gz`, pushes `main`, then creates the GitHub Release (and its tag) with the six archives. Binaries are not committed (`build/` is git-ignored).
