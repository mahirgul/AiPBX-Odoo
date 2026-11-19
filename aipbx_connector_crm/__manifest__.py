{
    "name": "AiPBX Connector - CRM",
    "version": "18.0.1.0.1",
    "category": "Sales/CRM",
    "summary": "Calls from the AiPBX phone system on CRM opportunities",
    "description": """
Adds the CRM to the AiPBX Connector: a call is linked to the contact's open opportunity (or to the
opportunity with the caller's number), written in its chatter with the AI summary and the
recording, offered in the caller pop-up, and an opportunity can be called with one click.
Installed by itself when both AiPBX Connector and CRM are installed.
""",
    "author": "AiPBX",
    "website": "https://github.com/mahirgul/AiPBX-Odoo",
    "license": "LGPL-3",
    "images": ["static/description/banner.png"],
    "depends": ["aipbx_connector", "crm"],
    "data": ["views/crm_lead_views.xml", "views/aipbx_call_views.xml"],
    "auto_install": True,
    "installable": True,
}
