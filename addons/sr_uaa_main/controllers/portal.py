# -*- coding: utf-8 -*-

import hashlib
import hmac
import logging
from unicodedata import normalize
import psycopg2
import pytz
import werkzeug
from odoo.addons.payment.controllers.portal import WebsitePayment
from odoo.addons.payment.controllers.portal import PaymentProcessing
from odoo import http, _, fields
from odoo.http import request
from odoo.osv import expression
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT, consteq, ustr
from odoo.tools.float_utils import float_repr
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)


class PaymentProcessing(PaymentProcessing):
    @staticmethod
    def remove_payment_transaction(transactions):
        tx_ids_list = request.session.get("__payment_tx_ids__", [])
        if transactions:
            for tx in transactions:
                if tx.id in tx_ids_list:
                    tx_ids_list.remove(tx.id)
        else:
            return False
        request.session["__payment_tx_ids__"] = tx_ids_list
        return True

    @staticmethod
    def add_payment_transaction(transactions):
        if not transactions:
            return False
        tx_ids_list = set(request.session.get("__payment_tx_ids__", [])) | set(transactions.ids)
        request.session["__payment_tx_ids__"] = list(tx_ids_list)
        return True

    @staticmethod
    def get_payment_transaction_ids():
        # return the ids and not the recordset, since we might need to
        # sudo the browse to access all the record
        # I prefer to let the controller chose when to access to payment.transaction using sudo
        return request.session.get("__payment_tx_ids__", [])

    @http.route(['/payment/process'], type="http", auth="public", website=True, sitemap=False)
    def payment_status_page(self, **kwargs):
        # When the customer is redirect to this website page,
        # we retrieve the payment transaction list from his session
        tx_ids_list = self.get_payment_transaction_ids()
        payment_transaction_ids = request.env['payment.transaction'].sudo().browse(tx_ids_list).exists()
        uaa_services = request.env['uaa.services'].sudo().search([])
        render_ctx = {
            'payment_tx_ids': payment_transaction_ids.ids,
            'uaa_services': uaa_services
        }
        return request.render("payment.payment_process_page", render_ctx)

    @http.route(['/payment/process/poll'], type="json", auth="public")
    def payment_status_poll(self):
        # retrieve the transactions
        tx_ids_list = self.get_payment_transaction_ids()

        payment_transaction_ids = request.env['payment.transaction'].sudo().search([
            ('id', 'in', list(tx_ids_list)),
            ('date', '>=', (datetime.now() - timedelta(days=1)).strftime(DEFAULT_SERVER_DATETIME_FORMAT)),
        ])
        if not payment_transaction_ids:
            return {
                'success': False,
                'error': 'no_tx_found',
            }

        processed_tx = payment_transaction_ids.filtered('is_processed')
        self.remove_payment_transaction(processed_tx)

        # create the returned dictionnary
        result = {
            'success': True,
            'transactions': [],
        }
        # populate the returned dictionnary with the transactions data
        for tx in payment_transaction_ids:
            message_to_display = tx.acquirer_id[tx.state + '_msg'] if tx.state in ['done', 'pending',
                                                                                   'cancel'] else None
            tx_info = {
                'reference': tx.reference,
                'state': tx.state,
                'return_url': tx.return_url,
                'is_processed': tx.is_processed,
                'state_message': tx.state_message,
                'message_to_display': message_to_display,
                'amount': tx.amount,
                'currency': tx.currency_id.name,
                'acquirer_provider': tx.acquirer_id.provider,
            }
            tx_info.update(tx._get_processing_info())
            result['transactions'].append(tx_info)

        tx_to_process = payment_transaction_ids.filtered(lambda x: x.state == 'done' and x.is_processed is False)
        try:
            tx_to_process._post_process_after_done()
        except psycopg2.OperationalError as e:
            request.env.cr.rollback()
            result['success'] = False
            result['error'] = "tx_process_retry"
        except Exception as e:
            request.env.cr.rollback()
            result['success'] = False
            result['error'] = str(e)
            _logger.exception("Error while processing transaction(s) %s, exception \"%s\"", tx_to_process.ids, str(e))

        return result


class WebsitePayment(WebsitePayment):

    @http.route(['/website_payment/pay'], type='http', auth='public', website=True, sitemap=False)
    def pay(self, reference='', order_id=None, amount=False, currency_id=None, acquirer_id=None, partner_id=False,
            access_token=None, **kw):

        """
        Generic payment page allowing public and logged in users to pay an arbitrary amount.

        In the case of a public user access, we need to ensure that the payment is made anonymously - e.g. it should not be
        possible to pay for a specific partner simply by setting the partner_id GET param to a random id. In the case where
        a partner_id is set, we do an access_token check based on the payment.link.wizard model (since links for specific
        partners should be created from there and there only). Also noteworthy is the filtering of s2s payment methods -
        we don't want to create payment tokens for public users.

        In the case of a logged in user, then we let access rights and security rules do their job.
        """
        env = request.env
        user = env.user.sudo()
        reference = normalize('NFKD', reference).encode('ascii', 'ignore').decode('utf-8')

        if partner_id and not access_token:
            raise werkzeug.exceptions.NotFound
        if partner_id and access_token:
            token_ok = request.env['payment.link.wizard'].check_token(access_token, int(partner_id), float(amount),
                                                                      int(currency_id))
            if not token_ok:
                raise werkzeug.exceptions.NotFound

        invoice_id = kw.get('invoice_id')

        # Default values
        values = {
            'amount': 0.0,
            'currency': user.company_id.currency_id,
        }
        # Check sale order
        if order_id:
            try:
                order_id = int(order_id)
                if partner_id:
                    # `sudo` needed if the user is not connected.
                    # A public user woudn't be able to read the sale order.
                    # With `partner_id`, an access_token should be validated, preventing a data breach.
                    order = env['sale.order'].sudo().browse(order_id)
                    # if order.enquiry_id:
                    #     if order.enquiry_id.arrival_date:
                    #         arrival_date = order.enquiry_id.arrival_date

                else:
                    order = env['sale.order'].browse(order_id)

                # order.check_order_expiry()
                validity_time = order.validity_date_hour
                if datetime.now() > validity_time:
                    payment_expired = True
                else:
                    if order.state != 'sent': #checking for already paid payments jan9
                        payment_expired = True
                    else:
                        payment_expired = False

                sale_order_line_id = False
                additional_charges_id = False
                if reference and len(reference) > 6 and reference[:7] == 'charge-':
                    additional_charges_id = reference.split('-')[1]
                    additional_charges_id = env['additional.charge.sale'].sudo().browse(int(additional_charges_id))
                else:
                    sale_order_line_id = order and order.order_line.filtered(lambda x: x.id == int(reference))

                departure_date = order and order.enquiry_id and (order.enquiry_id.departure_date_str and order.enquiry_id.departure_date_str.split() \
                                 and len(order.enquiry_id.departure_date_str.split()) > 1 \
                                 and order.enquiry_id.departure_date_str.split()[0] or order.enquiry_id.departure_date_str) or ''
                departure_time = order and order.enquiry_id and (order.enquiry_id.departure_date_str and order.enquiry_id.departure_date_str.split() \
                                 and len(order.enquiry_id.departure_date_str.split()) > 1 \
                                 and ' '.join(order.enquiry_id.departure_date_str.split()[1:]) or order.enquiry_id.departure_date_str) or ''
                arrival_date = order and order.enquiry_id and (order.enquiry_id.arrival_date_str and order.enquiry_id.arrival_date_str.split() \
                               and len(order.enquiry_id.arrival_date_str.split()) > 1 and \
                               order.enquiry_id.arrival_date_str.split()[0] or order.enquiry_id.arrival_date_str) or ''
                arrival_time = order and order.enquiry_id and (order.enquiry_id.arrival_date_str and order.enquiry_id.arrival_date_str.split() \
                               and len(order.enquiry_id.arrival_date_str.split()) > 1 and ' '.join(
                    order.enquiry_id.arrival_date_str.split()[1:]) or order.enquiry_id.arrival_date_str) or ''
                values.update({
                    'currency': order.currency_id,
                    'amount': order.amount_total,
                    'reference': order.uaa_services_id and order.uaa_services_id.name or '',
                    'sale_order': order,
                    'additional_charges_id': additional_charges_id,
                    'sale_order_line': sale_order_line_id,
                    'order_id': order.id,
                    'payment_expired': payment_expired,
                    'get_arrival_date': arrival_date,
                    'get_arrival_time': arrival_time,
                    'get_departure_date': departure_date,
                    'get_departure_time': departure_time,
                })

            except Exception as ex:
                order_id = None

        if invoice_id:
            try:
                values['invoice_id'] = int(invoice_id)
            except ValueError:
                invoice_id = None

        # Check currency
        if currency_id:
            try:
                currency_id = int(currency_id)
                values['currency'] = env['res.currency'].browse(currency_id)
            except:
                pass

        # Check amount
        if amount:
            try:
                amount = float(amount)
                values['amount'] = amount
            except:
                pass

        # Check reference
        reference_values = order_id and {'sale_order_ids': [(4, order_id)]} or {}
        values['reference'] = env['payment.transaction']._compute_reference(values=reference_values, prefix=reference)

        # Check acquirer
        acquirers = None
        if order_id and order:
            cid = order.company_id.id
        elif kw.get('company_id'):
            try:
                cid = int(kw.get('company_id'))
            except:
                cid = user.company_id.id
        else:
            cid = user.company_id.id

        # Check partner
        if not user._is_public():
            # NOTE: this means that if the partner was set in the GET param, it gets overwritten here
            # This is something we want, since security rules are based on the partner - assuming the
            # access_token checked out at the start, this should have no impact on the payment itself
            # existing besides making reconciliation possibly more difficult (if the payment partner is
            # not the same as the invoice partner, for example)
            partner_id = user.partner_id.id
        elif partner_id:
            partner_id = int(partner_id)

        values.update({
            'partner_id': partner_id,
            'bootstrap_formatting': True,
            'error_msg': kw.get('error_msg')
        })

        acquirer_domain = ['&', ('state', 'in', ['enabled', 'test']), ('company_id', '=', cid)]
        if partner_id:
            partner = request.env['res.partner'].browse([partner_id])
            acquirer_domain = expression.AND([
                acquirer_domain,
                ['|', ('country_ids', '=', False), ('country_ids', 'in', [partner.sudo().country_id.id])]
            ])
        if acquirer_id:
            acquirers = env['payment.acquirer'].browse(int(acquirer_id))
        if order_id:
            acquirers = env['payment.acquirer'].search(acquirer_domain)
        if not acquirers:
            acquirers = env['payment.acquirer'].search(acquirer_domain)

        values['acquirers'] = self._get_acquirers_compatible_with_current_user(acquirers)
        if partner_id:
            values['pms'] = request.env['payment.token'].search([
                ('acquirer_id', 'in', acquirers.ids),
                ('partner_id', 'child_of', partner.commercial_partner_id.id)
            ])
        else:
            values['pms'] = []
        uaa_services = request.env['uaa.services'].sudo().search([])
        values.update({
            'uaa_services': uaa_services
        })
        return request.render('payment.pay', values)

    @http.route(['/website_payment/transaction/<string:reference>/<string:amount>/<string:currency_id>',
                 '/website_payment/transaction/v2/<string:amount>/<string:currency_id>/<path:reference>',
                 '/website_payment/transaction/v2/<string:amount>/<string:currency_id>/<path:reference>/<int:partner_id>'],
                type='json', auth='public')
    def transaction(self, acquirer_id, reference, amount, currency_id, partner_id=False, **kwargs):

        acquirer = request.env['payment.acquirer'].browse(acquirer_id)
        order_id = kwargs.get('order_id')
        invoice_id = kwargs.get('invoice_id')

        reference_values = order_id and {'sale_order_ids': [(4, order_id)]} or {}
        reference = request.env['payment.transaction']._compute_reference(values=reference_values, prefix=reference)

        values = {
            'acquirer_id': int(acquirer_id),
            'reference': reference,
            'amount': float(amount),
            'currency_id': int(currency_id),
            'partner_id': partner_id,
            'type': 'form_save' if acquirer.save_token != 'none' and partner_id else 'form',
        }

        if order_id:
            values['sale_order_ids'] = [(6, 0, [order_id])]
            custorder_obj = request.env['sale.order'].sudo().browse(order_id)
            if custorder_obj and custorder_obj.partner_id:
                values['partner_id'] = custorder_obj.partner_id.id
        elif invoice_id:
            values['invoice_ids'] = [(6, 0, [invoice_id])]

        reference_values = order_id and {'sale_order_ids': [(4, order_id)]} or {}
        reference_values.update(acquirer_id=int(acquirer_id))
        values['reference'] = request.env['payment.transaction']._compute_reference(values=reference_values,
                                                                                    prefix=reference)
        tx = request.env['payment.transaction'].sudo().with_context(lang=None).create(values)
        secret = request.env['ir.config_parameter'].sudo().get_param('database.secret')
        token_str = '%s%s%s' % (
            tx.id, tx.reference, float_repr(tx.amount, precision_digits=tx.currency_id.decimal_places))
        token = hmac.new(secret.encode('utf-8'), token_str.encode('utf-8'), hashlib.sha256).hexdigest()
        tx.return_url = '/website_payment/confirm?tx_id=%d&access_token=%s' % (tx.id, token)

        PaymentProcessing.add_payment_transaction(tx)
        uaa_services = request.env['uaa.services'].sudo().search([])
        render_values = {
            'partner_id': partner_id,
            'type': tx.type,
            'uaa_services': uaa_services
        }
        if values.get('partner_id',False):
            render_values.update({'partner_id': values['partner_id']})

        return acquirer.sudo().render(tx.reference, float(amount), int(currency_id), values=render_values)

    @http.route(['/website_payment/token/<string:reference>/<string:amount>/<string:currency_id>',
                 '/website_payment/token/v2/<string:amount>/<string:currency_id>/<path:reference>',
                 '/website_payment/token/v2/<string:amount>/<string:currency_id>/<path:reference>/<int:partner_id>'],
                type='http', auth='public', website=True)
    def payment_token(self, pm_id, reference, amount, currency_id, partner_id=False, return_url=None, **kwargs):
        token = False
        if pm_id:
            if '_' in pm_id:
                pm_id = int(pm_id.split('_')[1])
            else:
                token = request.env['payment.token'].browse(int(pm_id))
        order_id = kwargs.get('order_id')
        invoice_id = kwargs.get('invoice_id')

        if not token:
            return request.redirect('/website_payment/pay?error_msg=%s' % _('Cannot setup the payment.'))

        values = {
            'acquirer_id': token.acquirer_id.id,
            'reference': reference,
            'amount': float(amount),
            'currency_id': int(currency_id),
            'partner_id': int(partner_id),
            'payment_token_id': int(pm_id),
            'type': 'server2server',
            'return_url': return_url,
        }

        if order_id:
            values['sale_order_ids'] = [(6, 0, [int(order_id)])]
        if invoice_id:
            values['invoice_ids'] = [(6, 0, [int(invoice_id)])]

        tx = request.env['payment.transaction'].sudo().with_context(lang=None).create(values)
        PaymentProcessing.add_payment_transaction(tx)

        try:
            tx.s2s_do_transaction()
            secret = request.env['ir.config_parameter'].sudo().get_param('database.secret')
            token_str = '%s%s%s' % (
                tx.id, tx.reference, float_repr(tx.amount, precision_digits=tx.currency_id.decimal_places))
            token = hmac.new(secret.encode('utf-8'), token_str.encode('utf-8'), hashlib.sha256).hexdigest()
            tx.return_url = return_url or '/website_payment/confirm?tx_id=%d&access_token=%s' % (tx.id, token)
        except Exception as e:
            _logger.exception(e)
        return request.redirect('/payment/process')

    @http.route(['/website_payment/confirm'], type='http', auth='public', website=True, sitemap=False)
    def confirm(self, **kw):
        tx_id = int(kw.get('tx_id', 0))
        access_token = kw.get('access_token')
        if tx_id:
            if access_token:
                tx = request.env['payment.transaction'].sudo().browse(tx_id)
                secret = request.env['ir.config_parameter'].sudo().get_param('database.secret')
                valid_token_str = '%s%s%s' % (
                    tx.id, tx.reference, float_repr(tx.amount, precision_digits=tx.currency_id.decimal_places))
                valid_token = hmac.new(secret.encode('utf-8'), valid_token_str.encode('utf-8'),
                                       hashlib.sha256).hexdigest()
                if not consteq(ustr(valid_token), access_token):
                    raise werkzeug.exceptions.NotFound
            else:
                tx = request.env['payment.transaction'].browse(tx_id)
            if tx.state in ['done', 'authorized']:
                status = 'success'
                message = tx.acquirer_id.done_msg
            elif tx.state == 'pending':
                status = 'warning'
                message = tx.acquirer_id.pending_msg
            else:
                status = 'danger'
                message = tx.state_message or _('An error occured during the processing of this payment')
            PaymentProcessing.remove_payment_transaction(tx)
            uaa_services = request.env['uaa.services'].sudo().search([])
            if tx and tx.sale_order_ids:
                partner_name = tx.sale_order_ids[0].traveler_name or ''
                if partner_name:
                    tx.partner_name = partner_name
            #print ('t' * 333, tx, tx.partner_name, tx.partner_id)
            return request.render('payment.confirm',
                                  {'tx': tx, 'status': status, 
                                   'message': message, 
                                   'uaa_services': uaa_services})
        else:
            return request.redirect('/')
