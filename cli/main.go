// looking-glass: thin command line client for looking_glass_server. See CLI.md.
//
//	looking-glass init                       save the server URL and token (checked first)
//	looking-glass list probes|remote|link    what can be asked
//	looking-glass list targets [remote|link] remote/link/src/dest table, optionally filtered
//	looking-glass ping|mtr|traceroute remote link
//
// Every command except init maps straight onto the server URL: "ping acme primary" asks GET /ping/acme/primary.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/term"
)

var version = "development"

const help = `Usage: looking-glass <command>

  init                                 set the server URL and token (asks, checks, then saves)
  list probes | remote | link          what you can ask for
  list targets [remote | link]         remote, link, src and dest; add a remote or a link name to filter
  ping|mtr|traceroute <remote> <link>  run a probe; the output appears as it arrives

`

// config is what "init" saves. The JSON keys are the names you type in "init".
type config struct {
	URL   string `json:"API_BASE_URL"`
	Token string `json:"API_TOKEN"`
}

// No overall timeout: the server ends every probe at its own timeout (an admin can raise it), and Ctrl-C stops
// the client. Only connecting is limited, so a wrong or unreachable URL fails fast.
var client = &http.Client{Transport: dialLimited()}

func dialLimited() *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone() // keeps the proxy settings from the environment
	t.DialContext = (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 15 * time.Second}).DialContext
	return t
}

// Seams for the tests: there is no real terminal there.
var (
	isTerminal   = term.IsTerminal
	readPassword = term.ReadPassword
)

// configPath is <user config dir>/looking-glass/config.json: ~/.config on Linux,
// ~/Library/Application Support on macOS, %AppData% on Windows.
func configPath() (string, error) {
	dir, err := os.UserConfigDir()
	if err != nil { // $HOME or %AppData% is not set: use the usual default instead
		home, herr := os.UserHomeDir()
		if herr != nil {
			return "", errors.New("cannot find a place for the config file: no home directory")
		}
		dir = filepath.Join(home, ".config")
	}
	return filepath.Join(dir, "looking-glass", "config.json"), nil
}

func loadConfig(path string) (config, error) {
	var c config
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return c, errors.New(`no config yet: run "looking-glass init" first`)
	}
	if err == nil {
		err = json.Unmarshal(data, &c)
	}
	if err != nil || c.URL == "" || c.Token == "" {
		return c, fmt.Errorf(`config %s is unreadable or incomplete: run "looking-glass init" again`, path)
	}
	return c, nil
}

// saveConfig keeps the token private: folder 0700, file 0600 (Windows ignores modes; its folder is per user).
func saveConfig(path string, c config) error {
	dir := filepath.Dir(path)
	data, _ := json.MarshalIndent(c, "", "  ")
	for _, step := range []func() error{
		func() error { return os.MkdirAll(dir, 0o700) },
		func() error { return os.Chmod(dir, 0o700) },
		func() error { return os.WriteFile(path, append(data, '\n'), 0o600) },
		func() error { return os.Chmod(path, 0o600) }, // WriteFile only sets the mode on a new file
	} {
		if err := step(); err != nil {
			return err
		}
	}
	return nil
}

// get asks the server for /seg1/seg2/... Each segment is escaped, so a name with odd characters stays one segment.
func get(c config, segs ...string) (*http.Response, error) {
	esc := make([]string, len(segs))
	for i, s := range segs {
		esc[i] = url.PathEscape(s)
	}
	req, err := http.NewRequest("GET", strings.TrimRight(c.URL, "/")+"/"+strings.Join(esc, "/"), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	return client.Do(req)
}

// check is what "init" does before saving: 200 = good, 401 = wrong token, no answer = wrong URL.
func check(c config) error {
	if !strings.HasPrefix(c.URL, "http://") && !strings.HasPrefix(c.URL, "https://") {
		return errors.New("API_BASE_URL must start with http:// or https://")
	}
	if c.Token == "" {
		return errors.New("API_TOKEN is empty")
	}
	resp, err := get(c, "list", "probes")
	if err != nil {
		return fmt.Errorf("cannot reach %s (wrong URL, or the server is down): %v", c.URL, err)
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusOK:
		return nil
	case http.StatusUnauthorized:
		return errors.New("the server rejected API_TOKEN (401)")
	}
	return fmt.Errorf("the server answered %s", resp.Status)
}

// ask prints a question and reads one line. A hidden answer (the token) is not echoed when stdin is a terminal;
// when stdin is a pipe or file (scripts, tests) it is read as a normal line.
// The error is only ever set when the hidden terminal read fails; a short or missing line is just an empty answer,
// which "check" then refuses.
func ask(in *bufio.Reader, stdin *os.File, out io.Writer, question string, hidden bool) (string, error) {
	fmt.Fprint(out, question)
	if hidden && isTerminal(int(stdin.Fd())) {
		b, err := readPassword(int(stdin.Fd()))
		fmt.Fprintln(out)
		return strings.TrimSpace(string(b)), err
	}
	line, _ := in.ReadString('\n')
	return strings.TrimSpace(line), nil
}

func initCmd(cfgPath string, stdin *os.File, stdout, stderr io.Writer) int {
	in := bufio.NewReader(stdin)
	if _, err := os.Stat(cfgPath); err == nil {
		a, _ := ask(in, stdin, stdout, "Config already exists at "+cfgPath+". Overwrite? [y/N] ", false)
		if a = strings.ToLower(a); a != "y" && a != "yes" {
			fmt.Fprintln(stdout, "Nothing changed.")
			return 0
		}
	}
	var c config
	c.URL, _ = ask(in, stdin, stdout, "API_BASE_URL (for example http://192.0.2.10:8080): ", false)
	var err error
	if c.Token, err = ask(in, stdin, stdout, "API_TOKEN (not shown): ", true); err != nil {
		fmt.Fprintln(stderr, "looking-glass: cannot read the token:", err, "- nothing saved.")
		return 1
	}
	if err := check(c); err != nil {
		fmt.Fprintln(stderr, "looking-glass:", err, "- nothing saved.")
		return 1
	}
	if err := saveConfig(cfgPath, c); err != nil {
		fmt.Fprintln(stderr, "looking-glass: cannot save the config:", err)
		return 1
	}
	fmt.Fprintln(stdout, "Checked and saved to", cfgPath)
	return 0
}

// validArgs: "list" takes a topic (and, for targets, an optional filter); a probe takes exactly a remote and a link.
// The server checks the names themselves and answers 400 with its own usage text.
func validArgs(a []string) bool {
	switch a[0] {
	case "list":
		return len(a) == 2 || (len(a) == 3 && a[1] == "targets")
	case "ping", "mtr", "traceroute":
		return len(a) == 3
	}
	return false
}

// run returns the exit code: 0 ok, 1 failed, 2 wrong usage.
func run(args []string, stdin *os.File, stdout, stderr io.Writer, cfgPath string) int {
	if version == "development" {
		fmt.Fprintln(stderr, "RUNNING A DEVELOPMENT VERSION OF CLIENT looking-glass")
	}
	if len(args) == 1 && (args[0] == "help" || args[0] == "-h" || args[0] == "--help") {
		fmt.Fprint(stdout, help)
		return 0
	}
	if len(args) == 1 && (args[0] == "version" || args[0] == "-V" || args[0] == "--version") {
		fmt.Fprintln(stdout, version)
		return 0
	}
	if len(args) == 0 || (args[0] != "init" && !validArgs(args)) || (args[0] == "init" && len(args) != 1) {
		fmt.Fprint(stderr, help)
		return 2
	}
	if args[0] == "init" {
		return initCmd(cfgPath, stdin, stdout, stderr)
	}
	fail := func(err error) int {
		fmt.Fprintln(stderr, "looking-glass:", err)
		return 1
	}
	c, err := loadConfig(cfgPath)
	if err != nil {
		return fail(err)
	}
	resp, err := get(c, args...)
	if err != nil {
		return fail(err)
	}
	defer resp.Body.Close()
	out := stdout
	if resp.StatusCode != http.StatusOK {
		out = stderr // the server's own message (usage, busy, ...) goes to stderr
	}
	if _, err := io.Copy(out, resp.Body); err != nil { // io.Copy streams: output shows while the probe runs
		return fail(fmt.Errorf("connection lost before the output ended: %v", err))
	}
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func main() {
	path, err := configPath()
	if err != nil {
		fmt.Fprintln(os.Stderr, "looking-glass:", err)
		os.Exit(1)
	}
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr, path))
}
