"""No bench needed: protect the SAR regressions' transaction boundary."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


SUPPORT = Path(__file__).resolve().parents[1] / 'ksa_compliance' / 'zerone_test_support.py'


class TestSiteBoundary(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('zerone_test_support', SUPPORT)
        self.support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.support)
        self.frappe = mock.MagicMock()
        self.frappe.session.user = 'original-user'
        self.frappe.flags.in_test = True
        self.modules = mock.patch.dict(sys.modules, {'frappe': self.frappe})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def run_case(self, fail_setup=False, fail_test=False):
        boundary = self.support.ZeroneSiteTestCase
        class Probe(boundary):
            def setUp(self):
                super().setUp()
                if fail_setup:
                    raise RuntimeError('fixture failed')
            def test_probe(self):
                if fail_test:
                    self.fail('assertion failed')
        result = unittest.TestResult()
        Probe('test_probe').run(result)
        return result

    def assert_cleaned_up(self):
        point = self.frappe.db.savepoint.call_args.args[0]
        self.frappe.db.rollback.assert_called_once_with(save_point=point)
        self.frappe.clear_cache.assert_called_once_with()
        self.frappe.set_user.assert_has_calls([mock.call('Administrator'), mock.call('original-user')])
        self.frappe.db.commit.assert_not_called()

    def test_success_rolls_back_and_restores_user_without_committing(self):
        self.assertTrue(self.run_case().wasSuccessful())
        self.assert_cleaned_up()

    def test_fixture_failure_still_rolls_back(self):
        self.assertEqual(len(self.run_case(fail_setup=True).errors), 1)
        self.assert_cleaned_up()

    def test_assertion_failure_still_rolls_back(self):
        self.assertEqual(len(self.run_case(fail_test=True).failures), 1)
        self.assert_cleaned_up()

    def test_refuses_to_write_outside_the_test_runner(self):
        self.frappe.flags.in_test = False
        result = self.run_case()
        self.assertEqual(len(result.errors), 1)
        self.assertIn('test runner', result.errors[0][1])
        self.frappe.db.savepoint.assert_not_called()
        self.frappe.set_user.assert_not_called()

    def test_uses_a_different_savepoint_for_each_case(self):
        self.run_case()
        self.run_case()
        points = [call.args[0] for call in self.frappe.db.savepoint.call_args_list]
        self.assertEqual(len(set(points)), 2)


if __name__ == '__main__':
    unittest.main()
