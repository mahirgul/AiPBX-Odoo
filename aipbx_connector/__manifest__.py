{
    "name": "AiPBX Connector",
    "version": "18.0.1.0.0",
    "category": "Productivity/VoIP",
    "summary": "Click-to-call, caller pop-up and call log for the AiPBX phone system",
    "description": """
Connects Odoo to an AiPBX phone system (https://aipbx.bid):

* Click-to-call from contacts: your own phone rings, then the number is dialled.
* Caller pop-up: when a call rings at your extension, Odoo shows who calls with a link to the contact.
* Call log: every call once, with direction, wait and talk time, outcome and the recording.
* Missed calls become a call-back activity.
* Optionally sends your contacts to the PBX address book, so desk phones show the caller's name.

Create a connection on the PBX (Integration > Connections, kind Odoo) and paste the
connection pack in AiPBX > Configuration > Connections.
""",
    "author": "AiPBX",
    "website": "https://github.com/mahirgul/AiPBX-Odoo",
    "license": "LGPL-3",
    "depends": ["base", "mail", "bus"],
    "external_dependencies": {"python": ["cryptography", "requests"]},
    "data": [
        "security/aipbx_security.xml",
        "security/ir.model.access.csv",
        "data/aipbx_cron.xml",
        "wizard/aipbx_pack_wizard_views.xml",
        "views/aipbx_connection_views.xml",
        "views/aipbx_call_views.xml",
        "views/res_partner_views.xml",
        "views/aipbx_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "aipbx_connector/static/src/js/aipbx_call_popup.js",
        ],
    },
    "images": ["static/description/banner.png"],
    "application": True,
    "installable": True,
}
