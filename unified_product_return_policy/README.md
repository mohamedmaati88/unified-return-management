# Unified Return Management (Odoo 20)

Technical name: `unified_product_return_policy` - version 20.0.1.0.0 - license LGPL-3 - free.

Return policy enforcement for Odoo 20: one return period, in days, per product, applied to Sales /
Inventory returns, the Customer Portal and the Point of Sale, checked on the server, with manager
approvals for exceptions and an audit trail.

* Installation and settings: [doc/INSTALLATION.md](doc/INSTALLATION.md)
* How each channel works: [doc/USER_GUIDE.md](doc/USER_GUIDE.md)
* Changes: [CHANGELOG.md](CHANGELOG.md)

## Features

* Return period per product, per category or per company (most specific wins); 0 days = non-returnable.
* Policy frozen at the sale: later changes are never retroactive.
* Return quantities never exceed what is still returnable, across Inventory returns, POS refunds and
  portal requests.
* Sales return requests from the sales order; exceptions (period ended, non-returnable) approved by
  a Sales Administrator with a mandatory reason.
* POS refunds: expired, non-returnable and standalone refunds need a POS Administrator approval (POS
  Manager approval PIN or administrator account), single-use and bound to the refund.
* Customer Portal return requests, executed by Inventory from the original delivery.
* Every policy change and approval is recorded; every record stays in its own company.

## Requirements

* Odoo 20. Tested on Odoo 20 Community and Odoo 20 Enterprise.
* Depends on `sale_stock`, `pos_stock` and `pos_hr` (installed automatically).
* No new security group: the standard Sales, Inventory and Point of Sale groups are used.

## Limits

* The policy applies to sales made after it was enabled for the company; earlier sales keep the
  standard Odoo return behaviour.
* Receipts created by hand from the customer location, not linked to the original delivery, are not
  covered: use *Return* on the delivery.
* Enter return quantities on the return lines, not only in *Detailed Operations* (see the user
  guide).
* POS refunds under the policy need a server connection: the POS does not take them offline.
* POS Ship Later after a partial delivery: the undelivered quantity is not refunded at the POS; it
  follows procedure URP-OPS-01 (see the user guide).
* Goods already returned through Inventory are refunded with a credit note from Accounting, never
  again from the POS.

## Tests

`--test-tags /unified_product_return_policy` (needs `auth_totp` for the TOTP tests and Chrome for the
browser tours).

## Author and license

Mohamed Maati <mohamedmaati325@gmail.com>. Licensed under the GNU Lesser General Public License v3
(see [LICENSE](LICENSE)).
