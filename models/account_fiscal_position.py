# -*- coding: utf-8 -*-
from odoo import models, fields, api, _

class AccountFiscalPosition(models.Model):

    _inherit = 'account.fiscal.position'

    check_tarjeta = fields.Boolean('Tarjeta')

