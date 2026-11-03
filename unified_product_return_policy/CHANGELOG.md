# Changelog - Unified Return Management

## 20.0.1.0.0 (Version 1.0) - Initial Public Release

First public release for Odoo 20.0. Free, LGPL-3.

* **Return policy per product, category or company:** return period in days (0 days =
  non-returnable), set per company, with the company time zone and the calculation basis (actual
  validation date or effective date); the policy in force is frozen at the sale and never applied
  retroactively.
* **Sales and Inventory returns:** return requests from the sales order; the period and the
  returnable quantity are checked when a customer return is created, when a quantity is entered and
  when it is validated.
* **POS refund controls:** refunds on the original receipt; expired, non-returnable and standalone
  refunds need a POS Administrator approval (POS Manager approval PIN or administrator account),
  single-use and bound to the refund; each refund line is checked against its original receipt line;
  temporary lockout after repeated wrong approval attempts; Ship Later deadlines per delivery.
* **Customer Portal return requests:** customers request returns online; Inventory executes them
  from the original delivery by selecting the request.
* **Manager approvals and exceptions:** Sales Administrator approval for sales exceptions, override
  at validation by the administrator of the sales channel, always with a mandatory reason.
* **Return quantity protection:** a return never exceeds what is still returnable across Inventory
  returns, POS refunds and portal requests; duplicate returns are refused.
* **Multi-company support:** settings and periods per company; every record stays in its own
  company.
* **Audit trail and policy history:** policy changes, approvals (who, how, why, quantities used) and
  deadline recalculations are recorded.
