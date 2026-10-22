"""Back-test against real output saved locally (it never leaves that PC).

Each folder under tests/fixtures_local/ holds an input/ folder and an expected.json made by
`python tools/debug_parse.py <exports> --save-fixture <name>` (then checked and corrected by
hand). This test re-runs the pipeline on every fixture and compares. With no fixtures here,
it is skipped.
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from netinv.splitter import split_inputs                       # noqa: E402
from netinv.model import build_devices                         # noqa: E402
from debug_parse import summarise                              # noqa: E402

FIXTURES = os.path.join(ROOT, 'tests', 'fixtures_local')


def _cases():
    if not os.path.isdir(FIXTURES):
        return []
    return sorted(d for d in os.listdir(FIXTURES)
                  if os.path.isfile(os.path.join(FIXTURES, d, 'expected.json')))


class TestLocalFixtures(unittest.TestCase):
    @unittest.skipUnless(_cases(), 'no local fixtures (see tools/debug_parse.py --save-fixture)')
    def test_fixtures(self):
        for case in _cases():
            with self.subTest(fixture=case):
                folder = os.path.join(FIXTURES, case)
                with open(os.path.join(folder, 'expected.json')) as fh:
                    expected = json.load(fh)
                sections, _ = split_inputs(os.path.join(folder, 'input'))
                devices, _ = build_devices(sections)
                actual = summarise(devices)
                for host, exp in expected['devices'].items():
                    self.assertIn(host, actual['devices'], 'device missing: %s' % host)
                    for field, value in exp.items():
                        self.assertEqual(actual['devices'][host].get(field), value,
                                         '%s: %s' % (host, field))


if __name__ == '__main__':
    unittest.main()
