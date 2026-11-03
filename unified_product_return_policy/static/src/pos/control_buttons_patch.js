import { patch } from "@web/core/utils/patch";
import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { urpAskOverride, urpNeed } from "./pos_store_patch";

patch(ControlButtons.prototype, {
    get urpHasRefundLines() {
        return (this.pos.getOrder()?.lines || []).some((l) => l.qty < 0);
    },
    async clickUrpOverride() {
        const order = this.pos.getOrder();
        const needs = order.lines
            .filter((l) => l.qty < 0)
            .map((l) => urpNeed(l));
        await urpAskOverride(this.pos, order, needs);
    },
});
