"""codexbar-usage-status.sh fixes from the 2026-10-22 review.

Function-level checks run the script's own function bodies (cut out with awk);
whole-run checks use test_codexbar_account's isolated HOME with fake
security/curl/tmux. The one tmux test runs a PRIVATE server (its own -L socket
under the temp HOME's TMUX_TMPDIR), never the user's.
"""
import calendar
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_codexbar_account import A_EMAIL, A_ORG, STATUS, AccountTestCase

JQ = shutil.which('jq')
TMUX = shutil.which('tmux')


def run_functions(names, body, **env):
    """Define `names` from the status script, then run `body` under bash."""
    cut = ''.join(f"eval \"$(awk '/^{n}\\(\\) {{/{{f=1}} f{{print}} f&&/^}}/{{exit}}' \"$STATUS\")\"\n"
                  for n in names)
    script = 'set -euo pipefail\nlog_warn() { :; }\nlog_info() { :; }\nnow_epoch() { date +%s; }\n' + cut + body
    full = dict(os.environ, STATUS=str(STATUS), **env)
    return subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True, timeout=20, env=full)


def utc(text):
    return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ'))


@unittest.skipUnless(JQ, 'needs jq')
class ResetsAtTests(unittest.TestCase):
    def test_fractional_seconds_round_to_the_nearest_second(self):
        cases = {'2026-10-22T17:59:59.912+00:00': utc('2026-10-22T18:00:00Z'),
                 '2026-10-22T17:59:59.4Z': utc('2026-10-22T17:59:59Z'),
                 '2026-10-22T18:00:00.012345+00:00': utc('2026-10-22T18:00:00Z'),
                 '2026-10-22T18:00:00Z': utc('2026-10-22T18:00:00Z')}
        for iso, expected in cases.items():
            with self.subTest(iso=iso):
                r = run_functions(['iso_utc_to_epoch'], f'iso_utc_to_epoch "{iso}"')
                self.assertEqual(r.stdout, str(expected), r.stderr)

    def test_jq_epoch_defs_round_the_same_way(self):
        lines = STATUS.read_text().splitlines()
        defs, current = [], None
        for line in lines:
            if line.strip() == 'def epoch:':
                current = [line]
            elif current is not None:
                current.append(line)
                if line.strip() == 'else null end;':
                    defs.append('\n'.join(current))
                    current = None
        self.assertEqual(len(defs), 2)
        for d in defs:
            for iso, expected in (('2026-10-22T17:59:59.912+00:00', '2026-10-22T18:00:00Z'),
                                  ('2026-10-22T17:59:59.1+00:00', '2026-10-22T17:59:59Z')):
                with self.subTest(iso=iso):
                    out = subprocess.run([JQ, '-n', f'{d} "{iso}" | epoch'], capture_output=True, text=True)
                    self.assertEqual(out.stdout.strip(), str(utc(expected)), out.stderr)

    def test_recent_rate_takes_the_whole_window_across_a_one_second_skew(self):
        # One window stored as both X and X-1 (history from before rounding).
        now, x = 1792690300, 1792705200
        rows = [(now - 500, x, 10), (now - 400, x - 1, 11), (now - 300, x, 12), (now - 100, x, 14),
                (now - 200, x + 18000, 90)]       # another window entirely: never counted
        with tempfile.TemporaryDirectory() as d:
            history = Path(d) / 'h.jsonl'
            history.write_text(''.join(json.dumps({'t': t, 'sr': r, 's': s}) + '\n' for t, r, s in rows))
            body = f'ACTIVE_HISTORY_FILE="{history}" HISTORY_FILE="{history}"; pace_recent_rate session {{r}} {now} 600 180'
            rates = []
            for r in (x, x - 1):
                out = run_functions(['pace_recent_rate'], body.format(r=r))
                self.assertEqual(out.returncode, 0, out.stderr)
                rates.append(out.stdout.strip())
        self.assertNotEqual(rates[0], '')
        self.assertEqual(rates[0], rates[1])           # the same answer for either variant
        self.assertAlmostEqual(float(rates[0]), 4 / 400, places=6)


class LockTests(unittest.TestCase):
    def test_release_lock_removes_only_its_own_lock(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / 'usage.json.lock'
            body = ('mkdir "$LOCKDIR"; echo 999999 >"$LOCKDIR/pid"; release_lock; [ -d "$LOCKDIR" ] && echo kept\n'
                    'echo $$ >"$LOCKDIR/pid"; release_lock; [ -d "$LOCKDIR" ] || echo mine-removed\n'
                    'mkdir "$LOCKDIR"; echo STARTING >"$LOCKDIR/pid"; release_lock STARTING;'
                    ' [ -d "$LOCKDIR" ] || echo starting-removed\n'
                    'mkdir "$LOCKDIR"; echo 999999 >"$LOCKDIR/pid"; release_lock --any;'
                    ' [ -d "$LOCKDIR" ] || echo stolen\n')
            out = run_functions(['release_lock'], body, LOCKDIR=str(lock))
            self.assertEqual(out.stdout.split(), ['kept', 'mine-removed', 'starting-removed', 'stolen'], out.stderr)


@unittest.skipUnless(JQ, 'needs jq')
class InheritedLockTests(AccountTestCase):
    def test_a_spawned_worker_whose_lock_was_stolen_yields(self):
        # The spawner leaves the lock at STARTING; a manual refresh that stole it
        # in the meantime wrote its own pid, and the late worker must not take
        # the lock back (two holders) nor fetch.
        self.home.login(A_EMAIL, A_ORG)
        lock = self.home.cache / 'usage.json.lock'
        held = dict(CODEXBAR_USAGE_LOCK_HELD='1', CODEXBAR_USAGE_LOCKDIR=str(lock))
        lock.mkdir()
        (lock / 'pid').write_text('999999\n')
        self.home.run(STATUS, '--refresh', **held)
        self.assertEqual((lock / 'pid').read_text().strip(), '999999')
        self.assertEqual(self.home.curl_calls(), 0)
        (lock / 'pid').write_text('STARTING\n')
        self.home.run(STATUS, '--refresh', **held)
        self.assertGreater(self.home.curl_calls(), 0)
        self.assertFalse(lock.exists())


# The Keychain item as a file, and a usage endpoint that answers 200 only for
# FAKE_GOOD_TOKEN (else FAKE_BAD_STATUS, default 401, with FAKE_BAD_BODY,
# default empty). The token
# endpoint answers FAKE_TOKEN_RESPONSE, after copying FAKE_ROTATE_TO over the
# Keychain when set (Claude Code rotating the same item meanwhile).
SECURITY = '''#!/bin/sh
case "$1" in
  find-generic-password) cat "$FAKE_KEYCHAIN" 2>/dev/null || exit 44 ;;
  add-generic-password)
    while [ $# -gt 0 ]; do
      if [ "$1" = -w ]; then printf '%s' "$2" >"$FAKE_KEYCHAIN"; exit 0; fi
      shift
    done
    exit 1 ;;
esac
'''
CURL = '''#!/bin/sh
cfg='' post=0 prev=''
for a in "$@"; do
  [ "$prev" = --config ] && cfg="$a"
  [ "$a" = POST ] && post=1
  prev="$a"
done
if [ "$post" = 1 ]; then
  echo token >>"$FAKE_TOKEN_LOG"
  [ -n "$FAKE_ROTATE_TO" ] && cp "$FAKE_ROTATE_TO" "$FAKE_KEYCHAIN"
  printf '%s' "$FAKE_TOKEN_RESPONSE"
  exit 0
fi
echo call >>"$FAKE_CURL_LOG"
wo=0; grep -q write-out "$cfg" 2>/dev/null && wo=1
if [ -n "$FAKE_CURL_RC" ]; then
  [ "$wo" = 1 ] && printf '\\n000'
  exit "$FAKE_CURL_RC"
fi
if grep -q "$FAKE_GOOD_TOKEN" "$cfg"; then
  cat "$FAKE_USAGE"; code=200
else
  printf '%s' "$FAKE_BAD_BODY"; code="${FAKE_BAD_STATUS:-401}"
fi
[ "$wo" = 1 ] && printf '\\n%s' "$code"
exit 0
'''


def keychain(access, refresh):
    return json.dumps({'claudeAiOauth': {'accessToken': access, 'refreshToken': refresh,
                                         'expiresAt': 4102444800000, 'scopes': ['user:inference']}})


@unittest.skipUnless(JQ, 'needs jq')
class OAuthTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        for name, body in (('security', SECURITY), ('curl', CURL), ('codex', '#!/bin/sh\nexit 1\n')):
            (self.home.bin / name).write_text(body)
            (self.home.bin / name).chmod(0o700)
        self.keychain = self.home.root / 'keychain.json'
        self.keychain.write_text(keychain('sk-ant-oat-old', 'sk-ant-ort-old'))
        self.token_log = self.home.root / 'token.log'
        self.home.login(A_EMAIL, A_ORG)

    def refresh(self, **extra):
        env = dict(FAKE_KEYCHAIN=str(self.keychain), FAKE_TOKEN_LOG=str(self.token_log),
                   FAKE_GOOD_TOKEN='sk-ant-oat-old', FAKE_TOKEN_RESPONSE='', FAKE_ROTATE_TO='',
                   FAKE_CURL_RC='', FAKE_BAD_BODY='', CODEXBAR_USAGE_FORCE_REFRESH='1',
                   CODEXBAR_OAUTH_ROTATION_WAIT='0')
        env.update(extra)
        return self.home.run(STATUS, '--refresh', **env)

    def tokens(self):
        return json.loads(self.keychain.read_text())['claudeAiOauth']

    def test_a_timeout_is_not_answered_with_a_token_rotation(self):
        self.refresh(FAKE_CURL_RC='28')
        self.assertFalse(self.token_log.exists(), 'a timed-out request rotated the tokens')
        self.assertEqual(self.tokens()['refreshToken'], 'sk-ant-ort-old')
        # A 5xx with no body is not an auth problem either.
        self.refresh(FAKE_GOOD_TOKEN='nope', FAKE_BAD_STATUS='503')
        self.assertFalse(self.token_log.exists())

    def test_a_permission_error_403_is_not_answered_with_a_token_rotation(self):
        # A scope or org-policy 403 says nothing about the token's validity: a
        # refresh just rotates Claude Code's tokens and gets the same 403.
        body = json.dumps({'type': 'error', 'error': {
            'type': 'permission_error',
            'message': 'OAuth token does not meet scope requirement user:profile'}})
        response = json.dumps({'access_token': 'sk-ant-oat-new', 'refresh_token': 'sk-ant-ort-new'})
        self.refresh(FAKE_GOOD_TOKEN='nope', FAKE_BAD_STATUS='403', FAKE_BAD_BODY=body,
                     FAKE_TOKEN_RESPONSE=response)
        self.assertFalse(self.token_log.exists(), 'a permission_error 403 rotated the tokens')
        self.assertEqual(self.tokens()['refreshToken'], 'sk-ant-ort-old')
        self.assertNotEqual(self.home.claude().get('state'), 'auth_required')
        # A 403 with no body is still taken for a rejected token.
        self.refresh(FAKE_GOOD_TOKEN='nope', FAKE_BAD_STATUS='403', FAKE_TOKEN_RESPONSE=response)
        self.assertTrue(self.token_log.exists())

    def test_a_rejected_token_is_refreshed_and_a_missing_expires_in_keeps_the_new_pair(self):
        response = json.dumps({'access_token': 'sk-ant-oat-new', 'refresh_token': 'sk-ant-ort-new'})
        self.refresh(FAKE_GOOD_TOKEN='sk-ant-oat-new', FAKE_TOKEN_RESPONSE=response)
        self.assertTrue(self.token_log.exists())
        tokens = self.tokens()
        self.assertEqual((tokens['accessToken'], tokens['refreshToken']), ('sk-ant-oat-new', 'sk-ant-ort-new'))
        self.assertIsInstance(tokens['expiresAt'], int)
        self.assertGreater(tokens['expiresAt'], int(time.time() * 1000))
        self.assertEqual(tokens['scopes'], ['user:inference'])          # other fields kept
        block = self.home.claude()
        self.assertEqual((block['state'], block['session_used']), ('ok', 12))

    def test_losing_the_rotation_race_is_not_a_login_problem(self):
        rotated = self.home.root / 'rotated.json'
        rotated.write_text(keychain('sk-ant-oat-cc', 'sk-ant-ort-cc'))
        grant = json.dumps({'error': 'invalid_grant', 'error_description': 'Refresh token not found or invalid'})
        self.refresh(FAKE_GOOD_TOKEN='sk-ant-oat-cc', FAKE_TOKEN_RESPONSE=grant, FAKE_ROTATE_TO=str(rotated))
        block = self.home.claude()
        self.assertEqual((block['state'], block['session_used']), ('ok', 12))
        self.assertEqual(self.tokens()['accessToken'], 'sk-ant-oat-cc')  # Claude Code's, untouched

    def test_a_revoked_login_still_reports_auth_required(self):
        grant = json.dumps({'error': 'invalid_grant', 'error_description': 'Refresh token not found or invalid'})
        self.refresh(FAKE_GOOD_TOKEN='nope', FAKE_TOKEN_RESPONSE=grant)
        self.assertEqual(self.home.claude()['state'], 'auth_required')


@unittest.skipUnless(JQ, 'needs jq')
class ProviderSetTests(AccountTestCase):
    def test_legacy_display_env_does_not_drop_the_codex_block(self):
        # The push hook passes CODEXBAR_USAGE_PROVIDER=claude; with tmux out of
        # reach @codexbar_providers reads empty, and that env used to become the
        # whole fetch set, so the Codex block was deleted from usage.json.
        (self.home.bin / 'codex').write_text('#!/bin/sh\nexit 1\n')
        (self.home.bin / 'codex').chmod(0o700)
        self.home.login(A_EMAIL, A_ORG)
        self.home.block(f'{A_EMAIL}/{A_ORG}', 12, 2)
        data = json.loads(self.home.usage.read_text())
        codex = dict(data['providers']['claude'], provider='codex', label='Codex')
        codex.pop('account')
        data['providers']['codex'] = codex
        self.home.usage.write_text(json.dumps(data))
        self.home.run(STATUS, '--refresh', CODEXBAR_USAGE_PROVIDERS='', CODEXBAR_USAGE_PROVIDER='claude',
                      CODEXBAR_USAGE_FORCE_REFRESH='1')
        providers = json.loads(self.home.usage.read_text())['providers']
        self.assertIn('codex', providers)
        self.assertIn('claude', providers)


@unittest.skipUnless(JQ and TMUX, 'needs jq and tmux')
class TickForkTests(AccountTestCase):
    def test_a_tick_with_nothing_due_makes_one_tmux_call(self):
        sock = f'cbfix-{os.getpid()}'
        env = self.home.env()
        real = [TMUX, '-L', sock, '-f', '/dev/null']
        subprocess.run(real + ['new-session', '-d', '-s', 'x'], env=env, check=True)
        self.addCleanup(lambda: subprocess.run(real + ['kill-server'], env=env, capture_output=True))
        stub = self.home.bin / 'tmux'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$FAKE_TMUX_LOG"\n'
                        f'exec "{TMUX}" -L "{sock}" -f /dev/null "$@"\n')
        self.home.login(A_EMAIL, A_ORG)
        self.home.run(STATUS, '--refresh', CODEXBAR_USAGE_FORCE_REFRESH='1')
        self.assertEqual(self.home.claude()['session_used'], 12)
        self.assertEqual(subprocess.run(real + ['show-option', '-gqv', '@codex_session_text'], env=env,
                                        capture_output=True, text=True).stdout.strip(),
                         self.home.claude()['session_text'])

        def tick():
            (self.home.cache / 'last_tick').unlink(missing_ok=True)
            self.home.tmux_log.unlink(missing_ok=True)
            self.home.run(STATUS, '--tick')
            return self.home.tmux_log.read_text().splitlines()

        tick()                      # settles anything the refresh left to the tick
        self.assertEqual(tick(), ['show-options -g'])
        # A re-sourced tmux.conf resets the colors: the next tick puts them back.
        subprocess.run(real + ['set', '-g', '@codex_session_color', 'colour239'], env=env, check=True)
        calls = tick()
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(subprocess.run(real + ['show-option', '-gqv', '@codex_session_color'], env=env,
                                        capture_output=True, text=True).stdout.strip(),
                         self.home.claude()['session_color'])
        self.assertEqual(tick(), ['show-options -g'])


if __name__ == '__main__':
    unittest.main()
