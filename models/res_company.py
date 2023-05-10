# -*- coding: utf-8 -*-

import logging
import phonenumbers

from odoo import models, fields, api
from odoo.addons.base.models.res_company import Company as OriginalCompany
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval

from . import api_facturae

_logger = logging.getLogger(__name__)

_TIPOS_CONFIRMACION = (
    # Provides listing of types of comprobante confirmations
    ('CCE_sequence_id', 'account.invoice.supplier.accept.',
     'Supplier invoice acceptance sequence'),
    ('CPCE_sequence_id', 'account.invoice.supplier.partial.',
     'Supplier invoice partial acceptance sequence'),
    ('RCE_sequence_id', 'account.invoice.supplier.reject.',
     'Supplier invoice rejection sequence'),
    ('FEC_sequence_id', 'account.invoice.supplier.reject.',
     'Supplier electronic purchase invoice sequence'),
    ('DGD_sequence_id', 'account.invoice.supplier.nodocument.',
     'Supplier dont generate document')
)


class CompanyElectronic(models.Model):
    _name = 'res.company'
    _inherit = ['res.company', 'mail.thread', ]

    skip_facturacion = fields.Boolean('NO usar FE', default=False)
    commercial_name = fields.Char(string="Commercial Name", required=False, )
    activity_id = fields.Many2one("economic.activity", string="Default economic activity", required=False, context={'active_test': False})
    economic_activities_ids = fields.Many2many(related='partner_id.economic_activities_ids')
    signature = fields.Binary(string="Llave Criptográfica", )
    identification_id = fields.Many2one("identification.type", string="Id Type", required=False)
    frm_ws_identificador = fields.Char(string="Electronic invoice user", required=False)
    frm_ws_password = fields.Char(string="Electronic invoice password", required=False)
    email_bool = fields.Boolean(string='Incluir correo de cliente en factura', default=True)
    contingencia = fields.Boolean(string='Contingencia')

    frm_ws_ambiente = fields.Selection(selection=[('disabled', 'Deshabilitado'), 
                                                  ('api-stag', 'Pruebas'),
                                                  ('api-prod', 'Producción')],
                                    string="Environment",
                                    required=True, 
                                    default='disabled',
                                    help='Es el ambiente en al cual se le está actualizando el certificado. Para el ambiente '
                                    'de calidad (stag), para el ambiente de producción (prod). Requerido.')

    frm_pin = fields.Char(string="Pin", 
                          required=False,
                          help='Es el pin correspondiente al certificado. Requerido')

    sucursal_MR = fields.Integer(string="Sucursal para secuencias de MRs", 
                                 required=False,
                                 default="1")

    terminal_MR = fields.Integer(string="Terminal para secuencias de MRs", 
                                 required=False,
                                 default="1")

    CCE_sequence_id = fields.Many2one(
        'ir.sequence',
        string='Secuencia Aceptación',
        help='Secuencia de confirmacion de aceptación de comprobante electrónico. Dejar en blanco '
        'y el sistema automaticamente se lo creará.',
        readonly=False, copy=False,
    )

    CPCE_sequence_id = fields.Many2one(
        'ir.sequence',
        string='Secuencia Parcial',
        help='Secuencia de confirmación de aceptación parcial de comprobante electrónico. Dejar '
        'en blanco y el sistema automáticamente se lo creará.',
        readonly=False, copy=False,
    )
    RCE_sequence_id = fields.Many2one(
        'ir.sequence',
        string='Secuencia Rechazo',
        help='Secuencia de confirmación de rechazo de comprobante electrónico. Dejar '
        'en blanco y el sistema automáticamente se lo creará.',
        readonly=False, copy=False,
    )
    FEC_sequence_id = fields.Many2one(
        'ir.sequence',
        string='Secuencia de Facturas Electrónicas de Compra',
        readonly=False, copy=False,
    )
    DGD_sequence_id = fields.Many2one(
        'ir.sequence',
        string='Secuencia Facturas que no crean documento',
        readonly=False, copy=False,
    )

    invoice_bank_accounts = fields.Text(string='Cuentas bancarias de factura')
    invoice_conditions = fields.Text(string='Condiciones de factura')

    def_partner_id = fields.Many2one('res.partner', string="Cliente por defecto para facturas")

    @api.onchange('mobile')
    def _onchange_mobile(self):
        if self.mobile:
            mobile = phonenumbers.parse(self.mobile, self.country_id.code)
            valid = phonenumbers.is_valid_number(mobile)
            if not valid:
                alert = {
                    'title': 'Atención',
                    'message': 'Número de teléfono inválido'
                }
                return {'value': {'mobile': ''}, 'warning': alert}

    @api.onchange('phone')
    def _onchange_phone(self):
        if self.phone:
            phone = phonenumbers.parse(self.phone, self.country_id.code)
            valid = phonenumbers.is_valid_number(phone)
            if not valid:
                alert = {
                    'title': 'Atención',
                    'message': _('Número de teléfono inválido')
                }
                return {'value': {'phone': ''}, 'warning': alert}

    @api.model
    def create(self, vals):
        if not vals.get('favicon'):
            vals['favicon'] = self._get_default_favicon()
        if not vals.get('name') or vals.get('partner_id'):
            self.clear_caches()
            return super(OriginalCompany, self).create(vals)
        partner = self.env['res.partner'].create({
            'name': vals['name'],
            'is_company': True,
            'image_1920': vals.get('logo'),
            'email': vals.get('email'),
            'phone': vals.get('phone'),
            'website': vals.get('website'),
            'vat': vals.get('vat'),
            'country_id': vals.get('country_id'),
            'identification_id': vals.get('identification_id'),
        })
        # compute stored fields, for example address dependent fields
        partner.flush()
        vals['partner_id'] = partner.id
        self.clear_caches()
        company = super(OriginalCompany, self).create(vals)
        # The write is made on the user to set it automatically in the multi company group.
        self.env.user.write({'company_ids': [(4, company.id)]})

        # Make sure that the selected currency is enabled
        if vals.get('currency_id'):
            currency = self.env['res.currency'].browse(vals['currency_id'])
            if not currency.active:
                currency.write({'active': True})
        return company


    OriginalCompany.create = create

    @api.model
    def create(self, vals):
        """ Try to automatically add the Comprobante Confirmation sequence to the company.
            It will attempt to create and assign before storing. The sequence that is
            created will be coded with the following syntax:
                account.invoice.supplier.<tipo>.<company_name>
            where tipo is: accept, partial or reject, and company_name is either the first word
            of the name or commercial name.
        """
        new_comp_id = super(CompanyElectronic, self).create(vals)
        #new_comp = self.browse(new_comp_id)
        new_comp_id.try_create_configuration_sequences()
        return new_comp_id #new_comp.id

    def try_create_configuration_sequences(self):
        """ Try to automatically add the Comprobante Confirmation sequence to the company.
            It will first check if sequence already exists before attempt to create. The s
            equence is coded with the following syntax:
                account.invoice.supplier.<tipo>.<company_name>
            where tipo is: accept, partial or reject, and company_name is either the first word
            of the name or commercial name.
        """
        company_subname = self.commercial_name
        if not company_subname:
            company_subname = getattr(self, 'name')
        company_subname = company_subname.split(' ')[0].lower()
        ir_sequence = self.env['ir.sequence']
        to_write = {}
        for field, seq_code, seq_name in _TIPOS_CONFIRMACION:
            if not getattr(self, field, None):
                seq_code += company_subname
                seq = self.env.ref(seq_code, raise_if_not_found=False) or ir_sequence.create({
                    'name': seq_name,
                    'code': seq_code,
                    'implementation': 'standard',
                    'padding': 10,
                    'use_date_range': False,
                    'company_id': getattr(self, 'id'),
                })
                to_write[field] = seq.id

        if to_write:
            self.write(to_write)

    def test_get_token(self):
        token_m_h = api_facturae.get_token_hacienda_company(
            self, self.frm_ws_ambiente)
        if token_m_h:
           _logger.info('E-INV CR - I got the token')
        return 

    def action_get_economic_activities(self):
        if self.vat:
            json_response = api_facturae.get_economic_activities(self)

            #self.env.cr.execute('update economic_activity set active=False')

            self.message_post(subject='Actividades Económicas',
                            body='Aviso!.\n Cargando actividades económicas desde Hacienda')

            if json_response["status"] == 200:
                activities = json_response["activities"]
                activities_codes = list()
                for activity in activities:
                    if activity["estado"] == "A":
                        activities_codes.append(activity["codigo"])

                economic_activities = self.env['economic.activity'].with_context(active_test=False)\
                    .search([('code', 'in', activities_codes)])

                if economic_activities:
                    economic_activities_ids = []
                    actividad_principal = economic_activities[0].id
                    for activity in economic_activities:
                        activity.active = True
                        economic_activities_ids.append((4, activity.id))

                    self.write(dict(
                        name=json_response["name"],
                        activity_id=actividad_principal,
                        economic_activities_ids=economic_activities_ids
                    ))
            else:
                alert = {
                    'title': json_response["status"],
                    'message': json_response["text"]
                }
                return {'value': {'vat': ''}, 'warning': alert}
        else:
            alert = {
                'title': 'Atención',
                'message': _('Company VAT is invalid')
            }
            return {'value': {'vat': ''}, 'warning': alert}

    def write(self, vals):
        if 'economic_activities_ids' in vals:
            for company in self:
                company.partner_id.economic_activities_ids=vals['economic_activities_ids']
        return super(CompanyElectronic, self).write(vals)