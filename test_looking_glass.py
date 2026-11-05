#!/usr/bin/env python3
"""
test_looking_glass.py -- tests for looking_glass_server.py backlog items B2, B3, B4 and D1
(B1 is checked by simply importing the file on the target Python).

Stdlib only, Python 3.8+. Runs the real request handler on a loopback port with stub
ping/mtr/traceroute scripts, so no network or root is needed. One test also runs the
script itself to check the startup path.

Usage
  python test_looking_glass.py                          # tests looking_glass_server.py next to this file
  LG_PATH=/path/to/other_copy.py python test_looking_glass.py

Changelog
  0.1.0  2026-10-24  Initial: B2 (error handler), B3 (clear config/target_defs errors).
  0.2.0  2026-10-24  B4 (timeouts incl. config validation), D1 (threaded server, black-box).
"""

import importlib.machinery
import importlib.util
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.environ.get("LG_PATH") or os.path.join(HERE, "looking_glass_server.py")

_spec = importlib.util.spec_from_file_location(
    "lg_under_test", SERVER, loader=importlib.machinery.SourceFileLoader("lg_under_test", SERVER)
)
lg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lg)  # B1: this line itself fails with SyntaxError on Python 3.8 if the file is not 3.8-safe

STUB = """#!{py}
import os, time
with open(os.environ["STUB_PID"], "w") as f:
    f.write(str(os.getpid()))
name = os.path.basename(__file__)
for i in range(int(os.environ.get("STUB_N", "3"))):
    print("%s stub line %d" % (name, i), flush=True)
    time.sleep(float(os.environ.get("STUB_DELAY", "0")))
raise SystemExit(int(os.environ.get("STUB_EXIT", "0")))
"""


GOOD_CONFIG = {
    "listen_host": "0.0.0.0",
    "listen_port": 8080,
    "auth_token": "x",
    "timeouts": {"ping": 30, "mtr": 60, "traceroute": 90},
}


def make_stubs(folder, names=("ping", "mtr", "traceroute"), ip_exit=0):
    os.makedirs(folder, exist_ok=True)
    for name in names:
        with open(os.path.join(folder, name), "w") as f:
            f.write(STUB.format(py=sys.executable))
        os.chmod(os.path.join(folder, name), 0o755)
    with open(os.path.join(folder, "ip"), "w") as f:  # the route gate: `ip route get ...`
        f.write('#!/bin/sh\necho "stub ip: $*"\nexit %d\n' % ip_exit)
    os.chmod(os.path.join(folder, "ip"), 0o755)


class ListHandler(logging.Handler):
    def __init__(self, records):
        super().__init__()
        self.records = records

    def emit(self, record):
        tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
        self.records.append((record.levelname, record.getMessage(), tb))


class FakeProc:
    """A probe process whose output dies half way, to test the error path after streaming started."""

    def __init__(self):
        self.killed = self.reaped = self.closed = False
        self.stdout = self

    def __iter__(self):
        yield "first line\n"
        yield "second line\n"
        raise RuntimeError("boom")

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self):
        self.reaped = True
        return -9

    def close(self):
        self.closed = True


class Base(unittest.TestCase):
    token = "test-token"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lgtest_")
        cls.bin_all = os.path.join(cls.tmp, "bin_all")
        cls.bin_no_traceroute = os.path.join(cls.tmp, "bin_no_traceroute")
        make_stubs(cls.bin_all)
        make_stubs(cls.bin_no_traceroute, ("ping", "mtr"))
        cls.bin_ip_fails = os.path.join(cls.tmp, "bin_ip_fails")
        make_stubs(cls.bin_ip_fails, ip_exit=2)
        cls.pidfile = os.path.join(cls.tmp, "stub.pid")
        cls.defs = os.path.join(cls.tmp, "target_defs.json")
        cls.records = []
        logger = logging.getLogger("lg_test_%d" % id(cls))
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(ListHandler(cls.records))
        cls.timeouts = {"ping": 30, "mtr": 30, "traceroute": 30}
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), lg.probe_api_server_handler(cls.defs, cls.token, logger, cls.timeouts, 2)
        )
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.write_defs({"t": {"p": {"src": "192.0.2.1", "dest": "192.0.2.2"}}})
        self.saved_env = dict(os.environ)
        os.environ.update(PATH=self.bin_all, STUB_PID=self.pidfile, STUB_N="3", STUB_DELAY="0")
        del self.records[:]
        self.timeouts.update(ping=30, mtr=30, traceroute=30)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    def write_defs(self, content):
        with open(self.defs, "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))

    def request_bytes(self, path, auth=True):
        req = "GET %s HTTP/1.0\r\n" % path
        if auth:
            req += "Authorization: Bearer %s\r\n" % self.token
        return (req + "\r\n").encode()

    def get(self, path, auth=True):
        """Return (status, body, raw_bytes). status 0 = the server dropped the connection without answering."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        s.sendall(self.request_bytes(path, auth))
        data = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        except ConnectionError:
            pass
        s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        status = int(head.split()[1]) if head else 0
        return status, body.decode("utf-8", "replace"), data


class TestNormalBehaviourUnchanged(Base):
    def test_unauthorized(self):
        self.assertEqual(self.get("/ping/t/p", auth=False)[0], 401)

    def test_three_probes_stream_with_exact_commands(self):
        expected = {
            "ping": "$ ping -c 3 -I 192.0.2.1 192.0.2.2",
            "mtr": "$ mtr -n -r -c 3 -a 192.0.2.1 192.0.2.2",
            "traceroute": "$ traceroute -I -n -q 3 -s 192.0.2.1 192.0.2.2",
        }
        for tool, command_line in expected.items():
            status, body, data = self.get("/%s/t/p" % tool)
            self.assertEqual(status, 200, tool)
            self.assertIn(command_line, body)
            self.assertIn("%s stub line 2" % tool, body)
            self.assertEqual(data.count(b"HTTP/1."), 1, tool)

    def test_bad_request_is_a_real_400_with_usage(self):
        for path in ("/", "/ping/t", "/ping/t/nope", "/list/zzz", "/ping/t/p/extra"):
            status, body, _ = self.get(path)
            self.assertEqual(status, 400, path)
            self.assertIn("ping remote link", body)
            self.assertNotIn("target_name", body)

    def test_log_response_has_no_doubled_newlines(self):
        self.get("/ping/t/p")
        logged = [msg for lvl, msg, _ in self.records if "Cmd Response" in msg][-1]
        self.assertNotIn("\n\n", logged.split("Cmd Response:")[1])

    def test_list_targets(self):
        status, body, _ = self.get("/list/targets")
        self.assertEqual(status, 200)
        self.assertIn("192.0.2.2", body)


class TestB2ErrorHandler(Base):
    def assert_no_unbound_local(self):
        self.assertFalse(any("UnboundLocalError" in tb for _, _, tb in self.records), "error handler crashed itself")

    def test_missing_tool_gives_clear_500_and_is_logged(self):
        os.environ["PATH"] = self.bin_no_traceroute
        status, body, data = self.get("/traceroute/t/p")
        self.assertEqual(status, 500, "client should get a 500, got a dropped connection" if status == 0 else body)
        self.assertIn("FileNotFoundError", body)
        self.assertIn("NOT A TEST RESULT", body)
        self.assertEqual(data.count(b"HTTP/1."), 1)
        self.assertTrue(any(lvl == "ERROR" and "An Exception occurred" in msg for lvl, msg, _ in self.records))
        self.assert_no_unbound_local()
        os.environ["PATH"] = self.bin_all
        self.assertEqual(self.get("/ping/t/p")[0], 200, "server must keep serving afterwards")

    def test_error_after_streaming_started_does_not_send_a_second_header(self):
        proc = FakeProc()
        original, original_kill = lg._run_cmd, lg._kill_group
        lg._run_cmd = lambda cmd: proc
        lg._kill_group = lambda p: setattr(p, "killed", True)  # never signal a real process group from a fake
        try:
            status, body, data = self.get("/ping/t/p")
        finally:
            lg._run_cmd, lg._kill_group = original, original_kill
        self.assertEqual(status, 200)
        self.assertIn("second line", body)
        self.assertIn("RuntimeError: boom", body)
        self.assertIn("NOT A TEST RESULT", body)
        self.assertEqual(data.count(b"HTTP/1."), 1, "a second HTTP status line was written into the output")
        self.assertTrue(proc.killed and proc.reaped and proc.closed, "process was not stopped and cleaned up")
        self.assert_no_unbound_local()

    def test_client_leaving_mid_stream_stops_the_process(self):
        os.environ.update(STUB_N="40", STUB_DELAY="0.25")  # would run ~10 s if left alone
        s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        s.sendall(self.request_bytes("/ping/t/p"))
        buf = b""
        while b"stub line" not in buf:
            chunk = s.recv(4096)
            self.assertTrue(chunk, "server closed before any probe output: %r" % buf)
            buf += chunk
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close with RST
        s.close()
        with open(self.pidfile) as f:
            pid = int(f.read())
        alive, deadline = True, time.time() + 4
        while alive and time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                alive = False
        self.assertFalse(alive, "probe process kept running after the client left")
        self.assertTrue(any("Disconnected" in msg for _, msg, _ in self.records))
        self.assert_no_unbound_local()
        self.assertEqual(self.get("/list/probes")[0], 200, "server must keep serving afterwards")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class TestB4Timeouts(Base):
    def stub_pid(self):
        with open(self.pidfile) as f:
            return int(f.read())

    def wait_dead(self, pid, seconds=4):
        deadline = time.time() + seconds
        while _pid_alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        return not _pid_alive(pid)

    def test_each_probe_uses_its_own_timeout(self):
        os.environ.update(STUB_N="40", STUB_DELAY="0.25")  # would run ~10 s
        for tool in ("ping", "mtr", "traceroute"):
            self.timeouts.update(ping=30, mtr=30, traceroute=30)
            self.timeouts[tool] = 1
            started = time.time()
            status, body, data = self.get("/%s/t/p" % tool)
            self.assertLess(time.time() - started, 5, tool)
            self.assertEqual(status, 200, tool)
            self.assertIn("%s stub line 0" % tool, body)
            self.assertIn("probe stopped after 1s", body, tool)
            self.assertIn("NOT A TEST RESULT", body)
            self.assertEqual(data.count(b"HTTP/1."), 1, tool)
            self.assertTrue(self.wait_dead(self.stub_pid()), "%s still running after timeout" % tool)

    def test_silent_probe_is_stopped(self):
        os.environ.update(STUB_N="1", STUB_DELAY="30")  # prints once, then nothing for 30 s
        self.timeouts["ping"] = 1
        started = time.time()
        status, body, _ = self.get("/ping/t/p")
        self.assertLess(time.time() - started, 5)
        self.assertIn("probe stopped after 1s", body)
        self.assertTrue(self.wait_dead(self.stub_pid()))

    def test_no_false_timeout_when_probe_finishes_in_time(self):
        status, body, _ = self.get("/ping/t/p")
        self.assertEqual(status, 200)
        self.assertNotIn("probe stopped", body)
        self.assertNotIn("NOT A TEST RESULT", body)

    def test_timeout_is_logged_with_the_message(self):
        os.environ.update(STUB_N="40", STUB_DELAY="0.25")
        self.timeouts["ping"] = 1
        self.get("/ping/t/p")
        self.assertTrue(any("probe stopped after 1s" in msg for _, msg, _ in self.records))


class TestB6Gates(Base):
    def test_unroutable_source_is_a_host_problem_not_a_result(self):
        os.environ["PATH"] = self.bin_ip_fails
        status, body, _ = self.get("/ping/t/p")
        self.assertEqual(status, 500)
        self.assertIn("probe host cannot route 192.0.2.1 to 192.0.2.2", body)
        self.assertIn("NOT A TEST RESULT", body)
        self.assertNotIn("stub line", body, "the probe must not run when the gate fails")

    def test_mtr_without_hops_is_flagged(self):
        status, body, _ = self.get("/mtr/t/p")  # the stub prints no '|--' hop lines
        self.assertIn("probe tool failed (exit 0)", body)
        self.assertIn("NOT A TEST RESULT", body)

    def test_ping_exit_1_is_a_real_result_but_exit_2_is_not(self):
        for code, flagged in (("1", False), ("2", True)):
            os.environ["STUB_EXIT"] = code
            body = self.get("/ping/t/p")[1]
            self.assertEqual("probe tool failed" in body, flagged, code)
        del os.environ["STUB_EXIT"]

    def test_traceroute_nonzero_exit_is_flagged(self):
        os.environ["STUB_EXIT"] = "1"
        self.assertIn("probe tool failed (exit 1)", self.get("/traceroute/t/p")[1])
        del os.environ["STUB_EXIT"]


class TestB10Cap(Base):
    def test_busy_server_answers_429_and_recovers(self):
        os.environ.update(STUB_N="3", STUB_DELAY="1")  # each probe takes ~3 s; the test server allows 2 at once
        threads = [threading.Thread(target=self.get, args=("/ping/t/p",)) for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(0.8)
        started = time.time()
        status, body, _ = self.get("/ping/t/p")
        self.assertEqual(status, 429, body)
        self.assertLess(time.time() - started, 1.0, "429 must be immediate")
        self.assertIn("busy, 2 probes already running", body)
        self.assertEqual(self.get("/list/probes")[0], 200, "list is never blocked")
        for t in threads:
            t.join()
        self.assertEqual(self.get("/ping/t/p")[0], 200, "slots must be released after the probes end")

    def test_slot_is_released_after_a_failed_probe(self):
        os.environ["PATH"] = self.bin_no_traceroute  # traceroute missing -> 500 inside the guarded section
        for _ in range(4):
            self.assertEqual(self.get("/traceroute/t/p")[0], 500)
        os.environ["PATH"] = self.bin_all
        self.assertEqual(self.get("/ping/t/p")[0], 200)

    def test_max_probes_config_is_validated(self):
        tmp = tempfile.mkdtemp(prefix="lgmp_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "config.json")
        for bad in (0, -1, "8", True, 1.5):
            with open(path, "w") as f:
                json.dump(dict(GOOD_CONFIG, max_probes=bad), f)
            with self.assertRaisesRegex(lg.ConfigError, "max_probes"):
                lg._load_config(path)
        with open(path, "w") as f:
            json.dump(dict(GOOD_CONFIG, max_probes=3), f)
        self.assertEqual(lg._load_config(path)["max_probes"], 3)


class TestD2ClientIp(unittest.TestCase):
    def client(self, peer, **headers):
        class Req:
            client_address = (peer, 5555)

        Req.headers = headers
        return lg._get_client(Req)

    def test_direct_peer_ignores_forged_headers(self):
        self.assertEqual(
            self.client("8.8.8.8", **{"X-Forwarded-For": "6.6.6.6", "X-Real-IP": "6.6.6.6"}), ("8.8.8.8", 5555)
        )

    def test_no_headers_gives_peer(self):
        self.assertEqual(self.client("127.0.0.1"), ("127.0.0.1", 5555))

    def test_behind_nginx(self):
        self.assertEqual(self.client("127.0.0.1", **{"X-Real-IP": "198.51.100.7"}), ("198.51.100.7", "Unknown"))
        # nginx appends the address it saw; anything to the left was supplied by the client
        self.assertEqual(
            self.client("10.0.0.2", **{"X-Forwarded-For": "6.6.6.6, 198.51.100.7"}), ("198.51.100.7", "Unknown")
        )

    def test_garbage_header_falls_back_to_peer(self):
        self.assertEqual(self.client("127.0.0.1", **{"X-Forwarded-For": "not-an-ip"}), ("127.0.0.1", 5555))


class TestB9Check(unittest.TestCase):
    def run_check(self, bindir, config=None, defs=None, **env):
        tmp = tempfile.mkdtemp(prefix="lgchk_")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(SERVER, os.path.join(tmp, "looking_glass_server.py"))
        with open(os.path.join(tmp, "config.json"), "w") as f:
            json.dump(GOOD_CONFIG if config is None else config, f)
        with open(os.path.join(tmp, "target_defs.json"), "w") as f:
            if isinstance(defs, str):  # raw text, so a test can write duplicate keys
                f.write(defs)
            else:
                json.dump({"acme": {"primary": {"src": "192.0.2.1", "dest": "192.0.2.2"}}} if defs is None else defs, f)
        run = subprocess.run(
            [sys.executable, os.path.join(tmp, "looking_glass_server.py"), "-c"],
            env=dict(os.environ, PATH=bindir, STUB_PID=os.path.join(tmp, "pid"), **env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=60,
        )
        self.assertFalse(os.path.exists(os.path.join(tmp, "app.log")), "-c must not start the server or log")
        return run.returncode, run.stdout

    def bins(self, **kw):
        folder = tempfile.mkdtemp(prefix="lgbin_")
        self.addCleanup(shutil.rmtree, folder, True)
        make_stubs(folder, **kw)
        return folder

    def test_all_good(self):
        code, out = self.run_check(self.bins())
        self.assertEqual(code, 0, out)
        self.assertIn("acme/primary", out)
        self.assertIn("all good", out)

    def test_no_reply_is_a_warning_not_a_failure(self):
        code, out = self.run_check(self.bins(), STUB_EXIT="1")
        self.assertEqual(code, 0, out)
        self.assertIn("NO REPLY", out)

    def test_route_failure(self):
        code, out = self.run_check(self.bins(ip_exit=2))
        self.assertEqual(code, 1)
        self.assertIn("ROUTE FAIL", out)

    def test_missing_tool(self):
        code, out = self.run_check(self.bins(names=("ping", "mtr")))
        self.assertEqual(code, 1)
        self.assertIn("MISSING: traceroute", out)

    def test_bad_files(self):
        code, out = self.run_check(
            self.bins(), config={"listen_host": "x"}, defs={"acme": {"primary": {"src": "1.1.1.1"}}}
        )
        self.assertEqual(code, 1)
        self.assertIn("config.json   ERROR", out)
        self.assertIn('"acme/primary" needs a non-empty "dest"', out)


class TestD1Threading(unittest.TestCase):
    """Black-box: start the real script (so run() is what is tested) and check that a slow probe does not block others."""

    def test_second_client_is_not_blocked_by_a_running_probe(self):
        tmp = tempfile.mkdtemp(prefix="lgthr_")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(SERVER, os.path.join(tmp, "looking_glass_server.py"))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with open(os.path.join(tmp, "config.json"), "w") as f:
            json.dump(dict(GOOD_CONFIG, listen_host="127.0.0.1", listen_port=port, auth_token="tok"), f)
        with open(os.path.join(tmp, "target_defs.json"), "w") as f:
            json.dump({"t": {"p": {"src": "192.0.2.1", "dest": "192.0.2.2"}}}, f)
        bindir = os.path.join(tmp, "bin")
        make_stubs(bindir)
        env = dict(os.environ, PATH=bindir, STUB_PID=os.path.join(tmp, "pid"), STUB_N="3", STUB_DELAY="1.5")
        server = subprocess.Popen(
            [sys.executable, os.path.join(tmp, "looking_glass_server.py")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(server.wait)
        self.addCleanup(server.kill)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)

        def fetch(path, auth=True):
            s = socket.create_connection(("127.0.0.1", port), timeout=30)
            head = "GET %s HTTP/1.0\r\n" % path + ("Authorization: Bearer tok\r\n" if auth else "") + "\r\n"
            s.sendall(head.encode())
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            s.close()
            return data

        slow_done = threading.Event()
        threading.Thread(target=lambda: (fetch("/ping/t/p"), slow_done.set()), daemon=True).start()
        time.sleep(0.7)  # the slow probe is now running (takes ~4.5 s)
        started = time.time()
        self.assertIn(b" 401 ", fetch("/ping/t/p", auth=False))
        self.assertIn(b" 200 ", fetch("/list/probes"))
        elapsed = time.time() - started
        self.assertLess(elapsed, 1.5, "second client waited for the running probe")
        self.assertFalse(slow_done.is_set(), "test is invalid: the slow probe had already finished")


class TestB3ClearConfigMessages(Base):
    def test_bad_json_in_target_defs(self):
        self.write_defs("{oops")
        status, body, _ = self.get("/list/targets")
        self.assertEqual(status, 500, "got a dropped connection instead of a message" if status == 0 else body)
        self.assertIn("target_defs.json is not valid JSON", body)
        self.assertIn("line 1", body)
        self.assertIn("NOT A TEST RESULT", body)
        self.assertTrue(any("Config error" in msg for _, msg, _ in self.records))
        self.write_defs({"t": {"p": {"src": "192.0.2.1", "dest": "192.0.2.2"}}})
        self.assertEqual(self.get("/list/targets")[0], 200, "fixing the file must work without a restart")

    def test_probe_request_with_bad_target_defs(self):
        self.write_defs("{oops")
        status, body, _ = self.get("/ping/t/p")
        self.assertEqual(status, 500)
        self.assertIn("target_defs.json is not valid JSON", body)

    def test_entry_missing_dest(self):
        self.write_defs({"t": {"p": {"src": "192.0.2.1"}}})
        status, body, _ = self.get("/list/targets")
        self.assertEqual(status, 500)
        self.assertIn('"t/p"', body)
        self.assertIn('"dest"', body)

    def test_wrong_shape(self):
        self.write_defs([1, 2])
        status, body, _ = self.get("/list/targets")
        self.assertEqual(status, 500)
        self.assertIn("must be a JSON object", body)

    def test_missing_file(self):
        os.remove(self.defs)
        status, body, _ = self.get("/list/targets")
        self.assertEqual(status, 500)
        self.assertIn("cannot read target_defs.json", body)


class TestB3Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lgcfg_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cfg(self, content):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        return path

    def test_valid(self):
        good = dict(GOOD_CONFIG, timeouts={"ping": 30, "mtr": 60.5, "traceroute": 90, "ip": 5})
        self.assertEqual(lg._load_config(self.cfg(good)), good)

    def test_the_shipped_sample_config_refuses_to_start(self):
        # config.json in the repository is a sample (empty host, port 0, empty token): it cannot be run by mistake
        sample = os.path.join(HERE, "config.json")
        if not os.path.exists(sample):
            self.skipTest("no sample config.json next to the tests")
        with self.assertRaisesRegex(lg.ConfigError, '"listen_host" must not be empty'):
            lg._load_config(sample)

    def test_problems_have_plain_messages(self):
        good = GOOD_CONFIG
        cases = [
            ("{oops", "config.json is not valid JSON"),
            ([1], "must be a JSON object"),
            ({"listen_host": "0.0.0.0"}, "missing required key\\(s\\): listen_port, auth_token"),
            (dict(good, listen_port="8080"), "from 1 to 65535"),
            (dict(good, listen_port=0), "from 1 to 65535"),
            (dict(good, listen_port=70000), "from 1 to 65535"),
            (dict(good, listen_port=True), "from 1 to 65535"),
            (dict(good, auth_token=""), "must not be empty or contain spaces"),
            (dict(good, auth_token="two words"), "must not be empty or contain spaces"),
            (dict(good, auth_token=" padded "), "must not be empty or contain spaces"),
            (dict(good, auth_token=5), "must be text"),
            (dict(good, listen_host=""), '"listen_host" must not be empty'),
            (dict(good, listen_host="  "), '"listen_host" must not be empty'),
            (dict(good, listen_host=5), "must be text"),
            (dict(good, log_file_path=5), '"log_file_path" must be text'),
            ({k: v for k, v in good.items() if k != "timeouts"}, '"timeouts" must be an object'),
            (dict(good, timeouts=[30]), '"timeouts" must be an object'),
            (dict(good, timeouts={"ping": 30}), "above 0 for: mtr, traceroute"),
            (dict(good, timeouts={"ping": 0, "mtr": 5, "traceroute": 5}), "above 0 for: ping"),
            (dict(good, timeouts={"ping": "30", "mtr": 5, "traceroute": 5}), "above 0 for: ping"),
            (dict(good, timeouts={"ping": True, "mtr": 5, "traceroute": 5}), "above 0 for: ping"),
        ]
        for content, pattern in cases:
            with self.assertRaisesRegex(lg.ConfigError, pattern):
                lg._load_config(self.cfg(content))
        with self.assertRaisesRegex(lg.ConfigError, "cannot read config.json"):
            lg._load_config(os.path.join(self.tmp, "nope", "config.json"))

    def test_startup_with_bad_config_exits_with_a_message_not_a_traceback(self):
        shutil.copy(SERVER, os.path.join(self.tmp, "looking_glass_server.py"))
        self.cfg({"listen_host": "127.0.0.1"})
        run = subprocess.run(
            [sys.executable, os.path.join(self.tmp, "looking_glass_server.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=20,
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("CONFIG ERROR", run.stderr)
        self.assertIn("missing required key", run.stderr)
        self.assertNotIn("Traceback", run.stderr)


R2_DEFS = {
    "acme": {
        "primary": {"src": "192.0.2.1", "dest": "192.0.2.2"},
        "backup-2": {"src": "192.0.2.3", "dest": "192.0.2.4"},
    },
    "globex": {"primary": {"src": "192.0.2.5", "dest": "192.0.2.6"}},
}


class TestR2Lists(Base):
    """Round 2: list remote / link / targets <filter>, headers on every table, and any link name (backup-2)."""

    def setUp(self):
        super().setUp()
        self.write_defs(R2_DEFS)

    def test_list_remote_and_link_tables(self):
        body = self.get("/list/remote")[1]
        self.assertRegex(body, r"remote\s*\| links")
        self.assertIn("acme   | primary + backup-2", body)
        body = self.get("/list/link")[1]
        self.assertRegex(body, r"link\s*\| remotes")
        self.assertIn("primary  | acme + globex", body)
        self.assertIn("backup-2 | acme", body)

    def test_list_probes_has_header(self):
        self.assertRegex(self.get("/list/probes")[1], r"probe\s*\n-+\n ping")

    def test_list_targets_filter_by_remote_or_link(self):
        full = self.get("/list/targets")[1]
        self.assertRegex(full, r"remote\s*\| link\s*\| src\s*\| dest")
        by_remote = self.get("/list/targets/acme")[1]
        self.assertIn("192.0.2.4", by_remote)
        self.assertNotIn("192.0.2.6", by_remote)
        by_link = self.get("/list/targets/primary")[1]
        self.assertIn("192.0.2.2", by_link)
        self.assertIn("192.0.2.6", by_link)
        self.assertNotIn("192.0.2.4", by_link)
        self.assertEqual(self.get("/list/targets/nope")[0], 400)

    def test_long_link_name_keeps_table_aligned(self):
        self.write_defs({"acme": {"sixteen-chars-ok": {"src": "192.0.2.1", "dest": "192.0.2.2"}}})
        rows = [r for r in self.get("/list/targets")[1].splitlines() if "|" in r]
        self.assertEqual(len({r.index("|") for r in rows}), 1, rows)

    def test_ping_works_on_any_link_name(self):
        status, body, _ = self.get("/ping/acme/backup-2")
        self.assertEqual(status, 200)
        self.assertIn("$ ping -c 3 -I 192.0.2.3 192.0.2.4", body)


class TestR4Duplicates(TestB9Check):
    def test_duplicate_names_are_reported_not_fixed(self):
        raw = (
            '{"acme": {"primary": {"src": "192.0.2.1", "dest": "192.0.2.2"},'
            ' "primary": {"src": "192.0.2.1", "dest": "192.0.2.9"}},'
            ' "globex": {}, "globex": {}}'
        )
        code, out = self.run_check(self.bins(), defs=raw)
        self.assertEqual(code, 1, out)
        self.assertIn("duplicate     acme/primary", out)
        self.assertIn("duplicate     globex", out)

    def test_same_src_dest_under_two_names_is_a_warning(self):
        same = {
            "acme": {
                "primary": {"src": "192.0.2.1", "dest": "192.0.2.2"},
                "secondary": {"src": "192.0.2.1", "dest": "192.0.2.2"},
            }
        }
        code, out = self.run_check(self.bins(), defs=same)
        self.assertEqual(code, 0, out)
        self.assertIn("same src/dest acme/primary and acme/secondary", out)


class TestNameRule(Base):
    """Names: trimmed, lower-case, ^[a-z][a-z0-9._-]{0,15}$. Bad names are discarded."""

    E = {"src": "192.0.2.1", "dest": "192.0.2.2"}

    def test_bad_names_are_discarded_everywhere(self):
        ok16, bad17 = "a" * 16, "a" * 17
        self.write_defs(
            {
                "acme": {"primary": self.E, "na me": self.E, "2fast": self.E, bad17: self.E, ok16: self.E},
                "bad remote": {"primary": self.E},
                "_x": {"primary": self.E},
                "onlybad": {"x y": self.E},
            }
        )
        listed = self.get("/list/targets")[1]
        self.assertIn("acme", listed)
        self.assertIn(ok16, listed)
        for gone in ("na me", "2fast", bad17, "bad remote", "_x", "onlybad"):
            self.assertNotIn(gone, listed)
        self.assertNotIn("onlybad", self.get("/list/remote")[1])
        self.assertEqual(self.get("/ping/acme/2fast")[0], 400)
        self.assertEqual(self.get("/ping/acme/na%20me")[0], 400)
        self.assertEqual(self.get("/ping/acme/primary")[0], 200, "good names keep working")

    def test_names_ignore_case_and_outer_spaces(self):
        self.write_defs({" ACME ": {"Primary": self.E}})
        self.assertIn("acme", self.get("/list/remote")[1])
        for url in ("/ping/ACME/PRIMARY", "/PING/acme/primary", "/ping/%20acme%20/primary"):
            self.assertEqual(self.get(url)[0], 200, url)
        self.assertEqual(self.get("/list/targets/ACME")[0], 200)


class TestNameRuleCheck(TestB9Check):
    def test_bad_name_is_a_problem(self):
        code, out = self.run_check(self.bins(), defs={"acme": {"primary": self.E, "na me": self.E}})
        self.assertEqual(code, 1, out)
        self.assertIn("bad name", out)
        self.assertIn("acme/na me", out)

    def test_case_clash_is_a_duplicate(self):
        code, out = self.run_check(self.bins(), defs={"acme": {"primary": self.E}, "ACME": {"primary": self.E}})
        self.assertEqual(code, 1, out)
        self.assertIn("duplicate", out)

    def test_name_needing_a_trim_is_only_a_note(self):
        code, out = self.run_check(self.bins(), defs={" Acme ": {"primary": self.E}})
        self.assertEqual(code, 0, out)
        self.assertIn("note", out)
        self.assertIn("is read as acme", out)

    E = {"src": "192.0.2.1", "dest": "192.0.2.2"}


class TestBadValues(Base):
    """The JSON is fine but a value is wrong: src and dest must be IPv4 addresses, else the entry is ignored."""

    def entry(self, src="192.0.2.1", dest="192.0.2.2"):
        return {"src": src, "dest": dest}

    def test_bad_ips_are_ignored_everywhere(self):
        bad = {
            "b1": self.entry(src="banana"),
            "b2": self.entry(dest="192.168.1.300"),
            "b3": self.entry(dest="-f"),
            "b4": self.entry(dest="http://192.0.2.9"),
            "b5": self.entry(dest="::1"),
            "b6": self.entry(dest="localhost"),
        }
        self.write_defs({"acme": dict(bad, ok=self.entry())})
        listed = self.get("/list/targets")[1]
        self.assertIn("ok", listed)
        for name in bad:
            self.assertNotIn(name, listed)
            self.assertEqual(self.get("/ping/acme/" + name)[0], 400, name)
        self.assertEqual(self.get("/ping/acme/ok")[0], 200)

    def test_ip_with_outer_spaces_is_trimmed(self):
        self.write_defs({"acme": {"primary": self.entry(src=" 192.0.2.1 ", dest="192.0.2.2 ")}})
        self.assertIn("$ ping -c 3 -I 192.0.2.1 192.0.2.2", self.get("/ping/acme/primary")[1])

    def test_a_remote_with_only_bad_entries_vanishes(self):
        self.write_defs({"acme": {"primary": self.entry(src="banana")}, "globex": {"primary": self.entry()}})
        self.assertNotIn("acme", self.get("/list/remote")[1])


class TestBadValuesCheck(TestB9Check):
    def test_bad_ip_is_a_problem(self):
        code, out = self.run_check(
            self.bins(), defs={"acme": {"primary": {"src": "192.0.2.1", "dest": "192.168.1.300"}}}
        )
        self.assertEqual(code, 1, out)
        self.assertIn("bad ip", out)
        self.assertIn("acme/primary", out)
        self.assertIn("192.168.1.300", out)

    def test_short_token_warns_but_does_not_fail(self):
        code, out = self.run_check(self.bins(), config=dict(GOOD_CONFIG, auth_token="short"))
        self.assertEqual(code, 0, out)
        self.assertIn("WARN", out)

    def test_long_token_has_no_warning(self):
        code, out = self.run_check(self.bins(), config=dict(GOOD_CONFIG, auth_token="a" * 20))
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARN", out)

    def test_log_file_path_is_checked_and_never_created(self):
        folder = tempfile.mkdtemp(prefix="lglog_")
        self.addCleanup(shutil.rmtree, folder, True)
        good = os.path.join(folder, "my.log")
        code, out = self.run_check(self.bins(), config=dict(GOOD_CONFIG, log_file_path=good))
        self.assertEqual(code, 0, out)
        self.assertIn("log file      OK (%s)" % good, out)
        self.assertFalse(os.path.exists(good), "-c must not create the log file")
        code, out = self.run_check(
            self.bins(), config=dict(GOOD_CONFIG, log_file_path=os.path.join(folder, "no", "x.log"))
        )
        self.assertEqual(code, 1, out)
        self.assertIn("does not exist", out)
        code, out = self.run_check(self.bins(), config=dict(GOOD_CONFIG, log_file_path=folder))
        self.assertEqual(code, 1, out)
        self.assertIn("is a folder", out)

    def test_bad_port_is_reported(self):
        code, out = self.run_check(self.bins(), config=dict(GOOD_CONFIG, listen_port=70000))
        self.assertEqual(code, 1, out)
        self.assertIn("from 1 to 65535", out)


class TestWeirdUrls(Base):
    """Whatever a client sends, the answer is 200 or 400: never a 500 and never a dropped connection."""

    def test_odd_paths(self):
        paths = [
            "/list",
            "/list/",
            "/ping//t/p",
            "//ping/t/p",
            "/ping/t/p/",
            "/ping/t/p?x=1",
            "/ping/t/p#frag",
            "/ping/t%2Fp",
            "/ping/t/p%00",
            "/ping/t/%ff",
            "/ping/t/" + "a" * 5000,
            "/" + "/".join(["a"] * 300),
            "/..",
            "/ping/../list/probes",
        ]
        for path in paths:
            status = self.get(path)[0]
            self.assertIn(status, (200, 400), path)


def real_tools_missing():
    """Names of the tools the real-tool tests need and cannot find (all of them off Linux, where the flags differ)."""
    return [t for t in ("ping", "mtr", "traceroute", "ip") if not shutil.which(t)] or (
        [] if sys.platform.startswith("linux") else ["a Linux host"]
    )


class TestRealTools(Base):
    """The real ping, mtr and traceroute, run through the server, from 127.0.0.1 to 127.0.0.1.
    No stubs. If this class fails because a tool is missing or not allowed to run, this machine is not ready for
    testing: fix the machine, do not skip the test."""

    def setUp(self):
        super().setUp()
        os.environ["PATH"] = self.saved_env["PATH"]  # the real tools, not the stubs
        missing = real_tools_missing()
        if missing:
            self.fail("real-tool tests need Linux with ping, mtr, traceroute, ip. Missing: " + ", ".join(missing))
        self.write_defs({"lo": {"loop": {"src": "127.0.0.1", "dest": "127.0.0.1"}}})

    def probe(self, tool):
        status, body, _ = self.get("/%s/lo/loop" % tool)
        self.assertEqual(status, 200, body)
        self.assertNotIn("NOT A TEST RESULT", body, "the tool failed or could not run, so this machine is not ready")
        return body

    def test_real_ping(self):
        body = self.probe("ping")
        self.assertIn("$ ping -c 3 -I 127.0.0.1 127.0.0.1", body)
        self.assertIn("3 received", body)

    def test_real_mtr(self):
        body = self.probe("mtr")
        self.assertIn("$ mtr -n -r -c 3 -a 127.0.0.1 127.0.0.1", body)
        self.assertIn("|-- 127.0.0.1", body)

    def test_real_traceroute(self):
        body = self.probe("traceroute")
        self.assertIn("$ traceroute -I -n -q 3 -s 127.0.0.1 127.0.0.1", body)
        self.assertRegex(body, r"\n\s*1\s+127\.0\.0\.1")

    def test_real_mtr_is_stopped_at_the_timeout_and_leaves_nothing_behind(self):
        self.timeouts.update(mtr=1)
        started = time.time()
        status, body, _ = self.get("/mtr/lo/loop")
        self.assertEqual(status, 200)
        self.assertIn("probe stopped after 1s", body)
        self.assertLess(time.time() - started, 5, "mtr-packet kept the connection open")
        # exact process names, so this cannot match a shell whose command line merely mentions mtr.
        # The server cleans up just after the client has its answer, so give it up to 3 seconds to finish.
        for name in ("mtr", "mtr-packet"):
            for _ in range(30):
                left = subprocess.run(["pgrep", "-x", name], stdout=subprocess.PIPE).stdout
                if not left:
                    break
                time.sleep(0.1)
            self.assertEqual(left, b"", "a stray %s process is still running (is another mtr running?)" % name)


class TestLogFilePath(unittest.TestCase):
    """Real start-up of the server with "log_file_path" set."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lgstart_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copy(SERVER, os.path.join(self.tmp, "looking_glass_server.py"))
        with open(os.path.join(self.tmp, "target_defs.json"), "w") as f:
            json.dump({"t": {"p": {"src": "127.0.0.1", "dest": "127.0.0.1"}}}, f)

    def start(self, log_file_path):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        config = dict(GOOD_CONFIG, listen_host="127.0.0.1", listen_port=self.port, log_file_path=log_file_path)
        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            json.dump(config, f)
        return subprocess.Popen(
            [sys.executable, os.path.join(self.tmp, "looking_glass_server.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def test_log_goes_to_the_configured_file_only(self):
        wanted = os.path.join(self.tmp, "custom.log")
        proc = self.start(wanted)
        self.addCleanup(proc.kill)
        for _ in range(50):  # wait up to 5s for the port to open
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        req = "GET /list/probes HTTP/1.0\r\nAuthorization: Bearer x\r\n\r\n".encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(req)
            while s.recv(4096):
                pass
        proc.terminate()
        proc.communicate(timeout=10)
        with open(wanted) as f:
            self.assertIn("Starting LOOKING_GLASS", f.read())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "app.log")), "the default log must not be used")

    def test_unusable_log_path_stops_start_up_with_a_message(self):
        proc = self.start(os.path.join(self.tmp, "missing_folder", "x.log"))
        out, err = proc.communicate(timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('cannot use "log_file_path"', err)
        self.assertNotIn("Traceback", err)

    def test_empty_path_means_the_default_app_log(self):
        proc = self.start("")
        self.addCleanup(proc.kill)
        for _ in range(50):
            if os.path.exists(os.path.join(self.tmp, "app.log")):
                break
            time.sleep(0.1)
        proc.terminate()
        proc.communicate(timeout=10)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "app.log")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
