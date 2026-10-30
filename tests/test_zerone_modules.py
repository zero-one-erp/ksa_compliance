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

    def test_customer_and_group_names_are_distinct_per_run(self):
        tree = ast.parse((PACKAGE / 'test_zerone_xml_totals.py').read_text())
        records = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                fields = {key.value: value for key, value in zip(node.keys, node.values)
                          if isinstance(key, ast.Constant)}
                kind = fields.get('doctype')
                if isinstance(kind, ast.Constant) and kind.value in ('Customer', 'Customer Group'):
                    field = 'customer_name' if kind.value == 'Customer' else 'customer_group_name'
                    records[kind.value] = ast.dump(fields[field])
        self.assertNotEqual(records['Customer'], records['Customer Group'],
                            'ERPNext rejects a customer with the same name as its customer group')
        self.assertIn('uuid.uuid4()', (PACKAGE / 'test_zerone_xml_totals.py').read_text())

    def test_xml_checks_cannot_silently_skip_when_the_site_has_no_default_template(self):
        tree = ast.parse((PACKAGE / 'test_zerone_xml_totals.py').read_text())
        self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr == 'skipTest' for node in ast.walk(tree)),
                         'The XML checks must create their own VAT template')


if __name__ == '__main__':
    unittest.main()
