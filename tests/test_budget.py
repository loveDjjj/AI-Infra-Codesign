"""验证预算跨恢复累计，缓存复用退款且正式额度独立。"""
import unittest
from unittest.mock import patch
from codesign_lab.search.budget import Budget


class BudgetChecks(unittest.TestCase):
    def test_resume_preserves_deadline_and_call_reservations(self):
        state = {}
        with patch('codesign_lab.search.budget.time.time', return_value=100):
            first = Budget(state, wall_seconds=10, case_calls=1)
            self.assertTrue(first.reserve('case-1', 'case'))
        with patch('codesign_lab.search.budget.time.time', return_value=105):
            second = Budget(state, wall_seconds=10, case_calls=1)
            self.assertFalse(second.reserve('case-2', 'case'))
            self.assertTrue(second.reserve('full-1', 'full'))
            self.assertEqual(second.snapshot()['remaining_wall_seconds'], 5)
        with patch('codesign_lab.search.budget.time.time', return_value=111):
            self.assertTrue(second.expired())
            self.assertFalse(second.reserve('case-3', 'case'))

    def test_cache_reuse_frees_reserved_cold_call_once(self):
        budget = Budget({}, wall_seconds=100, case_calls=1)
        self.assertTrue(budget.reserve('first', 'case'))
        self.assertFalse(budget.reserve('second', 'case'))
        budget.reused('first');budget.reused('first')
        self.assertEqual(budget.used('case'), 0)
        self.assertTrue(budget.reserve('second', 'case'))
        self.assertEqual(budget.used('case'), 1)

    def test_resume_cannot_reset_limits(self):
        state = {}
        Budget(state, wall_seconds=100, case_calls=1)
        with self.assertRaises(ValueError):
            Budget(state, wall_seconds=100, case_calls=10)
