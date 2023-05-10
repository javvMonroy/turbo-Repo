# -*- coding: utf-8 -*-
from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    purchase_order = fields.Char(string='N° Orden de Compra')

    def _prepare_invoice(self):
        res = super(SaleOrder, self)._prepare_invoice()
        res['sale_purchase_order'] = self.purchase_order or False
        res['sale_create_user_id'] = self.create_uid and self.create_uid.id or False
        return res

