import * as ProductScreen from "@point_of_sale/../tests/pos/tours/utils/product_screen_util";
import * as PaymentScreen from "@point_of_sale/../tests/pos/tours/utils/payment_screen_util";
import * as TicketScreen from "@point_of_sale/../tests/pos/tours/utils/ticket_screen_util";
import * as FeedbackScreen from "@point_of_sale/../tests/pos/tours/utils/feedback_screen_util";
import * as Dialog from "@point_of_sale/../tests/generic_helpers/dialog_util";
import * as Chrome from "@point_of_sale/../tests/pos/tours/utils/chrome_util";
import * as StockPaymentScreen from "@pos_stock/../tests/pos/tours/utils/payment_screen_util";
import { registry } from "@web/core/registry";

const { DateTime } = luxon;

const dlg = ".modal-dialog:has(.modal-title:contains('POS Administrator approval'))";

function payByBank() {
    return [
        PaymentScreen.clickPaymentMethod("Bank", true, { remaining: "0.00" }),
        PaymentScreen.clickValidate(),
    ].flat();
}

// The ticket screen shows the refund numpad only for a selected order that has lines, is paid or
// done and is listed under the "Paid" filter (point_of_sale ticket_screen showSubPads /
// isOrderSynced). Unlike Odoo's own refund tours, this order was sold in an earlier tour and is
// loaded from the server: if the numpad does not come, report what the screen and the POS hold.
function numpadOrFacts() {
    return {
        content: "refund numpad shown for the selected order (else: report why)",
        trigger: ".ticket-screen .order-row.highlight",
        run: async () => {
            for (let i = 0; i < 50 && !document.querySelector(".ticket-screen .numpad"); i++) {
                await new Promise((resolve) => setTimeout(resolve, 100));
            }
            if (document.querySelector(".ticket-screen .numpad")) {
                return;
            }
            const text = (el) => (el?.innerText || "").replace(/\s+/g, " ").slice(0, 200);
            const pos = window.posmodel || odoo.__WOWL_DEBUG__?.root?.env?.services?.pos;
            const orders = pos
                ? pos.models["pos.order"].getAll().map((o) => ({
                      name: o.name, ref: o.pos_reference, uuid: o.uuid, state: o.state, ship: o.shipping_date,
                      lines: (o.getOrderlines?.() || o.lines || []).length,
                  }))
                : "POS store not reachable";
            const facts = {
                rows: [...document.querySelectorAll(".ticket-screen .order-row")].map(text),
                highlighted: text(document.querySelector(".ticket-screen .order-row.highlight")),
                found: Object.fromEntries([".pads", ".subpads", ".numpad", ".orderline", ".pay-order-button"]
                    .map((s) => [s, document.querySelectorAll(`.ticket-screen ${s}`).length])),
                filter: text(document.querySelector(".pos-search-bar .filter")),
                orders,
            };
            throw new Error(`No refund numpad on the selected receipt: ${JSON.stringify(facts)}`);
        },
    };
}

// The ticket screen refreshes itself
// after it opens (onWillStart: pos.getServerOrders with pos.loadingOrderState; onMounted: a deferred
// onFilterSelected that reloads the server orders): a click made before that refresh is lost. Odoo's
// own tour on a server-loaded receipt (test_paid_order_with_archived_product_loads) also waits for the
// list before selecting. Wait for the refresh, then select.
function waitTicketScreenRefreshed() {
    return {
        content: "ticket screen finished loading the server orders",
        trigger: ".ticket-screen .order-row",
        run: async () => {
            const pos = window.posmodel || odoo.__WOWL_DEBUG__?.root?.env?.services?.pos;
            for (let i = 0; i < 100 && pos?.loadingOrderState; i++) {
                await new Promise((resolve) => setTimeout(resolve, 100));
            }
            await new Promise((resolve) => setTimeout(resolve, 1000));      // the deferred filter reload
        },
    };
}

function refundOneFromReceipt() {
    return [
        ...ProductScreen.clickRefund(),
        waitTicketScreenRefreshed(),
        TicketScreen.selectOrder("001"),
        numpadOrFacts(),
        ProductScreen.clickNumpad("1"),
        TicketScreen.toRefundTextContains("1"),
        TicketScreen.confirmRefund(),
        ...payByBank(),
    ];
}

// Sale of 3 units to a customer, delivered later (Ship Later).
registry.category("web_tour.tours").add("UrpShipLaterSale", {
    steps: () =>
        [
            Chrome.startPoS(),
            Dialog.confirm("Open Register"),
            ProductScreen.clickDisplayedProduct("URP ShipLater"),
            ProductScreen.clickDisplayedProduct("URP ShipLater"),
            ProductScreen.clickDisplayedProduct("URP ShipLater"),
            ProductScreen.clickPartnerButton(),
            ProductScreen.clickCustomer("URP SL Customer"),
            ProductScreen.clickPayButton(),
            // the date picker needs a real date (as Odoo's own test_pos_order_shipping_date tour):
            // confirmed empty, the order is not set to ship later
            StockPaymentScreen.setShipLaterDate(`${DateTime.now().year + 1}-05-30`),
            StockPaymentScreen.shippingLaterHighlighted(),
            ...payByBank(),
            FeedbackScreen.isShown(),
        ].flat(),
});

// 2 of the 3 units were delivered and their return period has ended: refunding 1 from the receipt
// opens the manager approval dialog (asked from the server), then the refund is paid.
registry.category("web_tour.tours").add("UrpShipLaterExpiredRefund", {
    steps: () =>
        [
            Chrome.startPoS(),
            ...refundOneFromReceipt(),
            { content: "approval asked, period ended", trigger: `${dlg} p:contains('The return period has ended')` },
            { content: "product and quantity listed", trigger: `${dlg} .urp-approval-lines li:contains('URP ShipLater: 1')` },
            { trigger: `${dlg} input[placeholder='Manager login']`, run: "edit pos_admin" },
            { trigger: `${dlg} input[placeholder='Password']`, run: "edit pos_admin" },
            { trigger: `${dlg} textarea`, run: "edit Late return accepted by the manager" },
            { trigger: `${dlg} .btn-primary:contains('Authorize'):enabled`, run: "click" },
            FeedbackScreen.isShown(),
        ].flat(),
});

// Same refund while the pre-check cannot be answered: the POS stops, nothing is paid.
registry.category("web_tour.tours").add("UrpShipLaterPrecheckDown", {
    steps: () =>
        [
            Chrome.startPoS(),
            {
                content: "the server does not answer the pre-check",
                trigger: "body",
                run: () => {
                    const pos = window.posmodel || odoo.__WOWL_DEBUG__?.root?.env?.services?.pos;
                    if (!pos) {
                        throw new Error("POS store not reachable from the tour");
                    }
                    const call = pos.data.call.bind(pos.data);
                    pos.data.call = (model, method, ...args) =>
                        method === "urp_refund_needs" ? Promise.reject(new Error("no answer")) : call(model, method, ...args);
                },
            },
            ...refundOneFromReceipt(),
            { content: "refund stopped", trigger: ".modal-dialog .modal-title:contains('Return not possible now')" },
            { content: "no approval dialog", trigger: `body:not(:has(${dlg}))` },
        ].flat(),
});
