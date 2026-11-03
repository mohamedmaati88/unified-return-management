# User guide - Unified Return Management

## 1. Return policy

* **Priority:** product, then its direct category, then the company default. Values are per company.
* **Non-returnable:** a period of 0 days means the product cannot be returned in any channel without
  an approved exception.
* **Deadline:** delivery day = day 0; the right ends at the end of local day N (company time zone).
* **Calculation basis:** *Actual Validation Date* (server time of the delivery validation) or
  *Effective Date* (follows later corrections of the delivery date; each recalculation is logged in
  *Inventory → Configuration → Return Deadline Changes*).
* **Frozen at the sale:** the policy in force at the sale is stored with the sale (sales orders) or
  the receipt line (POS). Later changes of the product, category or company values never apply to
  past sales.
* **History:** *Inventory → Configuration → Return Policy History* records every change of a policy
  value. A daily scheduled action detects values changed outside the application and records them.

## 2. Return quantities (every channel)

A customer return can never exceed what is still returnable for a delivery line:
delivered − returned − other open returns − open portal quantity not yet held by a return.

* Checked when the return is created, when a quantity is entered and when it is validated, for every
  user including administrators.
* *Return* on a fully returned delivery is refused; *Return All* is capped at the remaining quantity.
  Return lines show a *Still Returnable* column.
* Purchase returns and other transfers are not concerned.

**Return drafted within the period.** The quantity entered on a return line while the delivery is
still within its period is recorded with the time it was entered. That return keeps its right after
the deadline, up to that quantity, even if the goods arrive later.

**Enter return quantities on the return lines.** Type the quantity in the *Quantity* column of the
return lines (*Operations* tab). A quantity typed only in *Detailed Operations* is not recorded as an
in-period return and is not checked when it is entered. The validation still checks the period and
the quantity, so nothing is returned beyond the rules, but a return validated after the deadline
then needs an approval.

**Only returns from the original delivery are covered.** A receipt created by hand from the
customer location, not linked to the delivery (*Return* button), is a standard receipt: the policy
cannot check it. Restrict such receipts in your procedures and use *Return* on the delivery.

## 3. Sales and Inventory returns

* **Within the period:** the standard *Return* of the delivery works as usual, within the quantity
  rules above.
* **Return Request** button on the sales order (*Sales → Orders → Return Requests*): choose the lines,
  quantities and a reason.
  * Lines within the period need no approval: *Create Return* creates the standard Return Transfer at
    once.
  * Lines whose period ended, or non-returnable lines, are submitted (*Submit for Approval*) to a
    **Sales Administrator**, who approves or rejects. The Return Transfer is created on approval.
  * Approved quantities are used once. Requested, approved, used and remaining quantities stay
    visible on the request. If an open Return Transfer was cancelled before the goods arrived,
    *Create Missing Returns* creates it again, never above what was approved.
  * Cancelling a request withdraws its open Return Transfers. A request whose approval has been fully
    used can no longer be cancelled.
* **Approval and execution are separate:** an approval never grants stock rights. The Return
  Transfer is validated by an **Inventory User** when the goods arrive.
* **Override at validation:** a return blocked at validation shows the reason (*Return Policy*
  dialog). Only an administrator of the original sales channel can override it, with a mandatory
  reason (*Override return period*); others can return the eligible quantities only.
* **Sales order changes:** a confirmed line can be lowered down to the delivered quantity, as in
  standard Odoo; delivered quantities and their policy never change.

## 4. Customer Portal return requests

1. The customer opens the delivered sales order in the portal and requests a return, within the
   portal period (*Allow Spontaneous Returns*), capped by the product's return policy.
2. The request is **not** a stock movement and does **not** create a Return Transfer. It appears on
   the sales order, tab *Portal Return Requests*.
3. When the goods arrive, an Inventory user creates the standard Return from the **original
   delivery** and, on each return line that executes the request, selects it in the **Portal
   Request** column (optional column; nothing is selected automatically). Only the open requests of
   that delivery line are offered.
4. At validation only the selected request is consumed, with the quantity really received; the rest
   stays open (a backorder keeps the link). A return without a selected request consumes no request.
5. A request submitted within the period keeps its right after the deadline, for the return lines
   linked to it only, up to its open quantity.
6. *Cancel remaining* cancels what is not held by an open linked return. It never releases a
   quantity an open return is executing: validate or cancel that return first.

## 5. Point of Sale

* **POS Direct (paid and delivered at once):** governed quantities are returned only with *Refund*
  on the original receipt, so money and stock stay one operation. An Inventory return of these
  quantities is refused.
* **Within the period:** refunds go through as usual.
* **Exceptions:** expired or non-returnable refunds, and **standalone refunds** (no original receipt),
  need a **POS Administrator** approval with a reason, given on the spot:
  * with the **approval PIN** of a POS Manager employee of that point of sale, or
  * with a POS Administrator's Odoo login and password (and TOTP code when enabled).

  The approval is single-use, valid 10 minutes, bound to the session, the refund order and its
  quantities, and checked again when the refund is paid.
* **Lockout:** 5 wrong approval PINs within 15 minutes on a point of sale lock PIN approvals there for
  15 minutes from the fifth failure. Wrong manager passwords lock that account for the password path.
  Every refusal gets the same answer, so the POS never tells whether a password was right.
* **Offline:** refunds under the policy need the server; the POS does not take them offline.
* **Ship Later:** each delivery has its own deadline.
  * A POS refund of a ship-later receipt line asks the server first. When the period of its delivery
    ended or the product is non-returnable, the same POS Administrator approval opens; a refund the
    server refuses (above the delivered quantity, an undelivered quantity, or already returned
    through Inventory) is stopped before the approval.
  * Returns can also be made from Inventory within the period; exceptions are requested with
    *Request Return Exception* on the POS order and approved in *Point of Sale → Orders → Return
    Exceptions*.
  * Goods returned through Inventory are refunded with a credit note from Accounting, never again
    from the POS. Never refund the same quantity in both.

## 6. Procedure URP-OPS-01: undelivered quantity of a POS Ship Later order

The module blocks the POS refund of an undelivered quantity after a partial delivery (standard Odoo
would book a stock return while the backorder still ships). Cancelling the pending delivery does not
make the POS refund safe. Instead:

1. POS: refund only the delivered quantity, if the customer returns it.
2. Inventory: reduce or cancel the pending delivery of the order.
3. POS backend: invoice the POS order if it is not invoiced yet (customer required).
4. Accounting: create a credit note for the undelivered quantity only, then confirm it.
5. Accounting: register the refund payment.

Validate this procedure with your accountant and your stock valuation settings before use.

## 7. Approvals and audit

* *Sales → Reporting → Return Overrides & Exceptions* and *Point of Sale → Reporting → Return
  Overrides & Exceptions*: every approval, who approved, how the identity was verified (logged-in
  user, account re-authentication or approval PIN), the reason and the quantities used. No secret is
  stored.
* *Inventory → Configuration → Return Period Overrides*: approvals and overrides seen from Inventory.
* Visibility: POS users see POS approvals; salesmen see the Sales approvals of the orders they may
  read; Inventory users see all approvals of their company.

## 8. Multi-company

Settings and product/category periods are per company. Every record of the module (requests,
approvals, logs, authentication attempts) is visible only in its own company.
