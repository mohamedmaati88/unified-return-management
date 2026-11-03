import * as ProductScreen from "@point_of_sale/../tests/pos/tours/utils/product_screen_util";
import * as PaymentScreen from "@point_of_sale/../tests/pos/tours/utils/payment_screen_util";
import * as Dialog from "@point_of_sale/../tests/generic_helpers/dialog_util";
import * as FeedbackScreen from "@point_of_sale/../tests/pos/tours/utils/feedback_screen_util";
import * as Chrome from "@point_of_sale/../tests/pos/tours/utils/chrome_util";
import * as Numpad from "@point_of_sale/../tests/generic_helpers/numpad_util";
import { inLeftSide } from "@point_of_sale/../tests/pos/tours/utils/common";
import { registry } from "@web/core/registry";

const dlg = ".modal-dialog:has(.modal-title:contains('POS Administrator approval'))";

function fill(login, password, reason) {
    return [
        { trigger: `${dlg} input[placeholder='Manager login']`, run: `edit ${login}` },
        { trigger: `${dlg} input[placeholder='Password']`, run: `edit ${password}` },
        { trigger: `${dlg} textarea`, run: `edit ${reason}` },
        { trigger: `${dlg} .btn-primary:contains('Authorize'):enabled`, run: "click" },
    ];
}

registry.category("web_tour.tours").add("UrpStandaloneRefundOverride", {
    steps: () =>
        [
            Chrome.startPoS(),
            Dialog.confirm("Open Register"),
            ProductScreen.clickDisplayedProduct("URP Gadget", true, "1", "10.00"),
            inLeftSide([...["+/-"].map(Numpad.click),
                ...ProductScreen.selectedOrderlineHasDirect("URP Gadget", "-1", "-10.00")]),
            ProductScreen.clickPayButton(),
            PaymentScreen.clickPaymentMethod("Bank", true, { remaining: "0.00" }),
            PaymentScreen.clickValidate(),
            // validateOrder patch opens the manager dialog; a wrong password shows an error
            fill("pos_admin", "wrong-password", "No receipt, checked by manager"),
            { trigger: `${dlg} .alert-danger:contains('Authentication failed')` },
            fill("pos_admin", "pos_admin", "No receipt, checked by manager"),
            // the token is stored on the order and validation continues automatically
            FeedbackScreen.isShown(),
        ].flat(),
});
