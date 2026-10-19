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


def _with_caps(members):
    """[(used, resets_at)] or [(used, resets_at, cap)] -> [(used, resets_at, cap)]."""
    return [(m[0], m[1], m[2] if len(m) > 2 else 100) for m in members]


def reference_horizons(members):
    """The contract's horizons: avail(T) = sum max(0, cap - u) + sum of the caps
    of the windows that reset strictly before T."""
    ms = _with_caps(members)
    base = sum(max(0, c - u) for u, _, c in ms)
    return [{'at': T, 'avail': base + sum(c for _, t, c in ms if t < T)}
            for T in sorted({t for _, t, _ in ms})]


def reference_level(members):
    """The contract's level: 100 * sum min(u, cap) / C, half away from zero."""
    ms = _with_caps(members)
    return round_away(100 * sum(min(u, c) for u, _, c in ms) / sum(c for _, _, c in ms))


def reference_delta(members, window_seconds, now=NOW):
    """The contract's formula, independently: members = [(used, resets_at[, cap])],
    cap 100 when not given. With every cap 100 this is the pre-reserve formula
    (C = 100 n, so 100 avail / C = avail / n)."""
    ms = _with_caps(members)
    C = sum(c for _, _, c in ms)
    best = None
    for h in reference_horizons(ms):
        T = h['at']
        if 0 < T - now <= window_seconds:
            v = 100 * (T - now) / window_seconds - 100 * h['avail'] / C
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

    def cswap(self, slot1=None, slot2=None, seq_extra=None, sequence=(1, 2), disabled=(),
              reserves=None):
        """sequence.json and cache/usage.json as claude-swap writes them.

        slot1/slot2 override the lastGood readings (None keeps today's).
        reserves: {slot: weeklyReservePct value, written as given}."""
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
        for s, pct in (reserves or {}).items():
            accounts[str(s)]['weeklyReservePct'] = pct
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

    def pace_opts(self):
        """The @codex_<w>_{used,expected,delta,resets,members} options, from the
        LAST tmux invocation that set them, asserting that every invocation
        setting any of them set all fifteen at once."""
        names = [f'@codex_{w}_{f}' for w in ('session', 'weekly', 'scoped')
                 for f in ('used', 'expected', 'delta', 'resets', 'members')]
        found = None
        for line in self.tmux_log.read_text().splitlines():
            if not any(f' {n} ' in f'{line} ' for n in names):
                continue
            opts = {}
            for part in line.split(' ; '):
                cmd, flags, name, value = (part.split(' ', 3) + [''])[:4]
                assert (cmd, flags) == ('set-option', '-gq'), part
                opts[name] = value
            assert sorted(opts) == sorted(names), (line, sorted(opts))
            found = opts
        assert found is not None, 'pace options never published'
        return found


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
        # No pool reset in account scope: the block's own reset is the one.
        self.assertNotIn('resets_at', pace)


class WorkedExampleTests(PoolPaceTestCase):
    def test_todays_numbers(self):
        self.home.cswap()
        block = self.home.refresh()

        weekly = block['weekly_pace']
        self.assertEqual(weekly, {
            'scope': 'pool', 'accounts': 2, 'capacity': 200, 'window_minutes': 10080,
            'used': 59, 'resets_at': A1_WEEKLY_RESETS,
            'horizons': [{'at': A1_WEEKLY_RESETS, 'avail': 83},
                         {'at': A2_WEEKLY_RESETS, 'avail': 183}],
            'delta': 16,
            'members': [
                {'slot': '1', 'email': A1_EMAIL, 'used': 90, 'resets_at': A1_WEEKLY_RESETS,
                 'active': False, 'as_of': NOW - 600, 'cap': 100},
                {'slot': '2', 'email': A2_EMAIL, 'used': 27, 'resets_at': A2_WEEKLY_RESETS,
                 'active': True, 'as_of': NOW, 'cap': 100},
            ]})
        self.assertEqual(reference_delta([(90, A1_WEEKLY_RESETS), (27, A2_WEEKLY_RESETS)],
                                         WEEK_W), 16)
        # The text is the pool's level and delta; weekly_used stays the
        # active account's own reading.
        self.assertEqual(block['weekly_used'], 27)
        self.assertEqual(block['weekly_text'], '59% (+16%)')
        self.assertEqual(block['weekly_pace_color'], 'red')       # +16 > 15
        self.assertEqual(block['weekly_color'], 'red')
        self.assertEqual(self.home.published('@codex_weekly_text'), '59% (+16%)')
        self.assertEqual(self.home.published('@codex_weekly_color'), 'red')

        # Session: never pooled — the active account's own 12%, paced alone.
        session_delta = old_pace_delta(12, SESSION_W, A2_SESSION_RESETS)
        self.assertEqual(session_delta, -8)
        self.assertAccountScope(block['session_pace'], 12, A2_SESSION_RESETS, session_delta)
        self.assertEqual(block['session_pace']['members'][0]['slot'], '2')
        self.assertEqual(block['session_used'], 12)
        self.assertEqual(block['session_text'], '12% (-8%)')
        self.assertEqual(block['session_pace_color'], 'green')

        scoped = block['scoped_pace']
        self.assertEqual(scoped['scope'], 'pool')
        self.assertEqual(scoped['capacity'], 200)
        self.assertEqual([(m['slot'], m['used'], m['cap']) for m in scoped['members']],
                         [('1', 2, 100), ('2', 2, 100)])
        self.assertEqual(scoped['delta'],
                         reference_delta([(2, A1_WEEKLY_RESETS), (2, A2_WEEKLY_RESETS)], WEEK_W))
        self.assertEqual(block['scoped_text'], f"2% ({scoped['delta']:+d}%)")

        # The plain-number options, all fifteen in one tmux invocation. The
        # pooled windows' resets are the POOL's (account 1's, the earlier);
        # the session's is the active account's own.
        self.assertEqual(self.home.pace_opts(), {
            '@codex_session_used': '12', '@codex_session_expected': '20',
            '@codex_session_delta': '-8', '@codex_session_resets': str(A2_SESSION_RESETS),
            '@codex_session_members': '',
            '@codex_weekly_used': '59', '@codex_weekly_expected': '43',
            '@codex_weekly_delta': '+16', '@codex_weekly_resets': str(A1_WEEKLY_RESETS),
            '@codex_weekly_members': '1:90:0:100,2:27:1:100',
            '@codex_scoped_used': '2', '@codex_scoped_expected': '42',
            '@codex_scoped_delta': '-40', '@codex_scoped_resets': str(A1_WEEKLY_RESETS),
            '@codex_scoped_members': '1:2:0:100,2:2:1:100',
        })

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
        d = old_pace_delta(2, WEEK_W, A2_WEEKLY_RESETS)
        self.assertEqual(block['scoped_text'], f'2% ({d:+d}%)')
        opts = self.home.pace_opts()
        self.assertEqual((opts['@codex_scoped_used'], opts['@codex_scoped_delta'],
                          opts['@codex_scoped_members']), ('2', f'{d:+d}', ''))
        self.assertEqual(opts['@codex_weekly_members'], '1:90:0:100,2:27:1:100')


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
                # Plain numbers in account scope: the account's own, no members.
                opts = self.home.pace_opts()
                self.assertEqual(
                    (opts['@codex_weekly_used'], opts['@codex_weekly_delta'],
                     opts['@codex_weekly_expected'], opts['@codex_weekly_resets'],
                     opts['@codex_weekly_members']),
                    (str(weekly), '' if delta is None else f'{delta:+d}',
                     '' if delta is None else str(min(100, max(0, weekly - delta))),
                     str(resets), ''))


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
        # The other account's session (passed or not) never reaches the
        # session: that window is the active account's alone.
        self.assertAccountScope(block['session_pace'], 12, A2_SESSION_RESETS,
                                old_pace_delta(12, SESSION_W, A2_SESSION_RESETS))
        self.assertEqual(block['session_text'], '12% (-8%)')

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
        self.assertEqual(block['weekly_text'], '36% (-15%)')       # mean 35.5 -> 36
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
        # The text says idle, in gray; the session is the account's own.
        self.assertEqual(block['session_text'], 'idle')
        self.assertEqual(block['session_color'], 'brightblack')
        self.assertEqual(block['session_pace']['scope'], 'account')
        # An idle window publishes no numbers; the others still do.
        opts = self.home.pace_opts()
        self.assertEqual([opts[f'@codex_session_{f}'] for f in
                          ('used', 'expected', 'delta', 'resets', 'members')], [''] * 5)
        self.assertEqual(opts['@codex_weekly_used'], '59')

    def test_session_is_never_pooled(self):
        # The active account is idle, account 1 is 20% into an open window:
        # the session is still the active account's own, so it is "idle".
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 20.0, 'resets_at': iso_cswap(NOW + 2 * HOUR)},
                              'seven_day': {'pct': 90.0, 'resets_at': iso_cswap(A1_WEEKLY_RESETS)},
                              'scoped': []}}
        self.home.cswap(slot1=slot1)
        self.home.endpoint(session=0, session_resets=None)
        block = self.home.refresh()
        self.assertEqual(block['session_pace']['scope'], 'account')
        self.assertEqual(block['session_text'], 'idle')
        self.assertEqual(block['session_color'], 'brightblack')
        self.assertEqual(block['weekly_pace']['scope'], 'pool')
        opts = self.home.pace_opts()
        self.assertEqual({opts[f'@codex_session_{f}'] for f in
                          ('used', 'expected', 'delta', 'resets', 'members')}, {''})
        # The active account busy: its own number and pace, never the mean.
        self.home.endpoint(session=30, session_resets=NOW + 4 * HOUR)
        block = self.home.refresh()
        delta = old_pace_delta(30, SESSION_W, NOW + 4 * HOUR)
        self.assertAccountScope(block['session_pace'], 30, NOW + 4 * HOUR, delta)
        self.assertEqual(block['session_text'], f'30% ({delta:+d}%)')
        opts = self.home.pace_opts()
        self.assertEqual((opts['@codex_session_used'], opts['@codex_session_delta'],
                          opts['@codex_session_members']), ('30', f'{delta:+d}', ''))

    def test_matched_by_account_not_by_slot_number(self):
        # cswap's usage cache keyed the other way round: still matched by account.
        self.home.cswap()
        data = json.loads(self.home.cswap_usage.read_text())
        data['accounts'] = {'1': data['accounts']['2'], '2': data['accounts']['1']}
        self.home.cswap_usage.write_text(json.dumps(data))
        block = self.home.refresh()
        self.assertEqual(block['weekly_pace']['delta'], 16)
        self.assertEqual(block['weekly_pace']['members'][0]['used'], 90)


class ReserveTests(PoolPaceTestCase):
    """sequence.json accounts["<n>"].weeklyReservePct: the weekly pool is measured
    against the caps (100 - reserve), not 100 per account."""

    def test_reserve_on_slot_1(self):
        self.home.cswap(reserves={1: 10})
        block = self.home.refresh()
        weekly = block['weekly_pace']
        members = [(90, A1_WEEKLY_RESETS, 90), (27, A2_WEEKLY_RESETS, 100)]
        self.assertEqual(weekly['scope'], 'pool')
        self.assertEqual(weekly['capacity'], 190)
        self.assertEqual([(m['slot'], m['used'], m['cap']) for m in weekly['members']],
                         [('1', 90, 90), ('2', 27, 100)])
        # Level: 100 * (90 + 27) / 190 = 61.58 -> 62 (the cap-100 pool says 59).
        self.assertEqual(weekly['used'], 62)
        self.assertEqual(weekly['used'], reference_level(members))
        # Slot 1 is at its cap: nothing left before its reset, and at its reset
        # it refills by 90, not 100.
        self.assertEqual(weekly['horizons'], [{'at': A1_WEEKLY_RESETS, 'avail': 73},
                                              {'at': A2_WEEKLY_RESETS, 'avail': 163}])
        self.assertEqual(weekly['horizons'], reference_horizons(members))
        # 100*4.04/7 - 100*73/190 = 57.71 - 38.42 = +19.29
        self.assertEqual(weekly['delta'], 19)
        self.assertEqual(weekly['delta'], reference_delta(members, WEEK_W))
        self.assertEqual(block['weekly_text'], '62% (+19%)')
        self.assertEqual(block['weekly_used'], 27)       # still the account's own
        opts = self.home.pace_opts()
        self.assertEqual(opts['@codex_weekly_members'], '1:90:0:90,2:27:1:100')
        self.assertEqual((opts['@codex_weekly_used'], opts['@codex_weekly_delta']),
                         ('62', '+19'))

        # The reserve is the 7-day window only: scoped and session as before.
        self.home.cswap()
        plain = self.home.refresh()
        for key in ('scoped_pace', 'scoped_text', 'scoped_color', 'session_pace',
                    'session_text', 'session_color'):
            self.assertEqual(block[key], plain[key], key)
        self.assertEqual(block['scoped_pace']['capacity'], 200)
        self.assertEqual({m['cap'] for m in block['scoped_pace']['members']}, {100})
        self.assertNotIn('capacity', block['session_pace'])

    def test_contract_example(self):
        # The published shape for: slot 1 at 90% (cap 90) resetting in ~3.3
        # days, slot 2 (active) at 33% resetting in ~4.6 days.
        t1, t2 = NOW + int(3.3 * DAY), NOW + int(4.6 * DAY)
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                              'seven_day': {'pct': 90.0, 'resets_at': iso_cswap(t1)},
                              'scoped': []}}
        self.home.cswap(slot1=slot1, reserves={1: 10})
        self.home.endpoint(weekly=33, weekly_resets=t2)
        block = self.home.refresh()
        members = [(90, t1, 90), (33, t2, 100)]
        weekly = block['weekly_pace']
        self.assertEqual({k: weekly[k] for k in
                          ('scope', 'accounts', 'capacity', 'used', 'horizons', 'delta')},
                         {'scope': 'pool', 'accounts': 2, 'capacity': 190, 'used': 65,
                          'horizons': [{'at': t1, 'avail': 67}, {'at': t2, 'avail': 157}],
                          'delta': 12})
        self.assertEqual(weekly['used'], reference_level(members))
        self.assertEqual(weekly['delta'], reference_delta(members, WEEK_W))
        self.assertEqual(block['weekly_text'], '65% (+12%)')
        self.assertEqual(self.home.pace_opts()['@codex_weekly_members'], '1:90:0:90,2:33:1:100')

    def test_caps_all_100_is_todays_pool(self):
        self.home.cswap()
        baseline = self.home.refresh()
        for reserves in ({1: 0, 2: 0}, {1: None}, {1: 0.0}):
            with self.subTest(reserves=reserves):
                self.home.cswap(reserves=reserves)
                block = self.home.refresh()
                for key in ('weekly_pace', 'weekly_text', 'weekly_color', 'weekly_pace_color',
                            'scoped_pace', 'session_pace'):
                    self.assertEqual(block[key], baseline[key], key)
                self.assertEqual(self.home.pace_opts()['@codex_weekly_members'],
                                 '1:90:0:100,2:27:1:100')

    def test_invalid_reserve_values_are_ignored(self):
        self.home.cswap()
        baseline = self.home.refresh()
        for bad in (-5, 7.5, 50.5, 51, 100, '10', True, [10], {'pct': 10}):
            with self.subTest(bad=bad):
                self.home.cswap(reserves={1: bad})
                block = self.home.refresh()
                self.assertEqual(block['weekly_pace'], baseline['weekly_pace'])
                self.assertEqual(block['weekly_text'], '59% (+16%)')
        # 50 is the largest reserve honoured: cap 50.
        self.home.cswap(reserves={1: 50})
        weekly = self.home.refresh()['weekly_pace']
        self.assertEqual(weekly['capacity'], 150)
        self.assertEqual(weekly['members'][0]['cap'], 50)
        members = [(90, A1_WEEKLY_RESETS, 50), (27, A2_WEEKLY_RESETS, 100)]
        self.assertEqual(weekly['used'], reference_level(members))          # 77/150 -> 51
        self.assertEqual(weekly['horizons'], reference_horizons(members))
        self.assertEqual(weekly['delta'], reference_delta(members, WEEK_W))

    def test_member_over_its_cap_clamps(self):
        # Slot 1 was switched into its reserve by hand and is at 95% of a cap
        # of 90: it counts as full (90), never as more, and has nothing left.
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                              'seven_day': {'pct': 95.0, 'resets_at': iso_cswap(A1_WEEKLY_RESETS)},
                              'scoped': []}}
        self.home.cswap(slot1=slot1, reserves={1: 10})
        block = self.home.refresh()
        weekly = block['weekly_pace']
        members = [(95, A1_WEEKLY_RESETS, 90), (27, A2_WEEKLY_RESETS, 100)]
        self.assertEqual(weekly['members'][0]['used'], 95)        # its own reading
        self.assertEqual(weekly['used'], 62)                       # as at exactly 90
        self.assertEqual(weekly['used'], reference_level(members))
        self.assertEqual(weekly['horizons'], [{'at': A1_WEEKLY_RESETS, 'avail': 73},
                                              {'at': A2_WEEKLY_RESETS, 'avail': 163}])
        self.assertEqual(weekly['delta'], reference_delta(members, WEEK_W))
        self.assertEqual(weekly['delta'], 19)
        self.assertEqual(self.home.pace_opts()['@codex_weekly_members'], '1:95:0:90,2:27:1:100')

    def test_active_account_at_its_cap_is_red(self):
        # The active slot 2 keeps 10% back; slot 1 is untouched, so the pool's
        # own pace is green throughout.
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                              'seven_day': {'pct': 0.0, 'resets_at': iso_cswap(NOW + 6 * DAY)},
                              'scoped': []}}
        for reserve, active, expected in ((10, 92, 'red'), (10, 90, 'red'), (10, 89, 'green'),
                                          (None, 90, 'green'), (15, 85, 'red'), (15, 84, 'green')):
            with self.subTest(reserve=reserve, active=active):
                self.home.cswap(slot1=slot1, reserves={2: reserve})
                self.home.endpoint(weekly=active, weekly_resets=NOW + HOUR)
                block = self.home.refresh()
                weekly = block['weekly_pace']
                cap = 100 - (reserve or 0)
                self.assertEqual(weekly['scope'], 'pool')
                self.assertEqual(weekly['members'][1]['cap'], cap)
                self.assertEqual(weekly['delta'],
                                 reference_delta([(0, NOW + 6 * DAY), (active, NOW + HOUR, cap)],
                                                 WEEK_W))
                self.assertEqual(block['weekly_pace_color'], 'green')
                self.assertEqual(block['weekly_color'], expected)
        # The scoped window has no reserve: the active account at 90% there
        # is not at its cap.
        self.home.cswap(slot1={'fetchedAt': NOW - 60,
                               'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                                            'seven_day': {'pct': 0.0,
                                                          'resets_at': iso_cswap(NOW + 6 * DAY)},
                                            'scoped': [{'name': 'Fable', 'pct': 0.0,
                                                        'resets_at': iso_cswap(NOW + 6 * DAY)}]}},
                        reserves={2: 10})
        self.home.endpoint(weekly=10, weekly_resets=NOW + HOUR, scoped=(90, NOW + HOUR))
        block = self.home.refresh()
        self.assertEqual(block['scoped_pace']['scope'], 'pool')
        self.assertEqual(block['scoped_pace']['members'][1]['cap'], 100)
        self.assertEqual(block['scoped_pace_color'], 'green')
        self.assertEqual(block['scoped_color'], 'green')


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
        self.assertEqual(block['weekly_text'], f'60% ({delta:+d}%)')      # mean of 90, 30
        self.assertEqual(block['session_pace']['scope'], 'account')
        # The tick publishes before it adopts; the adopted text reaches tmux
        # through commit_claude_rerender's own publish, and so do the numbers.
        self.assertEqual(self.home.published('@codex_weekly_text'), f'60% ({delta:+d}%)')
        opts = self.home.pace_opts()
        self.assertEqual((opts['@codex_weekly_used'], opts['@codex_weekly_delta'],
                          opts['@codex_weekly_members']),
                         ('60', f'{delta:+d}', '1:90:0:100,2:30:1:100'))


class PublishTests(PoolPaceTestCase):
    def tick(self, at):
        """One --tick at FAKE_NOW=at; returns how many tmux calls set the pace options."""
        (self.home.cache / 'last_tick').unlink(missing_ok=True)      # its debounce
        self.home.tmux_log.unlink(missing_ok=True)
        env = self.home.env(FAKE_NOW=str(at))
        env.pop('CODEXBAR_USAGE_FORCE_REFRESH')
        result = subprocess.run(['/bin/bash', str(STATUS), '--tick'], capture_output=True,
                                text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.home.tmux_log.read_text().splitlines() if self.home.tmux_log.exists() else []
        # The text options are published on every tick regardless.
        self.assertIsNotNone(self.home.published('@codex_weekly_text'))
        return sum(1 for line in lines if '@codex_weekly_used' in line)

    def test_tick_skips_the_pace_options_until_something_changes(self):
        self.home.cswap()
        self.home.refresh()                         # a render: publishes, stamps the marker
        marker = self.home.cache / 'pace-opts.published'
        self.assertTrue(marker.exists())
        self.assertEqual(self.tick(NOW + 60), 0)    # cache unchanged: no jq, no tmux
        self.assertEqual(self.tick(NOW + 65), 0)

        # No marker (first run, or it was lost): the tick publishes, once.
        marker.unlink()
        self.assertEqual(self.tick(NOW + 70), 1)
        self.assertEqual(self.home.pace_opts()['@codex_weekly_used'], '59')
        self.assertEqual(self.tick(NOW + 75), 0)

        # A cswap switch with no cache rewrite: the block is now another
        # account's ("--%"), so the numbers empty on the very next tick.
        self.home.login(A1_EMAIL, A1_ORG)
        self.assertEqual(self.tick(NOW + 80), 1)
        self.assertEqual(set(self.home.pace_opts().values()), {''})
        self.assertEqual(self.tick(NOW + 85), 0)
        # And back.
        self.home.login(A2_EMAIL, A2_ORG)
        self.assertEqual(self.tick(NOW + 90), 1)
        self.assertEqual(self.home.pace_opts()['@codex_weekly_used'], '59')

        # A cache rewrite that published nothing itself: the tick catches it.
        later = time.time() + 30
        os.utime(self.home.usage, (later, later))
        self.assertEqual(self.tick(NOW + 95), 1)
        self.assertEqual(self.tick(NOW + 100), 0)

    def test_restarted_tmux_server_gets_them_back(self):
        self.home.cswap()
        self.home.refresh()
        # A server that lists its options but has no @codex_weekly_used: the
        # tick republishes even though the cache has not changed.
        stub = self.home.bin / 'tmux'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$FAKE_TMUX_LOG"\n'
                        'case "$1" in show-options) printf \'%s\\n\' "$FAKE_TMUX_OPTS" ;; esac\n')
        os.environ['FAKE_TMUX_OPTS'] = 'status on'
        try:
            self.assertEqual(self.tick(NOW + 60), 1)
            os.environ['FAKE_TMUX_OPTS'] = 'status on\n@codex_weekly_used 59'
            self.assertEqual(self.tick(NOW + 65), 0)
            os.environ['FAKE_TMUX_OPTS'] = 'status on\n@codex_weekly_used ""'
            self.assertEqual(self.tick(NOW + 70), 0)        # set, if empty
        finally:
            os.environ.pop('FAKE_TMUX_OPTS', None)

    def publish(self):
        self.home.tmux_log.unlink(missing_ok=True)
        result = subprocess.run(['/bin/bash', str(STATUS), '--publish'], capture_output=True,
                                text=True, timeout=30, env=self.home.env())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rewrite_during_a_publish_is_not_marked_published(self):
        self.home.cswap()
        self.home.refresh()
        # A jq wrapper that, the one time it is armed, rewrites (bumps the
        # mtime of) usage.json while publish_pace_opts is reading it.
        real_jq = shutil.which('jq', path='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
        self.assertIsNotNone(real_jq)
        armed = self.home.root / 'bump.armed'
        ref = self.home.root / 'bump.ref'
        ref.touch()
        later = time.time() + 60
        os.utime(ref, (later, later))
        (self.home.bin / 'jq').write_text(
            '#!/bin/sh\n'
            'case "$*" in *\'+ "_pace"\'*)\n'
            f'  if [ -f "{armed}" ]; then rm -f "{armed}"; touch -r "{ref}" "{self.home.usage}"; fi ;;\n'
            'esac\n'
            f'exec "{real_jq}" "$@"\n')
        (self.home.bin / 'jq').chmod(0o700)
        armed.touch()
        self.publish()                          # one ungated publish; the cache moves mid-read
        self.assertFalse(armed.exists(), 'the bump never fired')
        marker = self.home.cache / 'pace-opts.published'
        self.assertLess(marker.stat().st_mtime, self.home.usage.stat().st_mtime)
        self.assertEqual(self.tick(NOW + 60), 1)    # the rewrite is published, not skipped
        self.assertEqual(self.tick(NOW + 65), 0)

    def test_login_needed_publishes_no_numbers(self):
        self.home.cswap()
        self.home.refresh()
        data = json.loads(self.home.usage.read_text())
        data['providers']['claude']['state'] = 'auth_required'
        self.home.usage.write_text(json.dumps(data))
        self.publish()
        self.assertEqual(self.home.published('@codex_weekly_text'), 'Need to log in')
        self.assertEqual(set(self.home.pace_opts().values()), {''})

    def test_another_accounts_block_publishes_no_numbers(self):
        self.home.cswap()
        self.home.refresh()
        self.assertEqual(self.home.pace_opts()['@codex_weekly_used'], '59')
        # cswap switched to account 1; the block is still account 2's ("--%").
        self.home.login(A1_EMAIL, A1_ORG)
        self.home.tmux_log.unlink()
        result = subprocess.run(['/bin/bash', str(STATUS), '--publish'], capture_output=True,
                                text=True, timeout=30, env=self.home.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.home.published('@codex_weekly_text'), '--%')
        self.assertEqual(set(self.home.pace_opts().values()), {''})


class PoolResetTests(PoolPaceTestCase):
    """The pool reset: the earliest member reset, shown wherever the pooled
    level is (the @codex_<w>_resets option and the status line's reset view),
    while the block's <w>_resets_at stays the active account's own."""

    def edit_pace(self, family, **fields):
        """Rewrite fields of the cached <family>_pace in place (a cache whose
        horizons have partly passed since it was written, or an older shape)."""
        data = json.loads(self.home.usage.read_text())
        data['providers']['claude'][f'{family}_pace'].update(fields)
        self.home.usage.write_text(json.dumps(data))

    def publish_resets(self):
        self.home.tmux_log.unlink(missing_ok=True)
        result = subprocess.run(['/bin/bash', str(STATUS), '--publish'], capture_output=True,
                                text=True, timeout=30, env=self.home.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        opts = self.home.pace_opts()
        return opts['@codex_weekly_resets'], opts['@codex_scoped_resets']

    def reset_view(self, mode):
        """What the status-line module prints for `mode` in the reset view."""
        stub = self.home.bin / 'tmux'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$FAKE_TMUX_LOG"\n'
                        'case "$1" in show-options) echo "@codexbar_view reset" ;; esac\n')
        env = self.home.env()
        env.pop('CODEXBAR_USAGE_FORCE_REFRESH')
        result = subprocess.run(['/bin/bash', str(STATUS), mode], capture_output=True,
                                text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_resets_at_is_the_earliest_member_reset(self):
        self.home.cswap()
        block = self.home.refresh()
        self.assertEqual(block['weekly_pace']['resets_at'], A1_WEEKLY_RESETS)
        self.assertEqual(block['weekly_pace']['resets_at'], block['weekly_pace']['horizons'][0]['at'])
        self.assertEqual(block['scoped_pace']['resets_at'], A1_WEEKLY_RESETS)
        # The block's own resets stay the active account's.
        self.assertEqual(block['weekly_resets_at'], A2_WEEKLY_RESETS)
        self.assertEqual(block['scoped_resets_at'], A2_WEEKLY_RESETS)
        # The session is never pooled, so it has none.
        self.assertNotIn('resets_at', block['session_pace'])
        # The ACTIVE account resetting first: it is the pool reset.
        self.home.endpoint(weekly_resets=NOW + HOUR)
        self.assertEqual(self.home.refresh()['weekly_pace']['resets_at'], NOW + HOUR)

    def test_a_rolled_forward_member_reset_counts_as_rolled(self):
        # Slot 1's reset passed 3 days ago: it rolled to NOW + 4 days, which is
        # before the active account's (NOW + 6.5 days), so it is the pool reset.
        passed = NOW - 3 * DAY
        slot1 = {'fetchedAt': NOW - 60,
                 'lastGood': {'five_hour': {'pct': 0.0, 'resets_at': None},
                              'seven_day': {'pct': 90.0, 'resets_at': iso_cswap(passed)},
                              'scoped': []}}
        self.home.cswap(slot1=slot1)
        active_resets = NOW + int(6.5 * DAY)
        self.home.endpoint(weekly_resets=active_resets)
        weekly = self.home.refresh()['weekly_pace']
        self.assertEqual(weekly['members'][0]['resets_at'], passed + WEEK_W)
        self.assertEqual(weekly['resets_at'], passed + WEEK_W)
        self.assertEqual(self.publish_resets()[0], str(passed + WEEK_W))
        # Rolled forward past the active account's reset: the active one is first.
        slot1['lastGood']['seven_day']['resets_at'] = iso_cswap(NOW - HOUR)
        self.home.cswap(slot1=slot1)
        self.home.endpoint()
        weekly = self.home.refresh()['weekly_pace']
        self.assertEqual(weekly['members'][0]['resets_at'], NOW - HOUR + WEEK_W)
        self.assertEqual(weekly['resets_at'], A2_WEEKLY_RESETS)

    def test_resets_option_pool_vs_account_scope(self):
        self.home.cswap()
        self.home.refresh()
        self.assertEqual(self.publish_resets(), (str(A1_WEEKLY_RESETS), str(A1_WEEKLY_RESETS)))
        # Pool off: account scope, the active account's own reset.
        self.home.refresh(CODEXBAR_USAGE_POOL_PACE='off')
        self.assertEqual(self.publish_resets(), (str(A2_WEEKLY_RESETS), str(A2_WEEKLY_RESETS)))
        # One live member: account scope too.
        self.home.cswap(disabled=(1,))
        self.home.refresh()
        self.assertEqual(self.publish_resets(), (str(A2_WEEKLY_RESETS), str(A2_WEEKLY_RESETS)))

    def test_resets_option_reads_past_a_passed_horizon(self):
        self.home.cswap()
        self.home.refresh()
        # The first horizon has passed since the write: the next one, not it.
        self.edit_pace('weekly', resets_at=NOW - 60,
                       horizons=[{'at': NOW - 60, 'avail': 83}, {'at': A1_WEEKLY_RESETS, 'avail': 183}])
        self.assertEqual(self.publish_resets()[0], str(A1_WEEKLY_RESETS))
        # No horizons (an odd cache): resets_at, while it is ahead...
        self.edit_pace('weekly', resets_at=A1_WEEKLY_RESETS, horizons=[])
        self.assertEqual(self.publish_resets()[0], str(A1_WEEKLY_RESETS))
        # ...then the block's own.
        self.edit_pace('weekly', resets_at=NOW - 60, horizons=[])
        self.assertEqual(self.publish_resets()[0], str(A2_WEEKLY_RESETS))
        # A cache from before resets_at: the horizons alone give the pool reset.
        data = json.loads(self.home.usage.read_text())
        del data['providers']['claude']['scoped_pace']['resets_at']
        self.home.usage.write_text(json.dumps(data))
        self.assertEqual(self.publish_resets()[1], str(A1_WEEKLY_RESETS))

    def test_reset_view_shows_the_pool_reset(self):
        # A1 is NOW + 4d0h57m, A2 NOW + 5d7h55m.
        self.home.cswap()
        self.home.refresh(CODEXBAR_USAGE_POOL_PACE='off')
        account = {m: self.reset_view(m) for m in ('weekly', 'scoped')}
        self.home.refresh()
        pooled = {m: self.reset_view(m) for m in ('weekly', 'scoped')}
        for mode in ('weekly', 'scoped'):
            with self.subTest(mode=mode):
                self.assertIn(' in 5d7h', account[mode])
                self.assertIn(' in 4d0h', pooled[mode])
                # The run-out projection is the active account's own in both
                # (its percent, its rate, its reset), so it does not move.
                self.assertEqual(account[mode].partition(' (')[1:],
                                 pooled[mode].partition(' (')[1:])
        self.assertNotEqual(self.reset_view('weekly').partition(' in ')[0],
                            account['weekly'].partition(' in ')[0])
        # A passed first horizon moves on to the next one, as delta(now) does.
        self.edit_pace('weekly', resets_at=NOW - 60,
                       horizons=[{'at': NOW - 60, 'avail': 83}, {'at': A2_WEEKLY_RESETS, 'avail': 183}])
        self.assertEqual(self.reset_view('weekly'), account['weekly'])
        # Nothing ahead: the block's own reset, as in account scope.
        self.edit_pace('weekly', resets_at=NOW - 60, horizons=[{'at': NOW - 60, 'avail': 83}])
        self.assertEqual(self.reset_view('weekly'), account['weekly'])
        # The session view is untouched by any of it.
        self.assertIn(self.reset_view('session').split(' ')[0], ('4h0m', '3h59m'))


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
