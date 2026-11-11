import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import racknerd_watcher as rw

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=rw.SHANGHAI)


def product(pid, name, price, period="Annually"):
    return f"""
<div class="product clearfix" id="product{pid}">
  <header><span id="product{pid}-name">{name}</span></header>
  <div class="product-desc">
    <p id="product{pid}-description">
      1 vCPU Core<br />20 GB <strong>PURE SSD</strong> RAID-10 Storage<br />
      <strong><font color="#008000">JUST ${price}/YEAR - WOW!!</font></strong><br />
    </p>
    <ul></ul>
  </div>
  <footer>
    <div class="product-pricing" id="product{pid}-price">
      Starting from<br /><span class="price">${price} USD</span><br />{period}<br>
    </div>
    <a href="/index.php?rp=/store/special-promos/{pid}-plan"
       class="btn btn-success btn-sm btn-order-now" id="product{pid}-order-button">Order Now</a>
  </footer>
</div>"""


def page(*items):
    return "<html><body>" + "".join(items) + "</body></html>"


PROMOS = (
    ("952", "1 GB KVM VPS Special", "21.99"),
    ("953", "2 GB KVM VPS Special", "35.99"),
    ("954", "4 GB KVM VPS Special", "59.99"),
    ("955", "6 GB KVM VPS Special", "89.99"),
    ("956", "8 GB KVM VPS Special", "119.99"),
)


def full_page(drop=None):
    html = []
    for pid, name, price in PROMOS:
        html.append(product(pid, name, drop.get(pid, price) if drop else price))
    return page(*html)


class ParseTests(unittest.TestCase):
    def test_parses_five_promos_with_details(self):
        promos = rw.parse_promos(full_page(), rw.PROMO_URL)
        self.assertEqual(len(promos), 5)
        first = promos[0]
        self.assertEqual(first["name"], "1 GB KVM VPS Special")
        self.assertEqual(first["price"], 21.99)
        self.assertEqual(first["currency"], "USD")
        self.assertEqual(first["period"], "Annually")
        self.assertTrue(first["url"].endswith("/952-plan"))
        self.assertIn("1 vCPU Core", first["specs"])
        self.assertNotIn("JUST", " ".join(first["specs"]))

    def test_empty_page_raises(self):
        with self.assertRaises(rw.WatcherError):
            rw.parse_promos("<html>no products</html>", rw.PROMO_URL)


class CompareTests(unittest.TestCase):
    def test_finds_only_price_drops(self):
        before = {p["name"]: p for p in rw.parse_promos(full_page(), rw.PROMO_URL)}
        after = rw.parse_promos(full_page({"952": "18.99", "953": "39.99"}), rw.PROMO_URL)
        drops = rw.find_drops(after, before)
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["current"]["name"], "1 GB KVM VPS Special")
        self.assertAlmostEqual(drops[0]["saving"], 3.0)

    def test_no_drop_when_unchanged(self):
        promos = rw.parse_promos(full_page(), rw.PROMO_URL)
        before = {p["name"]: p for p in promos}
        self.assertEqual(rw.find_drops(promos, before), [])

    def test_email_lists_previous_and_updated_prices_and_link(self):
        before = rw.parse_promos(full_page(), rw.PROMO_URL)
        after = rw.parse_promos(full_page({"952": "18.99"}), rw.PROMO_URL)
        body = rw.render_email(rw.find_drops(after, {p["name"]: p for p in before}),
                               len(after), NOW)
        self.assertIn("1 GB KVM VPS Special", body)
        self.assertIn("Previous: $21.99 USD / Annually", body)
        self.assertIn("Now:      $18.99 USD / Annually", body)
        self.assertIn("https://my.racknerd.com/", body)
        self.assertIn("4 other promo plan(s) unchanged", body)


class StateTests(unittest.TestCase):
    def test_state_round_trip_preserves_first_seen(self):
        promos = rw.parse_promos(full_page(), rw.PROMO_URL)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            rw.save_state(path, promos, None, NOW)
            first = rw.load_state(path)
            later = datetime(2026, 11, 8, 10, 0, tzinfo=rw.SHANGHAI)
            rw.save_state(path, promos, first, later)
            second = rw.load_state(path)
        self.assertEqual(len(second["items"]), 5)
        self.assertEqual(second["items"]["1 GB KVM VPS Special"]["first_seen"],
                         first["items"]["1 GB KVM VPS Special"]["first_seen"])
        self.assertEqual(second["updated_at"], later.isoformat(timespec="seconds"))


class RunTests(unittest.TestCase):
    def config(self, temp):
        config = copy.deepcopy(rw.DEFAULT_CONFIG)
        config["state_file"] = str(Path(temp) / "state.json")
        config["log_file"] = str(Path(temp) / "run.log")
        config["gmail_email"] = "sender@example.com"
        config["gmail_app_password"] = "secret"
        config["recipient_email"] = "to@example.com"
        return config

    def test_first_run_records_baseline_without_email(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(temp)
            with mock.patch.object(rw, "fetch_page", return_value=full_page()), \
                    mock.patch.object(rw, "send_email") as send, \
                    mock.patch.object(rw, "ping") as ping:
                self.assertEqual(rw.run(config, rw.PROMO_URL, False, True), 0)
            send.assert_not_called()
            ping.assert_called_once_with(config["healthcheck_url"])
            state = json.loads(Path(config["state_file"]).read_text(encoding="utf-8"))
            self.assertEqual(state["items"]["1 GB KVM VPS Special"]["price"], 21.99)

    def test_price_drop_sends_email_then_no_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(temp)
            with mock.patch.object(rw, "fetch_page", return_value=full_page()), \
                    mock.patch.object(rw, "ping"):
                rw.run(config, rw.PROMO_URL, False, True)
            with mock.patch.object(rw, "fetch_page",
                                   return_value=full_page({"952": "18.99"})), \
                    mock.patch.object(rw, "send_email") as send, \
                    mock.patch.object(rw, "ping") as ping:
                self.assertEqual(rw.run(config, rw.PROMO_URL, False, True), 0)
                send.assert_called_once()
                subject, body = send.call_args.args[1], send.call_args.args[2]
                self.assertIn("Price drop", subject)
                self.assertIn("$21.99", body)
                self.assertIn("$18.99", body)
                ping.assert_called_once_with(config["healthcheck_url"])
            with mock.patch.object(rw, "fetch_page",
                                   return_value=full_page({"952": "18.99"})), \
                    mock.patch.object(rw, "send_email") as send, \
                    mock.patch.object(rw, "ping"):
                self.assertEqual(rw.run(config, rw.PROMO_URL, False, True), 0)
            send.assert_not_called()
            state = json.loads(Path(config["state_file"]).read_text(encoding="utf-8"))
            self.assertEqual(state["items"]["1 GB KVM VPS Special"]["price"], 18.99)

    def test_fetch_failure_pings_fail_and_keeps_state(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(temp)
            with mock.patch.object(rw, "fetch_page",
                                   side_effect=rw.WatcherError("boom")), \
                    mock.patch.object(rw, "send_email") as send, \
                    mock.patch.object(rw, "ping") as ping:
                self.assertEqual(rw.run(config, rw.PROMO_URL, False, True), 1)
            send.assert_not_called()
            ping.assert_called_once_with(config["healthcheck_fail_url"])
            self.assertFalse(Path(config["state_file"]).exists())

    def test_email_failure_pings_fail_and_keeps_old_state(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(temp)
            with mock.patch.object(rw, "fetch_page", return_value=full_page()), \
                    mock.patch.object(rw, "ping"):
                rw.run(config, rw.PROMO_URL, False, True)
            before = Path(config["state_file"]).read_text(encoding="utf-8")
            with mock.patch.object(rw, "fetch_page",
                                   return_value=full_page({"952": "18.99"})), \
                    mock.patch.object(rw, "send_email",
                                      side_effect=rw.WatcherError("smtp down")), \
                    mock.patch.object(rw, "ping") as ping:
                self.assertEqual(rw.run(config, rw.PROMO_URL, False, True), 1)
            ping.assert_called_once_with(config["healthcheck_fail_url"])
            self.assertEqual(Path(config["state_file"]).read_text(encoding="utf-8"), before)

    def test_dry_run_writes_nothing_and_does_not_ping(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(temp)
            with mock.patch.object(rw, "fetch_page", return_value=full_page()), \
                    mock.patch.object(rw, "send_email") as send, \
                    mock.patch.object(rw, "ping") as ping:
                self.assertEqual(rw.run(config, rw.PROMO_URL, True, True), 0)
            send.assert_not_called()
            ping.assert_not_called()
            self.assertFalse(Path(config["state_file"]).exists())


if __name__ == "__main__":
    unittest.main()
