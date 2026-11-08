# Disabling external bank integrations

Set `disable_external_integrations` to JSON `1` in the site's `site_config.json`
(or use `bench --site <site> set-config disable_external_integrations 1`).
The setting is off when absent or `0`.

This switch is implemented for every Wise, PayPal and Frankfurter request made
by `bi_finance`. It covers Test connection, Sync now, dry runs, direct client
calls and scheduled bank syncs. Manual calls raise a clear Frappe error before
reading credentials; scheduled syncs log a skip through the `bi_finance` logger.
The shared HTTP boundary checks again before each request, including later
pages and PayPal token requests. The switch does not change the individual
Wise/PayPal `sync_enabled` settings.

This is a bank integration switch, not a site-wide network policy. Keep the
separate assistant switch, `mute_emails`, `pause_scheduler`, restored webhook
settings and dev network egress restrictions in place. The bexio host job is
outside the site and must stay off dev.

Deploy the updated app image before relying on this setting. No migration or
backfill is needed for the key. Apply it to dev before permitting site access.
