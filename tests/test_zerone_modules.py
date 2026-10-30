"""Guard test discovery against currency-dependent ERPNext fixture loading."""
import ast
from pathlib import Path
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / 'ksa_compliance'


class TestRegressionDiscovery(unittest.TestCase):
    def test_site_regressions_do_not_use_automatic_fixture_test_cases(self):
        for path in PACKAGE.glob('test_zerone_*.py'):
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text())
                imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
                self.assertFalse(any(name and name.startswith(('frappe.tests', 'erpnext.tests')) for name in imports),
                                 'Use explicit SAR fixtures instead of automatic test-master loading')

    def test_xml_checks_cannot_silently_skip_when_the_site_has_no_default_template(self):
        tree = ast.parse((PACKAGE / 'test_zerone_xml_totals.py').read_text())
        self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr == 'skipTest' for node in ast.walk(tree)),
                         'The XML checks must create their own VAT template')


if __name__ == '__main__':
    unittest.main()
