# -*- coding: utf-8 -*-
from odoo import models, fields, api, _


class UoM(models.Model):
    _inherit = "uom.uom"
    code = fields.Char(string="Code", required=False, )

# measure_type no existe en v15
class UoMCategory(models.Model):
    _inherit = "uom.category"
    # measure_type = fields.Selection(selection=[('area', 'Area'),
    #                                         ('services', 'Services'),
    #                                         ('rent', 'Rent'), ], )
