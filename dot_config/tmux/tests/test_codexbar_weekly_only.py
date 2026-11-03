"""Weekly-only accounts: a Claude account (or a Codex plan) with NO 5-hour
session window at all.

The usage endpoint answers such an account with "five_hour": null, and
claude-swap stores its lastGood without a five_hour key. The publisher marks
the block `session_limit: false` (Codex-style empty session: session_used
null, "n/a", grey, no pace) and publishes @codex_session_limit "0", which the
status line uses to hide the S module. Only the endpoint or cswap decides
"no limit"; a live sample lacking five_hour never does.

Same harness as test_codexbar_pool_pace (isolated HOME, fake
tmux/security/curl/cswap/date, `date +%s` pinned): no real account, Keychain,
network, cswap or tmux server is touched.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from test_codexbar_pool_pace import (A1_EMAIL, A1_ORG, A1_WEEKLY_RESETS, A2_EMAIL, A2_ORG,
                                     A2_SESSION_RESETS, A2_WEEKLY_RESETS, DAY, HOUR, NOW,
                                     STATUS, Home, iso_cswap, iso_z)

JQ = shutil.which('jq')
A1_KEY, A2_KEY = f'{A1_EMAIL}/{A1_ORG}', f'{A2_EMAIL}/{A2_ORG}'

FABLE = {'kind': 'weekly_scoped', 'percent': 2, 'resets_at': iso_z(A2_WEEKLY_RESETS),
         'scope': {'model': {'display_name': 'Fable'}}}


def weekly_only_payload(weekly=1, limits=None, five_hour=None, drop_five_hour=False):
    body = {'five_hour': five_hour,
            'seven_day': {'utilization': weekly, 'resets_at': iso_z(A2_WEEKLY_RESETS)},
            'limits': [{'kind': 'weekly_all', 'percent': weekly, 'is_active': True}, FABLE]
            if limits is None else limits}
    if drop_five_hour:
        del body['five_hour']
    return body


def weekly_only_slot(fetched_at, weekly, resets):
    """A cswap usage.json reading as claude-swap writes a weekly-only account
    (Home.cswap adds the slot's email and organization)."""
    return {'fetchedAt': fetched_at,
            'lastGood': {'seven_day': {'pct': float(weekly), 'resets_at': iso_cswap(resets)},
                         'scoped': [{'name': 'Fable', 'pct': 0.0, 'resets_at': iso_cswap(resets)}]}}


@unittest.skipUnless(JQ, 'needs jq')
class WeeklyOnlyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='codexbar-weekly-only-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = Home(self.tmp)
        self.backoff = self.home.cache / 'refresh_backoff_claude'
        self.log = self.home.cache / 'usage-refresh.log'

    def tearDown(self):
        self.assertFalse(self.home.cswap_log.exists(), 'cswap was called')

    # ── helpers ──
    def payload(self, body):
        (self.home.root / 'usage-fixture.json').write_text(json.dumps(body))

    def run_status(self, *args, at=NOW, force=True, **extra):
        env = self.home.env(FAKE_NOW=str(at), **extra)
        if not force:
            env.pop('CODEXBAR_USAGE_FORCE_REFRESH')
        result = subprocess.run(['/bin/bash', str(STATUS), *args], capture_output=True,
                                text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def block(self):
        return json.loads(self.home.usage.read_text())['providers']['claude']

    def published_all(self, option):
        prefix = f'set-option -gq {option}'
        values = []
        if self.home.tmux_log.exists():
            for line in self.home.tmux_log.read_text().splitlines():
                for part in line.split(' ; '):
                    if part == prefix:
                        values.append('')
                    elif part.startswith(prefix + ' '):
                        values.append(part[len(prefix) + 1:])
        return values

    def curl_calls(self):
        log = self.home.curl_log
        return len(log.read_text().splitlines()) if log.exists() else 0

    def tick(self, at):
        """One --tick at FAKE_NOW=at. The debounce marker is aged 10 s (not
        removed: a missing one reads as a wake and clears every ladder), and
        cswap's examined-marker is dropped so its file is read again."""
        marker = self.home.cache / 'last_tick'
        marker.touch()
        os.utime(marker, (at - 10, at - 10))
        (self.home.cache / 'cswap-usage.checked').unlink(missing_ok=True)
        self.home.tmux_log.unlink(missing_ok=True)
        return self.run_status('--tick', at=at, force=False)

    def hold_the_poll(self, account_key, until):
        """The tick must not spawn a background fetch: record the account as
        already noticed (a noticed switch clears the ladder) and arm the
        ladder past the test."""
        (self.home.cache / 'claude-account').write_text(account_key + '\n')
        self.backoff.write_text(f'6 {until}\n')

    def assertNoSession(self, block):
        self.assertIs(block['session_limit'], False)
        for key in ('session_used', 'session_window_minutes', 'session_resets_at', 'session_pace'):
            self.assertIsNone(block[key], key)
        self.assertEqual(block['session_text'], 'n/a')
        self.assertEqual(block['session_color'], 'brightblack')
        self.assertEqual(block['session_pace_color'], 'brightblack')
        self.assertEqual(block['session_severity'], 'normal')
        self.assertIs(block['session_locked'], False)


class FetchTests(WeeklyOnlyTestCase):
    def test_five_hour_null_is_a_weekly_only_account(self):
        self.payload(weekly_only_payload(weekly=1))
        block = self.home.refresh()
        self.assertEqual(block['state'], 'ok')
        self.assertNoSession(block)
        self.assertEqual((block['weekly_used'], block['weekly_resets_at']), (1, A2_WEEKLY_RESETS))
        self.assertEqual(block['scoped_used'], 2)
        self.assertEqual(block['weekly_pace']['scope'], 'account')
        self.assertFalse(self.backoff.exists(), 'a weekly-only answer armed the backoff')
        # The root (the primary's flattened block) says the same.
        self.assertIs(json.loads(self.home.usage.read_text())['session_limit'], False)
        # History: the session is null, the week is there.
        row = json.loads((self.home.cache / 'usage-history.jsonl').read_text().splitlines()[-1])
        self.assertEqual((row['s'], row['sr'], row['w']), (None, None, 1))
        # tmux: the flag, the Codex-style text, and empty session numbers.
        self.assertEqual(self.home.published('@codex_session_limit'), '0')
        self.assertEqual(self.home.published('@codex_session_text'), 'n/a')
        self.assertEqual(self.home.published('@codex_session_color'), 'brightblack')
        opts = self.home.pace_opts()
        self.assertEqual([opts[f'@codex_session_{f}'] for f in ('used', 'expected', 'delta', 'resets')],
                         ['', '', '', ''])
        self.assertEqual(opts['@codex_weekly_used'], '1')
        # The raw answer is persisted, and .limits[] is logged once per account.
        self.assertIsNone(json.loads((self.home.cache / 'usage-raw.json').read_text())['five_hour'])
        self.home.refresh()
        logged = [l for l in self.log.read_text().splitlines() if 'weekly-only answer' in l]
        self.assertEqual(len(logged), 1, logged)
        self.assertIn('"kind":"weekly_all"', logged[0])
        self.assertIn(A2_KEY, logged[0])

    def test_five_hour_absent_is_the_same(self):
        self.payload(weekly_only_payload(weekly=3, drop_five_hour=True))
        block = self.home.refresh()
        self.assertNoSession(block)
        self.assertEqual(block['weekly_used'], 3)

    def test_a_null_utilization_with_no_reset_is_the_same(self):
        # Parity with UsageBar's cswap bridge (cswap stores it as pct None).
        self.payload(weekly_only_payload(weekly=5, five_hour={'utilization': None,
                                                               'resets_at': None}))
        block = self.home.refresh()
        self.assertNoSession(block)
        self.assertEqual(block['weekly_used'], 5)
        self.assertFalse(self.backoff.exists())
        self.assertEqual(self.home.published('@codex_session_limit'), '0')

    def test_an_idle_session_entry_beside_a_null_five_hour_is_not_a_window(self):
        # What an idle 5-hour account lists beside its {0, null} five_hour.
        idle = {'kind': 'session', 'percent': 0, 'resets_at': None, 'is_active': False,
                'severity': 'critical', 'locked_reason': 'x'}
        self.payload(weekly_only_payload(limits=[idle, FABLE]))
        block = self.home.refresh()
        self.assertNoSession(block)          # severity/locked of it ignored too

    def test_a_session_moved_to_limits_is_taken_from_there(self):
        entry = {'kind': 'session', 'percent': 23, 'resets_at': iso_z(NOW + 2 * HOUR),
                 'is_active': False}
        self.payload(weekly_only_payload(limits=[entry, FABLE]))
        block = self.home.refresh()
        self.assertIs(block['session_limit'], True)
        self.assertEqual((block['session_used'], block['session_resets_at'],
                          block['session_window_minutes']), (23, NOW + 2 * HOUR, 300))
        self.assertEqual(self.home.published('@codex_session_limit'), '1')

    def test_a_session_window_is_session_limit_true(self):
        block = self.home.refresh()          # the default fixture: five_hour 12%
        self.assertIs(block['session_limit'], True)
        self.assertEqual(block['session_used'], 12)
        self.assertEqual(self.home.published('@codex_session_limit'), '1')

    def test_idle_between_windows_stays_a_limit(self):
        self.home.endpoint(session=0, session_resets=None)
        block = self.home.refresh()
        self.assertIs(block['session_limit'], True)
        self.assertEqual((block['session_used'], block['session_text']), (0, 'idle'))
        self.assertEqual(self.home.published('@codex_session_limit'), '1')

    def test_still_unusable_answers_fail(self):
        for body in ({'five_hour': 5, 'seven_day': {'utilization': 1}},
                     # A null utilization WITH a reset is a window we cannot read.
                     {'five_hour': {'utilization': None, 'resets_at': iso_z(NOW + HOUR)},
                      'seven_day': {'utilization': 1}},
                     {'five_hour': {'utilization': 1, 'resets_at': None}},
                     {'five_hour': None, 'seven_day': None}):
            with self.subTest(body=body):
                self.backoff.unlink(missing_ok=True)
                self.payload(body)
                self.run_status('--refresh')
                self.assertTrue(self.backoff.exists())
                self.assertEqual(self.block()['state'], 'error')

    def test_a_failed_fetch_keeps_the_flag_and_auth_required_publishes_unknown(self):
        self.payload(weekly_only_payload())
        self.home.refresh()
        self.payload({'unexpected': 1})
        self.run_status('--refresh')
        block = self.block()
        self.assertEqual(block['state'], 'error')
        self.assertIs(block['session_limit'], False)
        self.assertEqual(self.home.published('@codex_session_limit'), '0')
        # A login problem: nothing about the account is known for sure.
        data = json.loads(self.home.usage.read_text())
        data['providers']['claude']['state'] = 'auth_required'
        self.home.usage.write_text(json.dumps(data))
        self.home.tmux_log.unlink()
        self.run_status('--publish')
        self.assertEqual(self.home.published('@codex_session_limit'), '')

    def test_another_accounts_block_publishes_unknown(self):
        self.payload(weekly_only_payload())
        self.home.refresh()
        self.home.login(A1_EMAIL, A1_ORG)
        self.home.tmux_log.unlink()
        self.run_status('--publish')
        self.assertEqual(self.home.published('@codex_session_limit'), '')
        self.assertEqual(self.home.published('@codex_session_text'), '--%')

    def test_a_block_from_before_the_field_publishes_unknown(self):
        self.home.refresh()
        data = json.loads(self.home.usage.read_text())
        del data['providers']['claude']['session_limit']
        self.home.usage.write_text(json.dumps(data))
        self.home.tmux_log.unlink()
        self.run_status('--publish')
        self.assertEqual(self.home.published('@codex_session_limit'), '')


class ReaderTests(WeeklyOnlyTestCase):
    def test_the_reset_view_says_n_a(self):
        stub = self.home.bin / 'tmux'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$FAKE_TMUX_LOG"\n'
                        '[ "$1" = show-options ] && printf "@codexbar_view reset\\n"\nexit 0\n')
        self.payload(weekly_only_payload())
        self.home.refresh()
        self.home.tmux_log.unlink()
        self.run_status('--publish')
        self.assertEqual(self.home.published('@codex_session_text'), 'n/a')
        self.assertNotEqual(self.home.published('@codex_weekly_text'), 'n/a')

    def test_load_cache_fields_only_appends(self):
        # cua-notch's dev/check-invariants reads this jq line by regex and
        # takes `.<family>_used` as the window families: the new field is the
        # LAST one, and is not a family.
        text = STATUS.read_text()
        line = re.search(r'(\[\.state//\"ok\".*?\])\s*\|\s*@tsv', text, re.S).group(1)
        self.assertEqual(set(re.findall(r'\.(\w+)_used', line)), {'session', 'weekly', 'scoped'})
        self.assertTrue(line.startswith(
            '[.state//"ok", .session_text//"", .weekly_text//"", .session_color//"", '
            '.weekly_color//"", .session_resets_at//"", .weekly_resets_at//"", '
            '.session_used//"", .weekly_used//"", .session_window_minutes//"", '
            '.weekly_window_minutes//"", .scoped_text//"", .scoped_color//"", '
            '.scoped_resets_at//"", .scoped_used//"", .scoped_window_minutes//"", '
            '.scoped_label//"", pool_resets(.weekly_pace), pool_resets(.scoped_pace), '), line)
        self.assertTrue(line.endswith('(.session_limit | if . == false then "0" '
                                      'elif . == true then "1" else "" end)]'), line)


class CswapAdoptionTests(WeeklyOnlyTestCase):
    def test_a_reading_without_five_hour_is_adopted_as_no_limit(self):
        self.home.cswap()
        self.assertIs(self.home.refresh()['session_limit'], True)   # fetched: 12%
        calls = self.curl_calls()
        data = json.loads(self.home.cswap_usage.read_text())
        data['accounts']['2'] = dict(email=A2_EMAIL, organizationUuid=A2_ORG,
                                     **weekly_only_slot(NOW + 30.25, 4, A2_WEEKLY_RESETS))
        self.home.cswap_usage.write_text(json.dumps(data))
        self.tick(NOW + 60)
        self.assertEqual(self.curl_calls(), calls)                    # no fetch
        block = self.block()
        self.assertEqual(block['endpoint_at'], NOW + 30)              # adopted
        self.assertNoSession(block)
        self.assertEqual(block['weekly_used'], 4)
        self.assertEqual(self.published_all('@codex_session_limit')[-1], '0')

    def test_a_null_pct_with_no_reset_is_adopted_as_no_limit(self):
        self.home.cswap()
        self.home.refresh()
        data = json.loads(self.home.cswap_usage.read_text())
        data['accounts']['2']['fetchedAt'] = NOW + 30.25
        data['accounts']['2']['lastGood']['five_hour'] = {'pct': None, 'resets_at': None}
        self.home.cswap_usage.write_text(json.dumps(data))
        self.tick(NOW + 60)
        block = self.block()
        self.assertEqual(block['endpoint_at'], NOW + 30)
        self.assertNoSession(block)
        self.assertEqual(block['weekly_used'], 60)
        self.assertEqual(self.published_all('@codex_session_limit')[-1], '0')

    def test_an_idle_reading_stays_a_limit(self):
        self.home.cswap()
        self.home.refresh()
        data = json.loads(self.home.cswap_usage.read_text())
        data['accounts']['2']['fetchedAt'] = NOW + 30.25
        data['accounts']['2']['lastGood']['five_hour'] = {'pct': 0.0, 'resets_at': None}
        self.home.cswap_usage.write_text(json.dumps(data))
        self.tick(NOW + 60)
        block = self.block()
        self.assertEqual(block['endpoint_at'], NOW + 30)
        self.assertIs(block['session_limit'], True)
        self.assertEqual((block['session_used'], block['session_text']), (0, 'idle'))

    def test_switches_flip_the_flag_both_ways(self):
        # Slot 1 is weekly-only, slot 2 has a 5-hour window.
        self.home.cswap(slot1=weekly_only_slot(NOW - 600 + 0.37, 1, A1_WEEKLY_RESETS))
        self.home.refresh()                                          # logged in as slot 2
        self.assertEqual(self.home.published('@codex_session_limit'), '1')
        calls = self.curl_calls()

        self.home.login(A1_EMAIL, A1_ORG)                            # cswap switch -> 1
        self.hold_the_poll(A1_KEY, NOW + DAY)
        self.tick(NOW + 60)
        # The tick first draws slot 2's block as another account's (unknown),
        # then adopts slot 1's cswap reading: no session limit.
        self.assertEqual(self.published_all('@codex_session_limit'), ['', '0'])
        block = self.block()
        self.assertEqual(block['account'], A1_KEY)
        self.assertNoSession(block)
        self.assertEqual(block['weekly_used'], 1)
        self.assertEqual(block['weekly_pace']['scope'], 'pool')       # the week still pools

        self.home.login(A2_EMAIL, A2_ORG)                            # and back -> 2
        self.hold_the_poll(A2_KEY, NOW + DAY)
        self.tick(NOW + 120)
        self.assertEqual(self.published_all('@codex_session_limit'), ['', '1'])
        block = self.block()
        self.assertEqual(block['account'], A2_KEY)
        self.assertIs(block['session_limit'], True)
        self.assertEqual((block['session_used'], block['session_window_minutes']), (50, 300))
        self.assertEqual(self.curl_calls(), calls)                    # all without a fetch


class LiveSampleTests(WeeklyOnlyTestCase):
    def sample(self, **windows):
        (self.home.cache / 'claude-live.json').write_text(json.dumps(dict(account=A2_KEY, **windows)))

    def test_merge_and_seen_onto_a_weekly_only_block(self):
        self.payload(weekly_only_payload(weekly=27))
        self.home.refresh()
        # A live session reports the week moving. Its five_hour (an account
        # with no limit cannot have one; this is a stray) is not folded in.
        self.sample(seven_day={'used': 30, 'resets_at': A2_WEEKLY_RESETS, 't': NOW + 50},
                    five_hour={'used': 40, 'resets_at': NOW + HOUR, 't': NOW + 50})
        self.run_status('--merge-live', at=NOW + 60, force=False)
        block = self.block()
        self.assertEqual((block['weekly_used'], block['updated_at']), (30, NOW + 50))
        self.assertNoSession(block)
        self.assertEqual(self.home.published('@codex_session_limit'), '0')

        # The same week, seen again later, confirms the block (weekly alone).
        self.sample(seven_day={'used': 30, 'resets_at': A2_WEEKLY_RESETS, 't': NOW + 50},
                    seen=NOW + 100)
        self.run_status('--merge-live', at=NOW + 120, force=False)
        block = self.block()
        self.assertEqual((block['weekly_used'], block['updated_at']), (30, NOW + 100))
        self.assertNoSession(block)

    def test_a_sample_without_five_hour_never_means_no_limit(self):
        self.home.refresh()                                           # 12%, limit
        self.sample(seven_day={'used': 30, 'resets_at': A2_WEEKLY_RESETS, 't': NOW + 50})
        self.run_status('--merge-live', at=NOW + 60, force=False)
        block = self.block()
        self.assertEqual(block['weekly_used'], 30)
        self.assertIs(block['session_limit'], True)
        self.assertEqual(block['session_used'], 12)

    def test_a_weekly_only_sample_does_not_fill_another_accounts_block(self):
        # Unknown whether the new account has a limit: nothing is merged, and
        # adoption or the fetch decides (as before the change).
        self.home.refresh()
        self.home.login(A1_EMAIL, A1_ORG)
        before = self.block()
        (self.home.cache / 'claude-live.json').write_text(json.dumps(dict(
            account=A1_KEY, seven_day={'used': 3, 'resets_at': A1_WEEKLY_RESETS, 't': NOW + 50})))
        self.run_status('--merge-live', at=NOW + 60, force=False)
        self.assertEqual(self.block(), before)


class PoolTests(WeeklyOnlyTestCase):
    def test_the_weekly_pool_is_unchanged_by_a_weekly_only_active_account(self):
        self.home.cswap()
        with_session = self.home.refresh()
        self.home.endpoint(session=12)                                # same week
        self.payload(dict(json.loads((self.home.root / 'usage-fixture.json').read_text()),
                          five_hour=None))
        without = self.home.refresh()
        self.assertNoSession(without)
        for key in ('weekly_pace', 'weekly_text', 'weekly_color', 'scoped_pace', 'scoped_text'):
            self.assertEqual(without[key], with_session[key], key)
        self.assertEqual(without['weekly_pace']['scope'], 'pool')

    def test_a_weekly_only_member_counts_in_the_weekly_pool(self):
        self.home.cswap()
        idle_member = self.home.refresh()
        self.home.cswap(slot1=weekly_only_slot(NOW - 600 + 0.37, 90, A1_WEEKLY_RESETS))
        weekly_only_member = self.home.refresh()
        self.assertEqual(weekly_only_member['weekly_pace'], idle_member['weekly_pace'])
        self.assertEqual([m['slot'] for m in weekly_only_member['weekly_pace']['members']],
                         ['1', '2'])


@unittest.skipUnless(JQ, 'needs jq')
class CodexTests(unittest.TestCase):
    def refresh(self, usage):
        with tempfile.TemporaryDirectory(prefix='codex-weekly-only-') as directory:
            root = Path(directory)
            tools = root / 'bin'
            tools.mkdir()
            for name, code in {'codexbar': 'cat "$CODEX_WINDOW_FIXTURE"', 'codex': 'exit 0',
                               'tmux': 'printf "%s\\n" "$*" >>"$FAKE_TMUX_LOG"'}.items():
                (tools / name).write_text('#!/bin/sh\n' + code + '\n')
                (tools / name).chmod(0o700)
            fixture = root / 'fixture.json'
            fixture.write_text(json.dumps([{'provider': 'codex', 'usage': usage}]))
            log = root / 'tmux.log'
            env = {k: v for k, v in os.environ.items()
                   if k not in ('TMUX', 'TMUX_PANE') and not k.startswith('CODEXBAR_')}
            env.update(HOME=directory, TMUX_TMPDIR=directory,
                       PATH=str(tools) + ':/opt/homebrew/bin:/usr/bin:/bin',
                       CODEX_WINDOW_FIXTURE=str(fixture), CODEX_CLI_PATH=str(tools / 'codex'),
                       FAKE_TMUX_LOG=str(log), CODEXBAR_USAGE_PROVIDERS='codex',
                       CODEXBAR_USAGE_DISPLAY_PROVIDER='codex', CODEXBAR_USAGE_FORCE_REFRESH='1')
            result = subprocess.run(['/bin/bash', str(STATUS), '--refresh'], capture_output=True,
                                    text=True, timeout=30, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads((root / '.cache/codexbar-tmux/usage.json').read_text())['providers']['codex']
            published = [p.split(' ', 3)[3] if p.count(' ') >= 3 else ''
                         for line in log.read_text().splitlines() for p in line.split(' ; ')
                         if p.startswith('set-option -gq @codex_session_limit')]
            return block, published

    def test_no_primary_window_is_weekly_only(self):
        week = {'usedPercent': 29, 'windowMinutes': 10080, 'resetsAt': '2030-01-01T00:00:00Z'}
        short = {'usedPercent': 7, 'windowMinutes': 300, 'resetsAt': '2030-01-01T00:00:00Z'}
        block, published = self.refresh({'primary': None, 'secondary': week})
        self.assertIs(block['session_limit'], False)
        self.assertIsNone(block['session_used'])
        self.assertEqual(block['session_text'], 'n/a')
        self.assertEqual(published[-1], '0')
        block, published = self.refresh({'primary': short, 'secondary': week})
        self.assertIs(block['session_limit'], True)
        self.assertEqual(published[-1], '1')


if __name__ == '__main__':
    unittest.main()
