"""The Phase 2 XML's VAT subtotals add up to the VAT it states (Zero One audit F5c).

With VAT-inclusive prices ERPNext rounds each line's VAT and puts the difference into the tax row, so a restaurant
bill of 32 + 12 + 8 carries 6.78 VAT while its lines carry 6.77. The XML stated 6.78 in TaxTotal and 6.77 in its
subtotal: BR-CO-14 (total = sum of subtotals) and BR-CO-15 (inclusive = exclusive + sum of subtotals) are errors, and
ZATCA rejects such an invoice. Checked with the SDK's own rule sheets in the backend image (/opt/zatca).

The Frappe runner supplies the connection; all test records are explicit and rolled back.
"""

import re
import uuid
from unittest import TestCase, mock

import frappe
from ksa_compliance.zerone_test_support import ZeroneSiteTestCase

from ksa_compliance.output_models.tax import reconcile_tax_total


def subtotal(tax_amount, percent=15.0, taxable=0.0):
    return frappe._dict(tax_amount=tax_amount, taxable_amount=taxable, total_discount=0.0,
                        tax_category=frappe._dict(percent=percent))


class TestReconcileTaxTotal(TestCase):
    def test_the_rounding_halala_goes_to_the_taxed_subtotal(self):
        total = frappe._dict(tax_amount=6.77, tax_subtotal=[subtotal(0.0, percent=0.0), subtotal(6.77)])
        reconcile_tax_total(total, 6.78, lines=3)
        self.assertEqual([row.tax_amount for row in total.tax_subtotal], [0.0, 6.78])
        self.assertEqual(total.tax_amount, 6.78)

    def test_a_difference_beyond_rounding_is_left_for_validation(self):
        total = frappe._dict(tax_amount=6.0, tax_subtotal=[subtotal(6.0)])
        reconcile_tax_total(total, 6.78, lines=3)
        self.assertEqual(total.tax_subtotal[0].tax_amount, 6.0)

    def test_subtotals_that_already_agree_are_untouched(self):
        total = frappe._dict(tax_amount=3.0, tax_subtotal=[subtotal(3.0)])
        reconcile_tax_total(total, 3.0, lines=1)
        self.assertEqual(total.tax_subtotal[0].tax_amount, 3.0)


class TestRestaurantBillXml(ZeroneSiteTestCase):
    """The pilot restaurant's own bill, rendered as ksa_compliance renders it for ZATCA (unsigned, nothing sent)."""

    def xml_for(self, prices):
        from ksa_compliance.generate_xml import generate_xml_file
        from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields import (
            sales_invoice_additional_fields as siaf_module,
        )
        from ksa_compliance.output_models.e_invoice_output_model import Einvoice

        # Use a provisioned Saudi company, but none of its customer, tax or price-list defaults.
        company = frappe.db.get_value('Company', {'country': 'Saudi Arabia', 'default_currency': 'SAR'},
                                       'name', order_by='creation asc')
        self.assertTrue(company, 'Run on a test site with a provisioned Saudi SAR company')
        token = '_ZERONE-' + uuid.uuid4().hex[:12]
        category = frappe.get_doc({'doctype': 'Tax Category', 'title': token,
                                   'custom_zatca_category': 'Standard rate'}).insert(ignore_permissions=True)
        parent = frappe.db.get_value('Account', {'company': company, 'root_type': 'Liability', 'is_group': 1},
                                      'name', order_by='lft desc')
        self.assertTrue(parent, 'The Saudi company needs its chart of accounts')
        account = frappe.get_doc({'doctype': 'Account', 'account_name': token, 'company': company,
                                  'parent_account': parent, 'account_type': 'Tax',
                                  'account_currency': 'SAR'}).insert(ignore_permissions=True)
        tax = frappe.get_doc({'doctype': 'Sales Taxes and Charges Template', 'title': token,
                              'company': company, 'is_default': 0, 'tax_category': category.name,
                              'taxes': [{'charge_type': 'On Net Total', 'account_head': account.name,
                                         'description': 'VAT 15%', 'rate': 15, 'included_in_print_rate': 1}]}).insert(
            ignore_permissions=True)
        price_list = frappe.get_doc({'doctype': 'Price List', 'price_list_name': token,
                                     'enabled': 1, 'selling': 1, 'currency': 'SAR'}).insert(ignore_permissions=True)
        customer_group = frappe.get_doc({'doctype': 'Customer Group', 'customer_group_name': token + '-Group',
                                         'parent_customer_group': frappe.db.get_value(
                                             'Customer Group', {'is_group': 1}, 'name', order_by='lft asc')}).insert(
            ignore_permissions=True).name
        territory = frappe.get_doc({'doctype': 'Territory', 'territory_name': token,
                                    'parent_territory': frappe.db.get_value(
                                        'Territory', {'is_group': 1}, 'name', order_by='lft asc')}).insert(
            ignore_permissions=True).name
        customer = frappe.get_doc({'doctype': 'Customer', 'customer_name': token + '-Customer', 'customer_type': 'Individual',
                                   'customer_group': customer_group, 'territory': territory}).insert(
            ignore_permissions=True)
        address = frappe.get_doc({'doctype': 'Address', 'address_title': company, 'address_line1': 'Olaya St',
                                  'city': 'Riyadh', 'country': 'Saudi Arabia', 'pincode': '12345',
                                  'links': [{'link_doctype': 'Company', 'link_name': company}]}).insert(
            ignore_permissions=True).name
        settings = frappe.new_doc('ZATCA Business Settings')
        settings.update({'company': company, 'company_address': address, 'currency': 'SAR', 'seller_name': company,
                         'vat_registration_number': '300000000000003', 'street': 'Olaya St', 'building_number': '1234',
                         'city': 'Riyadh', 'district': 'Olaya', 'postal_code': '12345', 'country': 'Saudi Arabia',
                         'country_code': 'SA', 'company_unit': 'Main', 'company_unit_serial': '1-ZerOne|2-Test|3-1',
                         'company_category': 'Restaurant', 'status': 'Active', 'fatoora_server': 'Sandbox',
                         'tax_rate': 15, 'zatca_tax_category': 'Standard rate', 'cli_setup': 'Manual'})
        # The unsigned renderer gets this in-memory test settings document. Existing settings stay untouched.
        from ksa_compliance.ksa_compliance.doctype.zatca_business_settings.zatca_business_settings import (
            ZATCABusinessSettings,
        )
        settings.name = token
        settings.enable_zatca_integration = 0
        group = frappe.get_doc({'doctype': 'Item Group', 'item_group_name': token,
                                'parent_item_group': frappe.db.get_value(
                                    'Item Group', {'is_group': 1}, 'name', order_by='lft asc')}).insert(
            ignore_permissions=True).name
        uom = frappe.get_doc({'doctype': 'UOM', 'uom_name': token}).insert(ignore_permissions=True).name
        rows = []
        for price in prices:
            price, off = price if isinstance(price, tuple) else (price, 0)
            code = frappe.get_doc({'doctype': 'Item', 'item_code': f'_XML-{uuid.uuid4().hex[:6]}', 'item_name': 'Dish',
                                   'item_group': group, 'stock_uom': uom, 'is_stock_item': 0}).insert(
                ignore_permissions=True).name
            rows.append({'item_code': code, 'qty': 1, 'price_list_rate': price, 'discount_percentage': off,
                         'rate': price * (1 - off / 100)})
        invoice = frappe.get_doc({'doctype': 'Sales Invoice', 'company': company, 'taxes_and_charges': tax.name,
                                  'tax_category': category.name,
                                  'customer': customer.name, 'items': rows, 'currency': 'SAR', 'conversion_rate': 1,
                                  'selling_price_list': price_list.name, 'price_list_currency': 'SAR',
                                  'plc_conversion_rate': 1, 'ignore_pricing_rule': 1, 'disable_rounded_total': 1})
        invoice.set_missing_values()
        invoice.set_taxes()
        invoice.insert(ignore_permissions=True)

        siaf = frappe.new_doc('Sales Invoice Additional Fields')
        siaf.sales_invoice, siaf.invoice_doctype = invoice.name, 'Sales Invoice'
        siaf.uuid, siaf.tax_currency, siaf.invoice_counter, siaf.previous_invoice_hash = str(uuid.uuid4()), 'SAR', 1, 'x'
        siaf._set_buyer_details(siaf._get_buyer_doc(invoice), 'Simplified', invoice)
        siaf.sum_of_charges = siaf._compute_sum_of_charges(invoice.taxes)
        siaf.invoice_type_transaction = siaf_module._get_invoice_type_transaction('Simplified', False)
        siaf.invoice_type_code = siaf._get_invoice_type_code(invoice)
        siaf.payment_means_type_code = siaf._get_payment_means_type_code(invoice)
        with mock.patch.object(ZATCABusinessSettings, 'for_invoice', return_value=settings):
            xml = generate_xml_file(Einvoice(sales_invoice_additional_fields_doc=siaf, invoice_type='Simplified').result)
        return invoice, xml

    def test_the_bill_adds_up_as_zatca_checks_it(self):
        invoice, xml = self.xml_for([32, 12, 8])
        head = xml.split('<cac:InvoiceLine>')[0]

        def amounts(pattern):
            return [float(value) for value in re.findall(pattern, head)]

        total = amounts(r'<cac:TaxTotal>\s*<cbc:TaxAmount[^>]*>([^<]+)</cbc:TaxAmount>\s*<cac:TaxSubtotal>')
        subtotals = amounts(r'<cac:TaxSubtotal>\s*<cbc:TaxableAmount[^>]*>[^<]+</cbc:TaxableAmount>\s*'
                            r'<cbc:TaxAmount[^>]*>([^<]+)</cbc:TaxAmount>')
        exclusive = amounts(r'<cbc:TaxExclusiveAmount[^>]*>([^<]+)<')[0]
        inclusive = amounts(r'<cbc:TaxInclusiveAmount[^>]*>([^<]+)<')[0]
        self.assertEqual(total, [round(sum(subtotals), 2)], 'BR-CO-14')
        self.assertEqual(inclusive, round(exclusive + sum(subtotals), 2), 'BR-CO-15')
        self.assertEqual((inclusive, round(sum(subtotals), 2)), (invoice.grand_total, invoice.total_taxes_and_charges))

    def test_a_line_discount_is_stated_net_of_vat_like_the_price(self):
        # 32 at 10% off: the line's net price is 25.04, so its discount is 3.20 / 1.15 = 2.78 and its gross price the
        # menu price net of VAT, 27.83 (27.82 from the two rounded parts). It said 3.20 off a gross price of 28.24.
        _invoice, xml = self.xml_for([(32, 10)])
        line = xml.split('<cac:InvoiceLine>')[1]
        allowance = line.split('<cac:AllowanceCharge>')[1]
        amount = float(re.search(r'<cbc:Amount[^>]*>([^<]+)<', allowance).group(1))
        base = float(re.search(r'<cbc:BaseAmount[^>]*>([^<]+)<', allowance).group(1))
        net_price = float(re.search(r'<cbc:PriceAmount[^>]*>([^<]+)<', line).group(1))
        self.assertEqual(amount, 2.78)
        self.assertAlmostEqual(base, 32 / 1.15, delta=0.011)
        self.assertEqual(round(base - amount, 2), net_price)
