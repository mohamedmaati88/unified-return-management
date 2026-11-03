import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("UrpPortalReturnRequest", {
    url: "/my",
    steps: () => [
        { trigger: ".o_portal_sale_sidebar .o_return_button", run: "click" },
        { trigger: ".o_return_order_dialog .js_product:contains('URP Portal Widget')" },
        { trigger: ".o_return_order_dialog .js_product button[name=sale_quantity_button_plus]", run: "click" },
        { trigger: ".o_return_order_dialog select[name='return_reason']", run: "selectByIndex 1" },
        { trigger: ".o_return_order_dialog button[name=continue]:enabled", run: "click" },
        {
            // capture the label tab instead of opening a real pop-up (a PDF cannot host the tour)
            trigger: ".modal-footer .btn-primary:contains('Download Return Label')",
            run() {
                window.open = () => ({
                    close() {},
                    set location(url) {
                        document.body.dataset.urpLabelUrl = url;
                    },
                });
            },
        },
        { trigger: ".modal-footer .btn-primary:contains('Download Return Label')", run: "click" },
        { trigger: "body[data-urp-label-url*='urp_request_id=']" },
    ],
});
