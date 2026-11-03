import { Component, proxy, t, useProps } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";

/**
 * Approval of a refund outside the return policy. Everything is checked on the server, which
 * returns a one-time token bound to this session, order, lines and quantities.
 *  - "pin" mode (POS with employee login): the POS Manager types the separate approval PIN and
 *    the reason - two fields only. The browser sends no employee and no role: the server finds
 *    the manager from the PIN, among the POS Manager employees of this point of sale.
 *  - "account" mode: a POS Administrator's Odoo login, password (and authenticator code).
 */
export class UrpOverrideDialog extends Component {
    static template = "unified_product_return_policy.UrpOverrideDialog";
    static components = { Dialog };
    props = useProps({
        message: t.string().optional(""),
        lines: t.array(),
        labels: t.array().optional([]),
        sessionId: t.number(),
        orderUuid: t.string(),
        pinMode: t.boolean().optional(false),
        call: t.function(),
        getPayload: t.function(),
        close: t.function(),
    });

    setup() {
        this.state = proxy({
            mode: this.props.pinMode ? "pin" : "account",
            pin: "", login: "", password: "", totp: "", reason: "", error: "", busy: false,
        });
    }

    get canConfirm() {
        if (this.state.busy || !this.state.reason.trim()) {
            return false;
        }
        return this.state.mode === "pin" ? !!this.state.pin : !!(this.state.login && this.state.password);
    }

    switchMode(mode) {
        this.state.mode = mode;
        this.state.error = "";
    }

    async confirm() {
        this.state.busy = true;
        this.state.error = "";
        try {
            const args = this.state.mode === "pin"
                ? ["urp_request_override_pin",
                   [[this.props.sessionId], this.state.pin, this.state.reason, this.props.lines, this.props.orderUuid]]
                : ["urp_request_override",
                   [[this.props.sessionId], this.state.login, this.state.password, this.state.totp || null,
                    this.state.reason, this.props.lines, this.props.orderUuid]];
            const res = await this.props.call("pos.session", ...args);
            if (res && res.ok) {
                this.props.getPayload(res);
                this.props.close();
                return;
            }
            const labels = {
                locked: _t("Too many failed attempts. Try again in 15 minutes."),
                totp_required: _t("Enter the authenticator (TOTP) code."),
                not_pos_administrator: _t("This user is not a POS Administrator."),
                invalid_pin: _t("Wrong PIN: it is not the approval PIN of a POS Manager of this point of sale."),
                ambiguous_pin: _t("This approval PIN is not unique: ask a POS Administrator to reset it."),
                pin_unavailable: _t("Approval PINs are not available on this point of sale."),
                reason_required: _t("Enter the reason."),
                session_closed: _t("The session is closed."),
                no_order: _t("This order can no longer be authorized."),
            };
            this.state.error = labels[res && res.error] || _t("Authentication failed.");
        } finally {
            this.state.pin = "";
            this.state.password = "";
            this.state.totp = "";
            this.state.busy = false;
        }
    }
}
