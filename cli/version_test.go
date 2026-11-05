package main

// Tests for the version string, the --version flags and the development banner.

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// The other tests run as a release build, so the development banner does not mix into their output.
func TestMain(m *testing.M) {
	version = "v0.0.0-test"
	os.Exit(m.Run())
}

func withVersion(t *testing.T, v string) {
	t.Helper()
	old := version
	version = v
	t.Cleanup(func() { version = old })
}

func TestVersionFlagsPrintTheVersionWithANewline(t *testing.T) {
	withVersion(t, "v1.2.3")
	for _, arg := range []string{"version", "-V", "--version"} {
		// the config path does not exist: --version must not need a config or a server
		code, out, errb := do(t, filepath.Join(t.TempDir(), "none.json"), "", arg)
		if code != 0 || out != "v1.2.3\n" || errb != "" {
			t.Errorf("%s: code=%d out=%q err=%q", arg, code, out, errb)
		}
	}
}

func TestVersionWithExtraWordsIsUsageError(t *testing.T) {
	withVersion(t, "v1.2.3")
	for _, args := range [][]string{{"version", "x"}, {"--version", "x"}, {"-V", "-V"}} {
		if code, _, errb := do(t, "unused", "", args...); code != 2 || !strings.Contains(errb, "Usage") {
			t.Errorf("%v: code=%d err=%q", args, code, errb)
		}
	}
}

func TestReleaseBuildPrintsNoBanner(t *testing.T) {
	withVersion(t, "v1.2.3")
	srv, _ := fakeServer(t, 200, "output\n")
	code, out, errb := do(t, cfgIn(t, srv.URL), "", "ping", "acme", "primary")
	if code != 0 || out != "output\n" || errb != "" {
		t.Errorf("code=%d out=%q err=%q", code, out, errb)
	}
	if _, out, errb := do(t, "unused", "", "help"); out != help || errb != "" {
		t.Errorf("help output changed: out=%q err=%q", out, errb)
	}
}

const banner = "RUNNING A DEVELOPMENT VERSION OF CLIENT looking-glass\n"

// The banner goes to stderr, so a redirected or piped probe output (stdout) is never mixed with it.
func TestDevelopmentBuildPutsTheBannerOnStderrOnly(t *testing.T) {
	withVersion(t, "development")
	srv, last := fakeServer(t, 200, "output\n")
	cfg := cfgIn(t, srv.URL)

	code, out, errb := do(t, cfg, "", "ping", "acme", "primary")
	if code != 0 || out != "output\n" || errb != banner || *last != "/ping/acme/primary" {
		t.Errorf("ping: code=%d out=%q err=%q path=%q", code, out, errb, *last)
	}

	// the banner changes nothing else: same exit codes, same outputs
	code, out, errb = do(t, cfg, "", "nope")
	if code != 2 || out != "" || errb != banner+help {
		t.Errorf("usage error: code=%d out=%q err=%q", code, out, errb)
	}
	code, out, errb = do(t, cfg, "", "--version")
	if code != 0 || out != "development\n" || errb != banner {
		t.Errorf("--version: code=%d out=%q err=%q", code, out, errb)
	}
	code, out, errb = do(t, cfg, "", "help")
	if code != 0 || out != help || errb != banner {
		t.Errorf("help: code=%d out=%q err=%q", code, out, errb)
	}
	// a server error: the server's text and the banner both on stderr, exit 1
	bad, _ := fakeServer(t, 400, "usage text\n")
	code, out, errb = do(t, cfgIn(t, bad.URL), "", "ping", "x", "y")
	if code != 1 || out != "" || errb != banner+"usage text\n" {
		t.Errorf("server error: code=%d out=%q err=%q", code, out, errb)
	}
}

// The build.sh flag is -X main.version=...: a typo in it is ignored silently by the linker, so build for real.
func TestLinkerFlagSetsTheVersion(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the program")
	}
	bin := filepath.Join(t.TempDir(), "lg")
	build := exec.Command("go", "build", "-o", bin, "-ldflags", "-X main.version=v9.9.9", ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, out)
	}
	out, err := exec.Command(bin, "--version").Output()
	if err != nil || string(out) != "v9.9.9\n" {
		t.Errorf("built binary says %q, %v", out, err)
	}
	// and a build without the flag is a development build
	bin2 := filepath.Join(t.TempDir(), "lg")
	if o, err := exec.Command("go", "build", "-o", bin2, ".").CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, o)
	}
	var so, se strings.Builder
	cmd := exec.Command(bin2, "--version")
	cmd.Stdout, cmd.Stderr = &so, &se
	if err := cmd.Run(); err != nil || so.String() != "development\n" || se.String() != banner {
		t.Errorf("plain build: stdout=%q stderr=%q err=%v", so.String(), se.String(), err)
	}
}
