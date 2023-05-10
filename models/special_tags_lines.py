# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import UserError
import re

element_status = [('OtroTexto', 'OtroTexto'), ('OtroContenido', 'OtroContenido')]
status_comprobantes = [('aceptado', 'Aceptado'), ('rechazado', 'Rechazado'), ('no_encontrado', 'No encontrado'),
                       ('error', 'Error'), ('recibido', 'Recibido'), ('procesando', 'Procesando'),
                       ('enviado', 'Enviado'), ('enviado_error', 'Error al enviar'),
                       ('conexion_error', 'Fallo conexión'), ('esperando', 'Esperando envío'),
                       ('consultar_error', 'Error al consultar'), ('validating_signature', 'Validando firma')]
status_comprobantes_compra = [('1', 'Aceptado'), ('2', 'Aceptacion parcial'), ('3', 'Rechazado')]
status_send_options = [('no_answer', 'Enviar correo al validar factura y luego enviar correo de Respuesta Tributación.'),
                       ('answer', 'Enviar correo solamente cuando el Documento Electrónico esté Aceptada')]
status_company_fe = [('disabled', 'Deshabilitado'), ('stag', 'Pruebas'), ('prod', 'Producción')]


class fe_special_tags_invoice_line(models.Model):
    _name = 'fe.special.tags.invoice.line'
    code = fields.Char('Código Técnico')
    content = fields.Char('Contenido')
    content_label = fields.Char('Campo / Ayuda')
    element = fields.Selection(element_status, 'Elemento')
    invoice_id = fields.Many2one('account.move', string='Factura')
    python_code = fields.Text('Codigo Python')
    read_only = fields.Boolean('Solo lectura')
    read_only_content = fields.Boolean('Solo lectura (Contenido)')
    rel_id = fields.Integer('Id de special tags')
    required = fields.Boolean('Requerido')
    type_add = fields.Char('Tipo de agregado de linea')


class fe_special_tags_partner_line(models.Model):
    _name = 'fe.special.tags.partner.line'
    code = fields.Char('Código Técnico')
    content = fields.Char('Contenido')
    content_label = fields.Char('Nombre a mostrar / Ayuda')
    element = fields.Selection(element_status, 'Elemento')
    partner_id = fields.Many2one('res.partner', string='Contacto')
    python_code = fields.Text('Codigo Python')
    read_only = fields.Boolean('Solo lectura (Elemento y Código Técnico)')
    read_only_content = fields.Boolean('Solo lectura (Contenido)')
    required = fields.Boolean('Requerido (Contenido)')
    state = fields.Selection(element_status, 'Elemento')


class fe_special_tags_company_line(models.Model):
    _name = 'fe.special.tags.company.line'
    code = fields.Char('Código Técnico')
    company_id = fields.Many2one('res.company', string='Conpania')
    content = fields.Char('Contenido')
    content_label = fields.Char('Nombre a mostrar / Ayuda')
    element = fields.Selection(element_status, 'Elemento')
    python_code = fields.Text('Codigo Python')
    read_only = fields.Boolean('Solo lectura (Elemento y Código Técnico)')
    read_only_content = fields.Boolean('Solo lectura (Contenido)')
    required = fields.Boolean('Requerido (Contenido)')
    state = fields.Selection(element_status, 'Elemento')


class ResPartnerSpecialTags(models.Model):
    _inherit = 'res.partner'

    special_tags_lines = fields.One2many('fe.special.tags.partner.line', 'partner_id',
                                         string='Líneas de etiquetas adicionales XML')

    @api.onchange('special_tags_lines')
    def _onchange_content(self):
        for rec in self:
            if rec.special_tags_lines.content and not re.match("^(\w| |-|_)*$", rec.special_tags_lines.content):
                raise UserError("El ingreso de caracteres especiales no se encuentra permitido")


class ResCompanySpecialTags(models.Model):
    _inherit = 'res.company'
    special_tags_lines = fields.One2many('fe.special.tags.company.line', 'company_id',
                                         string='Líneas de etiquetas adicionales XML')



class AccountMoveSpecialTagsLines(models.Model):
    """Campos especiales en clientes, special_tags_lines"""
    _inherit = 'account.move'

    special_tags_lines = fields.One2many('fe.special.tags.invoice.line', 'invoice_id',
                                         string='Líneas de etiquetas adicionales XML')

    @staticmethod
    def special_tags_lines_function(self, retuurn):
        self.special_tags_lines = False
        for line in self.partner_id.special_tags_lines:
            vals = {
                'code': line.code,
                'content': line.content,
                'content_label': line.content_label,
                'element': line.element,
                'invoice_id': self.id,
                'python_code': line.python_code,
                'read_only': line.read_only,
                'read_only_content': line.read_only_content,
                'required': line.required,
            }
            self.env['fe.special.tags.invoice.line'].create(vals)
        return self if retuurn else True

    @api.onchange('partner_id')
    def compute_special_tags_lines(self):
        self.special_tags_lines_function(self, 0)

    @api.model
    def create(self, vals):
        res = super(AccountMoveSpecialTagsLines, self).create(vals)
        if res.partner_id:
            res = self.special_tags_lines_function(res, 1)
        return res
