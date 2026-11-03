import { patch } from "@web/core/utils/patch";
import { rpc } from "@web/core/network/rpc";
import { ReturnOrderDialog } from "@sale_stock/return_order_dialog/return_order_dialog";

/**
 * Unified Return Policy: the confirmation of the standard dialog now records the request
 * on the server (POST, idempotent) before the label is opened. No visual change.
 */
patch(ReturnOrderDialog.prototype, {
    _downloadReturnLabel(selectedLines) {
        if (!this.orderReturnData.urp_request_url) {
            return super._downloadReturnLabel(selectedLines);
        }
        if (!this._urpIdempotencyKey) {
            this._urpIdempotencyKey = crypto.randomUUID();
        }
        // open synchronously (user gesture) so that popup blockers do not stop the label tab
        const labelWindow = window.open("", "_blank");
        const returnDetails = Object.fromEntries(selectedLines.map((line) => [line.move_id, line.quantity]));
        return rpc(this.orderReturnData.urp_request_url, {
            access_token: this.props.accessToken,
            idempotency_key: this._urpIdempotencyKey,
            return_details: returnDetails,
            return_reason_id: this.state.returnReasonId,
        }).then((result) => {
            if (result.error) {
                if (labelWindow) {
                    labelWindow.close();
                }
                window.alert(result.error);
                window.location.reload();
                return;
            }
            if (labelWindow) {
                labelWindow.location = result.label_url;
            } else {
                window.location = result.label_url;
            }
        });
    },
});
