
import base64
import datetime
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import pytz
from lxml import etree
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import urllib.parse
from odoo.tools.safe_eval import safe_eval
from odoo.tools.misc import get_lang
from dateutil.parser import parse as parseFecha
from . import api_facturae
from .. import extensions
from . import fe_enums
from math import isclose
_logger = logging.getLogger(__name__)


class AccountInvoiceRefund(models.TransientModel):
    _inherit = "account.move.reversal"

    @api.model
    def _get_invoice_id(self):
        context = dict(self._context or {})
        active_id = context.get('active_id', False)
        if active_id:
            return active_id
        return ''

    @api.model
    def get_default_reference_document_id(self):
        doc_id = self.env['reference.document'].search([('code', '=', '03')])
        if doc_id:
            return doc_id.id
        return ''

    reference_code_id = fields.Many2one("reference.code", string="Code reference", required=False, )
    reference_document_id = fields.Many2one("reference.document", string="Reference Document Id", required=False, default=get_default_reference_document_id)
    invoice_id = fields.Many2one("account.move", string="Invoice Id", default=_get_invoice_id, required=False, )

    def _prepare_default_reversal(self, move):
        result = super(AccountInvoiceRefund, self)._prepare_default_reversal(move)
        if self.reference_code_id and self.reference_document_id:
            result.update({
                'invoice_id': move.id,
                'reference_document_id': self.reference_document_id.id,
                'reference_code_id': self.reference_code_id.id,
                'tipo_documento': 'NC' if move.move_type == 'out_invoice' else 'ND',
            })
        return result

class InvoiceLineElectronic(models.Model):
    _inherit = "account.move.line"

    @api.model
    def _get_default_activity_id(self):
        for line in self:
            line.economic_activity_id = line.product_id and line.product_id.categ_id and line.product_id.categ_id.economic_activity_id and line.product_id.categ_id.economic_activity_id.id

    @api.depends('currency_id')
    def _compute_always_set_currency_id(self):
        for line in self:
            line.always_set_currency_id = line.currency_id or line.company_currency_id

    discount_note = fields.Char(string="Nota de descuento", required=False, )
    total_tax = fields.Float(string="Total impuesto", required=False, )

    third_party_id = fields.Many2one("res.partner", string="Tercero otros cargos",)

    tariff_head = fields.Char(string="Partida arancelaria para factura de exportación", required=False, )

    categ_name = fields.Char(related='product_id.categ_id.name')
    product_code = fields.Char(related='product_id.default_code')
    economic_activity_id = fields.Many2one("economic.activity", string="Actividad Económica",
                                           required=False, store=True,
                                           context={'active_test': False},
                                           default=False)
    non_tax_deductible = fields.Boolean(string='Indicates if this invoice is non-tax deductible',)
    automatic_created = fields.Boolean(default=False, copy=False)
    always_set_currency_id = fields.Many2one('res.currency', string='Moneda foránea',
        compute='_compute_always_set_currency_id',
        help="Technical field used to compute the monetary field. As currency_id is not a required field, we need to use either the foreign currency, either the company one.")
    price_qty_amount = fields.Monetary(string='Importe', store=True, readonly=True,
                                       currency_field='always_set_currency_id')
    @api.model
    def _get_price_total_and_subtotal_model(self, price_unit, quantity, discount, currency, product, partner, taxes,
                                            move_type):
        res = super(InvoiceLineElectronic, self)._get_price_total_and_subtotal_model(price_unit, quantity, discount, currency,
                                                                               product, partner, taxes, move_type)
        res['price_qty_amount'] = price_unit * quantity
        return res
    """
    @api.onchange('product_id')
    def product_changed(self):
        # Check if the product is non deductible to use a non_deductible tax
        if self.product_id.non_tax_deductible:
            self.non_tax_deductible = True
        else:
            self.non_tax_deductible = False

        # Check for the economic activity in the product or product category or company respectively (already set in the invoice when partner selected)
        if self.product_id and self.product_id.economic_activity_id:
            self.economic_activity_id = self.product_id.economic_activity_id
        elif self.product_id and self.product_id.categ_id and self.product_id.categ_id.economic_activity_id:
            self.economic_activity_id = self.product_id.categ_id.economic_activity_id
        else:
            self.economic_activity_id = self.company_id.activity_id
            #self.economic_activity_id = self.invoice_id.economic_activity_id
    """


    @api.onchange('account_id')
    def _onchange_account_id_override(self):
        ''' Recompute 'tax_ids' based on 'account_id'.
        /!\ Don't remove existing taxes if there is no explicit taxes set on the account.
        '''
        if not self.display_type and (self.account_id.tax_ids or not self.tax_ids) and not self.automatic_created:
            taxes = self._get_computed_taxes()

            if taxes and self.move_id.fiscal_position_id:
                # taxes = self.move_id.fiscal_position_id.map_tax(taxes, partner=self.partner_id)
                taxes = self.move_id.fiscal_position_id.map_tax(taxes)

            self.tax_ids = taxes

    @api.onchange('product_id')
    def _onchange_product_id_override(self):
        for line in self:
            if not line.product_id or line.display_type in ('line_section', 'line_note'):
                continue
            line.account_id = line._get_computed_account()
            line.product_uom_id = line._get_computed_uom()
            line.tax_ids = line.product_id.taxes_id
            if not line.automatic_created:
                if not line.desde_correo:
                    line.price_unit = line._get_computed_price_unit()
                    line.name = line._get_computed_name()
                    line.tax_ids = line._get_computed_taxes()
            if line.move_id.move_type == 'in_invoice':
                line.tax_ids = line._get_computed_taxes()
            # Manage the fiscal position after that and adapt the price_unit.
            # E.g. mapping a price-included-tax to a price-excluded-tax must
            # remove the tax amount from the price_unit.
            # However, mapping a price-included tax to another price-included tax must preserve the balance but
            # adapt the price_unit to the new tax.
            # E.g. mapping a 10% price-included tax to a 20% price-included tax for a price_unit of 110 should preserve
            # 100 as balance but set 120 as price_unit.
            if line.tax_ids and line.move_id.fiscal_position_id:
                line.price_unit = line._get_price_total_and_subtotal()['price_subtotal']
                # line.tax_ids = line.move_id.fiscal_position_id.map_tax(line.tax_ids._origin, partner=line.move_id.partner_id)
                line.tax_ids = line.move_id.fiscal_position_id.map_tax(line.tax_ids._origin)
                accounting_vals = line._get_fields_onchange_subtotal(price_subtotal=line.price_unit, currency=line.move_id.company_currency_id)
                balance = accounting_vals['debit'] - accounting_vals['credit']
                # line.price_unit = line._get_fields_onchange_balance(balance=balance).get('price_unit', line.price_unit)
                line.price_unit = line._get_fields_onchange_balance(amount_currency=balance).get('price_unit', line.price_unit)

            # Convert the unit price to the invoice's currency.
            company = line.move_id.company_id
            #line.price_unit = company.currency_id._convert(line.price_unit, line.move_id.currency_id, company, line.move_id.date)

        if len(self) == 1:
            return {'domain': {'product_uom_id': [('category_id', '=', self.product_uom_id.category_id.id)]}}

    @api.onchange('product_uom_id')
    def _onchange_uom_id_inherit(self):
        ''' Recompute the 'price_unit' depending of the unit of measure. '''
        if not self.automatic_created:
            price_unit = self._get_computed_price_unit()
        else:
            price_unit = self.price_unit

        # See '_onchange_product_id' for details.
        taxes = self._get_computed_taxes()
        if taxes and self.move_id.fiscal_position_id:
            price_subtotal = self._get_price_total_and_subtotal(price_unit=price_unit, taxes=taxes)['price_subtotal']
            accounting_vals = self._get_fields_onchange_subtotal(price_subtotal=price_subtotal, currency=self.move_id.company_currency_id)
            balance = accounting_vals['debit'] - accounting_vals['credit']
            # price_unit = self._get_fields_onchange_balance(balance=balance).get('price_unit', price_unit)
            price_unit = self._get_fields_onchange_balance(amount_currency=balance).get('price_unit', price_unit)

        # Convert the unit price to the invoice's currency.
        company = self.move_id.company_id
        self.price_unit = company.currency_id._convert(price_unit, self.move_id.currency_id, company, self.move_id.date)

    _onchange_uom_id = _onchange_uom_id_inherit
    _onchange_product_id = _onchange_product_id_override
    _onchange_account_id = _onchange_account_id_override

class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _prepare_invoice(self):
        result = super(SaleOrder, self)._prepare_invoice()

        result.update({'economic_activity_id': self.company_id.activity_id.id, 'payment_methods_id': self.partner_id.payment_methods_id.id})
        return result

class AccountInvoiceElectronic(models.Model):
    _inherit = "account.move"

    sale_create_user_id = fields.Many2one(comodel_name='res.users', string='Creador de cotización')
    sale_purchase_order = fields.Char(string='N° Orden de Compra')
    amount_discount = fields.Monetary(string='Descuento', readonly=True, store=True, compute='_compute_amount_discount')
    amount_without_discount_and_tax = fields.Monetary(string='Subtotal', store=True, readonly=True, compute='_compute_amount_discount')
    amount_total_local = fields.Monetary(string='Total local', readonly=True, compute='_compute_amount_local', currency_field='company_currency_id')
    local_currency = fields.Many2one('res.currency', compute="_compute_amount_local", string="Otra moneda")
    not_required_ice_document = fields.Boolean('NO requiere documento del ICE', copy=False)
    partner_vat = fields.Char(string='Cédula del cliente', related='partner_id.vat')

    contingencia = fields.Boolean(string='Contingencia')
    contingencia_company = fields.Boolean(string='Contingenia de compañía', related='company_id.contingencia')
    contingencia_number = fields.Char('Número')

    @api.model
    def create(self, vals):
        res = super(AccountInvoiceElectronic, self).create(vals)
        for rec in res:
            if rec.partner_id.export and rec.move_type == 'out_invoice':
                rec.tipo_documento = 'FEE'
        return res

    def _compute_amount_local(self):
        for inv in self:
            inv.local_currency = inv.company_id.currency_id
            inv.amount_total_local = inv.currency_id._convert(inv.amount_total,inv.company_id.currency_id,inv.company_id,inv.create_date)

    @api.depends('line_ids.debit', 'line_ids.credit', 'line_ids.currency_id', 'line_ids.amount_currency', 'line_ids.amount_residual', 'line_ids.amount_residual_currency', 'line_ids.payment_id.state')
    def _compute_amount_discount(self):
        for move in self:
            price_unit_wo_discount = 0
            for line in move.line_ids:
                price_unit_wo_discount += line.price_unit * line.quantity * (line.discount / 100.0)

            move.amount_discount = price_unit_wo_discount
            move.amount_without_discount_and_tax = move.amount_untaxed + price_unit_wo_discount

    def get_default_tipo_documento_value(self):
        if self._context.get('default_move_type', ''):
            if self._context.get('default_move_type', '').startswith('out_'):
                return {
                    'out_invoice': 'FE',
                    'out_refund': 'NC',
                    'out_receipt': ''
                }[self._context['default_move_type']]
            elif self._context.get('default_move_type', False) == 'entry':
                return None
            else:
                return 'disabled'
        return False

    @api.depends('invoice_filter_type_domain')
    def get_tipos_documento(self):
        nc = [('NC', 'Nota de Crédito'),('ND','Nota de Débito'),('TE','Tiquete Electrónico')]
        if self._context.get('active_model', '') == 'account.move.reversal':
            if self._context.get('active_model', False) and self._context.get('active_id', False):
                type_last_doc = self.env[self._context['active_model']].browse(self._context['active_id']).invoice_id.move_type
                if type_last_doc == 'in_invoice':
                    return [
                        ('CCE', 'Aceptación de comprobante'),
                        ('CPCE', 'Aceptación Parcial de comprobante'),
                        ('RCE', 'Rechazo de comprobante'),
                        ('disabled', 'No generar documento')]

            return nc
        type_docs = {
            'purchase': [
                ('CCE', 'Aceptación de comprobante'),
                ('CPCE', 'Aceptación Parcial de comprobante'),
                ('RCE', 'Rechazo de comprobante'),
                ('FEC', 'Factura Electrónica de Compra'),
                ('FEE', 'Factura Electrónica de Exportación'),
                # ('ND', 'Nota de Débito'),
                ('disabled', 'No generar documento')],
            'sale': [
                ('FE', 'Factura Electrónica'),
                ('FEE', 'Factura Electrónica de Exportación'),
                ('TE', 'Tiquete Electrónico'),
                ('ND', 'Nota de Débito'),
                ('NC', 'Nota de Crédito'),
            ]
        }
        if self._context.get('default_move_type', '') and not self._context.get('from_print', False):
            type_doc = self._context.get('default_move_type', '').startswith('out_') and 'sale' or 'purchase'
            if type_doc == 'sale' and self._context['default_move_type'] == 'out_refund':
                return nc
            return type_docs[type_doc]
        return type_docs['sale'] + type_docs['purchase'] + nc
    name = fields.Char(string="Número", required=False, readonly=False)
    number_electronic = fields.Char(string="Número electrónico", required=False, copy=False, index=True)
    date_issuance = fields.Char(string="Fecha de emisión", required=False, copy=False)
    consecutive_number_receiver = fields.Char(string="Número Consecutivo Receptor", required=False, copy=False, readonly=True, index=True)
    reference_document_ids = fields.One2many('reference.document.line', 'move_id',string='Líneas de documento de referencia')

    state_tributacion = fields.Selection([('aceptado', 'Aceptado'),
                                          ('rechazado', 'Rechazado'),
                                          ('recibido', 'Recibido'),
                                          ('firma_invalida', 'Firma Inválida'),
                                          ('error', 'Error'),
                                          ('procesando', 'Procesando'),
                                          ('na', 'No Aplica'),
                                          ('ne', 'No Encontrado')],
                                         'Estado FE',
                                         copy=False)

    state_invoice_partner = fields.Selection(
        [('1', 'Aceptado'), 
         ('2', 'Aceptacion parcial'),
         ('3', 'Rechazado')], 
         'Respuesta del Cliente')

    reference_code_id = fields.Many2one("reference.code", string="Código de referencia", required=False, )

    reference_document_id = fields.Many2one("reference.document", string="Tipo Documento de referencia", required=False, )

    payment_methods_id = fields.Many2one("payment.methods", string="Métodos de Pago", required=False, )

    invoice_id = fields.Many2one("account.move", string="Documento de referencia", required=False, copy=False)

    xml_respuesta_tributacion = fields.Binary( string="Respuesta Tributación XML", required=False, copy=False, attachment=True)

    electronic_invoice_return_message = fields.Char(
        string='Respuesta Hacienda', readonly=True, )

    fname_xml_respuesta_tributacion = fields.Char(
        string="Nombre de archivo XML Respuesta Tributación", required=False,
        copy=False)
    xml_comprobante = fields.Binary(
        string="Comprobante XML", required=False, copy=False, attachment=True)
    fname_xml_comprobante = fields.Char(
        string="Nombre de archivo Comprobante XML", required=False, copy=False,
        attachment=True)
    xml_supplier_approval = fields.Binary(
        string="XML Proveedor", required=False, copy=False, attachment=True)
    fname_xml_supplier_approval = fields.Char(
        string="Nombre de archivo Comprobante XML proveedor", required=False,
        copy=False, attachment=True)
    amount_tax_electronic_invoice = fields.Monetary(
        string='Total de impuestos FE', readonly=False, )
    amount_total_electronic_invoice = fields.Monetary(
        string='Total FE', readonly=False, )
    xml_vat = fields.Char(string="Identificación XML")

    tipo_documento = fields.Selection(
        selection=get_tipos_documento,
        string="Tipo Comprobante",
        required=False, default=get_default_tipo_documento_value,
        help='Indica el tipo de documento de acuerdo a la '
             'clasificación del Ministerio de Hacienda')

    sequence = fields.Char(string='Consecutivo', readonly=True, copy=False)

    state_email = fields.Selection([('no_email', 'Sin cuenta de correo'), (
        'sent', 'Enviado'), ('fe_error', 'Error FE')], 'Estado email', copy=False)

    invoice_amount_text = fields.Char(string='Monto en Letras', readonly=True, required=False, )

    ignore_total_difference = fields.Boolean(string="Ingorar Diferencia en Totales", required=False, default=False)

    error_count = fields.Integer(string="Cantidad de errores", required=False, default="0")

    economic_activity_id = fields.Many2one("economic.activity", string="Actividad Económica", required=False, )

    economic_activities_ids = fields.Many2many('economic.activity', string=u'Actividades Económicas', compute='_get_economic_activities', )

    not_loaded_invoice = fields.Char(string='Numero Factura Original no cargada', readonly=True, )

    not_loaded_invoice_date = fields.Date(string='Fecha Factura Original no cargada', readonly=True, )
    date_invoice_sent = fields.Datetime('Fecha envio de correo', default=False, copy=False)
    #journal_id = fields.Many2one('account.journal', string='Journal', compute='_get_journal',)
    automatic_created = fields.Boolean(default=False, copy=False)

    _sql_constraints = [
        ('number_electronic_uniq', 'unique (company_id, number_electronic)',
         "La clave de comprobante debe ser única"),
    ]

    def get_qr_url(self):
        return ('%s%s' % (self.env['ir.config_parameter'].sudo().get_param('web.base.url'), self.get_portal_url()) )

    @api.onchange('partner_id', 'company_id')
    def _get_economic_activities(self):
        for inv in self:
            if inv.partner_id:
                if inv.move_type in ('in_invoice', 'in_refund'):
                    inv.economic_activities_ids = inv.partner_id.economic_activities_ids
                    inv.economic_activity_id = inv.partner_id.activity_id
                    inv.tipo_documento = 'FEE' if inv.partner_id.export else inv.tipo_documento
                else:
                    inv.economic_activities_ids = inv.company_id.economic_activities_ids
                    # inv.economic_activity_id = inv.company_id.activity_id
            else:
                inv.economic_activities_ids = inv.company_id.economic_activities_ids
                inv.economic_activity_id = inv.company_id.activity_id

    """ Las siguientes funciones corresponden al IVA Devuelto"""
    @api.onchange('payment_methods_id')
    def _set_tax_tarjeta(self):
        if self.move_type in ['in_invoice', 'in_refund', 'entry']:
            return
        val = True if self.payment_methods_id.check_tarjeta else False
        posiciones_fiscales = self.env['account.fiscal.position'].search([('check_tarjeta', '=', True)])
        if posiciones_fiscales and val:
            self.fiscal_position_id = posiciones_fiscales[0]
        elif not self.fiscal_position_id or self.fiscal_position_id.check_tarjeta:
            self.fiscal_position_id = False
        self._update_line_taxes()

    @api.onchange('fiscal_position_id')
    def _onchange_fiscal_position_id(self):
        if self.move_type in ['in_invoice', 'in_refund', 'entry']:
            return
        self._update_line_taxes()


    def _update_line_taxes(self):
        for inv_line in self.invoice_line_ids:
            # Esto permite que en la posición fiscal haya más de un impuesto
            if self.fiscal_position_id and inv_line.product_id.taxes_id.id in self.fiscal_position_id.tax_ids.tax_src_id.ids:
                if self.move_type in ['out_invoice', 'out_refund']:
                    inv_line.tax_ids = self.fiscal_position_id.tax_ids.tax_dest_id.filtered(
                        lambda x: x.type_tax_use == 'sale')
                elif self.move_type in ['in_invoice', 'in_refund']:
                    inv_line.tax_ids = self.fiscal_position_id.tax_ids.tax_dest_id.filtered(
                        lambda x: x.type_tax_use == 'purchase')
                # Verificación antigua solo soporta un impuesto por posición fiscal
                elif self.fiscal_position_id and inv_line.product_id.taxes_id.id == self.fiscal_position_id.tax_ids.tax_src_id.id:
                    inv_line.tax_ids = self.fiscal_position_id.tax_ids.tax_dest_id
                else:
                    inv_line.tax_ids = inv_line.product_id.taxes_id
            inv_line._onchange_mark_recompute_taxes()
        self._onchange_invoice_line_ids()

    """Fin de IVA devuelto"""


    @api.onchange('partner_id')
    def _partner_changed(self):
        if self.partner_id.export:
            self.tipo_documento = 'FEE'

        if self.move_type in ('in_invoice', 'in_refund'):
            if self.partner_id:
                #Nuevo para auto seleccionar la actividad economica del cliente al momento de seleccionarlo
                if self.partner_id.activity_id:
                    self.economic_activity_id = self.partner_id.activity_id
                #else:
                #    raise UserError(_('Partner does not have a default economic activity'))

                if self.partner_id.payment_methods_id:
                    self.payment_methods_id = self.partner_id.payment_methods_id
                #else:
                #    raise UserError(_('Partner does not have a default payment method'))


    def action_invoice_sent(self):
        self.ensure_one()

        if self.invoice_id.move_type == 'in_invoice' or self.invoice_id.move_type == 'in_refund':
            template = self.env.ref('cr_electronic_invoice.email_template_invoice_vendor', raise_if_not_found=False)
        else:
            template = self.env.ref('account.email_template_edi_invoice', raise_if_not_found=False)
        template.attachment_ids = [(5,0,0)]

        lang = get_lang(self.env)
        if template and template.lang:
            lang = template._render_template(template.lang, 'account.move', [self.id])[self.id]
        else:
            lang = lang.code


        if self.company_id.frm_ws_ambiente == 'disabled':
            pass
            #template.with_context(type='binary', default_type='binary').send_mail(self.id, raise_exception=False, force_send=True)  # default_type='binary'
        elif self.partner_id and self.partner_id.email_fe:  # and not i.partner_id.opt_out:
            attachment_env = self.env['ir.attachment'].sudo()
            attachment = attachment_env.sudo().search(
                [('res_model', '=', 'account.move'),
                 ('res_id', '=', self.id),
                 ('res_field', '=', 'xml_comprobante')], limit=1)
            if not self.partner_id.export:
                if attachment:


                    attachment_resp = attachment_env.sudo().search(
                        [('res_model', '=', 'account.move'),
                         ('res_id', '=', self.id),
                         ('res_field', '=', 'xml_respuesta_tributacion')], limit=1)

                    if attachment_resp:
                        attachment |= attachment_resp
                        for att in attachment:
                            if att.name[-4:] != ".xml":
                                att.name = att.name + ".xml"
                        new_attachments = [a.copy({'res_model': 'mail.compose.message', 'res_id': 0, 'res_field': False}).id for a in attachment]
                        template.attachment_ids = [
                            (6, 0, new_attachments)]

                        # template.email_to = self.partner_id._get_emails()
                    else:
                        raise UserError(_('La respuesta XML de Hacienda no ha sido aún recibida'))
                else:
                    raise UserError(_('El archivo XML no ha sido generado'))

        else:
            raise UserError(_('El destinatario no tiene definido un correo'))

        compose_form = self.env.ref('account.account_invoice_send_wizard_form', raise_if_not_found=False)
        ctx = dict(
            default_model='account.move',
            default_res_id=self.id,
            default_use_template=bool(template),
            default_template_id=template and template.id or False,
            default_composition_mode='comment',
            mark_invoice_as_sent=True,
            custom_layout="mail.mail_notification_paynow",
            model_description=self.with_context(lang=lang).type_name,
            force_email=True
        )

        return {
            'name': _('Send Invoice'),
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'account.invoice.send',
            'views': [(compose_form.id, 'form')],
            'view_id': compose_form.id,
            'target': 'new',
            'context': ctx,
        }

    def sent_email_cron(self, limit=5):
        invoices = self.search(
            [('invoice_date', '>', '2020-06-11'), ('state_tributacion', '=', 'aceptado'), ('date_invoice_sent', '=', False), ('move_type', 'in', ('out_invoice', 'out_refund',))], limit=limit)
        i = 0
        for inv in invoices:
            i += 1
            template = self.env.ref('account.email_template_edi_invoice', raise_if_not_found=False)
            # template.attachment_ids = [(5, 0, 0)]

            lang = get_lang(self.env)
            if template and template.lang:
                lang = template._render_template(template.lang, 'account.move', [inv.id])[inv.id]
            else:
                lang = lang.code

            if inv.partner_id and inv.partner_id.email:  # and not i.partner_id.opt_out:
                attachment_env = self.env['ir.attachment'].sudo()
                attachment = attachment_env.sudo().search(
                    [('res_model', '=', 'account.move'),
                     ('res_id', '=', inv.id),
                     ('res_field', '=', 'xml_comprobante')], limit=1)
                email_values = {
                    'model': 'account.move',  # We don't want to have the mail in the tchatter while in queue!
                    'res_id': inv.id,
                    'author_id': self.env.user.partner_id.id, }
                if attachment:
                    attachment.name = inv.fname_xml_comprobante
                    # attachment.datas_fname = inv.fname_xml_comprobante

                    attachment_resp = attachment_env.sudo().search(
                        [('res_model', '=', 'account.move'),
                         ('res_id', '=', inv.id),
                         ('res_field', '=', 'xml_respuesta_tributacion')], limit=1)
                    email_values['attachment_ids'] = [
                        (0, 0, {'name': inv.fname_xml_comprobante,
                                'mimetype': 'text/xml',
                                'datas': attachment.datas}),
                    ]
                    if attachment_resp:
                        attachment_resp.name = inv.fname_xml_respuesta_tributacion
                        # attachment_resp.datas_fname = inv.fname_xml_respuesta_tributacion
                        # template.attachment_ids = [
                        #     (6, 0, [attachment.id, attachment_resp.id])]
                        # # template.email_to = inv.partner_id._get_emails()
                        email_values['attachment_ids'].append((0, 0, {'name': inv.fname_xml_respuesta_tributacion,
                                                                      'mimetype': 'text/xml',
                                                                      'datas': attachment_resp.datas}))
                        try:
                            template.send_mail(inv.id, raise_exception=False, force_send=True, email_values=email_values)
                            _logger.info('%s/%s Correo enviado de la factura ID: %s' % (str(i), str(len(invoices)), str(inv.id)))
                            pass
                        except Exception as e:
                            _logger.error('ERROR cuando intenta enviar correo: %s' % (e,))
                        inv.date_invoice_sent = fields.Datetime.now()
            else:
                _logger.info('%s/%s Falta email en cliente, Factura ID: %s' % (str(i), str(len(invoices)), str(inv.id)))
                inv.date_invoice_sent = fields.Datetime.now()

    def sent_email(self): #DESCONTINUAR
        template = self.env.ref('account.email_template_edi_invoice', raise_if_not_found=False)
        template.attachment_ids = [(5, 0, 0)]

        lang = get_lang(self.env)
        if template and template.lang:
            lang = template._render_template(template.lang, 'account.move', [self.id])[self.id]
        else:
            lang = lang.code

        if self.partner_id and self.partner_id.email:  # and not i.partner_id.opt_out:
            attachment_env = self.env['ir.attachment'].sudo()
            attachment = attachment_env.sudo().search(
                [('res_model', '=', 'account.move'),
                 ('res_id', '=', self.id),
                 ('res_field', '=', 'xml_comprobante')], limit=1)

            if attachment:
                attachment.name = self.fname_xml_comprobante
                # attachment.datas_fname = self.fname_xml_comprobante

                attachment_resp = attachment_env.sudo().search(
                    [('res_model', '=', 'account.move'),
                     ('res_id', '=', self.id),
                     ('res_field', '=', 'xml_respuesta_tributacion')], limit=1)

                if attachment_resp:
                    attachment_resp.name = self.fname_xml_respuesta_tributacion
                    # attachment_resp.datas_fname = self.fname_xml_respuesta_tributacion

                    template.attachment_ids = [
                        (6, 0, [attachment.id, attachment_resp.id])]
                    # template.email_to = self.partner_id._get_emails()

                    try:
                        template.send_mail(self.id, raise_exception=False, force_send=True)
                    except Exception as e:
                        _logger.error('ERROR cuando intenta enviar correo: %s' % (e,))
                    self.date_invoice_sent = fields.Datetime.now()
    # @api.onchange('xml_supplier_approval')
    # def _onchange_xml_supplier_approval(self):
    #     if self.xml_supplier_approval:
    #         xml_decoded = base64.b64decode(self.xml_supplier_approval)
    #         try:
    #             factura = etree.fromstring(xml_decoded)
    #         except Exception as e:
    #             _logger.info(
    #                 'E-INV CR - This XML file is not XML-compliant.  Exception %s' % e)
    #             return {'status': 400,
    #                     'text': 'Excepción de conversión de XML'}
    #
    #         pretty_xml_string = etree.tostring(
    #             factura, pretty_print=True,
    #             encoding='UTF-8', xml_declaration=True)
    #         _logger.error('E-INV CR - send_file XML: %s' % pretty_xml_string)
    #         namespaces = factura.nsmap
    #         inv_xmlns = namespaces.pop(None)
    #         namespaces['inv'] = inv_xmlns
    #         self.tipo_documento = 'CCE'
    #
    #         if not factura.xpath("inv:Clave", namespaces=namespaces):
    #             return {'value': {'xml_supplier_approval': False},
    #                     'warning': {'title': 'Atención',
    #                                 'message': 'El archivo xml no contiene el nodo Clave. '
    #                                            'Por favor cargue un archivo con el formato correcto.'}}
    #
    #         if not factura.xpath("inv:FechaEmision", namespaces=namespaces):
    #             return {'value': {'xml_supplier_approval': False},
    #                     'warning': {'title': 'Atención',
    #                                 'message': 'El archivo xml no contiene el nodo FechaEmision. Por favor cargue un '
    #                                            'archivo con el formato correcto.'}}
    #
    #         if not factura.xpath("inv:Emisor/inv:Identificacion/inv:Numero",
    #                              namespaces=namespaces):
    #             return {'value': {'xml_supplier_approval': False},
    #                     'warning': {'title': 'Atención',
    #                                 'message': 'El archivo xml no contiene el nodo Emisor. Por favor '
    #                                            'cargue un archivo con el formato correcto.'}}
    #
    #         if not factura.xpath("inv:ResumenFactura/inv:TotalComprobante",
    #                              namespaces=namespaces):
    #             return {'value': {'xml_supplier_approval': False},
    #                     'warning': {'title': 'Atención',
    #                                 'message': 'No se puede localizar el nodo TotalComprobante. Por favor cargue '
    #                                            'un archivo con el formato correcto.'}}
    #
    #     else:
    #         self.state_tributacion = False
    #         self.xml_supplier_approval = False
    #         self.fname_xml_supplier_approval = False
    #         self.xml_respuesta_tributacion = False
    #         self.fname_xml_respuesta_tributacion = False
    #         self.date_issuance = False
    #         self.number_electronic = False
    #         self.state_invoice_partner = False

    def load_xml_data(self):
        account = False
        analytic_account = False
        product = False
        load_lines = False
        # load_lines = bool(self.env['ir.config_parameter'].sudo().get_param('load_lines'))

        # default_account_id = self.env['ir.config_parameter'].sudo().get_param('expense_account_id')
        # if default_account_id:
        #     account = self.env['account.account'].search([('id', '=', default_account_id)], limit=1)
        #
        # analytic_account_id = self.env['ir.config_parameter'].sudo().get_param('expense_analytic_account_id')
        # if analytic_account_id:
        #     analytic_account = self.env['account.analytic.account'].search([('id', '=', analytic_account_id)], limit=1)
        #
        # product_id = self.env['ir.config_parameter'].sudo().get_param('expense_product_id')
        # if product_id:
        #     product = self.env['product.product'].search([('id', '=', product_id)], limit=1)
        
        self.invoice_line_ids = [(5, 0, 0)]
        api_facturae.load_xml_data(self, load_lines, account, product, analytic_account)

    def action_send_mrs_to_hacienda(self):
        if self.state_invoice_partner:
            self.state_tributacion = False
            self.send_mrs_to_hacienda()
        else:
            raise UserError(_('You must select the aceptance state: Accepted, Parcial Accepted or Rejected'))


    def send_mrs_to_hacienda(self):
        for inv in self:
            if inv.xml_supplier_approval:
                inv.amount_total_electronic_invoice = inv.amount_total
                inv.amount_tax_electronic_invoice = inv.amount_tax
                '''Verificar si el MR ya fue enviado y estamos esperando la confirmación'''
                if inv.state_tributacion == 'procesando':

                    token_m_h = api_facturae.get_token_hacienda(
                        inv, inv.company_id.frm_ws_ambiente)

                    api_facturae.consulta_documentos(inv, inv,
                                                     inv.company_id.frm_ws_ambiente,
                                                     token_m_h,
                                                     api_facturae.get_time_hacienda(),
                                                     False)
                else:

                    if inv.state_tributacion and inv.state_tributacion in ('aceptado', 'rechazado', 'na'):
                        raise UserError('Aviso!.\n La factura de proveedor ya fue confirmada')
                    if not inv.amount_total_electronic_invoice and inv.xml_supplier_approval:
                        try:
                            inv.load_xml_data()
                        except UserError as error:
                            inv.state_tributacion='error'
                            inv.message_post(
                                subject='Error',
                                body='Aviso!.\n Error en carga del XML del proveedor'+str(error))
                            continue
                    if abs(inv.amount_total_electronic_invoice - inv.amount_total) > 1:
                        inv.state_tributacion = 'error'
                        inv.message_post(
                            subject='Error',
                            body='Aviso!.\n Monto total no concuerda con monto del XML')
                        continue

                    elif not inv.xml_supplier_approval:
                        inv.state_tributacion = 'error'
                        inv.message_post(
                            subject='Error',
                            body='Aviso!.\n No se ha cargado archivo XML')
                        continue

                    elif not inv.company_id.sucursal_MR or not inv.company_id.terminal_MR:
                        inv.state_tributacion = 'error'
                        inv.message_post(subject='Error',
                                         body='Aviso!.\nPor favor configure el diario de compras, terminal y sucursal')
                        continue

                    if not inv.tipo_documento:
                        inv.state_tributacion = 'error'
                        inv.message_post(subject='Error',
                                         body='Aviso!.\nDebe primero seleccionar el tipo de respuesta para el archivo cargado.')
                        continue

                    if inv.company_id.frm_ws_ambiente != 'disabled' and inv.tipo_documento:

                        # url = self.company_id.frm_callback_url
                        message_description = "<p><b>Enviando Mensaje Receptor</b></p>"

                        '''Si por el contrario es un documento nuevo, asignamos todos los valores'''
                        if not inv.xml_comprobante or inv.state_tributacion not in ['procesando', 'aceptado']:

                            if inv.tipo_documento == 'CCE':
                                detalle_mensaje = 'Aceptado'
                                tipo = 1
                                tipo_documento = 'CCE'
                                sequence = inv.company_id.CCE_sequence_id.next_by_id()

                            elif inv.tipo_documento == 'CPCE':
                                detalle_mensaje = 'Aceptado parcial'
                                tipo = 2
                                tipo_documento = 'CPCE'
                                sequence = inv.company_id.CPCE_sequence_id.next_by_id()

                            else:
                                detalle_mensaje = 'Rechazado'
                                tipo = 3
                                tipo_documento = 'RCE'
                                sequence = inv.company_id.RCE_sequence_id.next_by_id()

                            '''Si el mensaje fue rechazado, necesitamos generar un nuevo id'''
                            if inv.state_tributacion == 'rechazado' or inv.state_tributacion == 'error':
                                message_description += '<p><b>Cambiando consecutivo del Mensaje de Receptor</b> <br />' \
                                                       '<b>Consecutivo anterior: </b>' + inv.consecutive_number_receiver + \
                                                       '<br/>' \
                                                       '<b>Estado anterior: </b>' + inv.state_tributacion + '</p>'

                            '''Solicitamos la clave para el Mensaje Receptor'''
                            response_json = api_facturae.get_clave_hacienda(
                                inv, tipo_documento, sequence,
                                inv.company_id.sucursal_MR,
                                inv.company_id.terminal_MR)

                            inv.consecutive_number_receiver = response_json.get(
                                'consecutivo')
                            '''Generamos el Mensaje Receptor'''
                            if inv.amount_total_electronic_invoice is None or inv.amount_total_electronic_invoice == 0:
                                inv.state_tributacion = 'error'
                                inv.message_post(subject='Error',
                                                body='El monto Total de la Factura para el Mensaje Receptro es inválido')
                                continue

                            now_utc = datetime.datetime.now(pytz.timezone('UTC'))
                            now_cr = now_utc.astimezone(pytz.timezone('America/Costa_Rica'))
                            dia = inv.number_electronic[3:5]  # '%02d' % now_cr.day,
                            mes = inv.number_electronic[5:7]  # '%02d' % now_cr.month,
                            anno = inv.number_electronic[7:9]  # str(now_cr.year)[2:4],

                            date_cr = now_cr.strftime("20" + anno + "-" + mes + "-" + dia + "T%H:%M:%S-06:00")
                            inv.date_issuance = date_cr

                            clave_xml = inv.search_clave_xml()
                            if not inv.partner_id.vat:
                                inv.partner_id.vat = inv.xml_vat
                            xml = api_facturae.gen_xml_mr_43(
                                clave_xml,inv.partner_id.vat,
                                inv.date_issuance,
                                tipo, detalle_mensaje, inv.company_id.vat,
                                inv.consecutive_number_receiver,
                                inv.amount_tax_electronic_invoice,
                                inv.amount_total_electronic_invoice,
                                inv.company_id.activity_id.code,
                                '01')

                            xml_firmado = api_facturae.sign_xml(
                                inv.company_id.signature,
                                inv.company_id.frm_pin, xml)

                            inv.fname_xml_comprobante = tipo_documento + '_' + inv.number_electronic + '.xml'

                            inv.with_context(default_mimetype='application/xml', mimetype='application/xml').xml_comprobante = base64.encodestring(xml_firmado)
                            inv.tipo_documento = tipo_documento

                            if inv.state_tributacion != 'procesando':

                                env = inv.company_id.frm_ws_ambiente
                                token_m_h = api_facturae.get_token_hacienda(
                                    inv, inv.company_id.frm_ws_ambiente)

                                response_json = api_facturae.send_message(
                                    inv, api_facturae.get_time_hacienda(),
                                    xml_firmado,
                                    token_m_h, env)
                                status = response_json.get('status')

                                if 200 <= status <= 299:
                                    inv.state_tributacion = 'procesando'
                                else:
                                    inv.state_tributacion = 'error'
                                    _logger.error(
                                        'E-INV CR - Invoice: %s  Error sending Acceptance Message: %s',
                                        inv.number_electronic,
                                        response_json.get('text'))
                                    inv.message_post(
                                        subject='Error',
                                        body='E-INV CR - Invoice: %s  Error sending Acceptance Message: %s'%(inv.number_electronic,response_json.get('text')))

                                if inv.state_tributacion == 'procesando':
                                    token_m_h = api_facturae.get_token_hacienda(
                                        inv, inv.company_id.frm_ws_ambiente)

                                    if not token_m_h:
                                        _logger.error(
                                            'E-INV CR - Send Acceptance Message - HALTED - Failed to get token')
                                        return

                                    _logger.error(
                                        'E-INV CR - send_mrs_to_hacienda - 013')

                                    response_json = api_facturae.consulta_clave(
                                        inv.number_electronic + '-' + inv.consecutive_number_receiver,
                                        token_m_h,
                                        inv.company_id.frm_ws_ambiente)
                                    status = response_json['status']

                                    if status == 200:
                                        inv.state_tributacion = response_json.get(
                                            'ind-estado')
                                        inv.xml_respuesta_tributacion = response_json.get(
                                            'respuesta-xml')
                                        inv.fname_xml_respuesta_tributacion = 'ACH_' + \
                                                                              inv.number_electronic + '-' + inv.consecutive_number_receiver + '.xml'

                                        _logger.error(
                                            'E-INV CR - Estado Documento:%s',
                                            inv.state_tributacion)

                                        message_description += '<p><b>Ha enviado Mensaje de Receptor</b>' + \
                                                               '<br /><b>Documento: </b>' + inv.number_electronic + \
                                                               '<br /><b>Consecutivo de mensaje: </b>' + \
                                                               inv.consecutive_number_receiver + \
                                                               '<br/><b>Mensaje indicado:</b>' \
                                                               + detalle_mensaje + '</p>'

                                        self.message_post(
                                            body=message_description,
                                            subtype_id=self.env.ref('mail.mt_note').id,
                                            content_subtype='html')

                                        _logger.info(
                                            'E-INV CR - Estado Documento:%s',
                                            inv.state_tributacion)

                                    elif status == 400:
                                        inv.state_tributacion = 'ne'
                                        _logger.error(
                                            'MAB - Aceptacion Documento:%s no encontrado en Hacienda.',
                                            inv.number_electronic + '-' + inv.consecutive_number_receiver)
                                    else:
                                        _logger.error(
                                            'MAB - Error inesperado en Send Acceptance File - Abortando')
                                        return

    @api.returns('self')
    def refund(self, invoice_date=None, date=None, description=None,
               journal_id=None, invoice_id=None,
               reference_code_id=None, reference_document_id=None, 
               invoice_type=None, doc_type=None):

        #if self.env.user.company_id.frm_ws_ambiente == 'disabled':
        #description = description
        #invoice_type=invoice_type
        #doc_type=doc_type
        new_invoices = super(AccountInvoiceElectronic, self)._reverse_moves(default_values_list=[dict(
            invoice_date=invoice_date, date=date,
            journal_id=journal_id, invoice_id=invoice_id,
            reference_code_id=reference_code_id, reference_document_id=reference_document_id, tipo_documento=doc_type,
            type=invoice_type
        )], cancel=True)
        return new_invoices



    @api.onchange('tipo_documento')
    def onchange_tipo_documento(self):
        values = {
            'CCE': '1',
            'CPCE': '2',
            'RCE': '3',
        }
        if self.tipo_documento in values.keys():
            self.state_invoice_partner = values[self.tipo_documento]

    @api.onchange('partner_id', 'company_id')
    def _onchange_partner_id(self):
        result = super(AccountInvoiceElectronic, self)._onchange_partner_id()
        if self._context.get('default_move_type', 'entry') == 'entry':
            return result
        self.payment_methods_id = self.partner_id.payment_methods_id
        #fixme: BRYAN revisar tipo, está malo
        if self.move_type == 'out_refund':
            self.tipo_documento = 'NC'
        elif self.move_type in ('in_invoice', 'in_refund'):
            self.tipo_documento = 'disabled'
        elif self.move_type == 'in_invoice':
            self.tipo_documento = 'CCE'
        elif self.partner_id and self.partner_id.vat:
            if self.partner_id.country_id and self.partner_id.country_id.code != 'CR':
                self.tipo_documento = 'TE'
            elif self.partner_id.identification_id and self.partner_id.identification_id.code == '05':
                self.tipo_documento = 'TE'
            else:
                self.tipo_documento = 'FE'
        else:
            self.tipo_documento = 'FE'

        if self.move_type in ('in_invoice', 'in_refund'):
            self.economic_activity_id = self.partner_id.activity_id
        else:
            self.economic_activity_id = self.company_id.activity_id

    @api.model
    # cron Job that verifies if the invoices are Validated at Tributación
    def _check_hacienda_for_invoices(self, max_invoices=10):
        out_invoices = self.env['account.move'].search(
            [('move_type', 'in', ('out_invoice', 'out_refund')),
             ('state', 'in', ('posted', 'paid')),
             ('state_tributacion', 'in', ('recibido', 'procesando', 'ne'))],  # , 'error'
            limit=max_invoices)

        in_invoices = self.env['account.move'].search(
            [('move_type', '=', 'in_invoice'),
             ('tipo_documento', '=', 'FEC'),
             ('state', 'in', ('posted', 'paid')),
             ('state_tributacion', 'in', ('procesando', 'ne', 'error'))],
            limit=max_invoices)

        invoices = out_invoices | in_invoices

        total_invoices = len(invoices)
        current_invoice = 0

        _logger.info('E-INV CR - Consulta Hacienda - Facturas a Verificar: %s', total_invoices)

        for i in invoices:
            current_invoice += 1
            _logger.info('E-INV CR - Consulta Hacienda - Invoice %s / %s  -  number:%s', current_invoice, total_invoices, i.number_electronic)

            token_m_h = api_facturae.get_token_hacienda(i, i.company_id.frm_ws_ambiente)

            if not token_m_h:
                _logger.error('E-INV CR - Consulta Hacienda - HALTED - Failed to get token')
                return

            if not i.xml_comprobante:
                i.state_tributacion = 'error'
                _logger.warning(u'E-INV CR - Documento:%s no tiene documento XML.  Estado: %s', i.number_electronic, 'error')
                i.message_post(
                    subject='Error',
                    body=u'E-INV CR - Documento:%s no tiene documento XML.  Estado: %s' % (i.number_electronic, 'error'))
                continue

            if not i.number_electronic or len(i.number_electronic) != 50:
                i.state_tributacion = 'error'
                _logger.warning(u'E-INV CR - Documento:%s no cumple con formato de número electrónico.  Estado: %s', i.number, 'error')
                i.message_post(
                    subject='Error',
                    body=u'E-INV CR - Documento:%s no cumple con formato de número electrónico.  Estado: %s' % (i.number, 'error'))
                continue

            response_json = api_facturae.consulta_clave(i.number_electronic, token_m_h, i.company_id.frm_ws_ambiente)
            status = response_json['status']

            if status == 200:
                estado_m_h = response_json.get('ind-estado')
                _logger.info('E-INV CR - Estado Documento:%s', estado_m_h)
            elif status == 400:
                estado_m_h = response_json.get('ind-estado')
                i.state_tributacion = 'ne'
                _logger.warning('E-INV CR - Documento:%s no encontrado en Hacienda.  Estado: %s', i.number_electronic, estado_m_h)
                continue
            else:
                _logger.error('E-INV CR - Error inesperado en Consulta Hacienda - Abortando')
                return

            i.state_tributacion = estado_m_h

            if estado_m_h == 'aceptado':
                i.fname_xml_respuesta_tributacion = 'AHC_' + i.number_electronic + '.xml'
                i.xml_respuesta_tributacion = response_json.get('respuesta-xml')
                if i.tipo_documento != 'FEC' and i.partner_id and i.partner_id.email and False:  # and not i.partner_id.opt_out:
                    email_template = self.env.ref('account.email_template_edi_invoice', False)
                    attachment_env = self.env['ir.attachment'].sudo()
                    attachment = attachment_env.search(
                        [('res_model', '=', 'account.move'),
                         ('res_id', '=', i.id),
                         ('res_field', '=', 'xml_comprobante')], limit=1)
                    attachment.name = i.fname_xml_comprobante
                    # attachment.datas_fname = i.fname_xml_comprobante
                    attachment.mimetype = 'text/xml'

                    attachment_resp = attachment_env.search(
                        [('res_model', '=', 'account.move'),
                         ('res_id', '=', i.id),
                         ('res_field', '=', 'xml_respuesta_tributacion')],
                        limit=1)
                    attachment_resp.name = i.fname_xml_respuesta_tributacion
                    # attachment_resp.datas_fname = i.fname_xml_respuesta_tributacion
                    attachment_resp.mimetype = 'text/xml'

                    email_template.attachment_ids = [
                        (6, 0, [attachment.id, attachment_resp.id])]

                    email_template.with_context(type='binary',
                                                default_type='binary').send_mail(
                        i.id,
                        raise_exception=False,
                        force_send=True)  # default_type='binary'

                    email_template.attachment_ids = [(5,0,0)]

            elif estado_m_h in ('firma_invalida'):
                if i.error_count > 10:
                    i.fname_xml_respuesta_tributacion = 'AHC_' + i.number_electronic + '.xml'
                    i.xml_respuesta_tributacion = response_json.get('respuesta-xml')
                    i.state_email = 'fe_error'
                    _logger.info('email no enviado - factura rechazada')
                else:
                    i.error_count += 1
                    i.state_tributacion = 'procesando'

            elif estado_m_h == 'rechazado':
                i.state_email = 'fe_error'
                i.state_tributacion = estado_m_h
                i.fname_xml_respuesta_tributacion = 'AHC_' + i.number_electronic + '.xml'
                i.xml_respuesta_tributacion = response_json.get('respuesta-xml')
            else:
                if i.error_count > 10:
                    i.state_tributacion = 'error'
                    i.message_post(
                        subject='Error',
                        body='Máximo de intentos en error')
                elif i.error_count < 4:
                    i.error_count += 1
                    i.state_tributacion = 'procesando'
                else:
                    i.error_count += 1
                    i.state_tributacion = ''
                # doc.state_tributacion = 'no_encontrado'
                _logger.error('MAB - Consulta Hacienda - Invoice not found: %s  -  Estado Hacienda: %s', i.number_electronic, estado_m_h)

    def action_check_hacienda(self):
        if self.company_id.frm_ws_ambiente != 'disabled':
            for inv in self:
                inv.amount_total_electronic_invoice = inv.amount_total
                inv.amount_tax_electronic_invoice = inv.amount_tax
                token_m_h = api_facturae.get_token_hacienda(inv, inv.company_id.frm_ws_ambiente)
                api_facturae.consulta_documentos(self, inv, self.company_id.frm_ws_ambiente, token_m_h, False, False)


    @api.model
    def _check_hacienda_for_mrs(self, max_invoices=10):  # cron
        invoices = self.env['account.move'].search(
            [('move_type', 'in', ('in_invoice', 'in_refund')),
             ('tipo_documento', 'not in', ('FEC','disabled')),
             ('state', '=', 'posted'),
             ('xml_supplier_approval', '!=', False),
             # ('state_invoice_partner', '!=', False),
             ('state_tributacion', 'not in', ('aceptado', 'rechazado', 'error', 'na'))],
            limit=max_invoices)
        total_invoices = len(invoices)
        current_invoice = 0

        for inv in invoices:
            # CWong: esto no debe llamarse porque cargaría de nuevo los impuestos y ya se pusieron como debería
            # if not i.amount_total_electronic_invoice:
            #     i.charge_xml_data()
            current_invoice += 1
            _logger.info('_check_hacienda_for_mrs - Invoice %s / %s  -  number:%s  -  ID:%s', current_invoice, total_invoices, inv.number_electronic, inv.id)
            inv.send_mrs_to_hacienda()

    def action_create_fec(self):
        if self.company_id.frm_ws_ambiente == 'disabled':
            raise UserError(_('Hacienda API is disabled in company'))
        else:
            self.generate_and_send_invoices(self)

    @api.model
    def _send_invoices_to_hacienda(self, max_invoices=10):  # cron
        #if self.company_id.frm_ws_ambiente != 'disabled':
        _logger.debug('E-INV CR - Ejecutando _send_invoices_to_hacienda')
        invoice_env = self.env['account.move']
        company_ids = self.env['res.company'].search([('contingencia', '=', False)]).ids
        invoices = invoice_env.search([('move_type', 'in', ('out_invoice', 'out_refund')),
                                                      ('state', 'in', ('posted', 'paid')),
                                                      ('number_electronic', '!=', False),
                                                      ('invoice_date', '>=', '2019-07-01'),
                                                      '|', ('state_tributacion', '=', False),
                                                      ('state_tributacion', '=', 'ne'),
                                                      ('company_id', 'in', company_ids)],
                                                      order='id asc', limit=max_invoices)
        invoices2 = invoice_env.search([('move_type', '=', 'in_invoice'),
                                                      ('tipo_documento', '=', 'FEC'),
                                                      ('state', 'in', ('posted', 'paid')),
                                                      ('number_electronic', '!=', False),
                                                      ('invoice_date', '>=', '2019-07-01'),
                                                      '|', ('state_tributacion', '=', False),
                                                      ('state_tributacion', '=', 'ne'),
                                                      ('company_id', 'in', company_ids)],
                                                      order='id asc', limit=max_invoices)
        self.generate_and_send_invoices(invoices)
        self.generate_and_send_invoices(invoices2)

        _logger.info('E-INV CR - _send_invoices_to_hacienda - Finalizado Exitosamente')

    def generate_and_send_invoices(self, invoices):
        total_invoices = len(invoices)
        current_invoice = 0

        for inv in invoices:
            _logger.info('generate_and_send_invoices-Procesando factura: ' + str(inv.id))
            try:
                current_invoice += 1

                if not inv.sequence or not inv.sequence.isdigit():  # or (len(inv.number) == 10):
                    inv.state_tributacion = 'na'
                    _logger.info('E-INV CR - Ignored invoice:%s', inv.name)
                    continue

                _logger.debug('generate_and_send_invoices - Invoice %s / %s  -  number:%s',
                              current_invoice, total_invoices, inv.number_electronic)

                if not inv.xml_comprobante or (inv.tipo_documento == 'FEC' and inv.state_tributacion == 'rechazado'):

                    if inv.tipo_documento == 'FEC' and inv.state_tributacion == 'rechazado':
                        inv.message_post(body='Se está enviando otra FEC porque la anterior fue rechazada por Hacienda. Adjuntos los XMLs anteriores. Clave anterior: ' + inv.number_electronic,
                                         subject='Envío de una segunda FEC',
                                         message_type='notification',
                                         subtype_id=None,
                                         parent_id=False,
                                         attachments=[[inv.fname_xml_respuesta_tributacion, inv.fname_xml_respuesta_tributacion],
                                                      [inv.fname_xml_comprobante, inv.fname_xml_comprobante]],)

                        sequence = inv.company_id.FEC_sequence_id.next_by_id()
                        response_json = api_facturae.get_clave_hacienda(self,
                                                                        inv.tipo_documento,
                                                                        sequence,
                                                                        inv.journal_id.sucursal,
                                                                        inv.journal_id.terminal)

                        inv.number_electronic = response_json.get('clave')
                        inv.sequence = response_json.get('consecutivo')

                    now_utc = datetime.datetime.now(pytz.timezone('UTC'))
                    now_cr = now_utc.astimezone(pytz.timezone('America/Costa_Rica'))
                    dia = inv.number_electronic[3:5]  # '%02d' % now_cr.day,
                    mes = inv.number_electronic[5:7]  # '%02d' % now_cr.month,
                    anno = inv.number_electronic[7:9]  # str(now_cr.year)[2:4],

                    date_cr = now_cr.strftime("20" + anno + "-" + mes + "-" + dia + "T%H:%M:%S-06:00")

                    inv.date_issuance = date_cr

                    numero_documento_referencia = False
                    fecha_emision_referencia = False
                    codigo_referencia = False
                    tipo_documento_referencia = False
                    razon_referencia = False
                    currency = inv.currency_id
                    invoice_comments = inv.narration

                    if (inv.invoice_id or inv.not_loaded_invoice) and inv.reference_code_id and inv.reference_document_id:
                        if inv.invoice_id:
                            fecha_emision_referencia = inv.invoice_id.date_issuance or \
                                           (inv.invoice_id.invoice_date and
                                            inv.invoice_id.invoice_date.strftime("%Y-%m-%d") + "T12:00:00-06:00")
                            if inv.invoice_id.number_electronic:
                                numero_documento_referencia = inv.invoice_id.number_electronic
                            else:
                                numero_documento_referencia = (inv.invoice_id and inv.invoice_id.sequence) or (inv.ref and len(inv.ref)>35 and inv.ref[15:34] or '0000000') or '0000000'
                        else:
                            numero_documento_referencia = inv.not_loaded_invoice
                            fecha_emision_referencia = inv.not_loaded_invoice_date.strftime("%Y-%m-%d") + "T12:00:00-06:00"
                        tipo_documento_referencia = inv.reference_document_id.code
                        codigo_referencia = inv.reference_code_id.code
                        razon_referencia = inv.reference_code_id.name

                    if not(tipo_documento_referencia and numero_documento_referencia and fecha_emision_referencia) and inv.reference_document_ids:
                        r_doc = inv.reference_document_ids[0]
                        tipo_documento_referencia = r_doc.type_reference_document_id.code
                        fecha_emision_referencia = r_doc.external_document_id.date_issuance
                        numero_documento_referencia = r_doc.external_document_id.number_electronic
                        codigo_referencia = r_doc.reference_code_id.code
                        razon_referencia = r_doc.reference_reason

                    if inv.invoice_payment_term_id:
                        sale_conditions = inv.invoice_payment_term_id.sale_conditions_id and inv.invoice_payment_term_id.sale_conditions_id.sequence or '01'
                    else:
                        sale_conditions = '01'

                    # Validate if invoice currency is the same as the company currency
                    if currency.name == 'CRC': #self.company_id.currency_id.name:
                        currency_rate = 1
                    else:
                        if currency.rate >= 1:
                            currency_rate = currency_rate = round(currency.search([('name', '=', 'CRC')]).rate, 5)
                        else:
                            currency_rate = round(1.0 / currency.rate, 5)

                    # Generamos las líneas de la factura
                    lines = dict()
                    otros_cargos = dict()
                    otros_cargos_id = 0
                    line_number = 0
                    total_otros_cargos = 0.0
                    total_iva_devuelto = 0.0
                    total_servicio_salon = 0.0
                    total_timbre_odontologico = 0.0
                    total_servicio_gravado = 0.0
                    total_servicio_exento = 0.0
                    total_servicio_exonerado = 0.0
                    total_mercaderia_gravado = 0.0
                    total_mercaderia_exento = 0.0
                    total_mercaderia_exonerado = 0.0
                    total_descuento = 0.0
                    total_impuestos = 0.0
                    base_subtotal = 0.0
                    for inv_line in inv.invoice_line_ids:
                        if inv_line.display_type in ('line_note','line_section'):
                            continue
                        # Revisamos si está línea es de Otros Cargos
                        if False: #inv_line.product_id and inv_line.product_id.id == self.env.ref('cr_electronic_invoice.product_iva_devuelto').id:
                            total_iva_devuelto = -inv_line.price_total

                        elif inv_line.product_id and inv_line.product_id.categ_id.name == 'Otros Cargos':
                            otros_cargos_id += 1
                            otros_cargos[otros_cargos_id] = {
                                'TipoDocumento': inv_line.product_id.default_code,
                                'Detalle': escape(inv_line.name[:150]),
                                'MontoCargo': inv_line.price_total
                            }
                            if inv_line.third_party_id:
                                otros_cargos[otros_cargos_id]['NombreTercero'] = inv_line.third_party_id.name

                                if inv_line.third_party_id.vat:
                                    otros_cargos[otros_cargos_id]['NumeroIdentidadTercero'] = inv_line.third_party_id.vat

                            total_otros_cargos += inv_line.price_total

                        else:
                            line_number += 1
                            price = inv_line.price_unit
                            quantity = inv_line.quantity
                            if not quantity:
                                continue

                            line_taxes = inv_line.tax_ids.compute_all(
                                price, currency, 1,
                                product=inv_line.product_id,
                                partner=inv_line.partner_id)

                            price_unit = round(line_taxes['total_excluded'], 5)

                            base_line = round(price_unit * quantity, 5)
                            descuento = inv_line.discount and round(
                                price_unit * quantity * inv_line.discount / 100.0,
                                5) or 0.0

                            subtotal_line = round(base_line - descuento, 5)

                            # Corregir error cuando un producto trae en el nombre "", por ejemplo: "disco duro"
                            # Esto no debería suceder, pero, si sucede, lo corregimos
                            if inv_line.name[:156].find('"'):
                                detalle_linea = inv_line.name[:160].replace(
                                    '"', '')

                            line = {
                                "cantidad": quantity,
                                "detalle": escape(detalle_linea),
                                "precioUnitario": price_unit,
                                "montoTotal": base_line,
                                "subtotal": subtotal_line,
                                "BaseImponible": subtotal_line,
                                "unidadMedida": inv_line.product_uom_id and inv_line.product_uom_id.code or 'Sp'
                            }

                            if inv_line.product_uom_id and line['unidadMedida'] not in fe_enums.codigo_unidades:
                                raise UserError('El código de la unidad de medida seleccionado para %s, no es correcto, '
                                                'por favor actualizarlo.' % (inv_line.product_uom_id.name))

                            if inv_line.product_id:
                                line["codigo"] = inv_line.product_id.default_code or ''
                                line["codigoProducto"] = inv_line.product_id.code or ''

                            if inv_line.product_id.cabys_code:
                                line["codigoCabys"] = inv_line.product_id.cabys_code
                            elif inv_line.product_id.product_tmpl_id.cabys_code:
                                line["codigoCabys"] = inv_line.product_id.product_tmpl_id.cabys_code
                            elif inv_line.product_id.categ_id and inv_line.product_id.categ_id.cabys_code:
                                line["codigoCabys"] = inv_line.product_id.categ_id.cabys_code

                            if inv.tipo_documento == 'FEE' and inv_line.tariff_head:
                                line["partidaArancelaria"] = inv_line.tariff_head

                            if inv_line.discount and price_unit > 0:
                                total_descuento += descuento
                                line["montoDescuento"] = descuento
                                line["naturalezaDescuento"] = inv_line.discount_note or 'Descuento Comercial'

                            # Se generan los impuestos
                            taxes = dict()
                            _line_tax = 0.0
                            _tax_exoneration = False
                            _percentage_exoneration = 0
                            if inv_line.tax_ids:
                                tax_index = 0

                                taxes_lookup = {}
                                for i in inv_line.tax_ids:
                                    if i.has_exoneration:
                                        _tax_exoneration = i.has_exoneration
                                        taxes_lookup[i.id] = {'tax_code': i.tax_root.tax_code,
                                                              'tarifa': i.tax_root.amount,
                                                              'iva_tax_desc': i.tax_root.iva_tax_desc,
                                                              'iva_tax_code': i.tax_root.iva_tax_code,
                                                              'exoneration_percentage': i.percentage_exoneration,
                                                              'amount_exoneration': i.amount}
                                    elif i.check_tarjeta:
                                        taxes_lookup[i.id] = {'tax_code': i.tax_code,
                                                              'tarifa': i.amount_iva_devuelto,
                                                              'iva_tax_desc': i.iva_tax_desc,
                                                              'iva_tax_code': i.iva_tax_code,
                                                              'check_tarjeta': i.check_tarjeta}
                                    else:
                                        taxes_lookup[i.id] = {'tax_code': i.tax_code,
                                                              'tarifa': i.amount,
                                                              'iva_tax_desc': i.iva_tax_desc,
                                                              'iva_tax_code': i.iva_tax_code}


                                for i in line_taxes['taxes']:
                                    if taxes_lookup[i['id']]['tax_code'] == 'service':
                                        total_servicio_salon += round(
                                            subtotal_line * taxes_lookup[i['id']]['tarifa'] / 100, 5)
                                    elif taxes_lookup[i['id']]['tax_code']=='timbre':
                                        total_timbre_odontologico += round(
                                            subtotal_line * taxes_lookup[i['id']]['tarifa'] / 100, 5)

                                    elif taxes_lookup[i['id']]['tax_code'] != '00':
                                        tax_index += 1
                                        # tax_amount = round(i['amount'], 5) * quantity
                                        tax_amount = round(subtotal_line * taxes_lookup[i['id']]['tarifa'] / 100, 5)
                                        if taxes_lookup[i['id']].get('check_tarjeta'):
                                            total_iva_devuelto += tax_amount
                                        _line_tax += tax_amount
                                        tax = {
                                            'codigo': taxes_lookup[i['id']]['tax_code'],
                                            'tarifa': taxes_lookup[i['id']]['tarifa'],
                                            'monto': tax_amount,
                                            'iva_tax_desc': taxes_lookup[i['id']]['iva_tax_desc'],
                                            'iva_tax_code': taxes_lookup[i['id']]['iva_tax_code'],
                                        }
                                        # Se genera la exoneración si existe para este impuesto
                                        if _tax_exoneration:
                                            _tax_amount_exoneration = round(
                                                tax_amount - subtotal_line * taxes_lookup[i['id']][
                                                    'amount_exoneration'] / 100, 5)

                                            if _tax_amount_exoneration == 0.0:
                                                _tax_amount_exoneration = tax_amount

                                            _line_tax -= _tax_amount_exoneration
                                            _percentage_exoneration = ((100 / taxes_lookup[i['id']]['tarifa']) * taxes_lookup[i['id']]['exoneration_percentage']) / 100
                                            # _percentage_exoneration = taxes_lookup[i['id']]['exoneration_percentage']/100.0 #int(taxes_lookup[i['id']]['exoneration_percentage']) / 100
                                            tax["exoneracion"] = {
                                                "montoImpuesto": _tax_amount_exoneration,
                                                # "montoImpuesto": round(_line_tax, 5),
                                                "porcentajeCompra": int(
                                                    taxes_lookup[i['id']]['exoneration_percentage'])
                                            }

                                        taxes[tax_index] = tax

                                line["impuesto"] = taxes
                                line["impuestoNeto"] = round(_line_tax, 5)
                                # line["impuestoNeto"] = _tax_amount_exoneration

                            # Si no hay uom_id se asume como Servicio
                            if not inv_line.product_uom_id or inv_line.product_uom_id.code in ('Al', 'Alc', 'Cm', 'I', 'Sp', 'Os', 'Spe', 'St', 'h', 'd'):  # inv_line.product_id.type == 'service'
                                #if not inv_line.product_uom_id or inv_line.product_uom_id.category_id.name == 'Services':  # inv_line.product_id.type == 'service'
                                if taxes:
                                    if _tax_exoneration:
                                        if _percentage_exoneration < 1:
                                            total_servicio_gravado += (base_line *  (1-_percentage_exoneration))
                                        total_servicio_exonerado += (base_line * _percentage_exoneration)

                                    else:
                                        total_servicio_gravado += base_line

                                    total_impuestos += _line_tax
                                else:
                                    total_servicio_exento += base_line
                            else:
                                if taxes:
                                    if _tax_exoneration:
                                        if _percentage_exoneration < 1:
                                            total_mercaderia_gravado += (base_line *  (1-_percentage_exoneration))
                                        total_mercaderia_exonerado += (base_line * _percentage_exoneration)

                                    else:
                                        total_mercaderia_gravado += base_line

                                    total_impuestos += _line_tax
                                else:
                                    total_mercaderia_exento += base_line

                            base_subtotal += subtotal_line

                            line["montoTotalLinea"] = round(subtotal_line + _line_tax, 5)

                            lines[line_number] = line
                    if total_servicio_salon:
                        total_servicio_salon = round(total_servicio_salon, 5)
                        total_otros_cargos += total_servicio_salon
                        otros_cargos_id += 1
                        otros_cargos[otros_cargos_id] = {
                            'TipoDocumento': '06',
                            'Detalle': escape('Servicio salon 10%'),
                            'MontoCargo': total_servicio_salon
                        }
                    if total_timbre_odontologico:
                        total_timbre_odontologico = round(total_timbre_odontologico, 5)
                        total_otros_cargos += total_timbre_odontologico
                        otros_cargos_id += 1
                        otros_cargos[otros_cargos_id] = {
                            'TipoDocumento': '07',
                            'Detalle': escape('Timbre de Colegios Profesionales'),
                            'MontoCargo': total_timbre_odontologico
                        }

                    # convertir el monto de la factura a texto
                    inv.invoice_amount_text = extensions.text_converter.number_to_text_es(base_subtotal + total_impuestos - total_iva_devuelto)

                    # TODO: CORREGIR BUG NUMERO DE FACTURA NO SE GUARDA EN LA REFERENCIA DE LA NC CUANDO SE CREA MANUALMENTE
                    #if not inv.origin:
                    #    inv.move_name = inv.invoice_id.display_name
                    #    inv.origin = inv.invoice_id.display_name



                    if abs(base_subtotal + total_impuestos + total_otros_cargos - total_iva_devuelto - inv.amount_total) > 0.5:
                        inv.state_tributacion = 'error'
                        inv.message_post(
                            subject='Error',
                            body='Monto factura no concuerda con monto para XML. Factura: %s XML:%s base:%s impuestos:%s otros_cargos:%s iva_devuelto:%s' % (
                                inv.amount_total, (base_subtotal + total_impuestos + total_otros_cargos - total_iva_devuelto), base_subtotal, total_impuestos, total_otros_cargos, total_iva_devuelto))
                        continue
                    total_servicio_gravado = round(total_servicio_gravado, 5)
                    total_servicio_exento = round(total_servicio_exento, 5)
                    total_servicio_exonerado = round(total_servicio_exonerado,5)
                    total_mercaderia_gravado = round(total_mercaderia_gravado, 5)
                    total_mercaderia_exento = round(total_mercaderia_exento, 5)
                    total_mercaderia_exonerado = round(total_mercaderia_exonerado, 5)
                    total_otros_cargos = round(total_otros_cargos, 5)
                    total_iva_devuelto = round(total_iva_devuelto, 5)
                    base_subtotal = round(base_subtotal, 5)
                    total_impuestos = round(total_impuestos, 5)
                    total_descuento = round(total_descuento, 5)
                    # ESTE METODO GENERA EL XML DIRECTAMENTE DESDE PYTHON

                    if inv.partner_id.vat == '4000042139' and not inv.not_required_ice_document:
                        if not inv.ref:
                            raise UserError("Debe ingresar un 'Documento origen' cuando se factura al ICE")
                        numero_documento_referencia = inv.ref

                    xml_string_builder = api_facturae.gen_xml_v43(
                        inv, sale_conditions, total_servicio_gravado,
                        total_servicio_exento, total_servicio_exonerado,
                        total_mercaderia_gravado, total_mercaderia_exento,
                        total_mercaderia_exonerado, total_otros_cargos, total_iva_devuelto, base_subtotal,
                        total_impuestos, total_descuento, json.dumps(lines, ensure_ascii=False),
                        otros_cargos, currency_rate, invoice_comments,
                        tipo_documento_referencia, numero_documento_referencia,
                        fecha_emision_referencia, codigo_referencia, razon_referencia)

                    xml_to_sign = str(xml_string_builder)
                    xml_firmado = api_facturae.sign_xml(
                        inv.company_id.signature,
                        inv.company_id.frm_pin,
                        xml_to_sign)

                    inv.xml_comprobante = base64.encodestring(xml_firmado)
                    inv.fname_xml_comprobante = inv.tipo_documento + '_' + inv.number_electronic + '.xml'

                    _logger.info('E-INV CR - SIGNED XML:%s', inv.fname_xml_comprobante)
                else:
                    xml_firmado = inv.xml_comprobante

                # Get token from Hacienda
                token_m_h = api_facturae.get_token_hacienda(inv, inv.company_id.frm_ws_ambiente)

                response_json = api_facturae.send_xml_fe(inv, token_m_h, inv.date_issuance, xml_firmado, inv.company_id.frm_ws_ambiente)

                response_status = response_json.get('status')
                response_text = response_json.get('text')

                if 200 <= response_status <= 299:
                    if inv.tipo_documento == 'FEC':
                        inv.state_tributacion = 'procesando'
                    else:
                        inv.state_tributacion = 'procesando'
                    inv.electronic_invoice_return_message = response_text
                else:
                    if response_text.find('ya fue recibido anteriormente') != -1:
                        if inv.tipo_documento == 'FEC':
                            inv.state_tributacion = 'procesando'
                        else:
                            inv.state_tributacion = 'procesando'
                        inv.message_post(subject='Error', body='Ya recibido anteriormente, se pasa a consultar')
                    elif inv.error_count > 10:
                        inv.message_post(subject='Error', body=response_text)
                        inv.electronic_invoice_return_message = response_text
                        inv.state_tributacion = 'error'
                        _logger.error('E-INV CR  - Invoice: %s  Status: %s Error sending XML: %s' % (inv.number_electronic, response_status, response_text))
                        inv.message_post(
                            subject='Error',
                            body='E-INV CR  - Invoice: %s  Status: %s Error sending XML: %s' % (inv.number_electronic, response_status, response_text))
                    else:
                        inv.error_count += 1
                        if inv.tipo_documento == 'FEC':
                            inv.state_tributacion = 'procesando'
                        else:
                            inv.state_tributacion = 'procesando'
                        inv.message_post(subject='Error', body=response_text)
                        _logger.error('E-INV CR  - Invoice: %s  Status: %s Error sending XML: %s' % (inv.number_electronic, response_status, response_text))
            except Exception as e:
                _logger.error('E-INV CR  - Invoice: %s  Error sending XML: %s' % (inv.id, str(e)))

                ex_type, ex_value, ex_traceback = sys.exc_info()

                # Extract unformatter stack traces as tuples
                trace_back = traceback.extract_tb(ex_traceback)

                # Format stacktrace
                stack_trace = list()
                stack_trace_complete = ''
                for trace in traceback.extract_stack(sys.exc_info()[2].tb_frame):
                    stack_trace_complete += str(trace.filename) + ' ' + str(trace.lineno) + ' ' + str(
                        trace.name) + '\n ' + str(trace.line + '\n')
                for trace in trace_back:
                    stack_trace.append(
                        "File : %s , Line : %d, Func.Name : %s, Message : %s" % (
                        trace[0], trace[1], trace[2], trace[3]))
                raise UserError('Error al enviar la factura con id:  %s debido a %s \n %s  ' % (
                    str(stack_trace_complete), str(inv.id), str(stack_trace)))

    def _post(self, soft=True):
        for doc in self:
            if doc.company_id.skip_facturacion or doc.company_id.frm_ws_ambiente=='disabled':
                continue
            if doc.move_type not in ('out_invoice', 'out_refund', 'in_invoice', 'in_refund'):
                continue

            if doc.move_type == 'in_invoice' and doc.tipo_documento == 'FEC':
                if not doc.partner_id.country_id or not doc.partner_id.country_id.code == 'CR' \
                    or not doc.partner_id.identification_id or not doc.partner_id.vat or not doc.partner_id.email or not doc.partner_id.phone or not doc.economic_activity_id:
                    raise UserError('Las facturas de compra(Regimen Simplificado) requieren que el proveedor tenga '
                                    'definida los datos de la direccion, así como tambien la actividad económica y correo y teléfono')

            if doc.move_type in ('out_invoice','out_refund'):
                for l in doc.invoice_line_ids:
                    if not l.product_id.cabys_code and not l.product_id.categ_id.cabys_code and not l.product_id.product_tmpl_id.cabys_code and not l.display_type in ('line_note', 'line_section'):
                        raise UserError('En la línea %s no se ha establecido código cabys' % l.name)
            if doc.move_type in ('out_invoice','out_refund'):
                doc.invoice_date = (datetime.datetime.now() - datetime.timedelta(hours=6)).date()
            else:
                doc.invoice_date = doc.date

            (tipo_documento, sequence) = doc.get_invoice_sequence()
            doc.tipo_documento = tipo_documento

            if doc.tipo_documento == 'disabled' and doc.move_type == 'in_invoice':
                if sequence:
                    doc.write(dict(sequence=sequence, name=sequence))
                continue
            if not sequence:
                continue
            response_json = api_facturae.get_clave_hacienda(doc,
                                                            doc.tipo_documento,
                                                            sequence,
                                                            doc.journal_id.sucursal,
                                                            doc.journal_id.terminal)
            doc.write(dict(
                sequence=response_json.get('consecutivo'),
                name=response_json.get('consecutivo'),
                number_electronic=response_json.get('clave')
            ))
        return super(AccountInvoiceElectronic, self)._post(soft)

    def action_debit_note(self):

        accountmove=self
        invoice_lines = []

        for l in accountmove.invoice_line_ids:

            invoice_lines.append([0, 0, {
                'name': l.name, # a label so accountant can understand where this line come from
                # 'move_id': 59,
                'product_id': l.product_id,  # Id del producto en ODOO
                'price_unit': abs(l.price_unit),
                'quantity': l.quantity,
                # 'credit': 1000.0,
                # 'debit': 0.0,
                'discount': l.discount,
                # 'balance': -1000.0,
                # 'account_id': 363,
                # 'date': '2020-04-07',
                # 'partner_id': 12,
                # 'amount_currency': 0.0,
                # 'amount_residual': 0.0,
                # 'account_root_id': 48045,
                # 'amount_residual_currency': 0.0,
                'currency_id': False,
                'tax_ids': l.tax_ids
            }])

        nuevafacturaND = self.env['account.move'].create({
                # 'bank_partner_id': 1,
            # 'commercial_partner_id': 12,
            'currency_id': accountmove.currency_id.id,
            # 'display_name': 'Borrador de factura',
            # 'document_request_line_id': False,
            'fiscal_position_id': False,
            # 'has_reconciled_entries': True,
            # 'invoice_date': accountmove.invoice_date,
            # 'invoice_date_due': accountmove.invoice_date_due,
            # 'invoice_filter_type_domain': 'sale',
            # 'invoice_incoterm_id': False,
            'invoice_origin': False,  # Puede ponerse número de referencia de Factura de ECOM
            # 'invoice_partner_bank_id': False,
            'invoice_payment_term_id': accountmove.invoice_payment_term_id.id,
            'payment_methods_id':accountmove.payment_methods_id.id,
            'economic_activity_id':accountmove.economic_activity_id.id,
            # 'invoice_sequence_number_next': '0000000005',
            # 'invoice_sequence_number_next_prefix': '0010000101',
            'invoice_user_id': accountmove.invoice_user_id.id,  # comercial o vendedor
            'invoice_vendor_bill_id': False,
            'journal_id': accountmove.journal_id.id,
            # 'name': 'Borrador de factura',  # Numero de Factura
            'narration': False,
            'partner_id': accountmove.partner_id.id,  # id de Cliente de ODOO
            'ref': '',  # Pueden escribir cualquier referencia
            'move_type': 'out_invoice',  # Out_invoice Factura de Cliente
            'state': 'draft',
            'tipo_documento':'ND',
            'invoice_line_ids': invoice_lines
        })


        razon = self.env.ref('cr_electronic_invoice.ReferenceCode_4').id
        nuevafacturaND.reference_document_ids = [
            (0, 0, {'invoice_id': accountmove.id, 'move_id': nuevafacturaND.id, 'reference_code_id': razon})]

        nuevafacturaND.invoice_id = accountmove.id
        nuevafacturaND.reference_code_id = razon
        nuevafacturaND.reference_document_id = accountmove.reference_document_id


        form_view_id = self.env.ref("account.view_move_form").id
        return {
            'type': 'ir.actions.act_window',
            'res_id': nuevafacturaND.id,
            'name': 'Nueva Factura ND',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'account.move',
            'views': [(form_view_id, 'form')],
            'target': 'current',
        }

    def get_invoice_sequence(self):
        tipo_documento = self.tipo_documento
        sequence = False

        if self.move_type == 'out_invoice':
            # tipo de identificación
            # Verificar si es nota DEBITO
            # if self.invoice_id and self.journal_id and (
            #         self.journal_id.code == 'NDV'):
            #     tipo_documento = 'ND'
            #     sequence = self.journal_id.ND_sequence_id.next_by_id()
            #
            # else:
            # FIXME: BRYAN revisar tipo, está malo / no está respetando todas las secuencias puestas.
            if self.partner_id.identification_id.code == '00' or self.tipo_documento == 'TE':
                tipo_documento = 'TE'
            elif not self.partner_id.vat:
                raise UserError('El cliente no tiene asignado un número de cédula.\nSi tiene la información disponible \
                agreguela en la ficha del cliente, en caso contrario seleccione el tipo de documento como tiquete electrónico')

            elif not self.partner_id.identification_id:
                raise UserError('El cliente no tiene asignado un tipo de cédula')
            #if tipo_documento == 'FE' and (not self.partner_id.vat or self.partner_id.identification_id.code == '05'):
            #    tipo_documento = 'TE' #ELIMINAR: BRYAN
            sequence_obj = False
            if tipo_documento == 'FE':
                sequence_obj = self.journal_id.FE_sequence_id
            elif tipo_documento == 'ND':
                sequence_obj = self.journal_id.ND_sequence_id
            elif tipo_documento == 'TE':
                sequence_obj = self.journal_id.TE_sequence_id
            elif tipo_documento == 'FEE':
                sequence_obj = self.journal_id.FEE_sequence_id
            else:
                raise UserError('Tipo documento "%s" es inválido para una factura' % (tipo_documento,))
            if not sequence_obj:
                raise UserError('No hay definido una secuencia para el tipo documento "%s" '% tipo_documento)
            sequence = sequence_obj.next_by_id()
        # Credit Note
        elif self.move_type == 'out_refund':
            tipo_documento = 'NC'
            sequence = self.journal_id.NC_sequence_id.next_by_id()

        # Digital Supplier Invoice
        elif self.move_type == 'in_refund' and self.xml_supplier_approval and self.tipo_documento == 'CCE':
            sequence = self.company_id.CCE_sequence_id.next_by_id()
        elif self.move_type == 'in_invoice' and self.xml_supplier_approval:
            sequence = self.company_id.CCE_sequence_id.next_by_id()
        elif self.move_type == 'in_invoice' and not self.xml_supplier_approval and tipo_documento == 'disabled':
            sequence = self.company_id.DGD_sequence_id.next_by_id()
        elif self.move_type == 'in_invoice' and self.tipo_documento == 'FEC': #self.partner_id.country_id and \
            #self.partner_id.country_id.code == 'CR' and self.partner_id.identification_id and self.partner_id.vat and self.xml_supplier_approval is False:
            tipo_documento = 'FEC'
            if not self.company_id.FEC_sequence_id:
                raise UserError('No hay definido una secuencia para el tipo documento FEC')
            sequence = self.company_id.FEC_sequence_id.next_by_id()
        elif self.move_type == 'in_refund' and self.tipo_documento == 'RCE':
            sequence = self.company_id.RCE_sequence_id.next_by_id()

        return (tipo_documento,sequence)

    def action_post(self):

        # Que no permita publicar asientos contables cuando el debe y el haber son distintos
        if self.move_type == 'entry':
            total_debit = 0
            total_credit = 0
            for line in self.line_ids:
                total_debit += line.debit
                total_credit += line.credit
            if round(total_credit, ndigits=8) != round(total_debit, ndigits=8):
                # Validando el caso cuando un elemento tiene un valor muy corto y el otro es casi igual pero con un valor muy largo ejem; 0.99 vs 0.9899999999999
                if abs(round(total_credit, ndigits=8) - round(total_debit, ndigits=8)) >=0.0099999999:
                    raise UserError(f"No se puede publicar el asiento %s con id: %s dado que las lineas de crédito y débito no son iguales" % (self.name, self.id))

        # Solo aplica las siguiente validaciones para compañias con FE en pruebas o produccion
        if self.company_id.frm_ws_ambiente == 'disabled':
            return super(AccountInvoiceElectronic, self).action_post()

        # Validar datos requeridos para el ICE
        if self.partner_id.vat == '4000042139' and not self.not_required_ice_document:
            if not self.ref:
                raise UserError("Debe ingresar una 'Referencia' cuando se factura al ICE\n"
                                "En casos especiales donde no se requiera,  puede marcar la opción 'NO requiere documento del ICE'")
        # Validación para facturas de clientes. Para facturas con exoneración deben contar con toda la información
        if self.move_type == 'out_invoice':
            for rec in self.line_ids:
                exoneration = False
                for tax in rec.tax_ids:
                    if tax.has_exoneration:
                        exoneration = True

                if exoneration:
                    if not self.env['res.partner'].search([['id', '=', self.partner_id.id]]).has_exoneration:
                        raise UserError("El cliente no tiene exoneración")
                    elif not self.partner_id.exoneration_number:
                        raise UserError("El cliente no tiene número de exoneración")
                    elif not self.partner_id.type_exoneration:
                        raise UserError("El cliente no tiene tipo de exoneración")
                    elif not self.partner_id.institution_name:
                        raise UserError("El cliente no tiene Nombre de Institución")
                    elif not self.partner_id.date_issue:
                        raise UserError("El cliente no tiene Fecha del documento en exoneración")
                    elif not self.partner_id.date_expiration:
                        raise UserError("El cliente no tiene Fecha de expiración de documento de exoneración")
                    elif self.partner_id.date_expiration < datetime.datetime.today().date():
                        raise UserError("El documento de exoneración expiró")

        # Validación para facturas de clientes y proveedores, deben contar con actividad económica con código.
        if self.move_type == 'out_invoice' or self.tipo_documento == 'FEC':
            if not self.economic_activity_id:
                raise UserError("La factura no tiene actividad económica")
            elif self.env['economic.activity'].search([['id', '=', self.economic_activity_id.id]]).code == '':
                raise UserError("La actividad no tiene  código")

        # document_from_pos = self.company_id.def_partner_id == self.partner_id or False
        # if self.tipo_documento == 'NC' and not self.reversed_entry_id and not document_from_pos:
        #     raise UserError("El cliente seleccionado debe estar configurado en 'Terminal Punto de Venta/Configuración/Configuración/Facturación/Cliente por defecto para facturas'")

        # Para documentos tipo FEC el cliente debe contar con provincia, cantón y distrito
        if self.tipo_documento == 'FEC':
            if not self.partner_id.state_id:
                raise UserError("El cliente no tiene provincia")
            if not self.partner_id.county_id:
                raise UserError("El cliente no tiene cantón")
            if not self.partner_id.district_id:
                raise UserError("El cliente no tiene distrito")

        self.contingencia = self.contingencia_company

        # Actualizar fechas de apuntes contables
        res = super(AccountInvoiceElectronic, self).action_post()
        if self.move_type in ['out_invoice', 'out_refund']:
            for line in self.line_ids:
                line.date = self.invoice_date
        return res

    """
        def action_post(self):
        # Revisamos si el ambiente para Hacienda está habilitado
        for inv in self:
            if not inv.sequence or inv.company_id.frm_ws_ambiente == 'disabled':
                super(AccountInvoiceElectronic, inv).action_post()
                continue
            currency = inv.currency_id

            if (inv.invoice_id ) and not (inv.reference_code_id and inv.reference_document_id):
                raise UserError('Datos incompletos de referencia para nota de crédito')
            elif (inv.not_loaded_invoice or inv.not_loaded_invoice_date) and not (inv.not_loaded_invoice and inv.not_loaded_invoice_date and inv.reference_code_id and inv.reference_document_id):
                raise UserError('Datos incompletos de referencia para nota de crédito no cargada')

            if self.type == 'in_invoice' and self.tipo_documento == 'FEC':
                if not self.partner_id.country_id or not self.partner_id.country_id.code == 'CR' \
                    or not self.partner_id.identification_id or not self.partner_id.vat or not self.economic_activity_id:
                    raise UserError('Las facturas de compra(Regimen Simplificado) requieren que el proveedor tenga definida los datos de la direccion, así como tambien la actividad económica')

            # Digital Invoice or ticket
            '''
            (tipo_documento, sequence) = inv.get_invoice_sequence()
            inv.name = sequence
            if inv.type in ('out_invoice', 'out_refund') and inv.number_electronic:  # Keep original Number Electronic
                pass
            else:
                if tipo_documento and sequence:
                    inv.tipo_documento = tipo_documento
                else:
                    super(AccountInvoiceElectronic, inv).action_post()
                    continue

            '''

            # tipo de identificación
            if not inv.company_id.identification_id:
                raise UserError('Seleccione el tipo de identificación del emisor en el perfil de la compañía')

            if inv.partner_id and inv.partner_id.vat:
                identificacion = re.sub('[^0-9]', '', inv.partner_id.vat)
                id_code = inv.partner_id.identification_id and inv.partner_id.identification_id.code
                if not id_code:
                    if len(identificacion) == 9:
                        id_code = '01'
                    elif len(identificacion) == 10:
                        id_code = '02'
                    elif len(identificacion) in (11, 12):
                        id_code = '03'
                    else:
                        id_code = '05'

                if id_code == '01' and len(identificacion) != 9:
                    raise UserError('La Cédula Física del receptor debe de tener 9 dígitos')
                elif id_code == '02' and len(identificacion) != 10:
                    raise UserError('La Cédula Jurídica del receptor debe de tener 10 dígitos')
                elif id_code == '03' and len(identificacion) not in (11, 12):
                    raise UserError('La identificación DIMEX del receptor debe de tener 11 o 12 dígitos')
                elif id_code == '04' and len(identificacion) != 10:
                    raise UserError('La identificación NITE del receptor debe de tener 10 dígitos')

            if inv.invoice_payment_term_id and not inv.invoice_payment_term_id.sale_conditions_id:
                raise UserError('No se pudo Crear la factura electrónica: \n Debe configurar condiciones de pago para %s' % (inv.invoice_payment_term_id.name))

            # Validate if invoice currency is the same as the company currency
            if currency.name != inv.company_id.currency_id.name and (not currency.rate_ids or not (len(currency.rate_ids) > 0)):
                raise UserError(_('No hay tipo de cambio registrado para la moneda %s' % (currency.name)))

            # actividad_clinica = self.env.ref('cr_electronic_invoice.activity_851101')
            # if actividad_clinica.id == inv.economic_activity_id.id and inv.payment_methods_id.sequence == '02':
            if inv.economic_activity_id.name == 'CLINICA, CENTROS MEDICOS, HOSPITALES PRIVADOS Y OTROS' and inv.payment_methods_id.sequence == '02':
                iva_devuelto = 0
                for i in inv.invoice_line_ids:
                    for t in i.tax_ids:
                        if t.tax_code == '01' and t.iva_tax_code == '04':
                            iva_devuelto += i.price_total - i.price_subtotal
                if iva_devuelto:
                    prod_iva_devuelto = self.env.ref('cr_electronic_invoice.product_iva_devuelto')
                    inv_line_iva_devuelto = self.env['account.move.line'].create({
                        'name': 'IVA Devuelto',
                        'invoice_id': inv.id,
                        'product_id': prod_iva_devuelto.id,
                        'account_id': prod_iva_devuelto.property_account_income_id.id,
                        'price_unit': -iva_devuelto,
                        'quantity': 1,
                    })

            super(AccountInvoiceElectronic, inv).action_post()
            if not inv.number_electronic:
                response_json = api_facturae.get_clave_hacienda(inv,
                                                            inv.tipo_documento,
                                                            sequence,
                                                            inv.journal_id.sucursal,
                                                            inv.journal_id.terminal)
                inv.number_electronic = response_json.get('clave')
                inv.sequence = response_json.get('consecutivo')
            inv.name = inv.sequence
            inv.state_tributacion = False
            inv.invoice_amount_text = ''
    """

    def _check_balanced_override(self):
        pass

    _check_balanced = _check_balanced_override

    @api.onchange('xml_supplier_approval')
    def _onchange_xml_supplier_approval(self):
        if self.xml_supplier_approval:
            xml_decoded = base64.b64decode(self.xml_supplier_approval)
            try:
                parser = etree.XMLParser(ns_clean=True, recover=True, encoding='utf-8')
                factura = etree.fromstring((re.sub(' xmlns=(\'|\")([a-zA-Z]|\:|\/|\.|\d|\-)*(\'|\")', '',xml_decoded.decode('utf-8'), count=1)).encode('utf_8'), parser)
            except Exception as e:
                _logger.info(
                    'E-INV CR - This XML file is not XML-compliant.  Exception %s' % e)
                return {'status': 400,
                        'text': 'Excepción de conversión de XML'}


            # pretty_xml_string = etree.tostring(
            #     factura, pretty_print=True,

            #     encoding='UTF-8', xml_declaration=True)
            # #_logger.error('E-INV CR - send_file XML: %s' % pretty_xml_string)
            # namespaces = factura.nsmap
            # inv_xmlns = namespaces.pop(None)
            # namespaces['inv'] = inv_xmlns
            self.tipo_documento = 'CCE'


            if not factura.xpath("Clave"):
                return {'value': {'xml_supplier_approval': False},
                        'warning': {'title': 'Atención',
                                    'message': 'El archivo xml no contiene el nodo Clave. '
                                               'Por favor cargue un archivo con el formato correcto.'}}

            if not factura.xpath("FechaEmision"):
                return {'value': {'xml_supplier_approval': False},
                        'warning': {'title': 'Atención',
                                    'message': 'El archivo xml no contiene el nodo FechaEmision. Por favor cargue un '
                                               'archivo con el formato correcto.'}}

            if not factura.xpath("Emisor/Identificacion/Numero"):
                return {'value': {'xml_supplier_approval': False},
                        'warning': {'title': 'Atención',
                                    'message': 'El archivo xml no contiene el nodo Emisor. Por favor '
                                               'cargue un archivo con el formato correcto.'}}

            if not factura.xpath("ResumenFactura/TotalComprobante"):
                return {'value': {'xml_supplier_approval': False},
                        'warning': {'title': 'Atención',
                                    'message': 'No se puede localizar el nodo TotalComprobante. Por favor cargue '
                                               'un archivo con el formato correcto.'}}
            values = {}

            # self.invoice_line_ids.unlink()
            def buscador(factura):
                def x(path):
                    result = factura.xpath(path)
                    if len(result):
                        return result
                    return False

                return x

            buscar = buscador(factura)
            partner = buscar("Emisor/Identificacion/Numero")
            if partner:
                r = self.env['res.partner'].search([('ref', '=', partner[0].text)], limit=1)
                if r:
                    values.update({'partner_id': r.id})
            clave = buscar("Clave")
            if clave:
                values.update({'ref': clave[0].text[21:41]})
            MedioPago = buscar("MedioPago")
            if MedioPago:
                r = self.env['payment.methods'].search([('sequence', '=', MedioPago[0].text)], limit=1)
                if r:
                    values.update({'payment_methods_id': r.id})
            CodigoTipoMoneda = buscar("ResumenFactura/CodigoTipoMoneda/CodigoMoneda")
            if CodigoTipoMoneda:
                r = self.env['res.currency'].search([('name', '=', CodigoTipoMoneda[0].text)], limit=1)
                if r:
                    values.update({'currency_id': r.id})
                else:
                    values.update({'currency_id': self.env.company.currency_id.id})
            FechaEmision = buscar("FechaEmision")
            if FechaEmision:
                r = parseFecha(FechaEmision[0].text)
                values.update({'invoice_date': r, 'date': r,'date_issuance': FechaEmision[0].text})

                PlazoCredito = buscar("PlazoCredito")
                if PlazoCredito:
                    PlazoCredito = PlazoCredito[0].text
                    days = 0
                    if PlazoCredito:
                        if len(PlazoCredito) > 1:
                            if str(PlazoCredito[:2]).isdigit():
                                days = int(PlazoCredito[:2])
                            elif str(PlazoCredito[:1]).isdigit():
                                days = int(PlazoCredito[:1])
                    fecha_vencimiento = r + datetime.timedelta(days=days)
                    values.update({'invoice_date_due': fecha_vencimiento})

            amount_tax = buscar("ResumenFactura/TotalImpuesto")
            if not amount_tax:
                amount_tax = 0
            else:
                amount_tax = float(amount_tax[0].text)
                
            economic_activityp = buscar("CodigoActividad")
            if economic_activityp:
                economic_activityp = economic_activityp[0].text
                idactividadp = self.env['economic.activity'].search([('code', '=', str(economic_activityp))])

                if idactividadp:
                    idactividadp = idactividadp.id

            values.update({
                'economic_activity_id': idactividadp or False,
                # 'activity_type': 2,
                'amount_tax_electronic_invoice': amount_tax,
                'amount_total_electronic_invoice': float(buscar("ResumenFactura/TotalComprobante")[0].text),
                'xml_vat': str(partner[0].text)
            })

            detalleFactura = buscar("DetalleServicio/LineaDetalle")
            # values['invoice_line_ids'] = []
            # account_id = self.journal_id.default_account_id.id
            product_env = self.env['product.product']
            tax_env = self.env['account.tax']
            new_lines = self.env['account.move.line']
            if not self.invoice_line_ids:
                if detalleFactura:
                    for l in detalleFactura:
                        linea = {
                            # 'account_id': account_id
                        }
                        buscador2 = buscador(l)
                        Producto = buscador2("Codigo")
                        if Producto:
                            r = product_env.search([('name', '=', Producto[0].text)], limit=1)
                            if r:
                                linea.update({'product_id': r.id})
                        linea.update({'name': buscador2("Detalle")[0].text})
                        cantidad = float(buscador2("Cantidad")[0].text)
                        precio_unitario = float(buscador2("PrecioUnitario")[0].text)
                        subtotal = cantidad * precio_unitario
                        linea.update({
                            'quantity': cantidad,
                            'price_unit': precio_unitario,
                            'price_subtotal': subtotal,
                            'recompute_tax_line': True,
                            'automatic_created': True,
                            # 'display_type': True
                        })
                        Impuesto = buscador2("Impuesto")
                        # if Impuesto:
                        #     tax = tax_env.search(
                        #         [('amount', '=', float(Impuesto[0].xpath('Tarifa')[0].text)),
                        #          ('type_tax_use', '=', 'purchase')], limit=1)
                        #     if tax:
                        #         linea.update({'tax_ids': [(4, tax.id)]})

                        if Impuesto:
                            tax_list = []
                            for impu in Impuesto:
                                tax_code = impu.xpath('Codigo')[0].text
                                # Si el tax_code es 01 se refiere al iva
                                if tax_code == '01':
                                    tax = tax_env.search([('tax_code', '=', impu.xpath('Codigo')[0].text),
                                                          ('iva_tax_code', '=', impu.xpath('CodigoTarifa')[0].text),
                                                          ('type_tax_use', '=', 'purchase')], limit=1, order='id asc' )
                                    tax_list.append(tax.id)
                                # tax = tax_env.search([('tax_code', '=', int(impu.xpath('tax_code')[0].text))])
                                if tax_code == '02':
                                    tax = tax_env.search(
                                        [('tax_code', '=', impu.xpath('Codigo')[0].text),
                                         ('amount', '=', impu.xpath('Tarifa')[0].text),
                                         ('type_tax_use', '=', 'purchase'),
                                         ],limit=1, order='id asc')
                                    tax_list.append(tax.id)
                            linea.update({'tax_ids': [(6, 0, tax_list)]})

                        linea['move_id'] = self.id
                        new_line = new_lines.new(linea)
                        new_line._onchange_price_subtotal()
                        new_lines += new_line
                        # values['invoice_line_ids'].append((0, 0, linea))

            otrosCargos = buscar("OtrosCargos") or []
            if not self.invoice_line_ids:
                for l in otrosCargos:
                    linea = {
                        # 'account_id': account_id
                    }
                    buscador2 = buscador(l)
                    Producto = buscador2("Detalle")

                    r = product_env.search([('name', '=', Producto[0].text)], limit=1)
                    if r:
                        linea.update({'product_id': r.id})
                    linea.update({'name': Producto[0].text})

                    cantidad = 1
                    precio_unitario = float(buscador2("MontoCargo")[0].text)
                    subtotal = cantidad * precio_unitario
                    linea.update({
                        'quantity': cantidad,
                        'price_unit': precio_unitario,
                        'price_subtotal': subtotal,
                        'recompute_tax_line': True,
                        'move_id': self.id,
                        'automatic_created': True
                    })

                    # values['invoice_line_ids'].append((0, 0, linea))
                    new_line = new_lines.new(linea)
                    new_line._onchange_price_subtotal()
                    new_lines += new_line

            new_lines._onchange_mark_recompute_taxes()
            values.update({'xml_supplier_approval': self.xml_supplier_approval,
                           'fname_xml_supplier_approval': self.fname_xml_supplier_approval})
            self.update(values)
            self._onchange_currency()

        else:
            self.state_tributacion = False
            self.xml_supplier_approval = False
            self.fname_xml_supplier_approval = False
            self.xml_respuesta_tributacion = False
            self.fname_xml_respuesta_tributacion = False
            self.date_issuance = False
            self.number_electronic = False
            self.state_invoice_partner = False

    def search_clave_xml(self):
        xml_decoded = base64.b64decode(self.xml_supplier_approval)
        try:
            parser = etree.XMLParser(ns_clean=True, recover=True, encoding='utf-8')
            factura = etree.fromstring((re.sub(' xmlns=(\'|\")([a-zA-Z]|\:|\/|\.|\d|\-)*(\'|\")', '',xml_decoded.decode('utf-8'), count=1)).encode('utf_8'), parser)
        except Exception as e:
            _logger.info(
                'E-INV CR - This XML file is not XML-compliant.  Exception %s' % e)
            return {'status': 400,
                    'text': 'Excepción de conversión de XML'}

        # namespaces = factura.nsmap
        # inv_xmlns = namespaces.pop(None)
        # namespaces['inv'] = inv_xmlns

        def buscador(factura):
            def x(path):
                result = factura.xpath(path)
                if len(result):
                    return result
                return False

            return x

        buscar = buscador(factura)
        clave = buscar("Clave")
        if clave:
            return clave[0].text
        raise UserError('El xml no contiene clave')

class reference_document_line(models.Model):
    _name = 'reference.document.line'
    external_document_id = fields.Many2one('external.document', string='Documento Externo')
    invoice_id = fields.Many2one('account.move', string='Documento')
    move_id = fields.Many2one('account.move', string='Factura')
    reference_code_id = fields.Many2one('reference.code', string='Razón')
    reference_reason = fields.Char('Detalle')
    type_reference_document_id = fields.Many2one('type.reference.document', string='Tipo de documento')

class external_document(models.Model):
    _name = 'external.document'
    date_invoice = fields.Datetime('Date de emisión')
    date_issuance = fields.Char('Date de emisión formato')
    electronic_voucher = fields.Binary('Comprobante electrónico')
    fname_electronic_voucher = fields.Char('Nombre de archivo')
    is_electronic_invoice = fields.Boolean('Factura Electrónica')
    is_validate = fields.Boolean('Validada')
    number_electronic = fields.Char('Número electrónico')

class type_reference_document(models.Model):
    _name = 'type.reference.document'

    active = fields.Boolean('Activo')
    code = fields.Char('Código')
    name = fields.Char('Nombre')
    notes = fields.Text('Nota')




