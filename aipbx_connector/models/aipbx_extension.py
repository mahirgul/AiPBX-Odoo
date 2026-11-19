from odoo import fields, models


class AipbxExtension(models.Model):
    _name = "aipbx.extension"
    _description = "AiPBX extension"
    _order = "extension"
    _rec_name = "extension"

    connection_id = fields.Many2one("aipbx.connection", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", related="connection_id.company_id", store=True, index=True)
    extension = fields.Char(required=True, index=True)
    name = fields.Char("Name on the PBX")
    email = fields.Char("E-mail on the PBX")
    user_id = fields.Many2one("res.users", "Odoo user", index=True,
                              help="Calls offered to this extension pop up for this user; their click-to-call rings it.")

    _ext_unique = models.Constraint("unique(connection_id, extension)", "This extension is already listed.")
