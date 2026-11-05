package main

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeServer pretends to be looking_glass_server: it wants the token "tok" and records the last request path.
func fakeServer(t *testing.T, status int, body string) (*httptest.Server, *string) {
	t.Helper()
	var last string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		last = r.URL.EscapedPath()
		if r.Header.Get("Authorization") != "Bearer tok" {
			http.Error(w, "Unauthorized", 401)
			return
		}
		w.WriteHeader(status)
		w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv, &last
}

// cfgIn writes a config file for srv in a temp dir and returns its path.
func cfgIn(t *testing.T, url string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "lg", "config.json")
	if err := saveConfig(path, config{URL: url, Token: "tok"}); err != nil {
		t.Fatal(err)
	}
	return path
}

// stdinOf returns a file holding text, to stand in for what the user types.
func stdinOf(t *testing.T, text string) *os.File {
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "stdin")
	if err != nil {
		t.Fatal(err)
	}
	f.WriteString(text)
	f.Seek(0, 0)
	return f
}

func do(t *testing.T, cfgPath, stdin string, args ...string) (int, string, string) {
	t.Helper()
	var out, errb bytes.Buffer
	code := run(args, stdinOf(t, stdin), &out, &errb, cfgPath)
	return code, out.String(), errb.String()
}

func TestProbeAndListMapOntoServerUrls(t *testing.T) {
	srv, last := fakeServer(t, 200, "output\n")
	cfg := cfgIn(t, srv.URL)
	for args, want := range map[string]string{
		"ping acme primary":         "/ping/acme/primary",
		"mtr acme backup-2":         "/mtr/acme/backup-2",
		"traceroute acme secondary": "/traceroute/acme/secondary",
		"list probes":               "/list/probes",
		"list remote":               "/list/remote",
		"list link":                 "/list/link",
		"list targets":              "/list/targets",
		"list targets acme":         "/list/targets/acme",
	} {
		code, out, _ := do(t, cfg, "", strings.Fields(args)...)
		if code != 0 || out != "output\n" || *last != want {
			t.Errorf("%q: code=%d out=%q path=%q, want path %q", args, code, out, *last, want)
		}
	}
}

func TestArgumentsAreEscaped(t *testing.T) {
	srv, last := fakeServer(t, 200, "x")
	do(t, cfgIn(t, srv.URL), "", "ping", "na me", "a/b?c")
	if *last != "/ping/na%20me/a%2Fb%3Fc" {
		t.Errorf("got %q", *last)
	}
}

func TestTrailingSlashInBaseUrlIsHarmless(t *testing.T) {
	srv, last := fakeServer(t, 200, "x")
	do(t, cfgIn(t, srv.URL+"/"), "", "list", "remote")
	if *last != "/list/remote" {
		t.Errorf("got %q", *last)
	}
}

func TestServerErrorGoesToStderrWithExit1(t *testing.T) {
	srv, _ := fakeServer(t, 400, "usage text\n")
	code, out, errb := do(t, cfgIn(t, srv.URL), "", "ping", "x", "y")
	if code != 1 || out != "" || errb != "usage text\n" {
		t.Errorf("code=%d out=%q err=%q", code, out, errb)
	}
}

// firstWrite lets the test see the moment the first bytes reach "the terminal".
type firstWrite struct {
	once sync.Once
	seen chan struct{}
	buf  bytes.Buffer
	mu   sync.Mutex
}

func (w *firstWrite) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.once.Do(func() { close(w.seen) })
	return w.buf.Write(p)
}

func TestOutputStreamsBeforeTheProbeEnds(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("line 1\n"))
		w.(http.Flusher).Flush()
		<-release // the "probe" is still running
		w.Write([]byte("line 2\n"))
	}))
	defer srv.Close()
	var once sync.Once
	releaseAll := func() { once.Do(func() { close(release) }) }
	defer releaseAll() // runs before srv.Close, so a failing test cannot hang on the waiting handler
	cfg := cfgIn(t, srv.URL)
	w := &firstWrite{seen: make(chan struct{})}
	done := make(chan int)
	go func() { done <- run([]string{"ping", "a", "b"}, stdinOf(t, ""), w, &bytes.Buffer{}, cfg) }()
	select {
	case <-w.seen: // line 1 arrived while the server was still busy
	case <-time.After(5 * time.Second):
		t.Fatal("no output while the probe was running: the reply is buffered")
	}
	releaseAll()
	if code := <-done; code != 0 {
		t.Errorf("exit %d", code)
	}
}

func TestConnectionLostMidStream(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "100") // promise more than we send, then hang up
		w.Write([]byte("partial"))
	}))
	defer srv.Close()
	code, out, errb := do(t, cfgIn(t, srv.URL), "", "ping", "a", "b")
	if code != 1 || out != "partial" || !strings.Contains(errb, "connection lost") {
		t.Errorf("code=%d out=%q err=%q", code, out, errb)
	}
}

func TestNoConfigSaysRunInit(t *testing.T) {
	code, _, errb := do(t, filepath.Join(t.TempDir(), "none.json"), "", "list", "remote")
	if code != 1 || !strings.Contains(errb, `run "looking-glass init" first`) {
		t.Errorf("code=%d err=%q", code, errb)
	}
}

func TestBrokenConfigSaysRunInitAgain(t *testing.T) {
	path := filepath.Join(t.TempDir(), "c.json")
	os.WriteFile(path, []byte("{oops"), 0o600)
	if code, _, errb := do(t, path, "", "list", "remote"); code != 1 || !strings.Contains(errb, "init") {
		t.Errorf("code=%d err=%q", code, errb)
	}
}

func TestUsage(t *testing.T) {
	cfg := filepath.Join(t.TempDir(), "c.json")
	for _, args := range [][]string{{}, {"ping"}, {"ping", "a"}, {"ping", "a", "b", "c"}, {"list"}, {"list", "remote", "x"}, {"nope"}, {"init", "x"}} {
		if code, _, errb := do(t, cfg, "", args...); code != 2 || !strings.Contains(errb, "Usage") {
			t.Errorf("%v: code=%d", args, code)
		}
	}
	for _, h := range []string{"help", "-h", "--help"} {
		if code, out, _ := do(t, cfg, "", h); code != 0 || !strings.Contains(out, "Usage") {
			t.Errorf("%s: code=%d", h, code)
		}
	}
}

func TestInitChecksThenSaves(t *testing.T) {
	srv, _ := fakeServer(t, 200, "probes")
	path := filepath.Join(t.TempDir(), "lg", "config.json")
	code, out, errb := do(t, path, srv.URL+"\ntok\n", "init")
	if code != 0 {
		t.Fatalf("code=%d out=%q err=%q", code, out, errb)
	}
	c, err := loadConfig(path)
	if err != nil || c.URL != srv.URL || c.Token != "tok" {
		t.Errorf("saved %+v, %v", c, err)
	}
	if runtime.GOOS != "windows" {
		for p, want := range map[string]os.FileMode{path: 0o600, filepath.Dir(path): 0o700} {
			if st, _ := os.Stat(p); st.Mode().Perm() != want {
				t.Errorf("%s is %v, want %v", p, st.Mode().Perm(), want)
			}
		}
	}
	if code, out, _ := do(t, path, "", "list", "remote"); code != 0 || out != "probes" {
		t.Errorf("after init: code=%d out=%q", code, out)
	}
}

func TestInitRefusesWhatTheCheckRejects(t *testing.T) {
	srv, _ := fakeServer(t, 200, "x")
	dead := httptest.NewServer(http.NotFoundHandler())
	deadURL := dead.URL
	dead.Close() // nothing listens there now
	for name, tc := range map[string]struct{ input, want string }{
		"wrong token":    {srv.URL + "\nwrong\n", "rejected API_TOKEN"},
		"wrong URL":      {deadURL + "\ntok\n", "cannot reach"},
		"missing scheme": {"192.168.1.1:8080\ntok\n", "http:// or https://"},
		"empty":          {"\n\n", "http:// or https://"},
	} {
		path := filepath.Join(t.TempDir(), "c.json")
		code, _, errb := do(t, path, tc.input, "init")
		if code != 1 || !strings.Contains(errb, tc.want) || !strings.Contains(errb, "nothing saved") {
			t.Errorf("%s: code=%d err=%q", name, code, errb)
		}
		if _, err := os.Stat(path); err == nil {
			t.Errorf("%s: a config was saved", name)
		}
	}
}

func TestInitAsksBeforeOverwriting(t *testing.T) {
	srv, _ := fakeServer(t, 200, "x")
	path := cfgIn(t, "http://old.example")
	if code, out, _ := do(t, path, "n\n", "init"); code != 0 || !strings.Contains(out, "Nothing changed") {
		t.Errorf("answer n: code=%d out=%q", code, out)
	}
	if c, _ := loadConfig(path); c.URL != "http://old.example" {
		t.Errorf("config was changed after answering n: %+v", c)
	}
	if code, _, errb := do(t, path, "y\n"+srv.URL+"\ntok\n", "init"); code != 0 {
		t.Fatalf("answer y: code=%d err=%q", code, errb)
	}
	if c, _ := loadConfig(path); c.URL != srv.URL {
		t.Errorf("config was not overwritten: %+v", c)
	}
}

func TestConfigPathFailsClearlyWithoutAHome(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows finds %AppData% differently")
	}
	t.Setenv("HOME", "")
	t.Setenv("XDG_CONFIG_HOME", "")
	if _, err := configPath(); err == nil || !strings.Contains(err.Error(), "no home directory") {
		t.Errorf("err=%v", err)
	}
}

func TestConfigPathEndsInLookingGlass(t *testing.T) {
	p, err := configPath()
	if err != nil || !strings.HasSuffix(filepath.ToSlash(p), "looking-glass/config.json") {
		t.Errorf("%q %v", p, err)
	}
}
