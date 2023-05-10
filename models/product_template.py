 # -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging
_logger = logging.getLogger(__name__)


class ProductElectronic(models.Model):
    _inherit = "product.template"

    @api.model
    def _default_code_type_id(self):
        code_type_id = self.env['code.type.product'].search(
            [('code', '=', '04')], limit=1)
        return code_type_id or False

    commercial_measurement = fields.Char(string="Commercial Unit", )
    code_type_id = fields.Many2one("code.type.product", string="Code Type", default=_default_code_type_id)

    tariff_head = fields.Char(string="Tax rate for exporting invoices", )

    cabys_code = fields.Char(string="Código CABYS", help='CAByS code from Ministerio de Hacienda')#VALIDAR QUE SEA DIGITO Y QUE TENGA 13

    economic_activity_id = fields.Many2one("economic.activity", string="Economic Activity", )

    non_tax_deductible = fields.Boolean(string='Indicates if this product is non-tax deductible', default=False, )


    @api.onchange('cabys_code')
    def _onchange_cabys_code_validate(self):
        if self.cabys_code:
            if not self.cabys_code.isdigit():
                raise UserError(_('El código Cabys debe ser únicamente dígitos'))
            else:
                if len(self.cabys_code) > 13 or len(self.cabys_code) < 13:
                    raise UserError(_('El código Cabys debe ser de 13 dígitos'))

class ProductCategory(models.Model):
    _inherit = "product.category"

    economic_activity_id = fields.Many2one("economic.activity", string="Actividad Económica", )

    cabys_code = fields.Char(string="Código CABYS", help='CAByS code from Ministerio de Hacienda')


    @api.onchange('cabys_code')
    def _onchange_cabys_code_validate(self):
        if self.cabys_code:
            if not self.cabys_code.isdigit():
                raise UserError(_('El código Cabys debe ser únicamente dígitos'))
            else:
                if len(self.cabys_code) > 13 or len(self.cabys_code) < 13:
                    raise UserError(_('El código Cabys debe ser de 13 dígitos'))

class product_product(models.Model):
    _inherit = 'product.product'
    cabys_code = fields.Char('Código Cabys')

    @api.onchange('cabys_code')
    def _onchange_cabys_code_validate(self):
        if self.cabys_code:
            if not self.cabys_code.isdigit():
                raise UserError(_('El código Cabys debe ser únicamente dígitos'))
            else:
                if len(self.cabys_code) > 13 or len(self.cabys_code) < 13:
                    raise UserError(_('El código Cabys debe ser de 13 dígitos'))