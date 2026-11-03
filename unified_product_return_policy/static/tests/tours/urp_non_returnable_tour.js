import * as ProductScreen from "@point_of_sale/../tests/pos/tours/utils/product_screen_util";
import * as PaymentScreen from "@point_of_sale/../tests/pos/tours/utils/payment_screen_util";
import * as TicketScreen from "@point_of_sale/../tests/pos/tours/utils/ticket_screen_util";
import * as FeedbackScreen from "@point_of_sale/../tests/pos/tours/utils/feedback_screen_util";
import * as Dialog from "@point_of_sale/../tests/generic_helpers/dialog_util";
import * as Chrome from "@point_of_sale/../tests/pos/tours/utils/chrome_util";
import { registry } from "@web/core/registry";

const dlg = ".modal-dialog:has(.modal-title:contains('POS Administrator approval'))";

function pay() {
    return [
        ProductScreen.clickPayButton(),
        PaymentScreen.clickPaymentMethod("Bank", true, { remaining: "0.00" }),
        PaymentScreen.clickValidate(),
    ].flat();
}

// Sale of a product non-returnable when sold, then refund FROM THE RECEIPT: the cashier gets the
// POS Administrator approval dialog (products and quantities listed); the refund stays linked.
registry.category("web_tour.tours").add("UrpNonReturnableLinkedRefund", {
    steps: () =>
        [
            Chrome.startPoS(),
            Dialog.confirm("Open Register"),
            ProductScreen.clickDisplayedProduct("URP NoReturn"),
            ...pay(),
            FeedbackScreen.isShown(),
            FeedbackScreen.clickNextOrder(),
            ...ProductScreen.clickRefund(),
            TicketScreen.selectOrder("001"),
            ProductScreen.clickNumpad("1"),
            TicketScreen.toRefundTextContains("1"),
            TicketScreen.confirmRefund(),
            // the refund goes straight to the payment screen
            PaymentScreen.clickPaymentMethod("Bank", true, { remaining: "0.00" }),
            PaymentScreen.clickValidate(),
            { content: "approval asked, product non-returnable", trigger: `${dlg} p:contains('non-returnable')` },
            { content: "product and quantity listed", trigger: `${dlg} .urp-approval-lines li:contains('URP NoReturn: 1')` },
            { trigger: `${dlg} input[placeholder='Manager login']`, run: "edit pos_admin" },
            { trigger: `${dlg} input[placeholder='Password']`, run: "edit pos_admin" },
            { trigger: `${dlg} textarea`, run: "edit Defective, refund approved" },
            { trigger: `${dlg} .btn-primary:contains('Authorize'):enabled`, run: "click" },
            FeedbackScreen.isShown(),
        ].flat(),
});
