"""Pooled pace: Claude's pace computed over every account claude-swap holds.

Everything runs against an isolated HOME with fake tmux/security/curl/cswap on
PATH (as in test_codexbar_account.py), plus a fake `date` that pins `date +%s`
to a fixed epoch so the worked example's numbers are exact. No real account,
Keychain, network or tmux server is touched, and the fake cswap fails any test
that calls it (the status line must never run it).
"""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def script(name):
    plain = SCRIPTS / name
    return plain if plain.exists() else SCRIPTS / ('executable_' + name)


STATUS = script('codexbar-usage-status.sh')

NOW = 1792108800                     # fixed "now" for every run
HOUR, DAY = 3600, 86400
SESSION_W, WEEK_W = 300 * 60, 10080 * 60

A1_EMAIL, A1_ORG = 'mackhaymond@gmail.com', 'org-1'
A2_EMAIL, A2_ORG = 'ihave27kidsinmybasement@gmail.com', 'org-2'

# Today's numbers (the worked example in the script's "Pooled pace" block).
A1_WEEKLY_RESETS = NOW + int(4.04 * DAY)
A2_WEEKLY_RESETS = NOW + int(5.33 * DAY)
A2_SESSION_RESETS = NOW + 4 * HOUR

STUBS = {
    'tmux': 'printf "%s\\n" "$*" >>"$FAKE_TMUX_LOG"',
    'security': 'printf \'{"claudeAiOauth":{"accessToken":"sk-ant-oat-test",'
                '"refreshToken":"sk-ant-ort-test","expiresAt":4102444800000}}\'',
    'curl': 'echo call >>"$FAKE_CURL_LOG"\ncat "$FAKE_USAGE"',
    'cswap': 'echo "$*" >>"$FAKE_CSWAP_LOG"; exit 1',
    # Only the bare `date +%s` (now_epoch) is pinned; every other form —
    # ISO parsing, formatting, nonces — is the real date.
    'date': 'if [ "$#" = 1 ] && [ "$1" = "+%s" ] && [ -n "$FAKE_NOW" ]; then\n'
            '  echo "$FAKE_NOW"\nelse\n  exec /bin/date "$@"\nfi',
}


def iso_z(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def iso_cswap(t):
    """cswap's own spelling: fractional seconds and +00:00."""
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.339980+00:00'


def round_away(x):
    return -math.floor(-x + 0.5) if x < 0 else math.floor(x + 0.5)


def reference_delta(members, window_seconds, now=NOW):
    """The contract's formula, independently: members = [(used, resets_at)]."""
    n = len(members)
    base = sum(100 - u for u, _ in members)
    best = None
    for T in sorted({t for _, t in members}):
        avail = base + 100 * sum(1 for _, t in members if t < T)
        if 0 < T - now <= window_seconds:
            v = 100 * (T - now) / window_seconds - avail / n
            best = v if best is None else max(best, v)
    return None if best is None else round_away(best)


def old_pace_delta(used, window_seconds, resets_at, now=NOW):
    """pace_delta, transcribed: the one-account answer the pool must reduce to."""
    until = resets_at - now
    if until <= 0 or until > window_seconds:
        return None
    elapsed = window_seconds - until
    if elapsed == 0 and used > 0:
        return None
    return round_away(used - 100 * elapsed / window_seconds)


class Home:
    def __init__(self, root):
        self.root = Path(root)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name, body in STUBS.items():
            stub = self.bin / name
            stub.write_text('#!/bin/sh\n' + body + '\n')
            stub.chmod(0o700)
        self.cache = self.root / '.cache/codexbar-tmux'
        self.cache.mkdir(parents=True)
        self.usage = self.cache / 'usage.json'
        self.tmux_log = self.root / 'tmux.log'
        self.curl_log = self.root / 'curl.log'
        self.cswap_log = self.root / 'cswap.log'
        self.cswap_dir = self.root / '.claude-swap-backup'
        self.seq_file = self.cswap_dir / 'sequence.json'
        self.cswap_usage = self.cswap_dir / 'cache/usage.json'
        self.login(A2_EMAIL, A2_ORG)
        self.endpoint()

    def login(self, email, org):
        (self.root / '.claude.json').write_text(json.dumps(
            {'projects': {}, 'oauthAccount': {'emailAddress': email, 'organizationUuid': org}}))

    def endpoint(self, session=12, session_resets=A2_SESSION_RESETS,
                 weekly=27, weekly_resets=A2_WEEKLY_RESETS, scoped=(2, A2_WEEKLY_RESETS)):
        """What the fake usage endpoint answers for the ACTIVE account."""
        limits = []
        if scoped is not None:
            limits.append({'kind': 'weekly_scoped', 'percent': scoped[0],
                           'resets_at': iso_z(scoped[1]),
                           'scope': {'model': {'display_name': 'Fable'}}})
        (self.root / 'usage-fixture.json').write_text(json.dumps({
            'five_hour': {'utilization': session,
                          'resets_at': iso_z(session_resets) if session_resets else None},
            'seven_day': {'utilization': weekly, 'resets_at': iso_z(weekly_resets)},
            'limits': limits}))

    def cswap(self, slot1=None, slot2=None, seq_extra=None, sequence=(1, 2), disabled=()):
        """sequence.json and cache/usage.json as claude-swap writes them.

        slot1/slot2 override the lastGood readings (None keeps today's)."""
        self.cswap_usage.parent.mkdir(parents=True, exist_ok=True)
        accounts = {
            '1': {'email': A1_EMAIL, 'organizationUuid': A1_ORG, 'organizationName': 'One',
                  'uuid': 'u1', 'added': '2026-01-01'},
            '2': {'email': A2_EMAIL, 'organizationUuid': A2_ORG, 'organizationName': 'Two',
                  'uuid': 'u2', 'added': '2026-01-01'},
        }
        accounts.update(seq_extra or {})
        for s in disabled:
            accounts[str(s)]['disabled'] = True
        self.seq_file.write_text(json.dumps({'sequence': list(sequence),
                                             'activeAccountNumber': 2,
                                             'accounts': accounts}))
        today1 = {'fetchedAt': NOW - 600 + 0.37,
                  'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                               'seven_day': {'pct': 90.0,
                                             'resets_at': iso_cswap(A1_WEEKLY_RESETS)},
                               'scoped': [{'name': 'Fable', 'pct': 2.0,
                                           'resets_at': iso_cswap(A1_WEEKLY_RESETS)}]}}
        # cswap's copy of the ACTIVE account, deliberately different from the
        # endpoint fixture: the pool must use the rendered numbers instead.
        today2 = {'fetchedAt': NOW - 300 + 0.41,
                  'lastGood': {'five_hour': {'pct': 50.0, 'resets_at': iso_cswap(NOW + HOUR)},
                               'seven_day': {'pct': 60.0, 'resets_at': iso_cswap(NOW + DAY)},
                               'scoped': []}}
        u1 = dict(email=A1_EMAIL, organizationUuid=A1_ORG, **(slot1 or today1))
        u2 = dict(email=A2_EMAIL, organizationUuid=A2_ORG, **(slot2 or today2))
        self.cswap_usage.write_text(json.dumps({'schemaVersion': 2,
                                                'accounts': {'1': u1, '2': u2}}))

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ('CLAUDE_CONFIG_DIR', 'TMUX', 'TMUX_PANE')
               and not k.startswith('CODEXBAR_')}
        env.update(HOME=str(self.root), PATH=f'{self.bin}:/opt/homebrew/bin:/usr/bin:/bin',
                   TMUX_TMPDIR=str(self.root), FAKE_NOW=str(NOW),
                   FAKE_TMUX_LOG=str(self.tmux_log), FAKE_CURL_LOG=str(self.curl_log),
                   FAKE_CSWAP_LOG=str(self.cswap_log),
                   FAKE_USAGE=str(self.root / 'usage-fixture.json'),
                   CODEXBAR_USAGE_PROVIDERS='claude',
                   CODEXBAR_USAGE_FORCE_REFRESH='1')
        env.update(extra)
        return env

    def refresh(self, **extra):
        self.tmux_log.unlink(missing_ok=True)
        result = subprocess.run(['/bin/bash', str(STATUS), '--refresh'], capture_output=True,
                                text=True, timeout=30, env=self.env(**extra))
        assert result.returncode == 0, (result.returncode, result.stderr)
        block = json.loads(self.usage.read_text())['providers']['claude']
        assert block['state'] == 'ok', block
        assert block['updated_at'] == NOW, block['updated_at']
        return block

    def published(self, option):
        prefix = f'set-option -gq {option} '
        value = None
        if self.tmux_log.exists():
            for line in self.tmux_log.read_text().splitlines():
                if line.startswith(prefix):
                    value = line[len(prefix):]
        return value


class PoolPaceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='codexbar-pool-')
        self.home = Home(self.tmp)

    def tearDown(self):
        self.assertFalse(self.home.cswap_log.exists(), 'cswap was called')
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertAccountScope(self, pace, used, resets, delta):
        self.assertEqual(pace['scope'], 'account')
        self.assertEqual(pace['accounts'], 1)
        self.assertEqual(pace['used'], used)
        self.assertEqual(pace['horizons'],
                         [] if resets is None else [{'at': resets, 'avail': 100 - used}])
        self.assertEqual(pace['delta'], delta)
        self.assertEqual(len(pace['members']), 1)
        self.assertTrue(pace['members'][0]['active'])


class WorkedExampleTests(PoolPaceTestCase):
    def test_todays_numbers(self):
        self.home.cswap()
        block = self.home.refresh()

        weekly = block['weekly_pace']
        self.assertEqual(weekly, {
            'scope': 'pool', 'accounts': 2, 'window_minutes': 10080, 'used': 59,
            'horizons': [{'at': A1_WEEKLY_RESETS, 'avail': 83},
                         {'at': A2_WEEKLY_RESETS, 'avail': 183}],
            'delta': 16,
            'members': [
                {'slot': '1', 'email': A1_EMAIL, 'used': 90, 'resets_at': A1_WEEKLY_RESETS,
                 'active': False, 'as_of': NOW - 600},
                {'slot': '2', 'email': A2_EMAIL, 'used': 27, 'resets_at': A2_WEEKLY_RESETS,
                 'active': True, 'as_of': NOW},
            ]})
        self.assertEqual(reference_delta([(90, A1_WEEKLY_RESETS), (27, A2_WEEKLY_RESETS)],
                                         WEEK_W), 16)
        # The number is still the active account's; the delta is the pool's.
        self.assertEqual(block['weekly_used'], 27)
        self.assertEqual(block['weekly_text'], '27% (Σ+16%)')
        self.assertEqual(block['weekly_pace_color'], 'red')       # +16 > 15
        self.assertEqual(block['weekly_color'], 'red')
        self.assertEqual(self.home.published('@codex_weekly_text'), '27% (Σ+16%)')
        self.assertEqual(self.home.published('@codex_weekly_color'), 'red')

        # Session: account 1 is idle (0%, no reset) and counts as a fresh window.
        session = block['session_pace']
        self.assertEqual(session['scope'], 'pool')
        self.assertEqual(session['window_minutes'], 300)
        self.assertEqual(session['members'][0],
                         {'slot': '1', 'email': A1_EMAIL, 'used': 0,
                          'resets_at': NOW + SESSION_W, 'active': False, 'as_of': NOW - 600})
        self.assertEqual(session['horizons'],
                         [{'at': A2_SESSION_RESETS, 'avail': 188},
                          {'at': NOW + SESSION_W, 'avail': 288}])
        self.assertEqual(session['delta'], -14)
        self.assertEqual(session['used'], 6)
        self.assertEqual(block['session_text'], '12% (Σ-14%)')
        self.assertEqual(block['session_pace_color'], 'green')

        scoped = block['scoped_pace']
        self.assertEqual(scoped['scope'], 'pool')
        self.assertEqual([(m['slot'], m['used']) for m in scoped['members']],
                         [('1', 2), ('2', 2)])
        self.assertEqual(scoped['delta'],
                         reference_delta([(2, A1_WEEKLY_RESETS), (2, A2_WEEKLY_RESETS)], WEEK_W))
        self.assertTrue(block['scoped_text'].startswith('2% (Σ'))

    def test_scoped_member_without_a_matching_entry_is_left_out(self):
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                              'seven_day': {'pct': 90.0, 'resets_at': iso_cswap(A1_WEEKLY_RESETS)},
                              'scoped': [{'name': 'Other', 'pct': 50.0,
                                          'resets_at': iso_cswap(A1_WEEKLY_RESETS)}]}}
        self.home.cswap(slot1=slot1)
        block = self.home.refresh()
        self.assertEqual(block['weekly_pace']['scope'], 'pool')
        self.assertAccountScope(block['scoped_pace'], 2, A2_WEEKLY_RESETS,
                                old_pace_delta(2, WEEK_W, A2_WEEKLY_RESETS))
        self.assertEqual(block['scoped_pace']['members'][0]['slot'], '2')
        self.assertNotIn('Σ', block['scoped_text'])


class SingleAccountTests(PoolPaceTestCase):
    def test_formula_reduces_to_pace_delta(self):
        for used in (0, 5, 27, 50, 90, 100):
            for until in (60, HOUR, DAY, 3 * DAY, 6 * DAY, WEEK_W - 1, WEEK_W):
                with self.subTest(used=used, until=until):
                    old = old_pace_delta(used, WEEK_W, NOW + until)
                    if until == WEEK_W and used > 0:
                        # pace_delta's own guard (a window that has not started
                        # yet cannot have usage) is the one place they differ,
                        # which is why one account keeps going through it.
                        self.assertIsNone(old)
                        continue
                    self.assertEqual(reference_delta([(used, NOW + until)], WEEK_W), old)

    def test_one_member_publishes_exactly_what_it_used_to(self):
        cases = [(27, A2_WEEKLY_RESETS), (90, NOW + DAY), (5, NOW + 6 * DAY), (60, NOW + 2 * HOUR)]
        for weekly, resets in cases:
            with self.subTest(weekly=weekly, resets=resets):
                self.home.endpoint(weekly=weekly, weekly_resets=resets)
                # No cswap at all, and a cswap whose only other slot is disabled.
                shutil.rmtree(self.home.cswap_dir, ignore_errors=True)
                bare = self.home.refresh()
                self.home.cswap(disabled=(1,))
                lone = self.home.refresh()
                off = self.home.refresh(CODEXBAR_USAGE_POOL_PACE='off')
                delta = old_pace_delta(weekly, WEEK_W, resets)
                suffix = '' if delta is None else f' ({delta:+d}%)'
                for block in (bare, lone, off):
                    self.assertEqual(block['weekly_text'], f'{weekly}%{suffix}')
                    self.assertAccountScope(block['weekly_pace'], weekly, resets, delta)
                for key in ('weekly_text', 'weekly_color', 'weekly_pace_color',
                            'session_text', 'session_color', 'session_pace_color',
                            'scoped_text', 'scoped_color', 'scoped_pace_color'):
                    self.assertEqual(bare[key], lone[key], key)
                    self.assertEqual(bare[key], off[key], key)
                # The slot is known whenever cswap was read.
                self.assertEqual(lone['weekly_pace']['members'][0]['slot'], '2')
                self.assertIsNone(bare['weekly_pace']['members'][0]['slot'])


class MemberTests(PoolPaceTestCase):
    def test_passed_reset_rolls_forward(self):
        passed = NOW - HOUR
        slot1 = {'fetchedAt': NOW - 2 * DAY + 60,
                 'lastGood': {'five_hour': {'pct': 80.0, 'resets_at': iso_cswap(NOW - 9 * HOUR)},
                              'seven_day': {'pct': 90.0, 'resets_at': iso_cswap(passed)},
                              'scoped': []}}
        self.home.cswap(slot1=slot1)
        block = self.home.refresh()
        weekly = block['weekly_pace']
        self.assertEqual(weekly['members'][0]['used'], 0)
        self.assertEqual(weekly['members'][0]['resets_at'], passed + WEEK_W)
        self.assertEqual(weekly['delta'],
                         reference_delta([(0, passed + WEEK_W), (27, A2_WEEKLY_RESETS)], WEEK_W))
        # A passed SESSION reset is not rolled forward: 5-hour windows are not
        # on a fixed cycle, so the account is idle (a fresh window from now).
        session = block['session_pace']
        m1 = session['members'][0]
        self.assertEqual((m1['used'], m1['resets_at']), (0, NOW + SESSION_W))
        self.assertEqual(session['horizons'],
                         [{'at': A2_SESSION_RESETS, 'avail': 188},
                          {'at': NOW + SESSION_W, 'avail': 288}])
        self.assertEqual(session['delta'], -14)
        self.assertEqual(session['delta'],
                         reference_delta([(0, NOW + SESSION_W), (12, A2_SESSION_RESETS)], SESSION_W))
        self.assertEqual(block['session_text'], '12% (Σ-14%)')

        # The same for the ACTIVE account's own passed session reset.
        self.home.endpoint(session=50, session_resets=NOW - HOUR)
        session = self.home.refresh()['session_pace']
        self.assertEqual(session['members'][1],
                         {'slot': '2', 'email': A2_EMAIL, 'used': 0,
                          'resets_at': NOW + SESSION_W, 'active': True, 'as_of': NOW})
        self.assertEqual(session['delta'], 0)

    def test_tied_reset_times_make_one_horizon(self):
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                              'seven_day': {'pct': 90.0, 'resets_at': iso_cswap(A2_WEEKLY_RESETS)},
                              'scoped': []}}
        self.home.cswap(slot1=slot1)
        weekly = self.home.refresh()['weekly_pace']
        # Neither window resets strictly before the other: no refill counted.
        self.assertEqual(weekly['horizons'], [{'at': A2_WEEKLY_RESETS, 'avail': 83}])
        self.assertEqual(weekly['delta'], 35)                 # 76.14 - 41.5 = 34.64
        self.assertEqual(weekly['delta'],
                         reference_delta([(90, A2_WEEKLY_RESETS), (27, A2_WEEKLY_RESETS)], WEEK_W))

    def test_negative_half_rounds_away_from_zero(self):
        # Both reset at mid-window: 100 * 0.5 - (200 - 44 - 27) / 2 = 50 - 64.5 = -14.5
        # exactly, which rounds to -15 (Python's round() would give -14).
        mid = NOW + WEEK_W // 2
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                              'seven_day': {'pct': 44.0, 'resets_at': iso_cswap(mid)},
                              'scoped': []}}
        self.home.cswap(slot1=slot1)
        self.home.endpoint(weekly_resets=mid)
        block = self.home.refresh()
        self.assertEqual(block['weekly_pace']['horizons'], [{'at': mid, 'avail': 129}])
        self.assertEqual(block['weekly_pace']['delta'], -15)
        self.assertEqual(block['weekly_text'], '27% (Σ-15%)')
        self.assertEqual(round(-14.5), -14)        # the trap this guards against

    def test_active_account_at_its_limit_is_red(self):
        # The pool is comfortable (account 1 untouched), so the pool alone would
        # say green — but the login in use is blocked until cswap switches.
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                              'seven_day': {'pct': 0.0, 'resets_at': iso_cswap(NOW + 6 * DAY)},
                              'scoped': []}}
        self.home.cswap(slot1=slot1)
        for active, expected in ((100, 'red'), (96, 'red'), (95, 'red'), (94, 'green')):
            with self.subTest(active=active):
                self.home.endpoint(weekly=active, weekly_resets=NOW + HOUR)
                block = self.home.refresh()
                weekly = block['weekly_pace']
                self.assertEqual(weekly['scope'], 'pool')
                self.assertEqual(weekly['delta'],
                                 reference_delta([(0, NOW + 6 * DAY), (active, NOW + HOUR)], WEEK_W))
                self.assertLessEqual(weekly['delta'], 5)          # pool pace: green
                self.assertEqual(block['weekly_pace_color'], 'green')
                self.assertEqual(block['weekly_color'], expected)
        # The critical level is the configured one.
        self.home.endpoint(weekly=94, weekly_resets=NOW + HOUR)
        self.assertEqual(self.home.refresh(CODEXBAR_USAGE_CRITICAL_PERCENT='90')['weekly_color'],
                         'red')

    def test_disabled_slot_is_not_a_member(self):
        self.home.cswap(disabled=(1,))
        block = self.home.refresh()
        for k in ('session', 'weekly', 'scoped'):
            self.assertEqual(block[f'{k}_pace']['scope'], 'account', k)
        self.assertEqual(block['weekly_text'], '27% (+3%)')
        # A third, disabled slot next to two live ones changes nothing.
        three = {'3': {'email': 'third@example.com', 'organizationUuid': 'org-3',
                       'disabled': True}}
        self.home.cswap(seq_extra=three, sequence=(1, 2, 3))
        self.assertEqual(self.home.refresh()['weekly_pace']['accounts'], 2)
        # Nor is a slot that is not in `sequence`.
        self.home.cswap(sequence=(2,))
        self.assertEqual(self.home.refresh()['weekly_pace']['scope'], 'account')

    def test_stale_member_is_left_out(self):
        base = json.loads(json.dumps({
            'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                         'seven_day': {'pct': 90.0, 'resets_at': iso_cswap(A1_WEEKLY_RESETS)},
                         'scoped': []}}))
        self.home.cswap(slot1=dict(base, fetchedAt=NOW - 49 * HOUR))
        block = self.home.refresh()
        self.assertEqual(block['weekly_pace']['scope'], 'account')
        self.assertEqual(block['weekly_text'], '27% (+3%)')
        self.home.cswap(slot1=dict(base, fetchedAt=NOW - 47 * HOUR))
        block = self.home.refresh()
        self.assertEqual(block['weekly_pace']['scope'], 'pool')
        self.assertEqual(block['weekly_pace']['members'][0]['as_of'], NOW - 47 * HOUR)

    def test_idle_active_session(self):
        self.home.cswap()
        self.home.endpoint(session=0, session_resets=None)
        block = self.home.refresh()
        # The text still says idle, in gray; the pool still has a pace object,
        # with the idle account counted as a fresh, full window.
        self.assertEqual(block['session_text'], 'idle')
        self.assertEqual(block['session_color'], 'brightblack')
        session = block['session_pace']
        self.assertEqual(session['scope'], 'pool')
        self.assertEqual([(m['used'], m['resets_at']) for m in session['members']],
                         [(0, NOW + SESSION_W), (0, NOW + SESSION_W)])
        self.assertEqual(session['horizons'], [{'at': NOW + SESSION_W, 'avail': 200}])
        self.assertEqual(session['delta'], 0)

    def test_matched_by_account_not_by_slot_number(self):
        # cswap's usage cache keyed the other way round: still matched by account.
        self.home.cswap()
        data = json.loads(self.home.cswap_usage.read_text())
        data['accounts'] = {'1': data['accounts']['2'], '2': data['accounts']['1']}
        self.home.cswap_usage.write_text(json.dumps(data))
        block = self.home.refresh()
        self.assertEqual(block['weekly_pace']['delta'], 16)
        self.assertEqual(block['weekly_pace']['members'][0]['used'], 90)


class RenderPathTests(PoolPaceTestCase):
    def test_cswap_adoption_renders_the_pool(self):
        # A fetched block with no pool in it (toggle off for that render)...
        self.home.cswap()
        block = self.home.refresh(CODEXBAR_USAGE_POOL_PACE='off')
        self.assertEqual(block['weekly_pace']['scope'], 'account')
        # ...then cswap polls the ACTIVE account 30s later, and a --tick a
        # minute on adopts that reading (adopt_cswap_claude), not a fetch.
        data = json.loads(self.home.cswap_usage.read_text())
        data['accounts']['2']['fetchedAt'] = NOW + 30.25
        data['accounts']['2']['lastGood'] = {
            'five_hour': {'pct': 20.0, 'resets_at': iso_cswap(A2_SESSION_RESETS)},
            'seven_day': {'pct': 30.0, 'resets_at': iso_cswap(A2_WEEKLY_RESETS)},
            'scoped': []}
        self.home.cswap_usage.write_text(json.dumps(data))
        curl_before = self.home.curl_log.read_text()
        self.home.tmux_log.unlink(missing_ok=True)
        env = self.home.env(FAKE_NOW=str(NOW + 60))
        env.pop('CODEXBAR_USAGE_FORCE_REFRESH')
        result = subprocess.run(['/bin/bash', str(STATUS), '--tick'], capture_output=True,
                                text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.home.curl_log.read_text(), curl_before)     # no fetch
        block = json.loads(self.home.usage.read_text())['providers']['claude']
        self.assertEqual(block['endpoint_at'], NOW + 30)                 # adopted
        self.assertEqual(block['updated_at'], NOW + 30)
        self.assertEqual(block['weekly_used'], 30)
        weekly = block['weekly_pace']
        now = NOW + 30
        self.assertEqual(weekly['scope'], 'pool')
        self.assertEqual([(m['slot'], m['used'], m['active'], m['as_of'])
                          for m in weekly['members']],
                         [('1', 90, False, NOW - 600), ('2', 30, True, now)])
        self.assertEqual(weekly['horizons'], [{'at': A1_WEEKLY_RESETS, 'avail': 80},
                                              {'at': A2_WEEKLY_RESETS, 'avail': 180}])
        delta = reference_delta([(90, A1_WEEKLY_RESETS), (30, A2_WEEKLY_RESETS)], WEEK_W, now)
        self.assertEqual(weekly['delta'], delta)
        self.assertEqual(block['weekly_text'], f'30% (Σ{delta:+d}%)')
        self.assertEqual(block['session_pace']['scope'], 'pool')
        # The tick publishes before it adopts; the adopted text reaches tmux
        # through commit_claude_rerender's own publish.
        self.assertEqual(self.home.published('@codex_weekly_text'), f'30% (Σ{delta:+d}%)')


class ToggleAndFailureTests(PoolPaceTestCase):
    def test_toggle_off(self):
        self.home.cswap()
        for value in ('off', '0', 'false', 'NO'):
            with self.subTest(value=value):
                block = self.home.refresh(CODEXBAR_USAGE_POOL_PACE=value)
                self.assertEqual(block['weekly_text'], '27% (+3%)')
                self.assertEqual(block['weekly_color'], 'green')
                self.assertEqual(block['weekly_pace_color'], 'green')
                self.assertAccountScope(block['weekly_pace'], 27, A2_WEEKLY_RESETS, 3)
                self.assertAccountScope(block['session_pace'], 12, A2_SESSION_RESETS,
                                        old_pace_delta(12, SESSION_W, A2_SESSION_RESETS))
        block = self.home.refresh(CODEXBAR_USAGE_POOL_PACE='on')
        self.assertEqual(block['weekly_pace']['scope'], 'pool')

    def test_missing_or_garbage_cswap_files(self):
        def check(label):
            block = self.home.refresh()
            with self.subTest(label):
                self.assertEqual(block['weekly_text'], '27% (+3%)')
                self.assertAccountScope(block['weekly_pace'], 27, A2_WEEKLY_RESETS, 3)

        check('no cswap at all')
        self.home.cswap()
        self.home.cswap_usage.unlink()
        check('no usage cache')
        self.home.cswap()
        self.home.seq_file.unlink()
        check('no sequence')
        self.home.cswap()
        self.home.seq_file.write_text('{"sequence": [1, 2], "accounts": ')
        check('torn sequence')
        self.home.cswap()
        self.home.cswap_usage.write_text('not json')
        check('garbage usage cache')
        self.home.cswap()
        self.home.cswap_usage.write_text('[1, 2, 3]')
        check('usage cache of the wrong shape')
        self.home.cswap()
        self.home.seq_file.write_text('{"sequence": "1,2", "accounts": []}')
        check('sequence of the wrong shape')
        # The logged-in account is not one of cswap's slots.
        self.home.cswap()
        self.home.login('someone@example.com', 'org-x')
        block = self.home.refresh()
        self.assertEqual(block['weekly_pace']['scope'], 'account')
        self.assertIsNone(block['weekly_pace']['members'][0]['slot'])


if __name__ == '__main__':
    unittest.main()
