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

class ProjectProjectPricelist(orm.Model):
    ''' Pricelist linked to project
    '''
    _name = 'account.analytic.account.pricelist'
    _description = 'Account pricelist'
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
        'account_id': fields.many2one('account.analytic.account', 'Account', 
            ondelete='cascade'),
        'note': fields.text('Note'),
        'pricelist': fields.float('Pricelist', digits=(16, 2), required=True), 
        }

class ProjectTaskWork(orm.Model):
    ''' Add event to timesheet, override CRUD operation for manage creation 
        from analytic timesheet
    '''
    _inherit = 'project.task.work'

    def create(self, cr, uid, vals, *args, **kwargs):
        if 'context' not in kwargs:
            kwargs['context'] = {}

        context = kwargs.get('context', {})
        kwargs['context']['no_analytic_entry'] = True # Never create!
        res_id = super(ProjectTaskWork, self).create(
            cr, uid, vals, *args, **kwargs)
        
        ts_id = kwargs['context'].get('hr_analytic_timesheet_id', False)
        if ts_id:
            self.write(cr, uid, res_id, {
                'hr_analytic_timesheet_id': ts_id,
                }, context=context)
        return res_id


class HrAnalyticTimesheet(orm.Model):
    ''' Add event to timesheet
    '''
    _inherit = 'hr.analytic.timesheet'
    
    def _get_to_factor_id(self, cr, uid, vals, context=None):
        ''' Find invoice all
        '''
        if vals.get('extra_product_id', False):
            factor = 100
        else:
            factor = 0

        if factor == 100:
            factor = 0
            name = 'Yes (100%)'
        else: # 0    
            return False # TODO test if works better!!!!
            factor = 100
            name = 'No (0%)'
            
        factor_pool = self.pool.get('hr_timesheet_invoice.factor')
        factor_ids = factor_pool.search(cr, uid, [('factor', '=', factor)],
            context=context)
        if factor_ids:
            return factor_ids[0]    
        else: # Create
            return factor_pool.create(cr, uid, {
                'factor': factor,
                'name': name,
                'customer_name': '%s%s' % (factor, '%'),
                }, context=context)
    
    # Override for check creation of project.task.work
    def create(self, cr, uid, vals, context=None):
        """ Create a new record for a model ClassName
            @param cr: cursor to database
            @param uid: id of current user
            @param vals: provides a data for new record
            @param context: context arguments, like lang, time zone
            
            @return: returns a id of new record
        """
        context = context or {}
        vals['to_invoice'] = self._get_to_factor_id(cr, uid, vals, 
            context=context)

        #['no_analytic_entry']
        res_id = super(HrAnalyticTimesheet, self).create(
            cr, uid, vals, context=context)

        # Create task work if there's task selected:
        task_id = vals.get('project_task_id', False)
        if task_id:
            context['hr_analytic_timesheet_id'] = res_id
            self.pool.get('project.task.work').create(cr, uid, {
                'task_id': task_id,
                'name': vals.get('name', False),
                'hours': vals.get('unit_amount', False),
                'date': vals.get('date', False),
                'user_id': vals.get('user_id', False),
                }, context=context)
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
        # Load if not present:
        ts_id = ids[0]
        item_proxy = self.browse(cr, uid, ts_id, context=context)
        
        # to invoice check:
        vals['extra_product_id'] = vals.get(
            'extra_product_id', 
            item_proxy.extra_product_id.id)
        vals['to_invoice'] = self._get_to_factor_id(cr, uid, vals, 
            context=context)
        
        res = super(HrAnalyticTimesheet, self).write(
            cr, uid, ids, vals, context=context)

        # Delete and recreate:
        work_pool = self.pool.get('project.task.work')            
        work_ids = work_pool.search(cr, uid, [
            ('hr_analytic_timesheet_id', '=', ts_id)], context=context)
        if work_ids:
            work_id = work_ids[0]
            context['no_analytic_entry'] = True
            task_id = vals.get('project_task_id', 
                item_proxy.project_task_id.id)
            cr.execute(
                'DELETE from project_task_work where id = %s' % work_id)

            context['hr_analytic_timesheet_id'] = ts_id
            self.pool.get('project.task.work').create(cr, uid, {
                'task_id': task_id,
                'name': vals.get('name', item_proxy.name),
                'hours': vals.get('unit_amount', item_proxy.unit_amount),
                'date': vals.get('date', item_proxy.date),
                'user_id': vals.get('user_id', item_proxy.user_id.id),
                }, context=context)
        return True

    def unlink(self, cr, uid, ids, context=None):
        """ Delete all record(s) from table heaving record id in ids
            return True on success, False otherwise 
            @param cr: cursor to database
            @param uid: id of current user
            @param ids: list of record ids to be removed from table
            @param context: context arguments, like lang, time zone
            
            @return: True on success, False otherwise
        """
        query = '''
            DELETE from project_task_work
            WHERE hr_analytic_timesheet_id in %s;           
            ''' % ('%s' % (list(ids))).replace('[', '(').replace(']', ')')    
        cr.execute(query)
        return super(HrAnalyticTimesheet, self).unlink(
            cr, uid, ids, context=context)
        
    # ----------
    # On Change:
    # ----------
    def on_change_account_id(self, cr, uid, ids, account_id, user_id, 
            context=None):    
        ''' Add domain filter for tasks
        '''
        res = super(HrAnalyticTimesheet, self).on_change_account_id(
            cr, uid, ids, account_id, user_id)
        
        if not account_id:
            return res

        # Search account_id:
        project_pool = self.pool.get('project.project')
        project_ids = project_pool.search(cr, uid, [
            ('analytic_account_id', '=', account_id)], context=context)
        if not project_ids:
            return res

        if 'domain' not in res:
            res['domain'] = {
                'project_task_id': [('project_id', '=', project_ids[0])]}

        return res
        
    def onchange_extra_product_id(self, cr, uid, ids, extra_product_id, 
            context=None):
        ''' Write price used for this product in project pricelist
        '''   
        res = {}
        if not extra_product_id:
            return res
        res['value'] = {}
        pl_pool = self.pool.get('account.analytic.account.pricelist')
        res['value']['extra_qty'] = pl_pool.browse(
            cr, uid, extra_product_id, context=context).pricelist        
        return res
    
class AccountAnalyticLine(orm.Model):
    ''' Add extra field for invoice in analytic line
    '''
    _inherit = 'account.analytic.line'
    
    # ---------------------------------------
    # Override function hr_timesheet_invoice:
    # ---------------------------------------
    # Override function for get unit price of product / employee
    def _get_invoice_price(self, cr, uid, account, product_id, user_id, qty, 
            context=None):
        context = context or {}
        pro_price_obj = self.pool.get('product.pricelist')
        pricelist = account.pricelist_id or \
            account.partner_id.property_product_pricelist
        if pricelist:
            pl = pricelist.id
            price = pro_price_obj.price_get(
                cr,uid,[pl], product_id, qty or 1.0, account.partner_id.id, 
                context=context)[pl]
        else:
            price = 0.0
        return price
    
    # Override function for calculate invoice line from analytic:
    def _prepare_cost_invoice_line(self, cr, uid, invoice_id, product_id, uom, 
            user_id, factor_id, account, analytic_lines, journal_type, data, 
            context=None):
        ''' Create one line in invoice from analytic
        '''
        # Pool used:
        product_obj = self.pool['product.product']
        
        uom_context = dict(context or {}, uom=uom)

        total_price = 0.0
        total_qty = 0.0
        for l in analytic_lines:
            if l.extra_product_id:
                total_price += l.extra_product_id.pricelist * l.extra_qty
                total_qty += l.extra_qty
            else:
                total_price += l.amount
                total_qty += l.unit_amount

        if data.get('product'):
            # force product, use its public price
            if isinstance(data['product'], (tuple, list)):
                product_id = data['product'][0]
            else:
                product_id = data['product']
            unit_price = self._get_invoice_price(
                cr, uid, account, product_id, user_id, total_qty, uom_context)
        elif journal_type == 'general' and product_id: 
            # timesheets, use sale price
            # NEW: Check extra product:
            if l.extra_product_id:
                unit_price = l.extra_product_id.pricelist                
            else:
                unit_price = self._get_invoice_price(
                    cr, uid, account, product_id, user_id, total_qty, 
                    uom_context)
        else:
            # expenses, using price from amount field
            unit_price = total_price * -1.0 / total_qty

        factor = self.pool['hr_timesheet_invoice.factor'].browse(
            cr, uid, factor_id, context=uom_context)
        factor_name = factor.customer_name
        curr_invoice_line = {
            'price_unit': unit_price,
            'quantity': total_qty,
            'product_id': product_id,
            'discount': factor.factor,
            'invoice_id': invoice_id,
            'name': factor_name,
            'uos_id': uom,
            'account_analytic_id': account.id,
            }

        if product_id:
            product = product_obj.browse(cr, uid, product_id, 
                context=uom_context)
            factor_name = product_obj.name_get(cr, uid, [product_id], 
                context=uom_context)[0][1]
            if factor.customer_name:
                factor_name += ' - ' + factor.customer_name

                general_account = product.property_account_income or \
                    product.categ_id.property_account_income_categ
                if not general_account:
                    raise osv.except_osv(
                        _('Error!'), 
                        _("Configuration Error!") + '\n' + _(
                            "Please define income account for product '%s'."
                            ) % product.name)
                taxes = product.taxes_id or general_account.tax_ids
                tax = self.pool['account.fiscal.position'].map_tax(
                    cr, uid, account.partner_id.property_account_position, 
                    taxes)
                curr_invoice_line.update({
                    'invoice_line_tax_id': [(6, 0, tax)],
                    'name': factor_name,
                    'invoice_line_tax_id': [(6, 0, tax)],
                    'account_id': general_account.id,
                })

            note = []
            for line in analytic_lines:
                # set invoice_line_note
                details = []
                if data.get('date', False):
                    details.append(line['date'])
                if data.get('time', False):
                    if line['product_uom_id']:
                        details.append("%s %s" % (
                            line.unit_amount, line.product_uom_id.name))
                    else:
                        details.append("%s" % (line['unit_amount'], ))
                if data.get('name', False):
                    details.append(line['name'])
                if details:
                    note.append(u' - '.join(
                        map(lambda x: unicode(x) or '', details)))
            if note:
                curr_invoice_line['name'] += "\n" + ("\n".join(map(
                    lambda x: unicode(x) or '', note)))
        return curr_invoice_line
    
    # Override function for calculate invoice from analytic:
    def invoice_cost_create(self, cr, uid, ids, data=None, context=None):
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
            # Jump account with recurrent invoice:
            if line.account_id.recurring_invoices:
                continue # Jump recurring invoice

            # NEW: Get pricelist from account or partner:
            if line.account_id.pricelist_id:
                pricelist_id = line.account_id.pricelist_id
                currency = pricelist_id.currency_id.id
            else:
                pricelist_id = \
                    line.analytic_partner_id.property_product_pricelist
                currency = pricelist_id.currency_id.id
            
            key = (line.account_id.id,
                   line.account_id.company_id.id,
                   currency)
            invoice_grouping.setdefault(key, []).append(line)

        for (key_id, company_id, currency_id
                ), analytic_lines in invoice_grouping.items():
            # key_id is an account.analytic.account
            
            # will be the same for every line
            partner = analytic_lines[0].analytic_partner_id
            partner = analytic_lines[0].analytic_partner_id or analytic_lines[
                0].account_id.partner_id  

            curr_invoice = self._prepare_cost_invoice(
                cr, uid, partner, company_id, currency_id, analytic_lines, 
                context=context)
            invoice_context = dict(
                context,
                lang=partner.lang,
                # set force_company in context so the correct product 
                #properties are selected (eg. income account)
                force_company=company_id,  
                # set company_id in context, so the correct default journal 
                # will be selected
                company_id=company_id,
                )  
            last_invoice = invoice_obj.create(
                cr, uid, curr_invoice, context=invoice_context)
            invoices.append(last_invoice)

            # use key (product, uom, user, invoiceable, analytic account,
            # journal type)
            # creates one invoice line per key
            invoice_lines_grouping = {}
            for analytic_line in analytic_lines:
                account = analytic_line.account_id
                # NEW: Pricelist exception:
                #pricelist_id = analytic_line.analytic_partner_id.pricelist_id
                if (not partner) or not (pricelist_id):
                    raise osv.except_osv(
                        _('Error!'), 
                        _('Contract incomplete. Please fill in the Customer '
                            'and Pricelist fields for %s.') % (account.name))

                if not analytic_line.to_invoice:
                    raise osv.except_osv(
                        _('Error!'), 
                        _('Trying to invoice non invoiceable line for %s.') % (
                            analytic_line.product_id.name))
                
                # Find product or extra product:            
                if analytic_line.extra_product_id:
                    focus_product = analytic_line.extra_product_id.product_id 
                else:
                    focus_product = analytic_line.product_id
                    
                key = (
                    focus_product.id,
                    focus_product.uom_id.id, # TODO was line.product_uom_id !!
                    analytic_line.user_id.id,
                    analytic_line.to_invoice.id,
                    analytic_line.account_id,
                    analytic_line.journal_id.type,
                    )
                invoice_lines_grouping.setdefault(key, []).append(
                    analytic_line)

            # finally creates the invoice line
            for (product_id, uom, user_id, factor_id, account, journal_type
                    ), lines_to_invoice in invoice_lines_grouping.items():
                curr_invoice_line = self._prepare_cost_invoice_line(
                    cr, uid, last_invoice, product_id, uom, user_id, factor_id, 
                    account, lines_to_invoice, journal_type, data, 
                    context=context)

                invoice_line_obj.create(cr, uid, curr_invoice_line, 
                    context=context)
            self.write(cr, uid, [l.id for l in analytic_lines], {
                'invoice_id': last_invoice}, context=context)
            invoice_obj.button_reset_taxes(cr, uid, [last_invoice], context)
        return invoices

    _columns = {
        'extra_product_id': fields.many2one(
            'account.analytic.account.pricelist', 
            'Performance', ondelete='set null'),
        'extra_qty': fields.float('Q.ty', digit=(16, 4)),
        'project_task_id': fields.many2one(
            'project.task', 'Task', ondelete='set null'),
        }

class AccountAnalyticAccount(orm.Model):
    ''' Add relation field to project / account analytic account
    '''
    _inherit = 'account.analytic.account'

    _columns = {
        'reference': fields.boolean('Reference pricelist'),
        'reference_account_id': fields.many2one(
            'account.analytic.account', 'Parent account',
            help='Reference account pricelist custom',
            domain=[('reference', '=', True)],
            ),

        'pricelist_ids': fields.one2many('account.analytic.account.pricelist', 
             'account_id', 'Pricelist'),
        #'project_id': fields.function(_get_project_account_id, 
        #    method=True, type='many2one', string='Project', 
        #    relation='project.project', store=True
        #    ),                         
        }
# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
