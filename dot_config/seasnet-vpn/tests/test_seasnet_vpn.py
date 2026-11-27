"""Isolated lifecycle regressions; never connect VPN, launch GUI, or change routes."""
import contextlib
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import select
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


sys.dont_write_bytecode = True
source = Path(__file__).resolve().parents[3] / "dot_local/bin/private_executable_seasnet-vpn"
if not source.exists():
    source = Path.home() / ".local/bin/seasnet-vpn"
loader = importlib.machinery.SourceFileLoader("seasnet_helper", str(source))
spec = importlib.util.spec_from_loader(loader.name, loader)
vpn = importlib.util.module_from_spec(spec)
loader.exec_module(vpn)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        for name, value in {"STATE": self.state, "PIDFILE": self.state / "openconnect.pid",
                            "BASELINE": self.state / "network-before.json", "PORT": 0,
                            "CALLBACK_PORT": 0}.items():
            context = patch.object(vpn, name, value)
            context.start()
            self.addCleanup(context.stop)
        # Never depend on whether the test runner has a controlling terminal.
        for context in (patch.object(vpn, "has_tty", return_value=True),
                        patch.object(vpn, "to_terminal", lambda: redirect_stdout(sys.stderr))):
            context.start()
            self.addCleanup(context.stop)

    def request(self, identity="current", attempt=None):
        vpn.write_json(self.state / "login-request.json",
                       {"id": identity, "url": "https://shb.ais.ucla.edu/login", "attempt": attempt})

    def auth_context(self, auth, owned=None):
        contexts = [patch.object(vpn, "snapshot", return_value={}),
                    patch.object(vpn, "has_tty", return_value=True),
                    patch.object(vpn, "resume_sync"), patch.object(vpn, "build_signin"),
                    patch.object(vpn, "who_asks", return_value={}),
                    patch.object(vpn, "owned_pid", owned or Mock(return_value=None)),
                    patch.object(vpn.subprocess, "Popen", return_value=auth),
                    patch.object(vpn, "kill_strays"), patch.object(vpn, "stop", return_value=0)]
        results = [context.start() for context in contexts]
        for context in contexts:
            self.addCleanup(context.stop)
        return results[-1]

    def test_atomic_state_and_separate_cancellation(self):
        self.request()
        vpn.write_json(self.state / "login-cancel.json", {"id": "old", "result": "cancelled"})
        self.assertFalse(vpn.login_cancelled())
        vpn.write_json(self.state / "login-cancel.json", {"id": "current", "result": "cancelled"})
        vpn.write_login_result("connected")
        self.assertTrue(vpn.login_cancelled())
        self.assertEqual((self.state / "login-request.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.state.glob(".*.tmp")))

    def test_late_result_does_not_overwrite_new_request(self):
        self.request("new", "new-attempt")
        vpn.write_login_result("failed", request_id="old")
        vpn.write_login_result("failed", attempt="old-attempt")
        self.assertFalse((self.state / "login-result.json").exists())

    LOGIN = "https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login?tgname=x&acsamlcap=v2"
    CHALLENGE = {"challenge": "A" * 42 + "_", "expires_in": 60}
    CALLBACK = ("/api/sso/AAEAAQBbMFkwEwYHKoZIzj0CAQYI%2BA%3D%3D"
                "?return=https%3A%2F%2Fv2.ssl.vpn.ucla.edu%2F%2BCSCOE%2B%2Fsaml_ac_login.html")

    # A stand-in for the sign-in window, speaking its protocol from a list of steps.
    WINDOW = r"""#!PYTHON
import json, os, sys, time
D = os.path.dirname(sys.argv[0])
W = "-web" if "--web" in sys.argv else ""
json.dump(sys.argv[1:], open(D + "/argv" + W, "w")); open(D + "/pid" + W, "w").write(str(os.getpid()))
said = open(D + "/told" + W, "a")
def emit(**e): print(json.dumps(e), flush=True)
def listen():
    line = sys.stdin.readline()
    if not line: sys.exit(4)
    said.write(line); said.flush()
    return json.loads(line)
def hang():
    while True: listen()
listen()
for n, step in enumerate(json.load(open(D + "/steps" + W))):
    if step == "ready": emit(event="ready")
    elif isinstance(step, (int, float)): time.sleep(step)
    elif step == "cancel": sys.exit(1)
    elif step == "prepared": emit(event="prepared")
    elif step == "show": assert listen() == {"do": "show"}; open(D + "/shown", "w")
    elif isinstance(step, dict): emit(**step)
    elif step == "unknown": emit(event="unknown"); hang()
    elif step == "late": time.sleep(1); emit(event="signed", challenge="A" * 42 + "_", signature="S" * 86); hang()
    elif step == "deliver": emit(event="delivered"); sys.exit(0)
    elif step == "zen": emit(event="trace", kind="zen", zen="started", browser="zen")
    elif step == "await-done-and-linger":
        while listen().get("do") != "done": pass
        time.sleep(1.5); sys.exit(0)
    elif step == "await-done":
        while listen().get("do") != "done": pass
        open(D + "/done-at", "w").write(repr(time.monotonic())); sys.exit(0)
    elif isinstance(step, str) and step.startswith("web:"): emit(event="web", why=step[4:]); hang()
    elif step == "crash": os.kill(os.getpid(), 11)
    elif step == "hang": hang()
    elif step == "problem": listen()
    elif step in ("approve", "refuse", "mute"):
        emit(event="approved", n=str(n))
        while True:
            told = listen()
            if told["do"] == "done": sys.exit(0)
            if told["do"] == "problem": break
            if told["do"] == "web": hang()
            assert told["n"] == str(n)
            if step == "mute": continue
            if step == "refuse": emit(event="unsigned"); break
            emit(event="signed", challenge=told["challenge"], signature="S" * 86)
sys.exit(70)
"""
    VLT = r"""#!PYTHON
import json, os, sys, time
D = os.path.dirname(sys.argv[0])
text = sys.stdin.read() if "--input-stdin" in sys.argv else ""
open(D + "/vlt-calls", "a").write(json.dumps([sys.argv[1:], text]) + "\n")
answers = json.load(open(D + "/answers"))
answer = answers.pop(0)
json.dump(answers, open(D + "/answers", "w"))
if answer == "hang": time.sleep(30)
if answer == "no-json": sys.exit(1)
if answer == "garbage": print("<html>"); sys.exit(0)
if answer == "old": sys.stderr.write('vlt: unknown command "vpn"\n'); sys.exit(2)
(sys.stderr if answer.pop("on_stderr", False) else sys.stdout).write(json.dumps(answer) + "\n")
sys.exit(1 if "code" in answer else 0)
"""

    def signin(self, steps, answers=(), url=LOGIN, before=None, listener="302 Found", key=True, web=("deliver",), proceeds=False, **constants):
        """browser_login against a stand-in window, vlt and openconnect."""
        for name, text in (("window", self.WINDOW), ("vlt", self.VLT)):
            (self.state / name).write_text(text.replace("PYTHON", sys.executable))
            (self.state / name).chmod(0o700)
        for name in ("told", "told-web", "vlt-calls", "pid", "pid-web", "shown", "login-notice"):
            (self.state / name).unlink(missing_ok=True)
        (self.state / "steps").write_text(json.dumps(["ready", *steps] if steps and steps[0] != "silent" else steps[1:]))
        (self.state / "steps-web").write_text(json.dumps(["prepared", "show", "ready", *web] if web and web[0] != "silent" else web[1:]))
        (self.state / "answers").write_text(json.dumps(list(answers)))
        (self.state / "openconnect.log").write_text("POST https://ssl.vpn.ucla.edu/\nPlease complete the authentication process.\n")
        if key:
            (self.state / "approve-key.json").write_text("{}")
        vpn.write_json(self.state / "login-attempt.json",
                       {"id": "a", "pid": os.getpid(), "asker": {"command": "ssh seasnet", "by": "", "agent": False}})
        self.received = []
        server = socket.create_server(("::1", 0), family=socket.AF_INET6)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        if listener is None:
            server.close()
        else:
            def serve():
                while True:
                    try:
                        client, _ = server.accept()
                    except OSError:
                        return
                    with client:
                        self.received.append(client.recv(8192).decode())
                        if listener == "silence":
                            if proceeds:
                                server.close()
                            continue
                        client.sendall(f"HTTP/1.1 {listener}\r\nConnection: close\r\n"
                                       "Location: https://v2.ssl.vpn.ucla.edu/x\r\n\r\n".encode())
            threading.Thread(target=serve, daemon=True).start()
        settings = {"VLT": str(self.state / "vlt"), "APPROVE_KEY": self.state / "approve-key.json",
                    "CALLBACK_PORT": port, **constants}
        errors = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, value in settings.items():
                stack.enter_context(patch.object(vpn, name, value))
            stack.enter_context(patch.dict(os.environ, {"SEASNET_VPN_LOGIN_ATTEMPT": "a"}))
            stack.enter_context(redirect_stderr(errors))
            stack.enter_context(patch.object(vpn, "build_signin", return_value=self.state / "window"))
            run = stack.enter_context(patch.object(vpn.subprocess, "run", return_value=Mock(returncode=0)))
            if before:
                before()
            code = vpn.browser_login(url)
        self.told = [json.loads(line) for line in (self.state / "told").read_text().splitlines()] \
            if (self.state / "told").exists() else []
        self.told_web = [json.loads(line) for line in (self.state / "told-web").read_text().splitlines()] \
            if (self.state / "told-web").exists() else []
        calls = self.state / "vlt-calls"
        self.calls = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return code, run

    def later(self, seconds, act):
        timer = threading.Timer(seconds, act)
        timer.start()
        self.addCleanup(timer.cancel)

    def alive(self):
        for name in ("pid", "pid-web"):
            if (self.state / name).exists():
                try:
                    os.kill(int((self.state / name).read_text()), 0)
                    return True
                except ProcessLookupError:
                    pass
        return False

    def notice(self):
        output = io.StringIO()
        with redirect_stdout(output):
            vpn.relay_notice()
            vpn.relay_notice()
        return output.getvalue().splitlines()

    def problems(self):
        return [told for told in self.told if told.get("do") == "problem"]

    def assert_zen(self, run):
        self.assertEqual(run.call_args.args[0], [vpn.OPEN, "-b", "app.zen-browser.zen", self.LOGIN])
        self.assertFalse(vpn.login_cancelled())
        line, = self.notice()
        self.assertIn("could not run, so UCLA login opened in Zen", line)

    def assert_web(self, run, words):
        """The web window took over: told only the URL and why, and no Zen."""
        told, show = self.told_web
        self.assertEqual(show, {"do": "show"})
        self.assertEqual(set(told), {"url", "reason"})
        self.assertEqual(told["url"], self.LOGIN)
        self.assertEqual(json.loads((self.state / "argv-web").read_text()), ["--web"])
        run.assert_not_called()
        lines = self.notice()
        if words:
            self.assertIn(words, told["reason"])
            self.assertLessEqual(len(told["reason"]), 90)
            self.assertEqual(len(lines), 1)
            self.assertIn(words, lines[0])
        else:
            self.assertEqual((told["reason"], lines), ("", []))
        return lines

    def test_approved_login_is_delivered_once(self):
        code, run = self.signin(["approve"], [self.CHALLENGE, {"callback": self.CALLBACK}])
        self.assertEqual(code, 0)
        self.assertEqual([r.split("\r\n")[0] for r in self.received], [f"GET {self.CALLBACK} HTTP/1.1"])
        self.assertEqual([call[0] for call in self.calls], [["vpn", "challenge"], ["vpn", "login", "--input-stdin"]])
        self.assertEqual(json.loads(self.calls[1][1]), {"url": self.LOGIN, "challenge": self.CHALLENGE["challenge"],
                                                        "signature": "S" * 86})
        self.assertEqual(self.told[0], {"url": self.LOGIN, "key": str(self.state / "approve-key.json"),
                                        "command": "ssh seasnet", "by": "", "agent": False})
        self.assertEqual([told["do"] for told in self.told[1:]], ["sign", "done"])
        self.assertEqual(json.loads((self.state / "argv").read_text()), [])  # No URL on a command line.
        run.assert_not_called()
        self.assertFalse(vpn.login_cancelled())
        self.assertFalse(self.alive())
        for name in ("events.log", "login-request.json", "login-attempt.json"):  # Nor in logs or state.
            text = (self.state / name).read_text()
            for secret in ("saml", "/api/sso", self.CHALLENGE["challenge"], "SSSS"):
                self.assertNotIn(secret, text)

    def test_stale_challenge_is_replaced_before_signing(self):
        code, _ = self.signin(["approve"], [{**self.CHALLENGE, "expires_in": 1}, {"challenge": "B" * 43, "expires_in": 60},
                                            {"callback": self.CALLBACK}])
        self.assertEqual(code, 0)
        self.assertEqual([told["challenge"] for told in self.told if told.get("do") == "sign"], ["B" * 43])

    def test_expired_challenge_is_replaced_once(self):
        expired = {"code": "vpn_challenge_invalid"}
        code, _ = self.signin(["approve"], [self.CHALLENGE, dict(expired), {"challenge": "B" * 43, "expires_in": 60},
                                            {"callback": self.CALLBACK}])
        self.assertEqual(code, 0)
        self.assertEqual([told["challenge"][0] for told in self.told if told.get("do") == "sign"], ["A", "B"])
        # Twice in a row is an error, and the retry is a new approval.
        code, _ = self.signin(["approve", "approve"], [
            self.CHALLENGE, dict(expired), self.CHALLENGE, dict(expired), self.CHALLENGE, {"callback": self.CALLBACK}])
        self.assertEqual(code, 0)
        self.assertEqual([p["title"] for p in self.problems()], ["The approval expired"])
        self.assertEqual(len({told["n"] for told in self.told if told.get("do") == "sign"}), 2)

    def test_error_codes_that_stay_on_the_card(self):
        fresh = "again to start fresh"
        for answer, retry, words in (({"code": "vpn_rate_limited", "retry_after_s": 42}, True, "limiting"),
                                     ({"code": "vpn_rate_limited", "retry_after_s": 720}, False, "about 12 minutes"),
                                     ({"code": "vpn_rate_limited"}, False, "limiting"),
                                     ({"code": "vpn_login_failed"}, False, fresh), ({"code": "vpn_policy_changed"}, False, fresh),
                                     ({"code": "vpn_new_code"}, False, fresh), ({"code": "Weird \u001b[31m"}, False, fresh),
                                     ({"code": "transport_error"}, False, "won’t be sent again"),
                                     ({"code": "client_error", "detail": "invalid_response"}, False, "won’t be sent again"),
                                     ({"code": "client_error", "detail": "other"}, False, "won’t be sent again"), ({"code": "unavailable"}, False, "won’t be sent again"), ("no-json", False, "won’t be sent again")):
            for on_stderr in (False, True):
                with self.subTest(answer=answer, on_stderr=on_stderr):
                    reply = {**answer, "on_stderr": on_stderr} if isinstance(answer, dict) else answer
                    result, run = self.signin(["approve", "cancel"], [self.CHALLENGE, reply])
                    self.assertEqual(result, 1)  # Cancelled from the problem.
                    self.assertTrue(vpn.login_cancelled())
                    run.assert_not_called()
                    self.assertEqual((self.received, self.told_web), ([], []))
                    self.assertEqual(len([call for call in self.calls if call[0][1] == "login"]), 1)  # Never sent twice.
                    problem, = self.problems()
                    self.assertEqual((problem["retry"], problem["web"]), (retry, True))
                    self.assertIn(words, problem["detail"])
                    self.assertEqual(problem.get("wait"), 42 if retry else None)
                    self.assertNotIn("Weird", (self.state / "events.log").read_text())

    def test_the_callback_process_lets_go_of_openconnects_listener(self):
        # As openconnect does it: the helper inherits the listening socket. Once the
        # parent closes its copy, nobody may still be listening. The URL is refused by
        # the gate, so nothing is opened; a FIFO keeps the helper alive meanwhile.
        listener = socket.socket(socket.AF_INET6)
        self.addCleanup(listener.close)
        listener.bind(("::1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        os.set_inheritable(listener.fileno(), True)
        code = ("import importlib.machinery, importlib.util, sys, time\n"
                f"l = importlib.machinery.SourceFileLoader('h', {str(source)!r})\n"
                "m = importlib.util.module_from_spec(importlib.util.spec_from_loader('h', l)); l.exec_module(m)\n"
                "m.browser_login = lambda url: (print('in', flush=True), sys.stdin.read(), 0)[2]\n"
                "sys.argv = ['seasnet-vpn', 'https://refused.invalid/']; m.main()\n")
        with tempfile.TemporaryDirectory() as home:
            child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     close_fds=False, env={**os.environ, "HOME": home})
            try:
                self.assertEqual(child.stdout.readline(), b"in\n")
                listener.close()
                with self.assertRaises(ConnectionRefusedError):
                    socket.create_connection(("::1", port), timeout=2).close()
            finally:
                child.communicate(b"", timeout=10)

    def test_error_codes_that_hand_over(self):
        for code, words in {"vpn_not_configured": "no VPN login", "vpn_action_required": "sign in yourself",
                            "vpn_signature_invalid": "know this Mac", "vpn_url_denied": "refused",
                            "vpn_callback_unrecognized": "recognize", "bad_request": "refused the request",
                            "too_large": "refused the request", "unsupported_media_type": "refused the request", "unauthorized": "grant",
                            "forbidden": "grant", "not_found": "no VPN login", "unavailable": "unavailable",
                            "vpn_private_state_unavailable": "UCLA login"}.items():
            for answers in ([{"code": code}], [self.CHALLENGE, {"code": code}]):
                if code == "unavailable" and len(answers) == 2:
                    continue  # After the login was sent it is a lost answer, not a hand-over.
                with self.subTest(code=code, answers=len(answers)):
                    result, run = self.signin(["approve"], answers)
                    self.assertEqual(result, 0)
                    lines = self.assert_web(run, words)
                    self.assertEqual("approve-key --show" in lines[0], code == "vpn_signature_invalid")
                    self.assertEqual("vlt vpn reset" in lines[0], code == "vpn_action_required")
                    self.assertEqual((self.received, self.problems()), ([], []))
                    self.assertFalse(self.alive())
        for detail, words in {"keychain_unreadable": "Keychain", "credentials_invalid": "invalid", "credentials_missing": "no VPN credentials",
                              "access_blocked": "Access blocked", "invalid_input": "couldn’t run", "invalid_arguments": "couldn’t run"}.items():
            for answers in ([], [self.CHALLENGE]):
                with self.subTest(detail=detail, answers=len(answers)):
                    result, run = self.signin(["approve"], [*answers, {"code": "client_error", "detail": detail}])
                    self.assertEqual(result, 0)
                    self.assert_web(run, words)
        for detail in ("invalid_response", "other"):  # Nothing was signed yet, so nothing is lost.
            result, run = self.signin(["approve"], [{"code": "client_error", "detail": detail}])
            self.assertEqual(result, 0)
            self.assert_web(run, "couldn’t run")
        code, run = self.signin(["approve"], [{"code": "transport_error"}])  # Before anything was signed.
        self.assertEqual(code, 0)
        self.assert_web(run, "be reached")

    def test_retry_after_a_rate_limit_is_a_new_approval(self):
        code, _ = self.signin(["approve", "approve"], [self.CHALLENGE, {"code": "vpn_rate_limited", "retry_after_s": 1},
                                                       self.CHALLENGE, {"callback": self.CALLBACK}])
        self.assertEqual(code, 0)
        self.assertEqual(len({told["n"] for told in self.told if told.get("do") == "sign"}), 2)

    def test_a_signature_that_never_comes_is_a_problem_not_a_hang(self):
        code, run = self.signin(["mute", "approve"], [self.CHALLENGE, self.CHALLENGE, {"callback": self.CALLBACK}], SIGN_SECONDS=0.5, LOGIN_TIMEOUT=5)
        self.assertEqual(code, 0)
        problem, = self.problems()
        self.assertIn("didn’t answer", problem["title"])
        self.assertTrue(problem["retry"])
        # One that does come, after the window was told, is not used.
        code, run = self.signin(["mute", "late"], [self.CHALLENGE], SIGN_SECONDS=0.5, LOGIN_TIMEOUT=3)
        self.assertEqual([call for call in self.calls if call[0][1] == "login"], [])

    def test_refused_signature_waits_for_the_next_approval(self):
        code, _ = self.signin(["refuse", "approve"], [self.CHALLENGE, self.CHALLENGE, {"callback": self.CALLBACK}])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.received), 1)

    def test_vlt_that_cannot_help_hands_over(self):
        for answers, words, constants in ((["old"], "no VPN login", {}), (["garbage"], "be reached", {}),
                                          (["hang"], "be reached", {"CHALLENGE_TIMEOUT": 0.5}),
                                          ([{"challenge": "short"}], "unexpected", {}),
                                          ([], "be reached", {"VLT": "/nonexistent/vlt"})):
            with self.subTest(answers=answers):
                code, run = self.signin(["approve"], answers, **constants)
                self.assertEqual(code, 0)
                self.assert_web(run, words)
                self.assertEqual(self.received, [])
                self.assertFalse(self.alive())

    def test_no_key_goes_straight_to_the_web_window(self):
        code, run = self.signin(["approve"], key=False)
        self.assertEqual(code, 0)
        line, = self.assert_web(run, "No Touch ID approval key")
        self.assertIn("approve-key --create", line)
        self.assertEqual((self.told, self.calls), ([], []))  # No card, no vlt.

    def test_the_card_hands_over(self):
        for why, words in (("chosen", ""), ("touchid", "Touch ID isn’t available"), ("fingerprints", "No fingerprints"),
                           ("key", "no longer works"), ("keyfile", "private"), ("refused", "refused the touch"),
                           ("nonsense", "couldn’t run")):
            for answers in ([self.CHALLENGE], ["hang"]):  # Even while vlt is still answering.
                with self.subTest(why=why, answers=answers):
                    code, run = self.signin([0.1, f"web:{why}"], answers)
                    self.assertEqual(code, 0)
                    self.assert_web(run, words)
                    self.assertFalse(self.alive())
                    self.assertFalse([told for told in self.told if told.get("do") == "sign"])

    def test_the_card_stays_until_the_web_window_is_prepared(self):
        seen = []

        def look():
            os.kill(int((self.state / "pid").read_text()), 0)
            seen.append(not (self.state / "shown").exists())
        code, _ = self.signin(["web:touchid"], [self.CHALLENGE], web=("silent", 1.2, "prepared", "show", "ready", "deliver"),
                              before=lambda: self.later(0.9, look))
        self.assertEqual((code, seen), (0, [True]))
        self.assertFalse(self.alive())

    def test_cancel_during_the_hand_over_always_wins(self):
        # However the web window then fares: ready, slow, never ready, or dead.
        for web in (("deliver",), ("silent", 0.8, "prepared", "show", "ready", "deliver"), ("silent", 30), ("silent",), ("crash",)):
            with self.subTest(web=web):
                began = time.monotonic()
                code, run = self.signin(["problem", "cancel"], [{"code": "vpn_action_required"}], web=web, SIGNIN_READY_SECONDS=2)
                self.assertEqual(code, 1)
                self.assertTrue(vpn.login_cancelled())
                run.assert_not_called()  # No Zen.
                self.assertFalse((self.state / "shown").exists())  # Not even a flash.
                self.assertFalse(self.alive())
                self.assertLess(time.monotonic() - began, 4)

    def test_cancel_wins_when_the_web_window_cannot_even_start(self):
        real, starts = subprocess.Popen, []

        def popen(args, *rest, **kw):
            if "--web" in args:
                time.sleep(0.5)  # The card has cancelled by now.
                raise OSError("cannot start")
            starts.append(args)
            return real(args, *rest, **kw)
        with patch.object(vpn.subprocess, "Popen", side_effect=popen):
            code, run = self.signin(["problem", "cancel"], [{"code": "vpn_action_required"}])
        self.assertEqual(code, 1)
        self.assertTrue(starts and vpn.login_cancelled())
        run.assert_not_called()

    def test_cancel_in_the_web_window(self):
        code, run = self.signin(["web:touchid"], [self.CHALLENGE], web=("cancel",))
        self.assertEqual(code, 1)
        self.assertTrue(vpn.login_cancelled())
        run.assert_not_called()

    def test_end_of_attempt_closes_the_web_window(self):
        code, run = self.signin(["web:key"], [self.CHALLENGE], web=("hang",),
                                before=lambda: self.later(1, lambda: (self.state / "login-attempt.json").unlink()))
        self.assertEqual(code, 1)
        self.assertFalse(self.alive())
        self.assertFalse(vpn.login_cancelled())
        run.assert_not_called()

    def test_the_web_window_cannot_reach_vlt_or_the_key(self):
        # Whatever it says, only "ready" and "delivered" mean anything.
        self.WINDOW = self.WINDOW.replace('if step == "ready": emit(event="ready")', """if step == "ready":
        emit(event="ready")
        if W: emit(event="approved", n="1"); emit(event="signed", challenge="A" * 43, signature="S" * 86); emit(event="web", why="chosen")""")
        code, run = self.signin(["web:key"], [self.CHALLENGE], web=("deliver",))
        self.assertEqual(code, 0)
        self.assert_web(run, "no longer works")
        self.assertFalse(vpn.login_cancelled())
        self.assertEqual([call[0] for call in self.calls if call[0][1] == "login"], [])
        self.assertEqual(self.received, [])
        self.assertNotIn("approve-key", json.dumps(self.told_web))

    def test_closing_the_window_cancels_at_once(self):
        for steps, answers in ((["cancel"], [self.CHALLENGE]), ([0.1, "cancel"], ["hang"])):
            began = time.monotonic()
            code, run = self.signin(steps, answers)
            self.assertEqual(code, 1)
            self.assertTrue(vpn.login_cancelled())
            run.assert_not_called()
            self.assertLess(time.monotonic() - began, 3)

    def test_end_of_attempt_closes_the_window(self):
        # What Ctrl-C and the start timeout both do: drop the attempt marker.
        def ctrl_c():
            (self.state / "login-attempt.json").unlink()
        for answers in ([self.CHALLENGE], ["hang"]):
            code, run = self.signin(["hang"], answers, before=lambda: self.later(0.6, ctrl_c))
            self.assertEqual(code, 1)
            self.assertFalse(self.alive())
            self.assertFalse(vpn.login_cancelled())
            run.assert_not_called()

    def test_timeout_and_a_stubborn_window(self):
        code, run = self.signin([30], [self.CHALLENGE], LOGIN_TIMEOUT=1)
        self.assertEqual(code, 1)
        self.assertFalse(self.alive())
        run.assert_not_called()

    def test_nothing_is_delivered_after_the_attempt_ended(self):
        with patch.object(vpn, "callback_ok", side_effect=lambda c: (self.state / "login-attempt.json").unlink() or True):
            code, run = self.signin(["approve"], [self.CHALLENGE, {"callback": self.CALLBACK}])
        self.assertEqual((code, self.received), (1, []))
        run.assert_not_called()

    def test_a_card_that_cannot_run_gives_way_to_the_web_window(self):
        for steps in (["silent"], ["crash"], ["silent", 30]):
            with self.subTest(steps=steps):
                code, run = self.signin(steps, [self.CHALLENGE], SIGNIN_READY_SECONDS=1)
                self.assertEqual(code, 0)
                self.assert_web(run, "couldn’t run")
                self.assertFalse(self.alive())

    def test_only_a_window_that_cannot_run_goes_to_zen(self):
        for web in (("silent",), ("crash",), ("silent", 30)):
            for steps in (["web:key"], ["crash"]):
                with self.subTest(web=web, steps=steps):
                    code, run = self.signin(steps, [self.CHALLENGE], web=web, SIGNIN_READY_SECONDS=1)
                    self.assertEqual(code, 0)
                    self.assert_zen(run)
                    self.assertFalse(self.alive())

    def test_openconnect_that_stopped_listening(self):
        code, run = self.signin(["approve", "cancel"], [self.CHALLENGE, {"callback": self.CALLBACK}], listener=None)
        self.assertEqual(code, 1)
        problem, = self.problems()
        self.assertEqual((problem["title"], problem["retry"], problem["web"]), ("The VPN stopped waiting", False, False))
        code, run = self.signin(["approve"], [self.CHALLENGE, {"callback": self.CALLBACK}], listener="404 Not Found")
        self.assertEqual(code, 0)
        self.assert_web(run, "recognize")
        self.assertIn("openconnect answered 404", (self.state / "events.log").read_text())

    def test_unanswered_delivery_is_unknown_not_gone(self):
        for steps, answers, web in ((["approve"], [self.CHALLENGE, {"callback": self.CALLBACK}], ("deliver",)),
                                    (["web:key"], [self.CHALLENGE], ("unknown",))):
            with self.subTest(steps=steps):
                # openconnect moved on: the login stands, whatever its answer was.
                with patch.object(vpn, "callback_listeners", return_value=[]):
                    code, run = self.signin(steps, answers, web=web, listener="silence")
                self.assertEqual(code, 0)
                run.assert_not_called()
                self.assertFalse(vpn.login_cancelled())
                # Still listening: not delivered as far as anyone can tell.
                with patch.object(vpn, "callback_listeners", return_value=[7]):
                    code, run = self.signin(steps, answers, web=web, listener="silence", PROCEED_SECONDS=0.5)
                self.assertEqual(code, 1)
                run.assert_not_called()
                self.assertTrue(vpn.login_cancelled())
                line, = self.notice()
                self.assertIn("unknown whether", line)
                self.assertEqual(self.problems(), [])
                if steps == ["approve"]:
                    self.assertEqual((len(self.received), self.told_web), (1, []))
                self.assertFalse(self.alive())

    def test_trace_keeps_the_chain_and_nothing_private(self):
        good = {"event": "trace", "kind": "nav", "ms": "1234", "verdict": "handoff", "method": "POST", "frame": "main", "by": "form",
                "origin": "https://v2.ssl.vpn.ucla.edu", "path": "/+CSCOE+/saml/sp/acs"}
        secret = {name: "SECRETTOKEN?return=x#f" for name in vpn.SignInTrace.FIELDS}
        sneaky = {**good, "origin": "https://v2.ssl.vpn.ucla.edu/SECRET", "path": "/+CSCOE+/SECRET", "ms": "12SECRET", "extra": "SECRET",
                  "by": ["SECRET"], "verdict": "handoff\nSECRET"}
        lost = {"event": "trace", "kind": "lost", "ms": "9", "lost": "refused", "status": "404"}
        for attempt in range(5):
            code, _ = self.signin(["web:key"], [self.CHALLENGE], web=(good, {"event": "trace", **secret}, sneaky, lost, "deliver"))
            self.assertEqual(code, 0)
        trace = self.state / "signin-trace.log"
        self.assertEqual(trace.stat().st_mode & 0o777, 0o600)
        text = trace.read_text()
        self.assertNotIn("SECRET", text)
        self.assertEqual(text.count("== "), 3)  # The last three attempts.
        self.assertEqual(text.count("ms=1234 kind=nav verdict=handoff method=POST frame=main by=form "
                                    "origin=https://v2.ssl.vpn.ucla.edu path=/+CSCOE+/saml/sp/acs\n"), 3)
        self.assertIn("web sign-in lost: status=404 lost=refused", (self.state / "events.log").read_text())
        code, _ = self.signin(["web:key"], [self.CHALLENGE], web=(*[good] * 320, "deliver"))
        self.assertLess(len(trace.read_text().split("== ")[-1].splitlines()), 305)
        # The card has nothing to trace.
        before = trace.read_text()
        self.signin([good, "approve"], [self.CHALLENGE, {"callback": self.CALLBACK}])
        self.assertEqual(trace.read_text(), before)
        # Every path the window may name is one the trace accepts.
        swift = (source.parents[2] / "dot_config/seasnet-vpn/signin/Web.swift")
        swift = swift if swift.exists() else Path.home() / ".config/seasnet-vpn/signin/Web.swift"
        listed = re.findall(r'"(/[^"]*)"', swift.read_text().split("static let paths")[1].split("]\n")[0])
        self.assertGreater(len(listed), 10)
        for path in listed:
            self.assertTrue(re.fullmatch(vpn.SignInTrace.FIELDS["path"], path), path)

    ZEN = {"event": "trace", "kind": "zen", "browser": "zen"}

    def openconnect_moves_on(self, after, post=True, close=True):
        """What openconnect does once the browser's GET reached it: closes its listener, then logs its next POST."""
        def act():
            if close:
                self.listening.clear()
            if post:
                with (self.state / "openconnect.log").open("a") as log:
                    log.write("POST https://v2.ds.ssl.vpn.ucla.edu/\n")
            self.moved = time.monotonic()
        self.later(after, act)

    def zen(self, web, **kw):
        self.listening = {"openconnect"}
        with patch.object(vpn, "callback_listeners", side_effect=lambda: list(self.listening and [1234])):
            return self.signin(["web:key"], [self.CHALLENGE], web=web, **kw)

    def test_browser_route_closes_the_moment_the_result_lands(self):
        self.openconnect_moves_on(0.6)
        code, run = self.zen(("zen", "await-done"))
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertFalse(vpn.login_cancelled())
        self.assertEqual(self.told_web[-1], {"do": "done"})
        latency = float((self.state / "done-at").read_text()) - self.moved
        self.assertLess(latency, 0.15)  # kqueue on openconnect's own write, not the 0.2 s poll.
        trace = (self.state / "signin-trace.log").read_text()
        self.assertIn("kind=zen zen=started browser=zen", trace)
        self.assertRegex(trace, r"ms=\d+ kind=landed\n")
        self.assertIn("the browser's result reached openconnect", (self.state / "events.log").read_text())

    def test_browser_route_needs_both_signs(self):
        for post, close in ((True, False), (False, True)):
            with self.subTest(post=post, close=close):
                self.openconnect_moves_on(0.3, post=post, close=close)
                code, run = self.zen(("zen", "await-done"), LOGIN_TIMEOUT=1.5)
                self.assertEqual(code, 1)  # Timed out: not done, and nothing claimed.
                self.assertNotIn({"do": "done"}, self.told_web)
        # The POSTs logged before this sign-in began don't count.
        self.openconnect_moves_on(0.3, post=False)
        code, _ = self.zen(("zen", "await-done"), LOGIN_TIMEOUT=1.5)
        self.assertNotIn({"do": "done"}, self.told_web)

    def test_browser_route_cancel_and_late_close(self):
        code, run = self.zen(("zen", 0.3, "cancel"))
        self.assertEqual(code, 1)
        self.assertTrue(vpn.login_cancelled())
        run.assert_not_called()
        # Zen's window closed by hand just after the result landed: still a login.
        self.openconnect_moves_on(0.2)
        code, run = self.zen(("zen", 0.6, "cancel"))
        self.assertEqual(code, 0)
        self.assertFalse(vpn.login_cancelled())

    def test_browser_fallback_is_our_window_and_logged(self):
        for state in ("unstartable", "error", "matched", "chosen-here", "unsupported", "no-browser"):
            with self.subTest(state=state):
                (self.state / "events.log").unlink(missing_ok=True)
                self.openconnect_moves_on(0.2)
                code, run = self.zen(("zen", {**self.ZEN, "zen": state}, 0.6, "deliver"))
                self.assertEqual(code, 0)
                run.assert_not_called()
                events = (self.state / "events.log").read_text()
                self.assertNotIn("reached openconnect", events)  # Our own window delivers for itself.
                self.assertIn(f"the browser can't take it ({state})", events)

    def test_no_busy_loop_on_openconnect_writes(self):
        """The log keeps growing (tunnel setup) after done, and on routes that never watch it."""
        real, calls = select.select, []

        def counted(*args):
            calls.append(1)
            return real(*args)

        def chatter():
            for _ in range(20):
                with (self.state / "openconnect.log").open("a") as log:
                    log.write("POST https://v2.ds.ssl.vpn.ucla.edu/\nCSTP connected.\n")
                time.sleep(0.05)
        for web in (("zen", "await-done-and-linger"), (2.0, "deliver"), ({**self.ZEN, "zen": "chosen-here"}, 2.0, "deliver")):
            with self.subTest(web=web):
                calls.clear()
                if web[0] == "zen":
                    self.openconnect_moves_on(0.2)
                self.later(0.3, chatter)
                with patch.object(vpn.select, "select", side_effect=counted):
                    began = time.monotonic()
                    code, _ = self.zen(web)
                    took = time.monotonic() - began
                self.assertEqual(code, 0)
                # A 0.2 s poll plus a wake per event; a spinning select would be thousands.
                self.assertLess(len(calls), took / 0.2 + 60, f"{len(calls)} selects in {took:.1f} s")

    def test_landing_reads_only_whats_new_and_wakes_on_write(self):
        log = self.state / "openconnect.log"
        log.write_text("POST https://ssl.vpn.ucla.edu/\n")
        listening = [1]
        with patch.object(vpn, "callback_listeners", side_effect=lambda: listening):
            landing = vpn.Landing()
            self.addCleanup(landing.close)
            self.assertFalse(landing.landed())
            listening.clear()
            self.assertFalse(landing.landed())  # The old POST is not this sign-in's.
            self.assertEqual(select.select([landing], [], [], 0)[0], [])
            with log.open("a") as out:
                out.write("POST https://v2.ds.ssl.vpn.ucla.edu/\n")
            self.assertEqual(select.select([landing], [], [], 1)[0], [landing])
            self.assertTrue(landing.landed())
            listening.append(1)
            self.assertFalse(landing.landed())  # Still listening: not yet.
        with patch.object(vpn, "callback_listeners", side_effect=OSError):
            self.assertTrue(landing.landed())  # The log alone, if lsof can't say.
        log.unlink()
        self.assertFalse(vpn.Landing().landed())  # No log (browser-login by hand): never claims.

    def test_proceeding_is_seen_from_the_listener_alone(self):
        with socket.create_server(("::1", 0), family=socket.AF_INET6) as server, \
                patch.object(vpn, "CALLBACK_PORT", server.getsockname()[1]), patch.object(vpn, "PROCEED_SECONDS", 0.2):
            self.assertEqual(vpn.callback_listeners(), [os.getpid()])
            self.assertFalse(vpn.openconnect_proceeded())
            server.close()
            self.assertTrue(vpn.openconnect_proceeded())

    def test_cancel_between_vlts_answer_and_the_get(self):
        real = vpn.vlt

        def vlt(window, *args, **kw):
            result = real(window, *args, **kw)
            if args[0] == "login":
                window.process.kill()
                window.process.wait()
                window.process.returncode = vpn.SIGNIN_CANCELLED
            return result
        with patch.object(vpn, "vlt", side_effect=vlt):
            code, run = self.signin(["approve"], [self.CHALLENGE, {"callback": self.CALLBACK}])
        self.assertEqual((code, self.received), (1, []))
        self.assertTrue(vpn.login_cancelled())
        run.assert_not_called()

    def test_cancelling_a_login_already_sent_says_so(self):
        sent = self.state / "login-sent"
        real = vpn.vlt

        def vlt(window, *args, **kw):
            if args[0] == "login":
                self.later(0.8, lambda: os.kill(int((self.state / "pid").read_text()), signal.SIGKILL))
            return real(window, *args, **kw)
        with patch.object(vpn, "vlt", side_effect=vlt):
            self.signin(["approve"], [self.CHALLENGE, "hang"], web=("cancel",))
        self.assertEqual(sent.read_text(), "a\n")  # Not settled: vlt may still be at it.
        for attempt, said in (("other", 0), ("a", 1), ("a", 0)):
            errors = io.StringIO()
            with redirect_stderr(errors), patch.object(vpn, "kill_strays"), patch.object(vpn, "owned_pid", return_value=None):
                vpn.abandon_login(attempt, None)
            self.assertEqual(errors.getvalue().count("may still finish at UCLA"), said)
        # An answered login is settled.
        self.signin(["approve"], [self.CHALLENGE, {"callback": self.CALLBACK}])
        self.assertFalse(sent.exists())

    def test_only_the_first_signal_interrupts(self):
        before = [(number, signal.getsignal(number)) for number in vpn.STOP_SIGNALS]
        try:
            for first in vpn.STOP_SIGNALS:
                vpn.interrupt_on_hangup()
                with self.assertRaises(KeyboardInterrupt):
                    signal.raise_signal(first)
                for number in vpn.STOP_SIGNALS:
                    signal.raise_signal(number)
        finally:
            for number, handler in before:
                signal.signal(number, handler)

    def test_a_refused_url_or_a_crash_ends_the_attempt_at_once(self):
        code, run = self.signin([], url="https://shb.ais.ucla.edu/login")
        self.assertEqual(code, 1)
        self.assertTrue(vpn.login_cancelled())
        line, = self.notice()
        self.assertIn("isn’t UCLA’s VPN login", line)
        for error in (KeyError("secret-url"), RecursionError("secret-url")):
            with patch.object(vpn, "run_signin", side_effect=error):
                code, run = self.signin(["approve"])
            self.assertEqual(code, 1)
            self.assertTrue(vpn.login_cancelled())
            line, = self.notice()
            self.assertIn("stopped unexpectedly", line)
            self.assertNotIn("secret-url", line + (self.state / "events.log").read_text())
        # A failed attempt that is over stays quiet, and never fails its successor.
        vpn.write_json(self.state / "login-attempt.json", {"id": "new"})
        (self.state / "login-result.json").unlink()
        vpn.fail_attempt("old", "x")
        self.assertFalse((self.state / "login-result.json").exists() or (self.state / "login-notice").exists())

    def test_diagnostic_login_says_who_asks(self):
        with patch.object(vpn, "run_signin", return_value="stale") as window, patch.dict(os.environ), \
                patch.object(vpn, "who_asks", return_value={"command": "x", "by": "An agent", "agent": True}):
            os.environ.pop("SEASNET_VPN_LOGIN_ATTEMPT", None)
            vpn.browser_login(self.LOGIN)
        self.assertEqual(window.call_args.args[2], {"command": "seasnet-vpn browser-login", "by": "An agent", "agent": True})

    def test_the_environment_cannot_swap_programs(self):
        code = ("import importlib.machinery as m, importlib.util as u; l = m.SourceFileLoader('v', %r); "
                "v = u.module_from_spec(u.spec_from_loader('v', l)); l.exec_module(v); "
                "print(v.OPENCONNECT, v.OCPROXY, v.OPEN, v.VLT, v.SIGNIN_APP, v.APPROVE_KEY)" % str(source))
        runs = [subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, env=env).stdout
                for env in (dict(os.environ), {**os.environ, "SEASNET_VPN_TEST_BIN": str(self.state)})]
        self.assertTrue(runs[0].strip())
        self.assertEqual(runs[0], runs[1])

    def test_callback_validation(self):
        self.assertTrue(vpn.callback_ok(self.CALLBACK))
        for host in ("ssl.vpn.ucla.edu", "v2.ssl.vpn.ucla.edu:443", "v2.ssl.vpn.ucla.edu:"):
            self.assertTrue(vpn.callback_ok("/api/sso/" + "QUFB" * 8 + f"?return=https://{host}/x?y=z"))
        self.assertTrue(vpn.callback_ok("/api/sso/" + "QUFB" * 8 + "?return=https://v2.ssl.vpn.ucla.edu/+CSCOE+/x.html"))
        token, page = "QUFB" * 8, "https%3A%2F%2Fv2.ssl.vpn.ucla.edu%2Fx"
        for bad in (None, 5, "", "/api/sso/", f"/api/sso/{token}", f"/api/sso/?return={page}", f"/other/{token}?return={page}",
                    f"http://localhost:29786/api/sso/{token}?return={page}", f"/api/sso/{token}?return=",
                    f"/api/sso/{token}?x=1&return={page}", f"/api/sso/{token}?return={page}&x=1", f"/api/sso/{token}?return={page}#f",
                    f"/api/sso/!!!?return={page}", f"/api/sso/QUFB?return={page}", f"/api/sso/{token[:-1]}?return={page}",
                    f"/api/sso/{token}?return=http%3A%2F%2Fv2.ssl.vpn.ucla.edu%2F", f"/api/sso/{token}?return=https%3A%2F%2Fevil.com%2F",
                    f"/api/sso/{token}?return=https%3A%2F%2Fv2.ssl.vpn.ucla.edu.evil.com%2F",
                    f"/api/sso/{token}?return=https%3A%2F%2Fevilvpn.ucla.edu%2F", f"/api/sso/{token}?return=https%3A%2F%2Fvpn.ucla.edu%2F",
                    f"/api/sso/{token}?return=https%3A%2F%2Fwww.cisco.com%2F", f"/api/sso/{token}?return=https%3A%2F%2Fv3.ssl.vpn.ucla.edu%2F",
                    f"/api/sso/{token}?return={page}%23f", f"/api/sso/{token}?return={page}%23", f"/api/sso/{token}?return={page}%5c",
                    f"/api/sso/{token}?return={page}%7f", f"/api/sso/{token}?return={page}%20", f"/api/sso/{token}?return={page}%00",
                    f"/api/sso/{token}?return=https%3A%2F%2Fv2.ssl.vpn.ucla.edu%3A8443%2F",
                    f"/api/sso/{token}?return=https%3A%2F%2F%40v2.ssl.vpn.ucla.edu%2F", f"/api/sso/{token}?return=HTTPS%3A%2F%2Fv2.ssl.vpn.ucla.edu%2F",
                    f"/api/sso/{token}?return=https%3A%2F%2Fv2.ssl.vpn.ucla.edu.%2F", f"/api/sso/{token}?return={page}%zz",
                    f"/api/sso/{token}?return=" + "https%3A%2F%2Fv2.ssl.vpn.ucla.edu%2F" + "a" * 1100, f"/api/sso/{token}?return=https%3A%2F%2Fa%40v2.ssl.vpn.ucla.edu%2F",
                    f"/api/sso/{token}?return={page}%0d%0aX:%20y", f"/api/sso/{token}?return={page} HTTP/1.1\r\nX: y",
                    f"/api/sso/{token}\n?return={page}", f"/api/sso/{token}?return={page}\n", f"/api/sso/{token}?return=javascript:1",
                    f"/api/sso/{token}/../x?return={page}", f"/api/sso/{token * 200}?return={page}", f"/api/sso/{token}?return=%ff"):
            with self.subTest(bad=bad):
                self.assertFalse(vpn.callback_ok(bad))
        for bad in ("/api/sso/x", {"a": 1}, None):
            code, run = self.signin(["approve"], [self.CHALLENGE, {"callback": bad}])
            self.assertEqual((code, self.received), (0, []))
            self.assert_web(run, "recognize")

    def test_killed_start_is_not_current(self):
        child = subprocess.Popen(["/usr/bin/true"])
        child.wait()
        vpn.write_json(self.state / "login-attempt.json", {"id": "a", "pid": child.pid})
        self.assertFalse(vpn.attempt_current("a"))

    def test_build_failure_falls_back_to_zen(self):
        vpn.write_json(self.state / "login-attempt.json", {"id": "a", "asker": {"command": "ssh seasnet"}})
        (self.state / "approve-key.json").write_text("{}")
        for error in (OSError("no source"), subprocess.CalledProcessError(1, "swiftc")):
            with patch.dict(os.environ, {"SEASNET_VPN_LOGIN_ATTEMPT": "a"}), \
                    patch.object(vpn, "APPROVE_KEY", self.state / "approve-key.json"), \
                    patch.object(vpn, "build_signin", side_effect=error), \
                    patch.object(vpn.subprocess, "run", return_value=Mock(returncode=0)) as run:
                self.assertEqual(vpn.browser_login(self.LOGIN), 0)
            self.assert_zen(run)

    def test_zen_failure_reports_cancellation(self):
        vpn.write_json(self.state / "login-attempt.json", {"id": "a", "asker": {"command": "ssh seasnet"}})
        for outcome in (Mock(returncode=1), OSError("no open")):
            with self.subTest(outcome=outcome), patch.dict(os.environ, {"SEASNET_VPN_LOGIN_ATTEMPT": "a"}), \
                    patch.object(vpn, "run_signin", side_effect=vpn.UseZen), \
                    patch.object(vpn.subprocess, "run", side_effect=[outcome] if isinstance(outcome, Exception) else None,
                                 return_value=outcome), redirect_stderr(io.StringIO()):
                self.assertEqual(vpn.browser_login(self.LOGIN), 1)
                self.assertTrue(vpn.login_cancelled())

    def test_only_one_window(self):
        vpn.write_json(self.state / "login-attempt.json", {"id": "a", "asker": {"command": "ssh seasnet"}})
        with vpn.signin_slot(), patch.dict(os.environ, {"SEASNET_VPN_LOGIN_ATTEMPT": "a"}), \
                patch.object(vpn, "run_signin") as window, patch.object(vpn.subprocess, "run") as run, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(vpn.browser_login(self.LOGIN), 1)
        window.assert_not_called()
        run.assert_not_called()
        self.assertTrue(vpn.login_cancelled())

    def test_unexpected_urls_open_nothing(self):
        base = "https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login"
        for url in ("http://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login", "https://shb.ais.ucla.edu/login",
                    "https://v2.ssl.vpn.ucla.edu.evil.com/+CSCOE+/saml/sp/login", "https://v2.ssl.vpn.ucla.edu/",
                    base + "/x", base.replace("sp/", "sp/../sp/"), base.replace("https://", "https://a:b@"),
                    base.replace(".edu", ".edu:444"), base.replace(".edu", ".edu:x"), base + " x",
                    base + "\nhttps://evil.com", "file:///etc/hosts"):
            with self.subTest(url=url):
                code, run = self.signin(["approve"], url=url)
                self.assertEqual(code, 1)
                run.assert_not_called()
                self.assertEqual((self.told, self.told_web, self.calls), ([], [], []))
        self.assertTrue(vpn.login_url_ok(self.LOGIN))

    def test_start_builds_before_login_and_survives_a_failed_build(self):
        auth = Mock(returncode=1)
        auth.poll.return_value = 1
        self.auth_context(auth)
        with patch.object(vpn, "build_signin", side_effect=OSError("no swiftc")) as build, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(vpn.start(), 1)
        build.assert_called_once()

    def test_who_is_asking(self):
        self.assertEqual(vpn.asking_command(["-zsh", "/opt/homebrew/bin/tmux"]), "seasnet-vpn start")
        self.assertEqual(vpn.asking_command(["/bin/sh -c x", "ssh -o ServerAliveInterval=5 seasnet secret-arg", "-zsh"]), "ssh seasnet")
        self.assertEqual(vpn.asking_command(["/usr/bin/ssh other"]), "ssh")
        self.assertEqual(vpn.asking_command(["ssh seasnet", "/x/python3 /Users/m/.local/bin/relayctl push --token s3cret", "-zsh"]),
                         "relayctl push")
        self.assertEqual(vpn.asking_command(["ssh seasnet", "/x/relayctl --token s3cret"]), "relayctl")
        table = {10: (9, "python3"), 9: (8, "zsh"), 8: (1, "tmux"), 70: (8, "2.1.331")}
        args = {9: "-zsh", 8: "/opt/homebrew/bin/tmux", 10: "python3", 70: "claude --resume x"}

        def ps(argv, **_):
            pids = [int(p) for p in argv[argv.index("-p") + 1].split(",")]
            wide = "pid=,args=" in argv
            return SimpleNamespace(stdout="".join(f"{p} {args[p]}\n" if wide else args[p] for p in pids if p in args))

        def tmux(*argv):
            return {"show-options": "70\n", "list-panes": "8 d\n", "display-message": "main\n"}[argv[0]]

        clean = {k: v for k, v in os.environ.items() if k not in
                 ("AGENT_PTY", "AGENT_SHELL", "OPENCODE", "OPENCODE_TERMINAL", "CLAUDECODE")}
        with patch.object(vpn, "process_table", return_value=table), patch.object(vpn, "capture", side_effect=ps), \
                patch.object(vpn, "tmux", side_effect=tmux), patch.object(vpn.os, "getpid", return_value=10):
            with patch.dict(os.environ, {**clean, "TMUX_PANE": "%1"}, clear=True):
                self.assertEqual(vpn.who_asks(), {"command": "seasnet-vpn start", "by": "In the main space", "agent": False})
            with patch.dict(os.environ, {**clean, "TMUX_PANE": "%1", "AGENT_PTY": "1"}, clear=True):
                self.assertEqual(vpn.who_asks()["by"], "An agent: Claude, agent D")
            with patch.dict(os.environ, {**clean, "TMUX_PANE": "bad", "AGENT_PTY": "1"}, clear=True):
                self.assertEqual(vpn.who_asks()["by"], "An agent")
            with patch.dict(os.environ, {**clean, "TMUX_PANE": ""}, clear=True):
                self.assertEqual(vpn.who_asks()["by"], "")
            with patch.object(vpn, "process_table", side_effect=OSError):
                self.assertEqual(vpn.who_asks()["command"], "seasnet-vpn start")

    def test_approve_key_replaces_only_when_told_and_restores_on_failure(self):
        key = self.state / "share/approve-key.json"
        tool = self.state / "tool"

        def run(code, args):
            tool.write_text(f'#!/bin/sh\n[ -e "$2" ] && exit 3\n[ {code} = 0 ] && echo new > "$2"\nexit {code}\n')
            tool.chmod(0o700)
            with patch.object(vpn, "APPROVE_KEY", key), patch.object(vpn, "fresh_signin", lambda: nullcontext(tool)), \
                    patch.object(vpn, "build_signin", side_effect=AssertionError("--create must build fresh")):
                return vpn.approve_key(args)

        self.assertEqual(run(0, ["--create"]), 0)
        key.write_text("old")
        self.assertEqual(run(0, ["--create"]), 3)
        self.assertEqual(key.read_text(), "old")
        self.assertEqual(run(1, ["--create", "--replace"]), 1)
        self.assertEqual(key.read_text(), "old")
        self.assertEqual(run(0, ["--create", "--replace"]), 0)
        self.assertEqual(key.read_text(), "new\n")
        self.assertEqual([p.read_text() for p in key.parent.glob("*.old.json")], ["old"])

    def test_expired_callback_never_launches(self):
        vpn.write_json(self.state / "login-attempt.json", {"id": "new"})
        with patch.dict(os.environ, {"SEASNET_VPN_LOGIN_ATTEMPT": "old"}), \
                patch.object(vpn.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(vpn.browser_login(self.LOGIN), 1)
            run.assert_not_called()
            self.assertFalse((self.state / "login-request.json").exists())

    def test_verification_exception_rolls_back_new_daemon(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88, 88]))
        with patch.object(vpn, "listener_ready", return_value=True), \
                patch.object(vpn, "verify", side_effect=subprocess.TimeoutExpired("netstat", 15)):
            with self.assertRaises(subprocess.TimeoutExpired):
                vpn.start()
        stop.assert_called_once()
        self.assertFalse((self.state / "login-attempt.json").exists())

    def test_cancel_after_auth_exit_rejects_daemon(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88]))
        with patch.object(vpn, "login_cancelled", return_value=True), patch.object(vpn, "verify") as verify:
            self.assertEqual(vpn.start(), 1)
            verify.assert_not_called()
        stop.assert_called_once()

    def test_failed_result_write_cannot_skip_daemon_cleanup(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88, 88]))
        with patch.object(vpn, "listener_ready", return_value=True), \
                patch.object(vpn, "verify", side_effect=subprocess.TimeoutExpired("netstat", 15)), \
                patch.object(vpn, "write_login_result", side_effect=OSError("disk full")):
            with self.assertRaises(subprocess.TimeoutExpired):
                vpn.start()
        stop.assert_called_once()

    def test_connected_result_write_failure_rolls_back(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88, 88]))
        with patch.object(vpn, "listener_ready", return_value=True), \
                patch.object(vpn, "verify", return_value=0), patch.object(vpn, "ensure_idle_watch"), \
                patch.object(vpn, "write_login_result", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                vpn.start()
        stop.assert_called_once()

    def test_attempt_marker_unlink_failure_cannot_skip_cleanup(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88, 88]))
        unlink = Path.unlink

        def guarded_unlink(path, *args, **kwargs):
            if path.name == "login-attempt.json":
                raise PermissionError("state became unwritable")
            return unlink(path, *args, **kwargs)

        with patch.object(vpn, "listener_ready", return_value=True), \
                patch.object(vpn, "verify", side_effect=subprocess.TimeoutExpired("netstat", 15)), \
                patch.object(Path, "unlink", guarded_unlink):
            with self.assertRaises(subprocess.TimeoutExpired):
                vpn.start()
        stop.assert_called_once()

    def test_unresponsive_auth_is_reaped_on_cancel(self):
        auth = Mock(returncode=None)
        auth.poll.return_value = None
        auth.wait.side_effect = [subprocess.TimeoutExpired("openconnect", 10), 0]
        self.auth_context(auth)
        with patch.object(vpn, "login_cancelled", return_value=True):
            self.assertEqual(vpn.start(), 1)
        auth.terminate.assert_called_once()
        auth.kill.assert_called_once()
        self.assertEqual(auth.wait.call_args_list[1].kwargs, {"timeout": 5})

    def test_idle_releases_singleton_before_control_lock(self):
        events = []
        real_flock = fcntl.flock

        @contextmanager
        def control():
            events.append("control-enter")
            yield
            events.append("control-exit")

        def flock(file, operation):
            if operation == fcntl.LOCK_UN:
                events.append("idle-unlock")
            return real_flock(file, operation)

        with patch.object(vpn, "control_lock", control), patch.object(vpn, "owned_pid", return_value=None), \
                patch.object(vpn.fcntl, "flock", side_effect=flock):
            self.assertEqual(vpn.idle_watch(), 0)
        self.assertLess(events.index("idle-unlock"), events.index("control-exit"))
        self.assertFalse((self.state / "idle.pid").exists())

    def hold_lease(self, kind):
        leases = self.state / "connections"
        leases.mkdir(exist_ok=True)
        lease = leases / kind
        script = ("import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); "
                  "f.write(sys.argv[2]+'\\n'); f.flush(); print('ready',flush=True); sys.stdin.read()")
        child = subprocess.Popen([sys.executable, "-c", script, str(lease), kind], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and (child.terminate(), child.communicate(timeout=5)))
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child, lease

    def test_real_lease_protects_connection_then_cleans(self):
        child, lease = self.hold_lease("ssh")
        self.assertEqual(vpn.transports(clean=True), ["ssh"])
        self.assertTrue(lease.exists())
        child.communicate(timeout=5)
        self.assertEqual(vpn.transports(clean=True), [])
        self.assertFalse(lease.exists())

    def test_mutagen_transport_does_not_hold_idle_timer(self):
        self.hold_lease("mutagen")
        activity = self.state / "last-used"
        activity.touch()
        os.utime(activity, (0, 0))
        with patch.object(vpn, "tunnel_sessions", return_value=[]):
            idle, holders = vpn.idle_state()
        self.assertEqual(holders, [])
        self.assertGreater(idle, 10**8)
        self.hold_lease("interactive")
        with patch.object(vpn, "tunnel_sessions", return_value=[]):
            self.assertEqual(vpn.idle_state(), (0, ["interactive"]))
        self.assertGreater(activity.stat().st_mtime, 0)

    def test_synced_file_change_is_activity_but_ignored_and_paused_are_not(self):
        root = self.state / "project"
        (root / ".git").mkdir(parents=True)
        (root / "src").mkdir()
        (root / "src/main.c").write_text("int main;")
        activity = self.state / "last-used"
        activity.touch()
        for path in (activity, root, root / ".git", root / "src", root / "src/main.c"):
            os.utime(path, (0, 0))
        (root / ".git/index").write_text("noise")  # ignored path, new mtime
        session = {"alpha": {"path": str(root)}, "ignore": {"paths": [".git/"]}}
        os.utime(root / ".git", (0, 0))
        os.utime(root, (0, 0))
        with patch.object(vpn, "tunnel_sessions", return_value=[session]):
            self.assertGreater(vpn.idle_state()[0], 10**8)
            os.utime(root / "src/main.c", None)
            self.assertLess(vpn.idle_state()[0], 60)
        with patch.object(vpn, "tunnel_sessions", return_value=[{**session, "paused": True}]):
            self.assertGreater(vpn.idle_state()[0], 10**8)

    def test_ignore_patterns_follow_mutagen_shapes(self):
        patterns = [".git/", "__pycache__/", ".DS_Store", "/build", "*.o", "!keep.o"]
        self.assertTrue(vpn.ignored(".git", ".git", True, patterns))
        self.assertFalse(vpn.ignored(".git", ".git", False, patterns))
        self.assertTrue(vpn.ignored("a/b/__pycache__", "__pycache__", True, patterns))
        self.assertTrue(vpn.ignored("x/.DS_Store", ".DS_Store", False, patterns))
        self.assertTrue(vpn.ignored("build", "build", True, patterns))
        self.assertFalse(vpn.ignored("src/build", "build", True, patterns))
        self.assertTrue(vpn.ignored("src/a.o", "a.o", False, patterns))
        self.assertFalse(vpn.ignored("src/keep.o", "keep.o", False, patterns))
        # Doublestar: * stays in one component, **/ may match nothing, {a,b} alternates.
        self.assertFalse(vpn.ignored("src/deep/x.c", "x.c", False, ["src/*.c"]))
        self.assertTrue(vpn.ignored("src/x.c", "x.c", False, ["src/*.c"]))
        self.assertTrue(vpn.ignored("x.log", "x.log", False, ["**/x.log"]))
        self.assertTrue(vpn.ignored("a/b/x.log", "x.log", False, ["**/x.log"]))
        self.assertTrue(vpn.ignored("out/a/b", "b", True, ["out/**"]))
        self.assertTrue(vpn.ignored("a.pyc", "a.pyc", False, ["*.{pyc,pyo}"]))
        self.assertFalse(vpn.ignored("a.py", "a.py", False, ["*.{pyc,pyo}"]))

    def test_idle_expiry_pauses_sync_then_retries_incomplete_shutdown(self):
        events = []
        clock = SimpleNamespace(time=lambda: 1000, strftime=time.strftime, sleep=Mock())
        with patch.object(vpn, "owned_pid", return_value=88), \
                patch.object(vpn, "idle_state", return_value=(4000, [])), \
                patch.object(vpn, "transports", return_value=["mutagen"]), \
                patch.object(vpn, "pause_sync", side_effect=lambda reason: events.append("pause")), \
                patch.object(vpn, "stop", side_effect=lambda: events.append("stop") or len(events) < 4), \
                patch.object(vpn, "control_lock", return_value=nullcontext()), \
                patch.object(vpn, "time", clock):
            self.assertEqual(vpn.idle_watch(), 0)
        self.assertEqual(events, ["pause", "stop", "pause", "stop"])
        clock.sleep.assert_called_once_with(vpn.CHECK_SECONDS)

    def test_active_session_is_not_stopped(self):
        clock = SimpleNamespace(time=lambda: 1000, strftime=time.strftime, sleep=Mock(side_effect=[None, StopIteration]))
        with patch.object(vpn, "owned_pid", return_value=88), \
                patch.object(vpn, "idle_state", return_value=(0, ["interactive"])), \
                patch.object(vpn, "pause_sync") as pause, patch.object(vpn, "stop") as stop, \
                patch.object(vpn, "control_lock", return_value=nullcontext()), \
                patch.object(vpn, "time", clock):
            with self.assertRaises(StopIteration):
                vpn.idle_watch()
        pause.assert_not_called()
        stop.assert_not_called()

    def test_idle_minutes_config_and_env(self):
        config = self.state / "config.toml"
        with patch.object(vpn, "CONFIG", config), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SEASNET_VPN_IDLE_MINUTES", None)
            self.assertEqual(vpn.idle_seconds(), vpn.DEFAULT_IDLE_MINUTES * 60)
            config.write_text("idle_minutes = 45\n")
            self.assertEqual(vpn.idle_seconds(), 2700)
            config.write_text("idle_minutes = -1\n")
            self.assertEqual(vpn.idle_seconds(), vpn.DEFAULT_IDLE_MINUTES * 60)
            os.environ["SEASNET_VPN_IDLE_MINUTES"] = "0.5"
            self.assertEqual(vpn.idle_seconds(), 30)

    def proxy_context(self, kind, up=False):
        contexts = [patch.object(vpn, "caller_kind", return_value=kind),
                    patch.object(vpn, "parent_args", return_value="ssh seasnet"),
                    patch.object(vpn, "tunnel_up", return_value=up),
                    patch.object(vpn.signal, "signal")]
        mocks = [context.start() for context in contexts]
        for context in contexts:
            self.addCleanup(context.stop)
        return mocks

    def test_background_ssh_never_starts_login(self):
        for kind in ("mutagen", "batch"):
            self.proxy_context(kind)
            errors = io.StringIO()
            with patch.object(vpn, "start") as start, patch.object(vpn.subprocess, "Popen") as popen, \
                    redirect_stderr(errors):
                self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 1)
            start.assert_not_called()
            self.assertIn("no UCLA login opens" if kind != "mutagen" else "never opens UCLA login",
                          errors.getvalue())
            if kind == "mutagen":  # Only Mutagen's sessions get paused, detached.
                self.assertEqual(popen.call_args.args[0][1:], ["pause-sync"])
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
            else:
                popen.assert_not_called()

    def test_failed_logins_back_off_automatic_prompts(self):
        self.proxy_context("interactive")
        for _ in range(3):
            vpn.record_login(False)
        errors = io.StringIO()
        with patch.object(vpn, "start") as start, redirect_stderr(errors):
            self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 1)
        start.assert_not_called()
        self.assertIn("seasnet-vpn start", errors.getvalue())
        failures = json.loads((self.state / "login-failures.json").read_text())
        self.assertEqual(failures["count"], 3)
        vpn.write_json(self.state / "login-failures.json", {"count": 3, "last": failures["last"] - 241})
        self.assertIsNone(vpn.login_cooldown())  # 4 min cooldown after three failures
        vpn.record_login(True)
        self.assertFalse((self.state / "login-failures.json").exists())

    def test_start_refuses_without_terminal(self):
        with patch.object(vpn, "owned_pid", return_value=None), \
                patch.object(vpn.subprocess, "Popen") as popen, redirect_stderr(io.StringIO()):
            with patch.object(vpn, "has_tty", return_value=False), patch.dict(os.environ, {"CLAUDECODE": "1"}):
                self.assertEqual(vpn.start(), 1)
        popen.assert_not_called()
        self.assertFalse((self.state / "login-attempt.json").exists())

    def test_diagnostic_browser_login_needs_terminal(self):
        with patch.object(vpn, "has_tty", return_value=False), patch.object(vpn.subprocess, "run") as run, \
                patch.dict(os.environ, {"SEASNET_VPN_LOGIN_ATTEMPT": ""}), redirect_stderr(io.StringIO()):
            os.environ.pop("SEASNET_VPN_LOGIN_ATTEMPT")
            self.assertEqual(vpn.browser_login(self.LOGIN), 1)
        run.assert_not_called()

    def test_pause_and_resume_touch_only_own_sessions(self):
        sessions = [{"identifier": "a", "name": "mine"}, {"identifier": "b", "name": "manual", "paused": True}]
        calls = []
        ok = Mock(returncode=0, stderr="")
        with patch.object(vpn, "tunnel_sessions", return_value=sessions), \
                patch.object(vpn, "mutagen", side_effect=lambda *args, **kw: calls.append(args) or ok):
            vpn.pause_sync("idle")
        self.assertEqual(calls, [("sync", "pause", "a")])
        self.assertEqual(json.loads((self.state / "paused-sync.json").read_text()), ["a"])
        calls.clear()
        paused = [{**sessions[0], "paused": True}, sessions[1]]
        with patch.object(vpn, "tunnel_sessions", return_value=paused), \
                patch.object(vpn, "mutagen", side_effect=lambda *args, **kw: calls.append(args) or ok):
            vpn.resume_sync()
        self.assertEqual(calls, [("sync", "resume", "a")])
        self.assertFalse((self.state / "paused-sync.json").exists())

    def test_hangup_cleans_nc_and_lease_without_stdout_messages(self):
        handlers = {}
        client = Mock()
        client.poll.return_value = None
        client.wait.side_effect = [KeyboardInterrupt, 0]
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(vpn, "caller_kind", return_value="interactive"), \
                patch.object(vpn, "parent_args", return_value="ssh seasnet"), \
                patch.object(vpn, "tunnel_up", side_effect=[False, False]), \
                patch.object(vpn, "owned_pid", side_effect=[None, 88]), \
                patch.object(vpn, "start", side_effect=lambda: print("login progress") or 0), \
                patch.object(vpn, "ensure_idle_watch"), patch.object(vpn.subprocess, "Popen", return_value=client), \
                patch.object(vpn.signal, "signal", side_effect=lambda number, handler: handlers.update({number: handler})), \
                redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(KeyboardInterrupt):
                vpn.proxy(vpn.TARGET, "22")
        self.assertIn(signal.SIGHUP, handlers)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("login progress", errors.getvalue())
        client.terminate.assert_called_once()
        self.assertFalse(list((self.state / "connections").iterdir()))

    def test_resume_keeps_record_when_nothing_was_resumed(self):
        vpn.write_json(self.state / "paused-sync.json", ["a", "gone"])
        with patch.object(vpn, "tunnel_sessions", return_value=None), \
                patch.object(vpn, "mutagen") as mutagen:
            vpn.resume_sync()  # daemon down: touch nothing
        mutagen.assert_not_called()
        self.assertEqual(json.loads((self.state / "paused-sync.json").read_text()), ["a", "gone"])
        sessions = [{"identifier": "a", "name": "mine", "paused": True}]
        for failure in (Mock(returncode=1, stderr="boom"), subprocess.TimeoutExpired("mutagen", 30)):
            effect = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with patch.object(vpn, "tunnel_sessions", return_value=sessions), \
                    patch.object(vpn, "mutagen", **effect):
                vpn.resume_sync()
            # The terminated session is dropped; the failed one is kept for next time.
            self.assertEqual(json.loads((self.state / "paused-sync.json").read_text()), ["a"])

    def test_idle_stop_cancelled_by_new_session_resumes_sync(self):
        events = []
        clock = SimpleNamespace(time=lambda: 1000, strftime=time.strftime, sleep=Mock(side_effect=StopIteration))
        with patch.object(vpn, "owned_pid", return_value=88), \
                patch.object(vpn, "idle_state", return_value=(4000, [])), \
                patch.object(vpn, "transports", return_value=["mutagen", "interactive"]), \
                patch.object(vpn, "pause_sync", side_effect=lambda reason: events.append("pause")), \
                patch.object(vpn, "resume_sync", side_effect=lambda: events.append("resume")), \
                patch.object(vpn, "stop") as stop, \
                patch.object(vpn, "control_lock", return_value=nullcontext()), \
                patch.object(vpn, "time", clock):
            with self.assertRaises(StopIteration):
                vpn.idle_watch()
        self.assertEqual(events, ["pause", "resume"])
        stop.assert_not_called()

    def test_queued_caller_rechecks_backoff_under_lock(self):
        self.proxy_context("interactive")
        @contextmanager
        def lock(wait=False):
            vpn.record_login(False)  # the login ahead of us was cancelled meanwhile
            yield
        with patch.object(vpn, "control_lock", lock), patch.object(vpn, "start") as start, \
                redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 1)
        start.assert_not_called()
        self.assertIn("will not reopen UCLA login", errors.getvalue())

    def test_open_tunnel_resumes_sync_left_paused_for_people_not_mutagen(self):
        client = Mock()
        client.wait.return_value = 0
        for kind, expected in (("mutagen", 0), ("interactive", 1)):
            vpn.write_json(self.state / "paused-sync.json", ["a"])
            self.proxy_context(kind, up=True)
            with patch.object(vpn, "owned_pid", return_value=88), patch.object(vpn, "ensure_idle_watch"), \
                    patch.object(vpn, "resume_sync") as resume, \
                    patch.object(vpn.subprocess, "Popen", return_value=client), redirect_stderr(io.StringIO()):
                self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 0)
            self.assertEqual(resume.call_count, expected)

    def test_short_connect_timeout_refuses_instead_of_half_login(self):
        self.proxy_context("interactive")
        with patch.object(vpn, "parent_args", return_value="ssh -o ConnectTimeout=10 seasnet true"), \
                patch.object(vpn, "start") as start, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 1)
        start.assert_not_called()
        self.assertIn("ConnectTimeout too short", errors.getvalue())

    def test_caller_kind_from_parent_ssh(self):
        with patch.object(vpn, "has_tty", return_value=True):
            self.assertEqual(vpn.caller_kind("/usr/bin/ssh -oConnectTimeout=5 -oServerAliveInterval=10 seasnet "
                                             ".mutagen/agents/0.18.1/mutagen-agent synchronizer"), "mutagen")
            self.assertEqual(vpn.caller_kind("ssh -o BatchMode=true seasnet"), "batch")
            self.assertEqual(vpn.caller_kind("ssh -oBatchMode=yes seasnet"), "batch")
            self.assertEqual(vpn.caller_kind("ssh seasnet"), "interactive")
            # An agent's foreground pty command is interactive like Mack's.
            with patch.dict(os.environ, {"CLAUDECODE": "1"}):
                self.assertEqual(vpn.caller_kind("ssh seasnet"), "interactive")
        with patch.object(vpn, "has_tty", return_value=False):
            self.assertEqual(vpn.caller_kind("ssh seasnet"), "batch")

    def test_ours_tolerates_unparseable_args(self):
        with patch.object(vpn, "process", return_value=f"{os.getuid()} 1 /usr/bin/grep grep it's ocproxy"):
            self.assertIsNone(vpn.ours(123))
        with patch.object(vpn, "process", return_value=f"{os.getuid()} 1 /opt/homebrew/bin/ocproxy "
                                                       f"/opt/homebrew/bin/ocproxy -L {vpn.PORT}:{vpn.TARGET}:22 -k 30"):
            self.assertEqual(vpn.ours(123), "ocproxy")

    def test_nonfinite_idle_minutes_fall_back(self):
        with patch.dict(os.environ, {"SEASNET_VPN_IDLE_MINUTES": "nan"}):
            self.assertEqual(vpn.idle_seconds(), vpn.DEFAULT_IDLE_MINUTES * 60)


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/xcrun").exists(), "needs swiftc")
class SignInWindowTests(unittest.TestCase):
    def swift(self):
        here = source.parents[2] / "dot_config/seasnet-vpn/signin"
        return here if here.exists() else Path.home() / ".config/seasnet-vpn/signin"

    def test_release_build_is_clean_cached_and_closed(self):
        swift = self.swift()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            app = state / "share/UCLA VPN Sign-In.app"
            real, outputs = subprocess.run, []

            def run(*args, **kwargs):
                outputs.append(real(*args, **kwargs))
                return outputs[-1]

            with patch.object(vpn, "STATE", state), patch.object(vpn, "SIGNIN_SOURCE", swift), \
                    patch.object(vpn, "SIGNIN_APP", app), \
                    redirect_stderr(io.StringIO()), patch.object(vpn.subprocess, "run", side_effect=run):
                binary = vpn.build_signin()
                self.assertEqual((outputs[0].stdout, outputs[0].stderr), (b"", b""))
                built = len(outputs)
                self.assertEqual(vpn.build_signin(), binary)
                self.assertEqual(len(outputs), built)  # Cached on later starts.
                # Another file in its place, plist and all intact, is rebuilt and never run.
                good = binary.read_bytes()
                for tamper in (lambda: binary.write_bytes(good + b"x"), lambda: binary.chmod(0o777),
                               lambda: (binary.rename(state / "elsewhere"), binary.symlink_to(state / "elsewhere"))):
                    tamper()
                    self.assertEqual(vpn.build_signin(), binary)
                    self.assertFalse(binary.is_symlink())
                    self.assertEqual(binary.stat().st_mode & 0o022, 0)
                    self.assertFalse(binary.read_bytes().endswith(b"x"))
                built = len(outputs)
                # --create runs what it built just now, from a private place, and leaves it nowhere.
                binary.write_bytes(b"#!/bin/sh\nexit 0\n")
                with vpn.fresh_signin() as fresh:
                    self.assertNotEqual(fresh, binary)
                    self.assertEqual(fresh.parents[3].stat().st_mode & 0o077, 0)
                    self.assertGreater(len(fresh.read_bytes()), 100_000)
                self.assertFalse(fresh.exists())
                binary = vpn.build_signin()
                built = len(outputs)
                copy = state / "signin"
                shutil.copytree(swift, copy)
                (copy / "Web.swift").write_text((copy / "Web.swift").read_text() + "\n// changed\n")
                with patch.object(vpn, "SIGNIN_SOURCE", copy):
                    vpn.build_signin()
                self.assertGreater(len(outputs), built)  # Any file of it counts.
            self.assertEqual([p.name for p in app.parent.iterdir()], [app.name])
            signature = real(["/usr/bin/codesign", "-d", "-r-", "--verbose=2", str(app)], capture_output=True, text=True)
            self.assertIn(f'designated => identifier "{vpn.SIGNIN_BUNDLE}"', signature.stdout + signature.stderr)
            real(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True)
            # The app seasnet-vpn builds has no test doors, and no bad input opens it.
            for arguments, line in (([], b""), ([], b"https://evil.com/\n"), (["--self-test", directory], b""),
                                    (["--render-previews", directory], b""), (["--web"], b""), (["--web"], b"https://evil.com/\n"),
                                    (["--web"], b'{"url":"https://shb.ais.ucla.edu/"}\n'), (["--message", "a", "b"], b""),
                                    ([], b'{"url":"https://shb.ais.ucla.edu/","key":"/x"}\n'),
                                    ([], json.dumps({"url": LifecycleTests.LOGIN, "key": "relative"}).encode() + b"\n")):
                result = real([str(binary), *arguments], input=line, capture_output=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout), (64, b""), arguments)
            self.assertNotIn(b"SoftwareApprover", binary.read_bytes())
            missing = real([str(binary), "--show-key", directory + "/none"], capture_output=True, text=True, timeout=10)
            self.assertEqual((missing.returncode, missing.stdout), (1, ""))

    def test_web_and_approval_code_stay_apart(self):
        approval, web = ((self.swift() / name).read_text() for name in ("Approval.swift", "Web.swift"))
        web += (self.swift() / "Zen.swift").read_text()  # The browser route runs in the web process.
        for word in ("import WebKit", "WKWeb", "WebController"):
            self.assertFalse(word in approval, word)
        for word in ("CryptoKit", "LocalAuthentication", "EnclaveKey", "KeyFile", "Approver", '"key"', "evaluateJavaScript",
                     "addUserScript", "userContentController", "WKUserScript", "URLSession(", "URLSessionConfiguration"):
            self.assertFalse(word in web, word)
        # Nothing of a web login is remembered, nor is an earlier build's store read.
        self.assertEqual(web.count("store: .nonPersistent()"), 1)
        for word in ("forIdentifier", "dataStoreID", ".default()"):
            self.assertFalse(word in web, word)

    def test_self_test_and_signed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = directory + "/signin-testing"
            built = subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", "-wmo", "-D", "TESTING",
                                    *map(str, sorted(self.swift().glob("*.swift"))), "-o", binary],
                                   capture_output=True, text=True)
            self.assertEqual((built.returncode, built.stderr), (0, ""))
            # Headless: windows are never put on screen, and the enclave is only
            # ever asked with interaction forbidden.
            result = subprocess.run([binary, "--self-test", directory], capture_output=True, text=True, timeout=120)
            self.assertIn(result.returncode, (0, 77), result.stdout)
            skipped = result.returncode == 77
            signed = subprocess.run([binary, "--message", "A" * 42 + "_", LifecycleTests.LOGIN], capture_output=True).stdout
            self.assertEqual(signed, b"vlt-vpn-login-v1\n" + b"A" * 42 + b"_\n"
                             b"366b3cc1db484f5b4842486e0e32433e3da8905784fa84c9587a2d0b0f007f7b")
            if skipped:
                self.skipTest("SECURE ENCLAVE CHECKS NOT RUN: no usable Touch ID here. The rest passed.")


STUB = r"""
#include <arpa/inet.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>
static void slow(int n) { usleep(700000); _exit(0); }
int main(int c, char **v) {
    struct sockaddr_in6 a = {0}; int on = 1, s = socket(AF_INET6, SOCK_STREAM, 0);
    signal(SIGTERM, slow);
    a.sin6_family = AF_INET6; a.sin6_port = htons(atoi(getenv("STUB_PORT"))); a.sin6_addr = in6addr_loopback;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &on, sizeof on);
    if (bind(s, (void *)&a, sizeof a) || listen(s, 1)) return 9;
    FILE *f = fopen(getenv("STUB_PIDFILE"), "w"); fprintf(f, "%d", getpid()); fclose(f);
    for (;;) pause();
}
"""
DRIVER = r"""
import importlib.machinery as m, importlib.util as u, json, pathlib, sys
from unittest.mock import patch
sys.dont_write_bytecode = True
l = m.SourceFileLoader("v", sys.argv[1]); v = u.module_from_spec(u.spec_from_loader("v", l)); l.exec_module(v)
state = pathlib.Path(sys.argv[2])
v.STATE, v.PIDFILE, v.BASELINE = state, state / "openconnect.pid", state / "before.json"
v.PORT, v.CALLBACK_PORT, v.OPENCONNECT = 0, int(sys.argv[3]), str(state / "openconnect")
for name, value in {"has_tty": lambda: True, "snapshot": dict, "build_signin": lambda: None, "who_asks": dict,
                    "resume_sync": lambda: None}.items():
    setattr(v, name, value)
sys.argv[:] = ["seasnet-vpn", "start"]
with patch.object(v, "login_cancelled", side_effect=lambda: (state / "end").exists()):
    try:
        sys.exit(v.main())
    except KeyboardInterrupt:
        sys.exit(130)
"""


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/xcrun").exists(), "needs clang")
class OrphanTests(unittest.TestCase):
    """The real `start`, against a stub openconnect that is slow to die like the real one."""

    @classmethod
    def setUpClass(cls):
        cls.built = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.built.cleanup)
        (Path(cls.built.name) / "stub.c").write_text(STUB)
        subprocess.run(["/usr/bin/xcrun", "clang", "-w", "-o", cls.built.name + "/stub", cls.built.name + "/stub.c"], check=True)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name).resolve()
        with socket.create_server(("::1", 0), family=socket.AF_INET6) as server:
            self.port = server.getsockname()[1]
        shutil.copy(self.built.name + "/stub", self.state / "openconnect")
        shutil.copy(self.built.name + "/stub", self.state / "other")
        self.env = {**os.environ, "STUB_PORT": str(self.port), "STUB_PIDFILE": str(self.state / "stub.pid")}
        self.addCleanup(self.sweep)

    def sweep(self):
        for line in subprocess.run(["/bin/ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True).stdout.splitlines():
            pid, _, args = line.strip().partition(" ")
            if args.strip().startswith(str(self.state)):
                os.kill(int(pid), signal.SIGKILL)

    def start(self):
        (self.state / "driver.py").write_text(DRIVER)
        child = subprocess.Popen([sys.executable, str(self.state / "driver.py"), str(source), str(self.state), str(self.port)],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.communicate()))
        return child

    def stub(self, deadline=10):
        until = time.monotonic() + deadline
        while time.monotonic() < until:
            try:
                return int((self.state / "stub.pid").read_text())
            except (OSError, ValueError):
                time.sleep(0.02)
        self.fail("the stub openconnect never started")

    def gone(self, pid):
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True

    def free(self):
        with socket.socket(socket.AF_INET6) as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                check.bind(("::1", self.port))
                return True
            except OSError:
                return False

    def test_repeated_signals_never_orphan_openconnect(self):
        for first, second, gap in ((signal.SIGHUP, signal.SIGHUP, 0.0002), (signal.SIGHUP, signal.SIGHUP, 0.0005),
                                   (signal.SIGHUP, signal.SIGTERM, 0.3), (signal.SIGTERM, signal.SIGINT, 0.05),
                                   (signal.SIGINT, signal.SIGINT, 0.5)):
            with self.subTest(first=first, second=second, gap=gap):
                (self.state / "stub.pid").unlink(missing_ok=True)
                child = self.start()
                stub = self.stub()
                time.sleep(0.3)
                child.send_signal(first)
                time.sleep(gap)
                child.send_signal(second)
                child.communicate(timeout=20)
                self.assertTrue(self.gone(stub))
                self.assertTrue(self.free())
                self.assertFalse((self.state / "login-attempt.json").exists())

    def test_a_signal_during_ordinary_cleanup_does_not_cut_it_short(self):
        child = self.start()
        stub = self.stub()
        (self.state / "end").touch()  # A cancelled login, already being cleaned up.
        time.sleep(0.4)
        child.send_signal(signal.SIGHUP)
        child.communicate(timeout=20)
        self.assertTrue(self.gone(stub) and self.free())
        self.assertEqual(child.returncode, 1)  # The cancelled login's own result.

    def test_our_orphan_on_the_callback_port_is_cleared(self):
        orphan = subprocess.Popen([str(self.state / "openconnect"), "--pid-file", str(self.state / "openconnect.pid")], env=self.env)
        self.addCleanup(lambda: (orphan.kill(), orphan.wait()))
        threading.Thread(target=orphan.wait, daemon=True).start()
        self.assertEqual(self.stub(), orphan.pid)
        (self.state / "stub.pid").unlink()
        child = self.start()
        self.assertNotEqual(self.stub(), orphan.pid)
        self.assertTrue(self.gone(orphan.pid))
        child.send_signal(signal.SIGTERM)
        child.communicate(timeout=20)

    def test_a_foreign_listener_is_never_killed(self):
        for name, args in (("other", ["--pid-file", str(self.state / "openconnect.pid")]),   # Not openconnect.
                           ("openconnect", ["--pid-file", "/somewhere/else.pid"])):           # Not this forward's.
            with self.subTest(name=name, args=args):
                (self.state / "stub.pid").unlink(missing_ok=True)
                foreign = subprocess.Popen([str(self.state / name), *args], env=self.env)
                try:
                    self.stub()
                    child = self.start()
                    _, errors = child.communicate(timeout=20)
                    self.assertEqual(child.returncode, 1)
                    self.assertIn("leaving it alone", errors)
                    self.assertIsNone(foreign.poll())
                finally:
                    foreign.kill()
                    foreign.wait()


class SnapshotTests(unittest.TestCase):
    def test_cloned_host_routes_do_not_count_as_route_changes(self):
        header = "Routing tables\n\nInternet:\nDestination Gateway Flags Netif Expire\n"
        static = "default link#41 UCSg utun14\n10.21/16 link#14 UCSI en0 !\n"

        def table(cloned):
            return SimpleNamespace(returncode=0, stdout=header + static + cloned,
                                   check_returncode=lambda: None)

        def snap(cloned):
            def fake(argv, *args, **kwargs):
                if argv[0].endswith("netstat"):
                    return table(cloned)
                return SimpleNamespace(returncode=0, stdout="same\n", check_returncode=lambda: None)
            with patch.object(vpn, "capture", side_effect=fake):
                return vpn.snapshot()

        before = snap("17.57.144.104 link#41 UHWIig utun14\n")
        after = snap("104.16.1.34 link#41 UHW3Ig utun14 6\n")
        self.assertEqual(before["routes"], after["routes"])
        self.assertNotEqual(snap("")["routes"],
                            snap("10.9/16 link#50 UCS utun15\n")["routes"])


if __name__ == "__main__":
    unittest.main()
