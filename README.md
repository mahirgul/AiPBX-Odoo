# AiPBX Connector for Odoo

Connects Odoo to an [AiPBX](https://github.com/mahirgul/AiPBX) phone system: click-to-call,
a caller pop-up for the user whose phone rings, one call record per call with the recording,
missed calls as call-back activities, and the PBX's AI call summary on the contact. Optionally sends Odoo contacts to the PBX address
book so desk phones show the caller's name.

| Branch | Odoo |
|---|---|
| `17.0` | 17 |
| `18.0` | 18 |
| `19.0` | 19 |

## Modules

- `aipbx_connector`: everything above.
- `aipbx_connector_crm`: installs by itself when the CRM app is there. A call is linked to the
  contact's only open opportunity (or the only open one with the caller's number; never guessed
  between two), written in its chatter with the AI summary and the recording, offered in the
  pop-up ("Open opportunity"), and opportunities get a Call button.

## Install

1. Put `aipbx_connector` in your addons path (or install it from the Odoo App Store:
   [AiPBX Connector](https://apps.odoo.com/apps/modules/19.0/aipbx_connector),
   [AiPBX Connector - CRM](https://apps.odoo.com/apps/modules/19.0/aipbx_connector_crm)) and install
   **AiPBX Connector**.
2. Optional but recommended: set `aipbx_secret_key = <a long random string>` in the Odoo
   configuration file. The PBX key and webhook secret are encrypted with it; without it the
   database secret is used.
3. Odoo: **AiPBX → Configuration → Connections → New**, save.
4. PBX: **Integration → Connections → New connection**, kind **Odoo**, the address of this Odoo.
   Copy the connection pack it shows.
5. Odoo: **Paste connection pack**. The extensions come in; users are matched by e-mail, assign the
   rest in the Extensions tab.

The PBX must reach Odoo over HTTPS at `/aipbx/webhook/<connection id>` (shown on the connection).

## How it works

- The PBX sends signed webhooks (`X-AiPBX-Signature-256`, HMAC-SHA256 over `timestamp.body`,
  five minutes of tolerance). Each is stored once per connection and event id; a repeat is
  answered 200 and not processed again; a failed one is retried every five minutes.
- `call.offered` pops up only for the Odoo user of the extension that rings (their own bus
  channel). `call.completed` writes one chatter line on the contact; `recording.ready` adds the
  link. Recordings stay on the PBX and are streamed through Odoo for users allowed to see the call.
- Click-to-call uses the PBX REST API with the connection's key, from the server; the extension is
  the one assigned to the signed-in user.

## Tests

`./run_tests.sh 17` (or 18, 19) installs the module in a fresh database of the official
`odoo:<version>` Docker image and runs its tests.

## License

LGPL-3.
