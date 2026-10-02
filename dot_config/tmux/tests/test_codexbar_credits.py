"""Run the publisher's credit parser against real and malformed payload shapes."""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest


class CreditsTests(unittest.TestCase):
    def test_credit_payload_shapes(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        source = scripts / 'codexbar-usage-status.sh'
        if not source.exists():
            source = scripts / 'executable_codexbar-usage-status.sh'
        code = re.search(r'  FETCH_CREDITS_REMAINING="\$\(printf.*?\n.*?\)"',
                         source.read_text(), re.S).group(0)
        for payload, expected in [
            ({'credits': {'remaining': 1881.11}}, 1881.11),
            ({'credits': {'remaining': 0}}, 0),
            ({'credits': {'remaining': -0.25}}, -0.25),
            ({}, None), ({'credits': None}, None),
            ({'credits': 'unknown'}, None), ({'credits': []}, None),
            ({'credits': {'remaining': True}}, None),
            ({'credits': {'remaining': '1881'}}, None),
        ]:
            with self.subTest(payload=payload):
                result = subprocess.run(
                    ['/bin/bash', '-ec', code + '\nprintf "%s" "$FETCH_CREDITS_REMAINING"'],
                    env={**os.environ, 'normalized': json.dumps(payload)},
                    text=True, capture_output=True, check=True)
                self.assertEqual(json.loads(result.stdout), expected)


if __name__ == '__main__':
    unittest.main()
