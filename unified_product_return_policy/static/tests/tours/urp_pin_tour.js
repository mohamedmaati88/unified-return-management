import * as ProductScreen from "@point_of_sale/../tests/pos/tours/utils/product_screen_util";
import * as PaymentScreen from "@point_of_sale/../tests/pos/tours/utils/payment_screen_util";
import * as TicketScreen from "@point_of_sale/../tests/pos/tours/utils/ticket_screen_util";
import * as FeedbackScreen from "@point_of_sale/../tests/pos/tours/utils/feedback_screen_util";
import * as Chrome from "@point_of_sale/../tests/pos/tours/utils/chrome_util";
import * as PosHr from "@pos_hr/../tests/tours/utils/pos_hr_helpers";
import * as Dialog from "@point_of_sale/../tests/generic_helpers/dialog_util";
import { negate } from "@point_of_sale/../tests/generic_helpers/utils";
import { registry } from "@web/core/registry";

const dlg = ".modal-dialog:has(.modal-title:contains('POS Administrator approval'))";

// Employee login (cashier), sale of a product non-returnable when sold, refund from the receipt:
// the POS Manager approves with the separate approval PIN and the reason - two fields only.
registry.category("web_tour.tours").add("UrpPinApprovalLinkedRefund", {
    steps: () =>
        [
            Chrome.clickBtn("Open Register"),
            PosHr.loginScreenIsShown(),
            PosHr.login("URP Cashier", "2580"),
            Dialog.confirm("Open Register"),
            ProductScreen.clickDisplayedProduct("URP NoReturn"),
            ProductScreen.clickPayButton(),
            PaymentScreen.clickPaymentMethod("Bank", true, { remaining: "0.00" }),
            PaymentScreen.clickValidate(),
            FeedbackScreen.isShown(),
            FeedbackScreen.clickNextOrder(),
            ...ProductScreen.clickRefund(),
            TicketScreen.selectOrder("001"),
            ProductScreen.clickNumpad("1"),
            TicketScreen.toRefundTextContains("1"),
            TicketScreen.confirmRefund(),
            PaymentScreen.clickPaymentMethod("Bank", true, { remaining: "0.00" }),
            PaymentScreen.clickValidate(),
            { content: "approval asked", trigger: `${dlg} p:contains('non-returnable')` },
            { content: "PIN field shown", trigger: `${dlg} input.urp-approval-pin` },
            { content: "no login field", trigger: negate(`${dlg} input[placeholder='Manager login']`) },
            { content: "no password field", trigger: negate(`${dlg} input[placeholder='Password']`) },
            { content: "no authenticator field", trigger: negate(`${dlg} input[placeholder*='Authenticator']`) },
            { content: "wrong PIN first", trigger: `${dlg} input.urp-approval-pin`, run: "edit 999999" },
            { trigger: `${dlg} textarea`, run: "edit Defective, approved by the manager" },
            { trigger: `${dlg} .btn-primary:contains('Authorize'):enabled`, run: "click" },
            { content: "wrong PIN refused", trigger: `${dlg} .alert-danger:contains('Wrong PIN')` },
            { trigger: `${dlg} input.urp-approval-pin`, run: "edit 482613" },
            { trigger: `${dlg} .btn-primary:contains('Authorize'):enabled`, run: "click" },
            FeedbackScreen.isShown(),
        ].flat(),
});
