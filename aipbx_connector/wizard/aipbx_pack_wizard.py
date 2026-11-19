from odoo import fields, models


class AipbxPackWizard(models.TransientModel):
    _name = "aipbx.pack.wizard"
    _description = "Paste the AiPBX connection pack"

    connection_id = fields.Many2one("aipbx.connection", required=True)
    pack = fields.Text("Connection pack", required=True,
                       help="The JSON the PBX showed once under Integration > Connections.")

    def action_apply(self):
        self.ensure_one()
        connection = self.connection_id
        connection._apply_pack(self.pack)
        connection.action_sync_extensions()
        self.unlink()
        return {"type": "ir.actions.act_window_close"}
