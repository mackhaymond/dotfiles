"""The active-Claude-account label and the account keying of the usage cache.

Everything runs against an isolated HOME with fake tmux/security/curl/cswap on
PATH: no real account, Keychain, network or tmux server is touched, and the
fake cswap fails any test that calls it (the status line must never run it).
"""
import json
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
LIVE = script('codexbar-usage-live.sh')

A_EMAIL, A_ORG = 'mackhaymond@gmail.com', 'org-a'
B_EMAIL, B_ORG = 'ihave27kidsinmybasement@gmail.com', 'org-b'
A_KEY, B_KEY = f'{A_EMAIL}/{A_ORG}', f'{B_EMAIL}/{B_ORG}'

STUBS = {
    # Every call is logged one per line, arguments space-joined.
    'tmux': 'printf "%s\\n" "$*" >>"$FAKE_TMUX_LOG"',
    # A fake, unexpired login: the fetch must not try a token refresh.
    'security': 'printf \'{"claudeAiOauth":{"accessToken":"sk-ant-oat-test",'
                '"refreshToken":"sk-ant-ort-test","expiresAt":4102444800000}}\'',
    # The usage endpoint. FAKE_SWITCH_TO, when set, is copied over
    # ~/.claude.json first: a cswap switch landing mid-request.
    'curl': 'echo call >>"$FAKE_CURL_LOG"\n'
            '[ -n "$FAKE_SWITCH_TO" ] && cp "$FAKE_SWITCH_TO" "$HOME/.claude.json"\n'
            'cat "$FAKE_USAGE"',
    'cswap': 'echo "$*" >>"$FAKE_CSWAP_LOG"; exit 1',
}


class Home:
    """An isolated HOME plus the fakes, and helpers to read what they saw."""

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
        self.sample = self.cache / 'claude-live.json'
        self.tmux_log = self.root / 'tmux.log'
        self.curl_log = self.root / 'curl.log'
        self.cswap_log = self.root / 'cswap.log'
        self.now = int(time.time())
        self.fixture(12, 2)

    def login(self, email, org=None, path=None):
        account = {'emailAddress': email}
        if org:
            account['organizationUuid'] = org
        target = path or (self.root / '.claude.json')
        target.write_text(json.dumps({'projects': {}, 'oauthAccount': account}))
        return target

    def aliases(self, accounts):
        seq = self.root / '.claude-swap-backup/sequence.json'
        seq.parent.mkdir(exist_ok=True)
        seq.write_text(json.dumps({'activeAccountNumber': 1, 'accounts': accounts}))

    def fixture(self, session, weekly):
        iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))
        self.session_resets = self.now + 4 * 3600
        self.weekly_resets = self.now + 3 * 86400
        (self.root / 'usage-fixture.json').write_text(json.dumps({
            'five_hour': {'utilization': session, 'resets_at': iso(self.session_resets)},
            'seven_day': {'utilization': weekly, 'resets_at': iso(self.weekly_resets)},
            'limits': []}))
        # iso() drops nothing: whole seconds in, whole seconds out.

    def raw_fixture(self, body):
        """Make the fake endpoint answer with this body instead."""
        (self.root / 'usage-fixture.json').write_text(json.dumps(body))

    def block(self, account, session, weekly, fetched_at=None, **extra):
        t = self.now if fetched_at is None else fetched_at
        block = dict(provider='claude', label='Claude', state='ok', updated_at=t,
                     checked_at=t, fetched_at=t, session_used=session,
                     weekly_used=weekly, session_text=f'{session}%',
                     weekly_text=f'{weekly}%', session_color='red',
                     weekly_color='red', scoped_used=50, scoped_text='50%',
                     scoped_label='Fable', session_window_minutes=300,
                     weekly_window_minutes=10080,
                     session_resets_at=self.session_resets,
                     weekly_resets_at=self.weekly_resets, **extra)
        if account is not None:
            block['account'] = account
        self.usage.write_text(json.dumps({'schema': 2, 'providers': {'claude': block}}))

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ('CLAUDE_CONFIG_DIR', 'TMUX', 'TMUX_PANE')
               and not k.startswith('CODEXBAR_')}
        # TMUX unset and TMUX_TMPDIR private: even a real tmux reached by
        # mistake could not find (or change) the user's server.
        env.update(HOME=str(self.root), PATH=f'{self.bin}:/opt/homebrew/bin:/usr/bin:/bin',
                   TMUX_TMPDIR=str(self.root),
                   FAKE_TMUX_LOG=str(self.tmux_log), FAKE_CURL_LOG=str(self.curl_log),
                   FAKE_CSWAP_LOG=str(self.cswap_log),
                   FAKE_USAGE=str(self.root / 'usage-fixture.json'),
                   CODEXBAR_USAGE_PROVIDERS='claude')
        env.update(extra)
        return env

    def run(self, path, *args, **extra):
        result = subprocess.run(['/bin/bash', str(path), *args], capture_output=True,
                                text=True, timeout=20, env=self.env(**extra))
        assert result.returncode == 0, (result.returncode, result.stderr)
        return result

    def published(self, option):
        """The last value the script set for a tmux option, or None."""
        prefix = f'set-option -gq {option} '
        value = None
        if self.tmux_log.exists():
            for line in self.tmux_log.read_text().splitlines():
                if line.startswith(prefix):
                    value = line[len(prefix):]
                elif line == prefix.rstrip():
                    value = ''
        return value

    def claude(self):
        return json.loads(self.usage.read_text())['providers']['claude']

    def curl_calls(self):
        return len(self.curl_log.read_text().splitlines()) if self.curl_log.exists() else 0


class AccountTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='codexbar-account-')
        self.home = Home(self.tmp)

    def tearDown(self):
        # The status line must never start cswap, in any test.
        self.assertFalse(self.home.cswap_log.exists(), 'cswap was called')
        shutil.rmtree(self.tmp, ignore_errors=True)


class LabelTests(AccountTestCase):
    def label(self, **extra):
        self.home.tmux_log.unlink(missing_ok=True)
        self.home.run(STATUS, '--publish', **extra)
        return self.home.published('@codex_account_label')

    def test_email_local_part_truncated_with_ellipsis(self):
        for email, expected in [(B_EMAIL, 'ihave27ki…'), (A_EMAIL, 'mackhaymo…'),
                                ('bob@example.com', 'bob'), ('ten4chars@x.io', 'ten4chars'),
                                ('Mixed.Case+tag@x.io', 'Mixed.Cas…')]:
            with self.subTest(email=email):
                self.home.login(email)
                self.assertEqual(self.label(), expected)
        self.home.login(B_EMAIL)
        self.label()
        self.assertEqual(self.home.published('@codex_account'), B_EMAIL)

    def test_width_option(self):
        self.home.login(B_EMAIL)
        self.assertEqual(self.label(CODEXBAR_USAGE_ACCOUNT_LABEL_MAX='0'), '')
        self.assertEqual(self.label(CODEXBAR_USAGE_ACCOUNT_LABEL_MAX='40'),
                         'ihave27kidsinmybasement')
        self.assertEqual(self.label(CODEXBAR_USAGE_ACCOUNT_LABEL_MAX='5'), 'ihav…')

    def test_cswap_alias_wins_and_is_sanitized(self):
        self.home.login(B_EMAIL, B_ORG)
        self.home.aliases({'1': {'email': A_EMAIL, 'organizationUuid': A_ORG, 'alias': 'main'},
                           '2': {'email': B_EMAIL.upper(), 'organizationUuid': B_ORG,
                                 'alias': 'spare'}})
        self.assertEqual(self.label(), 'spare')
        # Same email, other organization: not this login's alias.
        self.home.aliases({'2': {'email': B_EMAIL, 'organizationUuid': 'org-z',
                                 'alias': 'other-org'}})
        self.assertEqual(self.label(), 'ihave27ki…')
        # Characters tmux formats treat specially are squeezed out.
        self.home.aliases({'2': {'email': B_EMAIL, 'alias': 'my, #alias}%'}})
        self.assertEqual(self.label(), 'my-alias')
        self.home.aliases({'2': {'email': B_EMAIL, 'alias': 'a-very-long-alias'}})
        self.assertEqual(self.label(), 'a-very-lo…')
        # An alias that squeezes to nothing falls back to the email.
        self.home.aliases({'2': {'email': B_EMAIL, 'alias': '☃☃'}})
        self.assertEqual(self.label(), 'ihave27ki…')

    def test_missing_or_broken_files_show_nothing(self):
        self.assertEqual(self.label(), '')                        # no ~/.claude.json
        (self.home.root / '.claude.json').write_text('{"oauthAccount": ')
        self.assertEqual(self.label(), '')                        # torn file
        (self.home.root / '.claude.json').write_text('{"oauthAccount": "x"}')
        self.assertEqual(self.label(), '')                        # wrong shape
        (self.home.root / '.claude.json').write_text('{"projects": {}}')
        self.assertEqual(self.label(), '')                        # logged out
        self.home.login(B_EMAIL)
        seq = self.home.root / '.claude-swap-backup/sequence.json'
        seq.parent.mkdir()
        seq.write_text('not json')
        self.assertEqual(self.label(), 'ihave27ki…')          # broken cswap state

    def test_codex_display_has_no_label(self):
        self.home.login(B_EMAIL)
        self.assertEqual(self.label(CODEXBAR_USAGE_PROVIDERS='claude codex',
                                    CODEXBAR_USAGE_DISPLAY_PROVIDER='codex'), '')

    def test_module_draws_label_before_s(self):
        module = (SCRIPTS.parent / 'catppuccin-custom/executable_codex_session.sh').read_text()
        # != against empty, not truthiness: a "0" alias is still a label.
        self.assertIn('"#{?#{!=:#{@codex_account_label},},#{@codex_account_label} ,}S:"',
                      module)

    def test_label_zero_is_published(self):
        self.home.login('0@example.com')
        self.assertEqual(self.label(), '0')


class CacheKeyingTests(AccountTestCase):
    def test_other_accounts_numbers_are_not_drawn(self):
        self.home.login(B_EMAIL, B_ORG)
        self.home.block(A_KEY, 90, 100)
        self.home.run(STATUS, '--publish')
        self.assertEqual(self.home.published('@codex_session_text'), '--%')
        self.assertEqual(self.home.published('@codex_weekly_text'), '--%')
        self.assertEqual(self.home.published('@codex_scoped_text'), '--%')
        self.assertEqual(self.home.published('@codex_session_color'), 'brightblack')
        # The same block, logged in as its own account, draws as before.
        self.home.login(A_EMAIL, A_ORG)
        self.home.run(STATUS, '--publish')
        self.assertEqual(self.home.published('@codex_session_text'), '90%')
        # And so does a block from before keying (no account recorded).
        self.home.block(None, 90, 100)
        self.home.run(STATUS, '--publish')
        self.assertEqual(self.home.published('@codex_session_text'), '90%')

    def test_switch_refetches_a_fresh_cache_and_clears_the_old_backoff(self):
        self.home.login(A_EMAIL, A_ORG)
        self.home.block(A_KEY, 90, 100)
        # A fresh block for the logged-in account: an unforced refresh skips it.
        self.home.run(STATUS, '--refresh')
        self.assertEqual(self.home.curl_calls(), 0)
        self.assertEqual((self.home.cache / 'claude-account').read_text().strip(), A_KEY)

        # The old token was rate limited: its backoff ladder is armed for an hour.
        backoff = self.home.cache / 'refresh_backoff_claude'
        backoff.write_text(f'6 {self.home.now + 3600}\n')
        self.home.login(B_EMAIL, B_ORG)                           # cswap switch
        self.home.run(STATUS, '--refresh')
        self.assertEqual(self.home.curl_calls(), 1)
        self.assertFalse(backoff.exists())
        self.assertEqual((self.home.cache / 'claude-account').read_text().strip(), B_KEY)
        block = self.home.claude()
        self.assertEqual((block['account'], block['account_email']), (B_KEY, B_EMAIL))
        self.assertEqual((block['session_used'], block['weekly_used']), (12, 2))
        # "12%" plus whatever pace suffix the projection adds.
        self.assertTrue(self.home.published('@codex_session_text').startswith('12%'))
        self.assertEqual(self.home.published('@codex_account_label'), 'ihave27ki…')

        # Now keyed and fresh: no further fetch.
        self.home.run(STATUS, '--refresh')
        self.assertEqual(self.home.curl_calls(), 1)

    def test_unkeyed_block_is_refetched_once(self):
        self.home.login(B_EMAIL, B_ORG)
        self.home.block(None, 90, 100)
        self.home.run(STATUS, '--refresh')
        self.home.run(STATUS, '--refresh')
        self.assertEqual(self.home.curl_calls(), 1)
        self.assertEqual(self.home.claude()['account'], B_KEY)

    def test_first_sighting_leaves_backoff_alone(self):
        self.home.login(B_EMAIL, B_ORG)
        self.home.block(B_KEY, 12, 2, fetched_at=1)
        backoff = self.home.cache / 'refresh_backoff_claude'
        backoff.write_text(f'3 {self.home.now + 600}\n')
        self.home.run(STATUS, '--refresh')
        self.assertTrue(backoff.exists())
        self.assertEqual(self.home.curl_calls(), 0)

    def test_no_login_means_no_keying(self):
        self.home.block(A_KEY, 90, 100)
        self.home.run(STATUS, '--refresh')
        self.assertEqual(self.home.curl_calls(), 0)
        self.home.run(STATUS, '--publish')
        self.assertEqual(self.home.published('@codex_session_text'), '90%')
        self.assertFalse((self.home.cache / 'claude-account').exists())

    def test_switch_during_the_fetch_is_discarded(self):
        self.home.login(A_EMAIL, A_ORG)
        self.home.block(A_KEY, 90, 100, fetched_at=1)
        switched = self.home.login(B_EMAIL, B_ORG, path=self.home.root / 'b.json')
        self.home.run(STATUS, '--refresh', FAKE_SWITCH_TO=str(switched))
        self.assertEqual(self.home.curl_calls(), 1)
        block = self.home.claude()
        self.assertEqual((block['account'], block['session_used']), (A_KEY, 90))
        self.assertFalse((self.home.cache / 'refresh_backoff_claude').exists())

    def test_old_tokens_failure_after_a_switch_arms_no_backoff(self):
        backoff = self.home.cache / 'refresh_backoff_claude'
        for body in ({'error': {'type': 'rate_limit_error'}}, {'unexpected': 1}):
            with self.subTest(body=body):
                backoff.unlink(missing_ok=True)
                self.home.raw_fixture(body)
                self.home.login(A_EMAIL, A_ORG)
                self.home.block(A_KEY, 90, 100, fetched_at=1)
                # Control: the same failure with no switch arms the ladder.
                self.home.run(STATUS, '--refresh')
                self.assertTrue(backoff.exists())
                backoff.unlink()
                before = self.home.claude()
                switched = self.home.login(B_EMAIL, B_ORG, path=self.home.root / 'b.json')
                self.home.run(STATUS, '--refresh', FAKE_SWITCH_TO=str(switched))
                self.assertFalse(backoff.exists())
                # Nor is a status patch written over the block.
                self.assertEqual(self.home.claude(), before)

    def test_cswap_run_profile_does_not_rekey_the_default_login(self):
        # `cswap run 2`: the Stop hook inherits the profile dir (account B),
        # while the Keychain token it fetches with is the default login's (A).
        profile = self.home.root / 'profile'
        profile.mkdir()
        self.home.login(B_EMAIL, B_ORG, path=profile / '.claude.json')
        self.home.login(A_EMAIL, A_ORG)
        self.home.block(A_KEY, 90, 100, fetched_at=1)
        (self.home.cache / 'claude-account').write_text(A_KEY + '\n')
        for _ in range(2):
            self.home.run(STATUS, '--refresh', CLAUDE_CONFIG_DIR=str(profile))
            block = self.home.claude()
            self.assertEqual((block['account'], block['account_email']), (A_KEY, A_EMAIL))
            self.assertEqual((self.home.cache / 'claude-account').read_text().strip(), A_KEY)
            self.assertEqual(self.home.published('@codex_account'), A_EMAIL)
        self.assertEqual(self.home.curl_calls(), 1)       # one fetch, not one per turn

    def test_callers_strip_claude_config_dir(self):
        for name in ('codexbar-usage-push.sh', 'codexbar-usage-live.sh'):
            with self.subTest(script=name):
                self.assertIn('env -u CLAUDE_CONFIG_DIR', script(name).read_text())


class LiveSampleTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        # Wire the live script to this copy of the status script, as installed.
        # Through a wrapper, not a symlink: the live script PREPENDS the system
        # bin directories to PATH, which would hand the status script the real
        # tmux; the wrapper puts the fakes back in front.
        target = self.home.root / '.config/tmux/scripts'
        target.mkdir(parents=True)
        wrapper = target / 'codexbar-usage-status.sh'
        wrapper.write_text('#!/bin/bash\n'
                           f'PATH="{self.home.bin}:$PATH" exec /bin/bash "{STATUS}" "$@"\n')
        wrapper.chmod(0o700)

    def reading(self, session, weekly, session_resets=None, weekly_resets=None):
        return json.dumps({
            'five_hour': {'used_percentage': session,
                          'resets_at': session_resets or self.home.session_resets},
            'seven_day': {'used_percentage': weekly,
                          'resets_at': weekly_resets or self.home.weekly_resets}})

    def live(self, *args):
        return self.home.run(LIVE, *args)

    def sample(self):
        return json.loads(self.home.sample.read_text())

    def test_sample_is_stamped_and_merged_within_one_account(self):
        self.home.login(B_EMAIL, B_ORG)
        self.home.block(B_KEY, 12, 2)
        self.live(self.reading(15, 3))
        sample = self.sample()
        self.assertEqual(sample['account'], B_KEY)
        self.assertNotIn('fence', sample)
        self.assertEqual(self.home.claude()['session_used'], 15)
        self.live(self.reading(14, 3))                    # lower, same window: kept 15
        self.assertEqual(self.sample()['five_hour']['used'], 15)

    def test_unstamped_sample_is_adopted_not_fenced(self):
        self.home.login(B_EMAIL, B_ORG)
        legacy = {'five_hour': {'used': 40, 'resets_at': self.home.session_resets, 't': 1},
                  'seven_day': {'used': 5, 'resets_at': self.home.weekly_resets, 't': 1}}
        self.home.sample.write_text(json.dumps(legacy))
        self.live(self.reading(41, 5))
        sample = self.sample()
        self.assertEqual((sample['account'], sample['five_hour']['used']), (B_KEY, 41))
        self.assertNotIn('fence', sample)

    def test_switch_fences_the_old_accounts_windows(self):
        now = self.home.now
        a_session, a_weekly = now + 1800, now + 86400
        b_session, b_weekly = now + 5 * 3600, now + 6 * 86400
        self.home.login(A_EMAIL, A_ORG)
        self.live(self.reading(90, 99, a_session, a_weekly))
        self.assertEqual(self.sample()['account'], A_KEY)

        self.home.login(B_EMAIL, B_ORG)                           # cswap switch
        # A session idle since before the switch repaints account A's numbers.
        self.live(self.reading(90, 99, a_session, a_weekly))
        sample = self.sample()
        self.assertEqual(sample['account'], B_KEY)
        self.assertNotIn('five_hour', sample)
        self.assertNotIn('seven_day', sample)
        self.assertEqual(sorted(f['r'] for f in sample['fence']), [a_session, a_weekly])
        self.assertEqual({f['a'] for f in sample['fence']}, {A_KEY})

        # Account B's own readings go through, and A's still cannot win.
        self.live(self.reading(3, 1, b_session, b_weekly))
        self.live(self.reading(90, 99, a_session, a_weekly))
        sample = self.sample()
        self.assertEqual((sample['five_hour']['used'], sample['seven_day']['used']), (3, 1))

        # Back to A: B's windows are fenced now, and A's own are free again.
        self.home.login(A_EMAIL, A_ORG)
        self.live(self.reading(91, 99, a_session, a_weekly))
        sample = self.sample()
        self.assertEqual(sample['account'], A_KEY)
        self.assertEqual(sample['five_hour']['used'], 91)
        self.assertEqual({(f['a'], f['r']) for f in sample['fence']},
                         {(B_KEY, b_session), (B_KEY, b_weekly)})

    def test_unreadable_login_leaves_a_stamped_sample_alone(self):
        self.home.login(A_EMAIL, A_ORG)
        self.live(self.reading(90, 99))
        before = self.home.sample.read_text()
        # Mid-switch: ~/.claude.json torn, and the reading is the NEW account's.
        (self.home.root / '.claude.json').write_text('{"oauthAccount": ')
        self.live(self.reading(95, 99))
        self.assertEqual(self.home.sample.read_text(), before)
        # Readable again as B: B's reading lands; nothing of it was fenced.
        self.home.login(B_EMAIL, B_ORG)
        self.live(self.reading(3, 1, self.home.now + 5 * 3600, self.home.now + 6 * 86400))
        sample = self.sample()
        self.assertEqual((sample['account'], sample['five_hour']['used']), (B_KEY, 3))
        # An unstamped sample still merges with no account readable.
        self.home.sample.unlink()
        (self.home.root / '.claude.json').unlink()
        self.live(self.reading(4, 1))
        self.assertNotIn('account', self.sample())
        self.assertEqual(self.sample()['five_hour']['used'], 4)

    def test_cswap_run_session_stamps_its_own_account_only(self):
        # A `cswap run 2` session (profile = B) repaints while the default
        # login, and the cache, are account A.
        profile = self.home.root / 'profile'
        profile.mkdir()
        self.home.login(B_EMAIL, B_ORG, path=profile / '.claude.json')
        self.home.login(A_EMAIL, A_ORG)
        self.home.block(A_KEY, 12, 2)
        (self.home.cache / 'claude-account').write_text(A_KEY + '\n')
        self.home.run(LIVE, self.reading(70, 80), CLAUDE_CONFIG_DIR=str(profile))
        self.assertEqual(self.sample()['account'], B_KEY)
        block = self.home.claude()
        self.assertEqual((block['account'], block['session_used']), (A_KEY, 12))
        self.assertEqual((self.home.cache / 'claude-account').read_text().strip(), A_KEY)

    def test_lapsed_fence_is_dropped(self):
        self.home.login(B_EMAIL, B_ORG)
        self.home.sample.write_text(json.dumps({
            'account': B_KEY, 'fence': [{'a': A_KEY, 'r': self.home.now - 10}]}))
        self.live(self.reading(3, 1))
        self.assertNotIn('fence', self.sample())

    def test_other_accounts_sample_is_never_folded_in(self):
        self.home.login(B_EMAIL, B_ORG)
        self.home.block(B_KEY, 12, 2)
        self.home.sample.write_text(json.dumps({
            'account': A_KEY,
            'five_hour': {'used': 90, 'resets_at': self.home.session_resets, 't': 1},
            'seven_day': {'used': 99, 'resets_at': self.home.weekly_resets, 't': 1}}))
        self.home.run(STATUS, '--merge-live')
        self.assertEqual(self.home.claude()['session_used'], 12)
        # Nor into a fetch.
        self.home.fixture(13, 2)
        self.home.run(STATUS, '--refresh', CODEXBAR_USAGE_FORCE_REFRESH='1')
        self.assertEqual(self.home.claude()['session_used'], 13)

    def test_live_numbers_replace_another_accounts_block_whole(self):
        self.home.login(A_EMAIL, A_ORG)
        self.home.block(A_KEY, 90, 100, session_severity='critical')
        self.home.login(B_EMAIL, B_ORG)
        self.live(self.reading(3, 1))
        block = self.home.claude()
        self.assertEqual(block['account'], B_KEY)
        self.assertEqual((block['session_used'], block['weekly_used']), (3, 1))
        # Nothing of account A survives, and the endpoint poll stays due.
        self.assertIsNone(block['scoped_used'])
        self.assertEqual(block['scoped_text'], 'n/a')
        self.assertEqual(block['session_severity'], 'normal')
        self.assertEqual(block['fetched_at'], 0)


if __name__ == '__main__':
    unittest.main()
