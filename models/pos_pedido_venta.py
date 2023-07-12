#-*- coding: utf-8 -*-
from odoo import models, api, fields, _
import logging
import warnings
from odoo.osv.expression import AND

_logger = logging.getLogger(__name__)

class PosPedidoVentas(models.Model):
    _inherit = 'pos.order'
    _description = 'Pedidos de Ventas en UDS'

    amount_paid = fields.Float(string='Paid', states={'draft': [('readonly', False)]}, readonly=True, digits=0, required=True)
    total_usd = fields.Float(compute='_rateusd_total', string='Total USD', store=True)
    currency_rate_order = fields.Float(compute='_rate_usd', string='Currency rate', store=True, digits=(6,10))


    @api.depends('amount_paid', 'currency_id')
    def _rateusd_total(self):
        for order in self:
            order.total_usd = order.currency_id._convert(order.amount_paid, self.env.ref("base.USD"), order.company_id, order.date_order)


    @api.depends('total_usd')
    def _rate_usd(self):
        for order in self:
            if order.amount_paid > 0 and order.total_usd > 0:
                order.currency_rate_order = 1 / (order.amount_paid / order.total_usd)


class PosOrderReport(models.Model):
    _inherit = "report.pos.order"

    currency_rate = fields.Float(string='Currency Rate', digits=(16,10),  readonly=True)
    price_usd = fields.Float(string='Total USD', readonly=True)
    price_subtotal_incl = fields.Float(string='Total (Impuesto incluido)', readonly=True)
    price_total_incl_usd = fields.Float(string='Total USD (Impuesto incluido)', readonly=True)

    def _select(self):
        return """
            SELECT
                MIN(l.id) AS id,
                COUNT(*) AS nbr_lines,
                s.date_order AS date,
                SUM(l.qty) AS product_qty,
                SUM(l.qty * l.price_unit) AS price_sub_total,
                SUM(ROUND((l.qty * l.price_unit) * (100 - l.discount) / 100, cu.decimal_places)) AS price_total,
                SUM(ROUND(price_subtotal_incl, cu.decimal_places)) AS price_subtotal_incl,
                SUM((l.qty * l.price_unit) * (l.discount / 100) / CASE COALESCE(s.currency_rate_order, 0) WHEN 0 THEN 1.0 ELSE s.currency_rate_order END) AS total_discount,
                SUM(ROUND(l.qty * l.price_unit * CASE COALESCE(s.currency_rate_order, 0) WHEN 0 THEN 1.0 ELSE s.currency_rate_order END,  cu.decimal_places)) AS price_usd,
                SUM(ROUND(price_subtotal_incl * CASE COALESCE(s.currency_rate_order, 0) WHEN 0 THEN 1.0 ELSE s.currency_rate_order END,  cu.decimal_places)) AS price_total_incl_usd,
                CASE
                    WHEN SUM(l.qty * u.factor) = 0 THEN NULL
                    ELSE (SUM(l.qty*l.price_unit / CASE COALESCE(s.currency_rate_order, 0) WHEN 0 THEN 1.0 ELSE s.currency_rate_order END)/SUM(l.qty * u.factor))::decimal
                END AS average_price,
                SUM(cast(to_char(date_trunc('day',s.date_order) - date_trunc('day',s.create_date),'DD') AS INT)) AS delay_validation,
                s.id as order_id,
                s.partner_id AS partner_id,
                s.state AS state,
                s.user_id AS user_id,
                s.location_id AS location_id,
                s.company_id AS company_id,
                s.sale_journal AS journal_id,
                l.product_id AS product_id,
                pt.categ_id AS product_categ_id,
                p.product_tmpl_id,
                ps.config_id,
                pt.pos_categ_id,
                s.pricelist_id,
                s.session_id,
                s.account_move IS NOT NULL AS invoiced,
                s.currency_rate_order AS currency_rate
        """

    def _group_by(self):
        return """
               GROUP BY
                   s.id, s.date_order, s.partner_id,s.state, pt.categ_id,
                   s.user_id, s.location_id, s.company_id, s.sale_journal,
                   s.pricelist_id, s.account_move, s.create_date, s.session_id, s.currency_rate_order,
                   l.product_id,
                   pt.categ_id, pt.pos_categ_id,
                   p.product_tmpl_id,
                   ps.config_id
           """


class ReportSaleDetails(models.AbstractModel):
    _inherit = 'report.point_of_sale.report_saledetails'

    @api.model
    def get_sale_details(self, date_start=False, date_stop=False, config_ids=False, session_ids=False):
        """ Serialise the orders of the requested time period, configs and sessions.

        :param date_start: The dateTime to start, default today 00:00:00.
        :type date_start: str.
        :param date_stop: The dateTime to stop, default date_start + 23:59:59.
        :type date_stop: str.
        :param config_ids: Pos Config id's to include.
        :type config_ids: list of numbers.
        :param session_ids: Pos Config id's to include.
        :type session_ids: list of numbers.

        :returns: dict -- Serialised sales.
        """
        domain = [('state', 'in', ['paid', 'invoiced', 'done'])]

        if (session_ids):
            domain = AND([domain, [('session_id', 'in', session_ids)]])
        else:
            if date_start:
                date_start = fields.Datetime.from_string(date_start)
            else:
                # start by default today 00:00:00
                user_tz = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')
                today = user_tz.localize(fields.Datetime.from_string(fields.Date.context_today(self)))
                date_start = today.astimezone(pytz.timezone('UTC'))

            if date_stop:
                date_stop = fields.Datetime.from_string(date_stop)
                # avoid a date_stop smaller than date_start
                if (date_stop < date_start):
                    date_stop = date_start + timedelta(days=1, seconds=-1)
            else:
                # stop by default today 23:59:59
                date_stop = date_start + timedelta(days=1, seconds=-1)

            domain = AND([domain,
                          [('date_order', '>=', fields.Datetime.to_string(date_start)),
                           ('date_order', '<=', fields.Datetime.to_string(date_stop))]
                          ])

            if config_ids:
                domain = AND([domain, [('config_id', 'in', config_ids)]])

        orders = self.env['pos.order'].search(domain)

        user_currency = self.env.company.currency_id

        total = 0.0
        total_currency = 0.0
        products_sold = {}
        taxes = {}
        for order in orders:
            if user_currency != order.pricelist_id.currency_id:
                total += order.pricelist_id.currency_id._convert(
                    order.amount_total, user_currency, order.company_id, order.date_order or fields.Date.today())
            else:
                total += order.amount_total
            total_currency += order.total_usd
            currency = order.session_id.currency_id

            for line in order.lines:
                key = (line.product_id, line.price_unit, line.discount, line.order_id.currency_rate_order)
                products_sold.setdefault(key, 0.0)
                products_sold[key] += line.qty
                if line.tax_ids_after_fiscal_position:
                    line_taxes = line.tax_ids_after_fiscal_position.compute_all(
                        line.price_unit * (1 - (line.discount or 0.0) / 100.0), currency, line.qty,
                        product=line.product_id, partner=line.order_id.partner_id or False)
                    for tax in line_taxes['taxes']:
                        taxes.setdefault(tax['id'], {'name': tax['name'], 'tax_amount': 0.0, 'base_amount': 0.0})
                        taxes[tax['id']]['tax_amount'] += tax['amount']
                        taxes[tax['id']]['base_amount'] += tax['base']
                else:
                    taxes.setdefault(0, {'name': _('No Taxes'), 'tax_amount': 0.0, 'base_amount': 0.0})
                    taxes[0]['base_amount'] += line.price_subtotal_incl

        payment_ids = self.env["pos.payment"].search([('pos_order_id', 'in', orders.ids)]).ids
        if payment_ids:
            self.env.cr.execute("""
                    SELECT method.name, sum(amount) total, sum(CASE WHEN account_currency > 0 THEN account_currency ELSE po.total_usd  END) as amount_currency
                    FROM pos_payment AS payment,
                         pos_payment_method AS method,
                         pos_order AS po
                    WHERE payment.payment_method_id = method.id AND payment.pos_order_id = po.id
                        AND payment.id IN %s
                    GROUP BY method.name
                """, (tuple(payment_ids),))
            payments = self.env.cr.dictfetchall()
        else:
            payments = []

        return {
            'user_currency': user_currency,
            'currency_rate': user_currency.decimal_places,
            'currency_precision': user_currency.decimal_places,
            'total_paid': user_currency.round(total),
            'total_currency_paid': user_currency.round(total_currency),
            'payments': payments,
            'company_name': self.env.company.name,
            'taxes': list(taxes.values()),
            'products': sorted([{
                'product_id': product.id,
                'product_name': product.name,
                'code': product.default_code,
                'quantity': qty,
                'price_unit': price_unit,
                'discount': discount,
                'uom': product.uom_id.name,
                'currency_rate': rate,
                'price_usd': price_unit * rate
            } for (product, price_unit, discount, rate), qty in products_sold.items()], key=lambda l: l['product_name'])
        }

    def _get_report_values(self, docids, data=None):
        data = dict(data or {})
        configs = self.env['pos.config'].browse(data['config_ids'])
        data.update(self.get_sale_details(data['date_start'], data['date_stop'], configs.ids))
        return data