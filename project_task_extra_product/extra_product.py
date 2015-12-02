# -*- coding: utf-8 -*-
###############################################################################
#
#    Copyright (C) 2001-2014 Micronaet SRL (<http://www.micronaet.it>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
import os
import sys
import logging
import openerp
import openerp.netsvc as netsvc
import openerp.addons.decimal_precision as dp
from openerp.osv import fields, osv, expression, orm
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from openerp import SUPERUSER_ID, api
from openerp import tools
from openerp.tools.translate import _
from openerp.tools.float_utils import float_round as round
from openerp.tools import (DEFAULT_SERVER_DATE_FORMAT, 
    DEFAULT_SERVER_DATETIME_FORMAT, 
    DATETIME_FORMATS_MAP, 
    float_compare)


_logger = logging.getLogger(__name__)

class AccountAnalyticLine(orm.Model):
    ''' Add extra field for invoice in analytic line
    '''
    _inherit = 'account.analytic.line'
    
    # Override original function:
    def invoice_cost_create(self, cr, uid, ids, data=None, context=None):
        import pdb; pdb.set_trace()
        invoice_obj = self.pool.get('account.invoice')
        invoice_line_obj = self.pool.get('account.invoice.line')
        invoices = []
        if context is None:
            context = {}
        if data is None:
            data = {}

        # use key (partner/account, company, currency)
        # creates one invoice per key
        invoice_grouping = {}

        currency_id = False
        # prepare for iteration on journal and accounts
        for line in self.browse(cr, uid, ids, context=context):

            key = (line.account_id.id,
                   line.account_id.company_id.id,
                   line.account_id.pricelist_id.currency_id.id)
            invoice_grouping.setdefault(key, []).append(line)

        for (key_id, company_id, currency_id), analytic_lines in invoice_grouping.items():
            # key_id is an account.analytic.account
            partner = analytic_lines[0].account_id.partner_id  # will be the same for every line

            curr_invoice = self._prepare_cost_invoice(cr, uid, partner, company_id, currency_id, analytic_lines, context=context)
            invoice_context = dict(context,
                                   lang=partner.lang,
                                   force_company=company_id,  # set force_company in context so the correct product properties are selected (eg. income account)
                                   company_id=company_id)  # set company_id in context, so the correct default journal will be selected
            last_invoice = invoice_obj.create(cr, uid, curr_invoice, context=invoice_context)
            invoices.append(last_invoice)

            # use key (product, uom, user, invoiceable, analytic account, journal type)
            # creates one invoice line per key
            invoice_lines_grouping = {}
            for analytic_line in analytic_lines:
                account = analytic_line.account_id
                if (not partner) or not (account.pricelist_id):
                    raise osv.except_osv(_('Error!'), _('Contract incomplete. Please fill in the Customer and Pricelist fields for %s.') % (account.name))

                if not analytic_line.to_invoice:
                    raise osv.except_osv(_('Error!'), _('Trying to invoice non invoiceable line for %s.') % (analytic_line.product_id.name))

                key = (analytic_line.product_id.id,
                       analytic_line.product_uom_id.id,
                       analytic_line.user_id.id,
                       analytic_line.to_invoice.id,
                       analytic_line.account_id,
                       analytic_line.journal_id.type)
                invoice_lines_grouping.setdefault(key, []).append(analytic_line)

            # finally creates the invoice line
            for (product_id, uom, user_id, factor_id, account, journal_type), lines_to_invoice in invoice_lines_grouping.items():
                curr_invoice_line = self._prepare_cost_invoice_line(cr, uid, last_invoice,
                    product_id, uom, user_id, factor_id, account, lines_to_invoice,
                    journal_type, data, context=context)

                invoice_line_obj.create(cr, uid, curr_invoice_line, context=context)
            self.write(cr, uid, [l.id for l in analytic_lines], {'invoice_id': last_invoice}, context=context)
            invoice_obj.button_reset_taxes(cr, uid, [last_invoice], context)
        return invoices
    
    _columns = {
        'extra_product_id': fields.many2one('project.project.pricelist', 
            'Performance', ondelete='set null'),
        'extra_qty': fields.integer('Q.ty'),
        }

class ProjectProjectPricelist(orm.Model):
    ''' Pricelist linked to project
    '''
    _name = 'project.project.pricelist'
    _description = 'Project pricelist'
    _rec_name = 'product_id'
    
    # Onchange:
    def onchange_product_id(self, cr, uid, ids, product_id, context=None):
        ''' On change product write pricelist 
        '''
        res = {}
        res['value'] = {}
        if not product_id:
           res['value']['pricelist'] = False
           return res

        product_pool = self.pool.get('product.product')
        product_proxy = product_pool.browse(
            cr, uid, product_id, context=context)
        res['value']['pricelist'] = product_proxy.lst_price
        return res
    
    # -------------------------------------------------------------------------
    #                           Postgres table:
    # -------------------------------------------------------------------------
    _columns = {
        'product_id': fields.many2one('product.product', 'Product', 
            required=True, ondelete='set null', 
            domain=[('type', '=', 'service')]),
        'project_id': fields.many2one('project.project', 'Project', 
            ondelete='cascade'),
        'note': fields.text('Note'),
        'pricelist': fields.float('Pricelist', digits=(16, 2), required=True), 
        }
        
class ProjectProject(orm.Model):
    ''' Add relation field to project
    '''
    _inherit = 'project.project'
    
    _columns = {
        'pricelist_ids': fields.one2many('project.project.pricelist', 
             'project_id', 'Pricelist'),             
        }

class ProjectTaskWork(osv.osv):
    ''' Add pricelist operation
    '''    
    _inherit = 'project.task.work'
    
    def _update_extra_product_analytic(self, cr, uid, ids, context=None):
        ''' Search and update line extra parameters
        '''
        if type(ids) not in (list, tuple):
            ids = [ids]
            
        for work in self.browse(cr, uid, ids, context=context):
            self.pool.get(
            'hr.analytic.timesheet').write(
                cr, uid, work.hr_analytic_timesheet_id.id, {
                    'extra_product_id': work.extra_product_id.id,
                    'extra_qty': work.extra_qty,
                    }, context=context)

    # Override function:
    def create(self, cr, uid, vals, context=None):
        """ Create a new record for a model ClassName
            @param cr: cursor to database
            @param uid: id of current user
            @param vals: provides a data for new record
            @param context: context arguments, like lang, time zone
            
            @return: returns a id of new record
        """
        context = context or {}
        res_id = super(ProjectTaskWork, self).create(
            cr, uid, vals, context=context)
        self._update_extra_product_analytic(cr, uid, [res_id], context=context)    
        return res_id

    def write(self, cr, uid, ids, vals, context=None):
        """ Update redord(s) comes in {ids}, with new value comes as {vals}
            return True on success, False otherwise
            @param cr: cursor to database
            @param uid: id of current user
            @param ids: list of record ids to be update
            @param vals: dict of new values to be set
            @param context: context arguments, like lang, time zone
            
            @return: True on success, False otherwise
        """
        context = context or {}        
        res = super(ProjectTaskWork, self).write(
            cr, uid, ids, vals, context=context)
        self._update_extra_product_analytic(cr, uid, ids, context=context)    
        return res

    _columns = {
        'extra_product_id': fields.many2one('project.project.pricelist', 
            'Performance', ondelete='set null'),
        'extra_qty': fields.integer('Q.ty'),
        }

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
