{
    "name": "Unified Return Management",
    "version": "20.0.1.0.0",
    "summary": "Return policy enforcement: one return period per product, category or company, "
               "enforced on Sales, Inventory, POS and Customer Portal returns, with manager approvals",
    "category": "Sales/Sales",
    "author": "Mohamed Maati",
    "maintainer": "Mohamed Maati",
    "license": "LGPL-3",
    "depends": ["sale_stock", "pos_stock", "pos_hr"],
    "data": [
        "security/ir.access.csv",
        "data/ir_cron.xml",
        "views/res_config_settings_views.xml",
        "views/product_views.xml",
        "views/urp_policy_history_views.xml",
        "views/sale_order_views.xml",
        "views/urp_return_views.xml",
        "views/urp_portal_views.xml",
        "views/urp_return_exception_views.xml",
        "views/urp_approval_pin_views.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "unified_product_return_policy/static/src/portal/*.js",
        ],
        "web.assets_tests": [
            "unified_product_return_policy/static/tests/tours/**/*",
        ],
        "point_of_sale._assets_pos": [
            "unified_product_return_policy/static/src/pos/*",
        ],
    },
    "images": ["static/description/banner.png"],
    "post_init_hook": "post_init_hook",
    "installable": True,
}
