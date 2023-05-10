from enum import Enum


UrlHaciendaToken = {
    'api-stag' : 'https://idp.comprobanteselectronicos.go.cr/auth/realms/rut-stag/protocol/openid-connect/token',
    'api-prod' : 'https://idp.comprobanteselectronicos.go.cr/auth/realms/rut/protocol/openid-connect/token',
}

UrlHaciendaRecepcion = {
    'api-stag' : 'https://api-sandbox.comprobanteselectronicos.go.cr/recepcion/v1/recepcion/',
    #'api-stag' : 'https://api.comprobanteselectronicos.go.cr/recepcion-sandbox/v1/recepcion/',
    'api-prod' : 'https://api.comprobanteselectronicos.go.cr/recepcion/v1/recepcion/',
}

TipoCedula = {   # no se está usando !!
    'Fisico' : 'fisico',
    'Juridico' : 'juridico',
    'Dimex' : 'dimex',
    'Nite' : 'nite',
    'Extranjero' : 'extranjero',
}

SituacionComprobante = {
    'normal' : '1',
    'contingencia' : '2',
    'sininternet' : '3',
}

TipoDocumento = {
    'FE' : '01',  # Factura Electrónica
    'ND' : '02',  # Nota de Débito
    'NC' : '03',  # Nota de Crédito
    'TE' : '04',  # Tiquete Electrónico
    'CCE' : '05',  # confirmacion comprobante electronico
    'CPCE' : '06',  # confirmacion parcial comprobante electronico
    'RCE' : '07',  # rechazo comprobante electronico
    'FEC' : '08',  # Factura Electrónica de Compra
    'FEE' : '09',  # Factura Electrónica de Exportación
    'disabled': ''
}

# Xmlns used by Hacienda
XmlnsHacienda = {
    'FE': 'https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/facturaElectronica',  # Factura Electrónica
    'ND': 'https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/notaDebitoElectronica',  # Nota de Débito
    'NC': 'https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/notaCreditoElectronica',  # Nota de Crédito
    'TE': 'https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/tiqueteElectronico',  # Tiquete Electrónico
    'FEC': 'https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/facturaElectronicaCompra',  # Factura Electrónica de Compra
    'FEE': 'https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/facturaElectronicaExportacion',  # Factura Electrónica de Exportación
}

schemaLocation = {
    'FE': 'https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/2016/v4.3/FacturaElectronica_V4.3.xsd',  # Factura Electrónica
    'ND': 'https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/2016/v4.3/NotaDebitoElectronica_V4.3.xsd',  # Nota de Débito
    'NC': 'https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/2016/v4.3/NotaCreditoElectronica_V4.3.xsd',  # Nota de Crédito
    'TE': 'https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/2016/v4.3/TiqueteElectronico_V4.3.xsd',  # Tiquete Electrónico
    'FEC': 'https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/2016/v4.3/FacturaElectronicaCompra_V4.3.xsd',  # Factura Electrónica de Compra
    'FEE': 'https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/2016/v4.3/FacturaElectronicaExportacion_V4.3.xsd',  # Factura Electrónica de Exportación
}

tagName = {
    'FE': 'FacturaElectronica',  # Factura Electrónica
    'ND': 'NotaDebitoElectronica',  # Nota de Débito
    'NC': 'NotaCreditoElectronica',  # Nota de Crédito
    'TE': 'TiqueteElectronico',  # Tiquete Electrónico
    'FEC': 'FacturaElectronicaCompra',  # Factura Electrónica de Compra
    'FEE': 'FacturaElectronicaExportacion',  # Factura Electrónica de Exportación
}


#, ('Cm', 'Comisiones'), ('S', 'siemens'), ('T', 'tesla'), ('H', 'henry')
unidad_medida = [('Al', 'Alquiler de uso habitacional'), ('Alc', 'Alquiler de uso comercial'), ('Cm', 'Comisiones'),
                 ('I', 'Intereses'), ('Os', 'Otro tipo de servicios'), ('Sp', 'Servicios Profesionales'),
                 ('Spe', 'Servicios Personales'), ('St', 'Servicios Técnicos'), ('m', 'Metro'), ('kg', 'Kilogramo'),
                 ('s', 'Segundo'), ('A', 'Ampere'), ('K', 'Kelvin'), ('mol', 'Mol'), ('cd', 'Candela'),
                 ('m²', 'metro cuadrado'), ('m³', 'metro cúbico'), ('m/s', 'metro por segundo'),
                 ('m/s²', 'metro por segundo cuadrado'), ('1/m', '1 por metro'),
                 ('kg/m³', 'kilogramo por metro cúbico'), ('A/m²', 'ampere por metro cuadrado'),
                 ('A/m', 'ampere por metro'), ('mol/m³', 'mol por metro cúbico'),
                 ('cd/m²', 'candela por metro cuadrado'), ('1', 'uno (indice de refracción)'), ('rad', 'radián'),
                 ('sr', 'estereorradián'), ('Hz', 'hertz'), ('N', 'newton'), ('Pa', 'pascal'), ('J', 'Joule'),
                 ('W', 'Watt'), ('C', 'coulomb'), ('V', 'volt'), ('F', 'farad'), ('Ω', 'ohm'),
                 ('Wb', 'weber'), ('°C', 'grado Celsius'), ('lm', 'lumen'), ('cm', 'Centímetro'),
                 ('lx', 'lux'), ('Bq', 'Becquerel'), ('Gy', 'gray'), ('Sv', 'sievert'), ('kat', 'katal'),
                 ('Pa·s', 'pascal segundo'), ('N·m', 'newton metro'), ('N/m', 'newton por metro'),
                 ('rad/s', 'radián por segundo'), ('rad/s²', 'radián por segundo cuadrado'),
                 ('W/m²', 'watt por metro cuadrado'), ('J/K', 'joule por kelvin'),
                 ('J/(kg·K)', 'joule por kilogramo kelvin'), ('J/kg', 'joule por kilogramo'),
                 ('W/(m·K)', 'watt por metro kevin'), ('J/m³', 'joule por metro cúbico'), ('V/m', 'volt por metro'),
                 ('C/m³', 'coulomb por metro cúbico'), ('C/m²', 'coulomb por metro cuadrado'),
                 ('F/m', 'farad por metro'), ('H/m', 'henry por metro'), ('J/mol', 'joule por mol'),
                 ('J/(mol·K)', 'joule por mol kelvin'), ('C/kg', 'coulomb por kilogramo'), ('Gy/s', 'gray por segundo'),
                 ('W/sr', 'watt por estereorradián'), ('W/(m²·sr)', 'watt por metro cuadrado estereorradián'),
                 ('kat/m³', 'katal por metro cúbico'), ('min', 'minuto'), ('h', 'hora'), ('d', 'día'), ('º', 'grado'),
                 ('´', 'minuto'), ('´´', 'segundo'), ('L', 'litro'), ('t', 'tonelada'), ('Np', 'neper'), ('B', 'bel'),
                 ('eV', 'electronvolt'), ('u', 'unidad de masa atómica unificada'), ('ua', 'unidad astronómica'),
                 ('Unid', 'unidad'), ('Gal', 'galón'), ('g', 'gramo'), ('Km', 'kilometro'), ('Kw', 'kilovatios'),
                 ('ln', 'pulgada'), ('mL', 'mililitro'), ('mm', 'milimetro'), ('Oz', 'onzas')]

codigo_unidades = [u[0] for u in unidad_medida]