from odoo import fields, models,api


class EmployeeTermination(models.Model):
    _name = 'employee.termination'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'Employee Termination Form'


    # name = fields.Char(string="Employee",required=True)
    email_from = fields.Char(string="From", required=True)

    @api.model
    def default_get(self, fields):
        defaults = super(EmployeeTermination, self).default_get(fields)

        user = self.env.user
        defaults['email_from'] = user.partner_id.email if user.partner_id else ''

        return defaults

    email_to = fields.Many2one('res.partner',string="To",required=True)
    inv_letter = fields.Binary(string="Invitation Letter")
    file_name = fields.Char()
    state = fields.Selection([
        ('req', 'Request'),
        ('app_1', 'Approves by CEO'),
        ('app_2', 'Sign requested'),
        ('app_3', 'Signed'),
        ('done', 'Terminated'),
    ],default='req', string='State')
    related_emp_con_id = fields.Many2one('hr.employee', string="Employee", domain="[('id', '=', related_emp_con_id)]")
    user_id = fields.Many2one('res.users',default=lambda self:self.env.user)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company)


    def get_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        db_name = self.env.cr.dbname
        base_url += f'/web/login?db={db_name}#id=%d&view_type=form&model=%s' % (self.id, self._name)
        return base_url

    def send_mail_hr(self, ctx=None):

        template = self.env.ref('tf_eos_due_to_contract_termination.employee_termination_email')
        compose_form_id = self.env.ref('mail.email_compose_message_wizard_form').id

        attachment_data = {
            'name': 'Termination Letter',
            'datas': self.inv_letter,
            'res_model': self._name,
            'res_id': self.id,
        }

        attachment = self.env['ir.attachment'].create(attachment_data) if attachment_data.get('datas') else False

        context = {
            'default_model': 'employee.termination',
            'default_res_ids': [self.id],
            'default_partner_ids':[self.email_to.id],
            'default_emp_ter_id': self.id,
            'default_use_template': bool(template),
            'default_template_id': template.id,
            'default_composition_mode': 'comment',
            'default_attachment_ids': [(6, 0, [attachment.id])] if attachment else None,
            'force_email': True,
            'default_email_from':self.email_from,
            'default_partner_id_bool': True,
            'default_partner_id':self.related_emp_con_id.parent_id.user_partner_id.id
        }

        if ctx:
            context.update(ctx)

        return {
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'mail.compose.message',
            'views': [(compose_form_id, 'form')],
            'views_id': compose_form_id,
            'target': 'new',
            'context': context,
        }

    def approval_of_ceo(self):
        return self.send_mail_hr({'default_partner_id_bool': False})

    def send_mail_hr_to_emp(self):
        return self.send_mail_hr()

    def signed_mail_emp(self):
        return self.send_mail_hr()

    def action_send_termination(self):
        return self.send_mail_hr()








