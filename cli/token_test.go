package main

// Tests for the hidden token prompt in "init", and for the client having no overall timeout.

import (
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeTerminal makes "init" believe stdin is a terminal and answers the hidden prompt with token/err.
func fakeTerminal(t *testing.T, token string, err error) {
	t.Helper()
	oldIs, oldRead := isTerminal, readPassword
	isTerminal = func(int) bool { return true }
	readPassword = func(int) ([]byte, error) { return []byte(token), err }
	t.Cleanup(func() { isTerminal, readPassword = oldIs, oldRead })
}

func TestInitStopsWhenTheTokenCannotBeRead(t *testing.T) {
	srv, last := fakeServer(t, 200, "probes")
	fakeTerminal(t, "", errors.New("input/output error"))
	path := filepath.Join(t.TempDir(), "c.json")
	code, _, errb := do(t, path, srv.URL+"\n", "init")
	if code != 1 || !strings.Contains(errb, "cannot read the token: input/output error") ||
		!strings.Contains(errb, "nothing saved") {
		t.Errorf("code=%d err=%q", code, errb)
	}
	if strings.Contains(errb, "rejected") {
		t.Errorf("the message blames the server: %q", errb)
	}
	if *last != "" {
		t.Errorf("the server was contacted (%q) with no token", *last)
	}
	if _, err := os.Stat(path); err == nil {
		t.Error("a config was saved")
	}
}

func TestInitUsesTheTokenTypedAtATerminal(t *testing.T) {
	srv, _ := fakeServer(t, 200, "probes")
	fakeTerminal(t, "tok", nil) // the fake server wants exactly "tok"
	path := filepath.Join(t.TempDir(), "c.json")
	code, out, errb := do(t, path, srv.URL+"\n", "init")
	if code != 0 || errb != "" {
		t.Fatalf("code=%d out=%q err=%q", code, out, errb)
	}
	if !strings.Contains(out, "API_TOKEN (not shown): \n") {
		t.Errorf("the prompt is not followed by a newline: %q", out)
	}
	if c, err := loadConfig(path); err != nil || c.Token != "tok" {
		t.Errorf("saved %+v, %v", c, err)
	}
}

func TestInitRefusesAnEmptyTokenWithoutAskingTheServer(t *testing.T) {
	srv, last := fakeServer(t, 200, "x")
	for name, in := range map[string]string{"blank line": srv.URL + "\n\n", "no line": srv.URL + "\n"} {
		path := filepath.Join(t.TempDir(), "c.json")
		code, _, errb := do(t, path, in, "init")
		if code != 1 || !strings.Contains(errb, "API_TOKEN is empty") || !strings.Contains(errb, "nothing saved") {
			t.Errorf("%s: code=%d err=%q", name, code, errb)
		}
		if *last != "" {
			t.Errorf("%s: the server was contacted (%q)", name, *last)
		}
		if _, err := os.Stat(path); err == nil {
			t.Errorf("%s: a config was saved", name)
		}
	}
}

// A probe may take as long as the server allows (an admin can raise that), so the client must not cut it off.
// The server ends every probe at its own timeout; connecting is the only thing the client limits.
func TestClientHasNoOverallTimeout(t *testing.T) {
	if client.Timeout != 0 {
		t.Errorf("client.Timeout is %v: a long probe would be cut off", client.Timeout)
	}
	if _, ok := client.Transport.(*http.Transport); !ok || client.Transport.(*http.Transport).DialContext == nil {
		t.Error("the connect limit is missing")
	}
}
