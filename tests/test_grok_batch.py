import sys
import unittest
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from grok_registration.batch import (
    _reconcile_result_counters,
    _registration_result_succeeded,
)


class GrokBatchResultTests(unittest.TestCase):
    def test_direct_sub2_import_is_success_without_local_account_ids(self):
        self.assertTrue(
            _registration_result_succeeded(
                {"status": "imported", "imported_account_ids": []}
            )
        )

    def test_legacy_import_ids_still_count_as_success(self):
        self.assertTrue(
            _registration_result_succeeded(
                {"status": "importing", "imported_account_ids": ["account-1"]}
            )
        )

    def test_failure_status_wins_over_import_ids(self):
        self.assertFalse(
            _registration_result_succeeded(
                {"status": "error", "imported_account_ids": ["account-1"]}
            )
        )

    def test_persisted_direct_import_counters_are_reconciled(self):
        counters = _reconcile_result_counters(
            [
                {"status": "imported", "imported_account_ids": []},
                {"status": "imported", "imported_account_ids": []},
                {"status": "error"},
                {"status": "paused"},
            ],
            finished=3,
            successes=0,
            failures=3,
        )
        self.assertEqual(counters, (3, 2, 1))


if __name__ == "__main__":
    unittest.main()
