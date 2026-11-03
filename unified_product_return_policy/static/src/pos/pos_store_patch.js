import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";
import { UrpOverrideDialog } from "./urp_override_dialog";

function toDate(value) {
    if (!value) {
        return null;
    }
    if (value.toJSDate) {
        return value.toJSDate();
    }
    return new Date(String(value).replace(" ", "T") + "Z");
}

/**
 * Client-side pre-check only (the server re-checks everything):
 *  - expired linked refunds and standalone refunds need a manager override;
 *  - refunds of governed sales are blocked while offline (D8).
 */
export function urpNeed(line) {
    return {
        product_id: line.product_id.id,
        refunded_line_id: line.refunded_orderline_id?.id || false,
        qty: -line.qty,
        label: line.product_id.display_name,
    };
}

export function urpRefundNeeds(pos, order) {
    const needs = [];
    const nonReturnable = [];
    const expired = [];
    let governed = false;
    const now = new Date();
    for (const line of order.lines || []) {
        if (!(line.qty < 0)) {
            continue;
        }
        const orig = line.refunded_orderline_id;
        if (orig) {
            if (!orig.urp_has_snapshot) {
                continue;
            }
            governed = true;
            if (orig.urp_return_days === 0 && !orig.order_id?.shipping_date) {
                // non-returnable when sold: Non-Returnable Exception of a POS Administrator
                // (the server checks it again, line by line, on the historical policy)
                needs.push(urpNeed(line));
                nonReturnable.push(line.product_id.display_name);
                continue;
            }
            const deadline = toDate(orig.urp_deadline);
            if (deadline && now >= deadline) {
                needs.push(urpNeed(line));
                expired.push(line.product_id.display_name);
            }
        } else if (line.product_id?.type === "consu" && pos.company?.urp_enabled) {
            governed = true;
            needs.push(urpNeed(line));
        }
    }
    return { needs, governed, nonReturnable, expired };
}

/**
 * Ship-later refund lines: their deadlines are per delivery and unknown here, so the
 * server tells, read only, which ones need a manager (or refuses them). Advisory only: the payment
 * checks everything again. Returns false when the refund must stop here.
 */
export async function urpShipLaterNeeds(pos, order, needs, nonReturnable, expired) {
    const lines = (order.lines || []).filter(
        (line) => line.qty < 0 && line.refunded_orderline_id?.urp_has_snapshot && line.refunded_orderline_id.order_id?.shipping_date
    );
    if (!lines.length) {
        return true;
    }
    const labels = Object.fromEntries(lines.map((line) => [line.refunded_orderline_id.id, line.product_id.display_name]));
    let res;
    try {
        res = await pos.data.call("pos.session", "urp_refund_needs", [[pos.session.id], lines.map(urpNeed)]);
    } catch {
        // no answer, no refund: the cashier retries (the payment would refuse it without approval anyway)
        pos.dialog.add(AlertDialog, {
            title: _t("Return not possible now"),
            body: _t("The return policy of this ship-later refund could not be checked. Please retry."),
        });
        return false;
    }
    if (res.blocked) {
        pos.dialog.add(AlertDialog, { title: _t("Refund not allowed"), body: res.blocked });
        return false;
    }
    for (const n of res.needs) {
        const label = labels[n.refunded_line_id];
        needs.push({ product_id: n.product_id, refunded_line_id: n.refunded_line_id, qty: n.qty, label });
        (n.kind === "non_returnable" ? nonReturnable : expired).push(label);
    }
    return true;
}

export async function urpAskOverride(pos, order, needs, message) {
    const res = await makeAwaitable(pos.dialog, UrpOverrideDialog, {
        message: message || needs.map((n) => `${n.label}: ${n.qty}`).join("\n"),
        lines: needs.map((n) => ({ product_id: n.product_id, refunded_line_id: n.refunded_line_id, qty: n.qty })),
        labels: needs.map((n) => `${n.label}: ${n.qty}`),
        sessionId: pos.session.id,
        orderUuid: order.uuid,
        // employee login on this POS: approval by the POS Manager's approval PIN (server-checked)
        pinMode: !!pos.config?.module_pos_hr,
        call: (...args) => pos.data.call(...args),
    });
    if (res && res.token) {
        order.urp_override_token = res.token;
        return true;
    }
    return false;
}

patch(PosStore.prototype, {
    async validateOrder(args = {}) {
        const order = args.order || this.getOrder();
        if (order) {
            const { needs, governed, nonReturnable, expired } = urpRefundNeeds(this, order);
            if (governed && this.data.network.offline) {
                this.dialog.add(AlertDialog, {
                    title: _t("Return not possible offline"),
                    body: _t("Returns subject to the return policy need a connection to the server. Please retry when online."),
                });
                return false;
            }
            if (!order.urp_override_token && !(await urpShipLaterNeeds(this, order, needs, nonReturnable, expired))) {
                return false;
            }
            if (needs.length && !order.urp_override_token) {
                const parts = [];
                if (nonReturnable.length) {
                    parts.push(_t("This product is non-returnable. POS Administrator approval is required for an exceptional refund: %s", nonReturnable.join(", ")));
                }
                if (expired.length) {
                    parts.push(_t("The return period has ended: %s", expired.join(", ")));
                }
                if (needs.length > nonReturnable.length + expired.length) {
                    parts.push(_t("Refund without the original receipt."));
                }
                parts.push(_t("A POS Administrator must authorize it and enter the reason."));
                const ok = await urpAskOverride(this, order, needs, parts.join("\n"));
                if (!ok) {
                    return false;
                }
            }
        }
        return await super.validateOrder(args);
    },
});
