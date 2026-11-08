"""Offline site-switch tests: invented credentials, patched HTTP, no site or network."""

import datetime
import unittest
from unittest import mock

import frappe
from bi_finance import (
    bank_feed,
    paypal,
    paypal_client,
    wise,
    wise_client,
)

DAY = datetime.date(2026, 1, 1)
MOMENT = datetime.datetime(2026, 1, 1)


class Disabled(unittest.TestCase):
    def setUp(self):
        self.conf = mock.patch.object(
            frappe, "conf", {"disable_external_integrations": 1}
        )
        self.conf.start()
        self.addCleanup(self.conf.stop)
        self.http = mock.patch("urllib.request.urlopen")
        self.opener = self.http.start()
        self.addCleanup(self.http.stop)
        self.throw = mock.patch.object(frappe, "throw", side_effect=RuntimeError)
        self.thrown = self.throw.start()
        self.addCleanup(self.throw.stop)

    def refused(self, call):
        with self.assertRaises(RuntimeError):
            call()
        self.thrown.assert_called_with(mock.ANY)
        message = self.thrown.call_args.args[0]
        self.assertIn("external integrations are disabled", message.lower())
        self.assertIn("disable_external_integrations", message)
        self.opener.assert_not_called()

    def test_manual_entrypoints(self):
        for module in (wise, paypal):
            for name, kwargs in (
                ("test_connection", {}),
                ("sync_now", {"dry_run": True}),
                ("sync_now", {"dry_run": False}),
                ("run", {"dry_run": True}),
                ("run", {"dry_run": False}),
            ):
                with self.subTest(module=module.__name__, method=name, kwargs=kwargs):
                    with mock.patch.object(frappe, "get_single") as settings:
                        self.refused(lambda: getattr(module, name)(**kwargs))
                        settings.assert_not_called()

    def test_scheduled_jobs_log_and_skip_without_reading_secrets(self):
        for module in (wise, paypal):
            with self.subTest(module=module.__name__):
                with mock.patch.object(frappe, "logger") as logger, mock.patch.object(
                    frappe, "get_single"
                ) as settings:
                    module.sync_scheduled()
                    logger.return_value.info.assert_called_once()
                    message = logger.return_value.info.call_args.args[0]
                    self.assertIn("external integrations are disabled", message.lower())
                    self.assertIn("disable_external_integrations", message)
                    settings.assert_not_called()
                    self.thrown.assert_not_called()
                    self.opener.assert_not_called()

    def test_every_client_request_path_including_injected_openers(self):
        calls = (
            lambda op: wise_client.profiles("invented", opener=op),
            lambda op: wise_client.balances("invented", 42, opener=op),
            lambda op: wise_client.activities("invented", 42, MOMENT, opener=op),
            lambda op: wise_client.transfers("invented", 42, MOMENT, MOMENT, opener=op),
            lambda op: wise_client.quote("invented", 42, "invented", opener=op),
            lambda op: wise_client.recipient("invented", 42, opener=op),
            lambda op: wise_client.payout_recipients(
                "invented",
                [
                    {
                        "targetAccount": 42,
                        "status": wise_client.COMPLETED_TRANSFER,
                        "sourceCurrency": "CHF",
                        "targetCurrency": "EUR",
                    }
                ],
                opener=op,
            ),
            lambda op: paypal_client.access_token("invented", "invented", opener=op),
            lambda op: paypal_client.transactions(
                "invented", MOMENT, MOMENT, opener=op
            ),
            lambda op: paypal_client.balances("invented", opener=op),
            lambda op: bank_feed.fetch_rates("EUR", DAY, DAY, opener=op),
            lambda op: bank_feed.load_rates("EUR", DAY, DAY, True, opener=op),
            lambda op: bank_feed.load_rates("EUR", DAY, DAY, False, opener=op),
        )
        for index, call in enumerate(calls):
            for opener in (None, mock.Mock()):
                with self.subTest(path=index, injected=opener is not None):
                    self.refused(lambda: call(opener))
                    if opener is not None:
                        opener.assert_not_called()

    def test_switch_rechecked_for_each_request(self):
        for index, call in enumerate(
            (
                lambda op: wise_client.profiles("invented", opener=op),
                lambda op: paypal_client.balances("invented", opener=op),
                lambda op: bank_feed.fetch_rates("EUR", DAY, DAY, opener=op),
            )
        ):
            with self.subTest(path=index):
                with mock.patch.object(frappe, "conf", {}):
                    response = mock.MagicMock()
                    response.__enter__.return_value.read.return_value = (
                        "[]" if index == 2 else "{}"
                    )
                    opener = mock.Mock(return_value=response)
                    call(opener)
                    opener.assert_called_once()
                opener.reset_mock()
                self.refused(lambda: call(opener))
                opener.assert_not_called()


class Enabled(unittest.TestCase):
    def test_unset_and_zero_allow_scheduled_jobs(self):
        for config in ({}, {"disable_external_integrations": 0}):
            for module in (wise, paypal):
                with self.subTest(config=config, module=module.__name__):
                    with mock.patch.object(frappe, "conf", config), mock.patch.object(
                        frappe, "get_single"
                    ) as settings, mock.patch.object(module, "run") as run:
                        settings.return_value.sync_enabled = True
                        module.sync_scheduled()
                        run.assert_called_once_with(dry_run=False)
