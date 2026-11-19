/** @odoo-module **/
// Who is calling: the PBX offered a call to this user's extension (aipbx.call._pop_up sends it on
// the user's own bus channel). One sticky notification per call, with the contact one click away.
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";

const shown = new Set();

export const aipbxCallPopupService = {
    dependencies: ["bus_service", "notification", "action"],
    start(env, { bus_service, notification, action }) {
        bus_service.subscribe("aipbx/call", (p) => {
            if (!p || shown.has(p.call_id)) {
                return; // several tabs or a reconnect: once per call
            }
            shown.add(p.call_id);
            const message = [p.name, p.number, p.queue].filter(Boolean).join(" · ");
            let close = () => {};
            const open = (act) => {
                action.doAction(act);
                close();
            };
            const buttons = p.lead_id
                ? [{
                    name: _t("Open opportunity"),
                    primary: true,
                    onClick: () => open({ type: "ir.actions.act_window", res_model: "crm.lead", res_id: p.lead_id, views: [[false, "form"]], target: "current" }),
                }]
                : [];
            buttons.push(...(p.partner_id
                ? [{
                    name: _t("Open contact"),
                    primary: !p.lead_id,
                    onClick: () => open({ type: "ir.actions.act_window", res_model: "res.partner", res_id: p.partner_id, views: [[false, "form"]], target: "current" }),
                }]
                : [{
                    name: _t("New contact"),
                    onClick: () => open({ type: "ir.actions.act_window", res_model: "res.partner", views: [[false, "form"]], target: "current", context: { default_phone: p.number } }),
                }]));
            close = notification.add(message || _t("Unknown number"), {
                title: _t("Incoming call"),
                type: "info",
                sticky: true,
                buttons,
            });
            setTimeout(() => close(), 60000);
        });
        if (bus_service.start) {
            bus_service.start();
        }
    },
};

registry.category("services").add("aipbx_call_popup", aipbxCallPopupService);
