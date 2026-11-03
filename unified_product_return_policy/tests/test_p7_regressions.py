"""Regression tests of core return-policy behaviour."""
import re
from datetime import timedelta

from freezegun import freeze_time
from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests import Form, HttpCase, tagged

from odoo.addons.unified_product_return_policy.models.urp_guard import UrpPayload

from .common import UrpCommon
from .test_p5_pos import UrpPosCommon

# every flag a client could try to put in the context to switch a protection off
FORGED = {"urp_skip_guard": True, "urp_checked": True, "urp_internal": True,
          "urp_batch_map": {}, "urp_sync_meta": {}}


class UrpReturnsCommon(UrpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.sales_user = Users.create({
            "name": "R Sales", "login": "urp_r_su", "password": "R-Sales-Pass-1!",
            "group_ids": [(6, 0, [cls.env.ref("sales_team.group_sale_salesman_all_leads").id,
                                  cls.env.ref("stock.group_stock_user").id])]})
        cls.product.product_tmpl_id.write({"use_custom_return_period": True, "custom_return_period_days": 7})

    def _delivered(self, qty=2):
        so = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [
            (0, 0, {"product_id": self.product.id, "product_uom_qty": qty})]})
        so.action_confirm()
        pick = so.picking_ids
        pick.move_ids.quantity = qty
        pick.move_ids.picked = True
        pick.button_validate()
        return so, so.order_line.move_ids.filtered(lambda m: m.state == "done")

    def _return(self, origin, qty, user=None):
        ret = origin.picking_id._create_return()
        ret.move_ids.write({"product_uom_qty": qty, "quantity": qty, "picked": True})
        return ret.with_user(user) if user else ret


# ====================================================================== 1. context bypass
@tagged("post_install", "-at_install", "urp", "urp_regr")
class TestR1ContextBypass(UrpReturnsCommon):

    def test_r1_01_forged_context_cannot_skip_final_guard(self):
        so, origin = self._delivered()
        with freeze_time(origin.urp_deadline + timedelta(days=1)):
            ret = self._return(origin, 1, self.sales_user).with_context(**FORGED)
            action = ret.button_validate()
            self.assertEqual(action["res_model"], "urp.return.check.wizard", "forged urp_checked ignored")
            with self.assertRaises(UserError):
                ret.move_ids._action_done()                               # forged urp_skip_guard ignored
            self.assertNotEqual(ret.state, "done")

    def test_r1_02_forged_context_cannot_write_protected_data(self):
        so, origin = self._delivered()
        far = fields.Datetime.now() + timedelta(days=999)
        with self.assertRaises(UserError):
            origin.with_context(**FORGED).sudo().write({"urp_deadline": far})
        with self.assertRaises(UserError):
            so.urp_batch_ids.with_context(**FORGED).sudo().write({"return_days": 999})
        with self.assertRaises(UserError):
            self.env["urp.policy.history"].search([], limit=1).with_context(**FORGED).write({"default_days": 1})

    def test_r1_03_override_cannot_be_attached_manually(self):
        so, origin = self._delivered()
        ov = self.env["urp.return.override"].sudo().create({
            "channel": "sale", "user_id": self.env.ref("base.user_admin").id, "reason": "forged",
            "company_id": self.company.id})
        with freeze_time(origin.urp_deadline + timedelta(days=1)):
            ret = self._return(origin, 1)
            with self.assertRaises(UserError):
                ret.with_context(**FORGED).sudo().write({"urp_override_id": ov.id})
            with self.assertRaises(UserError):
                ret.move_ids._action_done()

    def test_r1_04_forged_batch_map_ignored(self):
        so, origin = self._delivered(1)
        other = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [
            (0, 0, {"product_id": self.product.id, "product_uom_qty": 1})]})
        other.action_confirm()
        foreign = other.urp_batch_ids
        line = so.order_line
        line.with_context(urp_batch_map={line.id: foreign.id}).write({"product_uom_qty": 2})
        new_move = line.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        self.assertTrue(new_move.urp_batch_id)
        self.assertNotEqual(new_move.urp_batch_id, foreign)
        self.assertEqual(new_move.urp_batch_id.sale_line_id, line)

    def test_r1_05_move_create_cannot_carry_policy_data(self):
        so, origin = self._delivered(1)
        other = self.env["sale.order"].create({"partner_id": self.partner.id, "order_line": [
            (0, 0, {"product_id": self.product.id, "product_uom_qty": 1})]})
        other.action_confirm()
        vals = {"product_id": self.product.id, "product_uom_qty": 1, "location_id": origin.location_id.id,
                "location_dest_id": origin.location_dest_id.id, "picking_type_id": origin.picking_type_id.id}
        move = self.env["stock.move"].with_context(**FORGED).create(dict(vals, urp_deadline=fields.Datetime.now() + timedelta(days=999)))
        self.assertFalse(move.urp_deadline, "deadline stripped on create")
        with self.assertRaises(UserError):
            self.env["stock.move"].with_context(**FORGED).create(dict(vals, urp_batch_id=other.urp_batch_ids.id))


@tagged("post_install", "-at_install", "urp", "urp_regr")
class TestR1ContextBypassRpc(UrpReturnsCommon, HttpCase):
    """Same as above through real JSON-RPC calls, with the context sent by the client."""

    def _call(self, model, method, args, context):
        return self.url_open("/web/dataset/call_kw", data=__import__("json").dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": model, "method": method, "args": args, "kwargs": {"context": context}}}),
            headers={"Content-Type": "application/json"}).json()

    def test_r1_06_rpc_forged_context(self):
        so, origin = self._delivered()
        with freeze_time(origin.urp_deadline + timedelta(days=1)):
            ret = self._return(origin, 1)
            self.authenticate("urp_r_su", "R-Sales-Pass-1!")
            res = self._call("stock.picking", "button_validate", [ret.ids], FORGED)
            self.assertEqual(res["result"]["res_model"], "urp.return.check.wizard")
            res = self._call("stock.move", "write", [origin.ids, {"urp_deadline": "2099-01-01 00:00:00"}], FORGED)
            self.assertIn("error", res)
            res = self._call("stock.picking", "write", [ret.ids, {"urp_override_id": 1}], FORGED)
            self.assertIn("error", res)
            ret.invalidate_recordset()
            self.assertNotEqual(ret.state, "done")
            origin.invalidate_recordset()
            self.assertNotEqual(fields.Datetime.to_string(origin.urp_deadline), "2099-01-01 00:00:00")


# ====================================================================== 3. wizard view
@tagged("post_install", "-at_install", "urp", "urp_regr")
class TestR3WizardView(UrpReturnsCommon):

    def test_r3_01_flags_declared_in_view(self):
        arch = etree.fromstring(self.env["urp.return.check.wizard"].get_view(
            self.env.ref("unified_product_return_policy.urp_return_check_wizard_form").id, "form")["arch"])
        names = {f.get("name") for f in arch.iter("field")}
        for flag in ("can_override", "can_return_eligible"):
            self.assertIn(flag, names)
            node = arch.xpath(f"//field[@name='{flag}']")[0]
            self.assertIn(node.get("invisible"), ("1", "True", "true"))

    def test_r3_02_all_module_views_valid_and_modifier_fields_declared(self):
        views = self.env["ir.ui.view"].browse(self.env["ir.model.data"].search([
            ("module", "=", "unified_product_return_policy"), ("model", "=", "ir.ui.view")]).mapped("res_id"))
        self.assertTrue(views)
        for view in views.filtered(lambda v: v.type in ("form", "list")):
            root = view.inherit_id and view.inherit_id or view
            while root.inherit_id:
                root = root.inherit_id
            arch = etree.fromstring(self.env[view.model].get_view(root.id, view.type)["arch"])
            declared = {f.get("name") for f in arch.iter("field")}
            Model = self.env[view.model]
            for node in arch.iter():
                for attr in ("invisible", "readonly", "required"):
                    expr = node.get(attr)
                    if not expr or expr in ("1", "0", "True", "False"):
                        continue
                    for name in set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", expr)):
                        if name in Model._fields and (name.startswith("urp_") or name.startswith("can_")):
                            self.assertIn(name, declared, f"{view.xml_id or view.name}: '{name}' used in {attr} but not in the view")

    def test_r3_03_wizard_opens_in_form(self):
        so, origin = self._delivered()
        with freeze_time(origin.urp_deadline + timedelta(days=1)):
            action = self._return(origin, 1).button_validate()
            wiz = Form(self.env["urp.return.check.wizard"].browse(action["res_id"]))
            self.assertFalse(wiz.can_return_eligible, "nothing eligible after the deadline")
            self.assertEqual(wiz.can_override, self.env.user.has_group("sales_team.group_sale_manager"))


# ====================================================================== POS: 1, 2, 4, 5
@tagged("post_install", "-at_install", "urp", "urp_regr")
class TestRPos(UrpPosCommon):

    def _manual_refund(self, specs, session=None):
        """Refund order with several lines: specs = [(original line or False, qty)]."""
        sess = session or self._session()
        lines = [Command.create({"name": "R", "product_id": (orig.product_id if orig else self.p).id, "price_unit": 10.0,
                                 "qty": -q, "refunded_orderline_id": orig.id if orig else False,
                                 "price_subtotal": -10.0 * q, "price_subtotal_incl": -10.0 * q, "discount": 0})
                 for orig, q in specs]
        total = -10.0 * sum(q for _o, q in specs)
        return self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id, "partner_id": self.partner.id, "lines": lines,
            "amount_tax": 0, "amount_total": total, "amount_paid": 0, "amount_return": 0})

    # ---------------------------------------------------------------- 1. POS forged context / fields
    def test_r1_10_pos_protected_fields_and_meta(self):
        order = self._sale(1)
        order.with_context(**FORGED).write({"urp_governed": False})
        self.assertTrue(order.urp_governed, "forged write ignored")
        # a plain dict in the context (what a client can send) is not accepted as sync metadata
        order3, _ = self._unsnapshotted()
        order3.with_context(urp_sync_meta={"uuid": order3.uuid, "orig_session_id": self._session().id,
                                           "device_date": "2000-01-01 00:00:00", "server_time": False})._urp_snapshot_lines()
        self.assertEqual(order3.lines.urp_date_trust, "server")

    def _unsnapshotted(self):
        sess = self._session()
        order = self.env["pos.order"].create({
            "company_id": self.company.id, "session_id": sess.id,
            "lines": [Command.create({"name": "O", "product_id": self.p.id, "price_unit": 10.0, "qty": 1,
                                     "price_subtotal": 10.0, "price_subtotal_incl": 10.0, "discount": 0})],
            "amount_tax": 0, "amount_total": 10.0, "amount_paid": 0, "amount_return": 0})
        return order, sess

    # ---------------------------------------------------------------- 2. several refund lines, one sale line
    def test_r2_01_two_lines_same_sale_line_capped_on_total(self):
        order = self._sale(2)
        ref = self._manual_refund([(order.lines, 2), (order.lines, 2)])
        with self.assertRaises(UserError):
            self._pay(ref, ref.amount_total)
        ref2 = self._manual_refund([(order.lines, 1), (order.lines, 1)])
        self.assertEqual(self._pay(ref2, ref2.amount_total).state, "paid")
        ref3 = self._manual_refund([(order.lines, 1)])
        with self.assertRaises(UserError):
            self._pay(ref3, ref3.amount_total)

    def test_r2_02_ship_later_two_lines_cannot_exceed_delivered(self):
        order = self._sale(5, ship_later=True)
        self._deliver(order, 2)
        ref = self._manual_refund([(order.lines, 1), (order.lines, 2)])
        with self.assertRaises(UserError) as cm:
            self._pay(ref, ref.amount_total)
        self.assertIn("URP-OPS-01", str(cm.exception))
        ref2 = self._manual_refund([(order.lines, 1), (order.lines, 1)])
        self._pay(ref2, ref2.amount_total)
        allocs = ref2.lines.urp_allocation_ids
        self.assertEqual(sum(allocs.mapped("qty")), 2)
        for move in allocs.delivery_move_id:
            self.assertLessEqual(sum(allocs.filtered(lambda a: a.delivery_move_id == move).mapped("qty")), move.quantity)

    def test_r2_03_expired_two_lines_need_override_for_total(self):
        order = self._sale(3)
        with freeze_time(order.lines.urp_deadline + timedelta(days=1)):
            ref = self._manual_refund([(order.lines, 1), (order.lines, 1)])
            tok = self._token([{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 1}], ref.uuid)
            with self.assertRaises(UserError):
                self._pay_refund(ref, tok["token"])                   # authorised 1, requested 2
            ref2 = self._manual_refund([(order.lines, 1), (order.lines, 1)])
            tok2 = self._token([{"product_id": self.p.id, "refunded_line_id": order.lines.id, "qty": 2}], ref2.uuid)
            self.assertEqual(self._pay_refund(ref2, tok2["token"]).state, "paid")

    # ---------------------------------------------------------------- 4. token bound to one order
    def test_r4_01_token_bound_to_order_uuid(self):
        order = self._sale(3)
        with freeze_time(order.lines.urp_deadline + timedelta(days=1)):
            ref_a = self._draft_refund(order, 1)
            ref_b = self._draft_refund(order, 1)
            tok = self._token(self._lines(ref_a), ref_a.uuid)
            with self.assertRaises(UserError) as cm:
                self._pay_refund(ref_b, tok["token"])
            self.assertIn("another order", str(cm.exception))
            self.assertEqual(self._pay_refund(ref_a, tok["token"]).state, "paid", "still valid for its own order")

    def test_r4_02_token_bound_to_session(self):
        order = self._sale(3)
        cfg2 = self.pos_config_eur
        cfg2.open_ui()
        sess2 = cfg2.current_session_id
        if sess2.state == "opening_control":
            sess2.set_opening_control(0, "")
        with freeze_time(order.lines.urp_deadline + timedelta(days=1)):
            ref = self._draft_refund(order, 1)
            tok = self._token(self._lines(ref), ref.uuid, session=sess2)
            self.assertTrue(tok["ok"], tok)
            with self.assertRaises(UserError):
                self._pay_refund(ref, tok["token"])

    def test_r4_03_token_bound_to_original_line(self):
        sale_x, sale_y = self._sale(2), self._sale(2)
        with freeze_time(sale_x.lines.urp_deadline + timedelta(days=1)):
            ref = self._draft_refund(sale_x, 1)
            tok = self._token([{"product_id": self.p.id, "refunded_line_id": sale_y.lines.id, "qty": 1}], ref.uuid)
            with self.assertRaises(UserError):
                self._pay_refund(ref, tok["token"])

    def test_r4_04_no_token_for_paid_order_or_missing_uuid(self):
        order = self._sale(1)
        self.assertEqual(self._token(self._lines(self._draft_refund(order, 1)), order.uuid)["error"], "no_order")
        self.assertEqual(self._token([{"product_id": self.p.id, "qty": 1}], False)["error"], "no_order")

    # ---------------------------------------------------------------- 5. historical category for offline sales
    def _offline_snapshot(self, order, device_dt):
        meta = {"uuid": order.uuid, "orig_session_id": order.session_id.id,
                "device_date": fields.Datetime.to_string(device_dt), "server_time": False}
        order.with_context(urp_sync_meta=UrpPayload(meta))._urp_snapshot_lines()
        return order.lines

    def test_r5_01_offline_sale_uses_category_at_sale_time(self):
        cat_a = self.env["product.category"].create({"name": "URP A", "use_custom_return_period": True, "custom_return_period_days": 10})
        cat_b = self.env["product.category"].create({"name": "URP B", "use_custom_return_period": True, "custom_return_period_days": 20})
        tmpl = self.p.product_tmpl_id
        tmpl.write({"use_custom_return_period": False, "categ_id": cat_a.id})
        order, sess = self._unsnapshotted()
        start = sess.start_at
        with freeze_time(start + timedelta(hours=1)):
            tmpl.categ_id = cat_b                                       # moved after the offline sale
            self.assertEqual(self.p._urp_resolve(self.company, at=start + timedelta(seconds=1))["return_days"], 10)
            line = self._offline_snapshot(order, start + timedelta(seconds=1))
        self.assertEqual((line.urp_return_days, line.urp_policy_source), (10, "category"))
        self.assertTrue(line.urp_policy_ambiguous, "category change inside the window is flagged")

    def test_r5_02_change_of_old_category_detected_after_move(self):
        cat_a = self.env["product.category"].create({"name": "URP A2", "use_custom_return_period": True, "custom_return_period_days": 10})
        cat_b = self.env["product.category"].create({"name": "URP B2", "use_custom_return_period": True, "custom_return_period_days": 20})
        tmpl = self.p.product_tmpl_id
        tmpl.write({"use_custom_return_period": False, "categ_id": cat_a.id})
        order, sess = self._unsnapshotted()
        start = sess.start_at
        with freeze_time(start + timedelta(minutes=30)):
            cat_a.custom_return_period_days = 3                         # policy of the OLD category changed
        with freeze_time(start + timedelta(hours=1)):
            tmpl.categ_id = cat_b
            line = self._offline_snapshot(order, start + timedelta(seconds=1))
        self.assertEqual(line.urp_return_days, 10)
        hist = line.urp_ambiguous_history_ids
        self.assertTrue(hist.filtered(lambda h: h.scope == "category" and h.res_id == cat_a.id),
                        "change of the category held at sale time is reported")

    def test_r5_03_product_never_logged_records_previous_state(self):
        cat_a = self.env["product.category"].create({"name": "URP A3"})
        cat_b = self.env["product.category"].create({"name": "URP B3"})
        tmpl = self.env["product.template"].create({"name": "URP fresh", "categ_id": cat_a.id, "is_storable": True})
        Hist = self.env["urp.policy.history"]
        self.assertFalse(Hist.search([("scope", "=", "product"), ("res_id", "=", tmpl.id)]))
        with freeze_time(fields.Datetime.now() + timedelta(hours=1)):
            tmpl.categ_id = cat_b
        rows = Hist.search([("scope", "=", "product"), ("res_id", "=", tmpl.id)], order="valid_from, id")
        self.assertEqual([(r.origin, r.categ_id) for r in rows], [("baseline", cat_a.id), ("change", cat_b.id)])
        self.assertEqual(Hist._urp_categ_at(tmpl, tmpl.create_date + timedelta(minutes=1)), cat_a.id)
