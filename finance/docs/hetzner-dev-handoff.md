# Hetzner dev handoff for IT

For yard-7epr and yard-f1hw, project `finance`, environment `dev`.
This is the procedure to review before the one-time copy, not an instruction to
copy or start it now. Benchi's 2026-11-07 exception covers current real data on
dev only, tailnet only, with mail, scheduling and integrations off. Benchi runs
the copy; IT removes it after the tests. The exception does not authorize a
production deployment. The normal [deployment rules](https://github.com/svc-bi-concepts/engineering-standards/blob/main/deploy.md)
still require synthetic or scrubbed dev data; permission/DPA remains mem-p2cg.

Only setting names and non-secret off values appear below. Replace angle-bracket
placeholders privately. Never paste site configuration, credentials or backup
contents into GitHub.

## 1. Compose, branch and image

| Item                      | Answer and evidence                                                                                                                                                                                                                                                                                 |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Repository                | `svc-bi-concepts/frappe_docker`                                                                                                                                                                                                                                                                     |
| Planned production branch | `main`, after PR review and a human merge; never deploy a feature branch to production. See the engineering standards linked above, Release flow.                                                                                                                                                   |
| Compose path              | Repository-root `finance-coolify.yml`, used alone, without `pwd.yml` or `finance-local.yml` (`finance-coolify.yml:1-4`).                                                                                                                                                                            |
| Frontend service          | `frontend`, container port `8080`, site name `frontend`; `FRAPPE_SITE_NAME_HEADER=frontend` (`finance-coolify.yml:137-168`). Container names are Coolify's, not a fixed name.                                                                                                                       |
| Runtime setting names     | `LEDGER_HOST`, `DB_ROOT_PASSWORD`, `ADMIN_PASSWORD`; all required, no values supplied here (`finance-coolify.yml:10-13,39-40,87-88,164`). Set the dev host privately in Coolify.                                                                                                                    |
| Current compose image     | `frappe-finance-custom:v16.51.1-swiss-bi30`, `pull_policy: never`. There is currently no `build:` block: this file expects an image already loaded into the host (`finance-coolify.yml:26-28,137-139`). `finance-local.yml:5-22` uses the same pin; it is the local override, not the Hetzner file. |
| Pending dev build source  | `yard/erp-ve60`; use the PR head being checked. Build configuration, app inclusion, secret handling and the baked gate are owned by [Coolify build](coolify-build.md).                                                                                                                              |

The branch-built image is the pending dev path, not a change already present in
`finance-coolify.yml`. IT must wire its built image into the dev resource and
record the tested source SHA and image digest. Do not start the old pin and
assume it contains the branch. erp-ve60/yard-x1sn owns the build and baked gate;
production image pinning is erp-mnkv. This document does not wait for either.

The three compose networks are not an integration kill switch: `ledger-data` is
internal, but application services also join `ledger-out` for outbound traffic
(`finance-coolify.yml:19-24,26-31,170-237`). Tailnet-only ingress does not prevent
outgoing calls. IT must establish dev egress denial before restoring real data,
including HTTPS and SMTP, while retaining only required internal service links.
This is a requirement for IT's isolated resource, not an existing compose flag.

## 2. Off switches before first application start

There are no compose environment variables for these off switches today. They
are site configuration and restored database settings. Do not run an unrestricted
Coolify start and then turn them off. Keep the scheduler, `queue-long`,
`queue-short`, backend HTTP, websocket and frontend stopped while restoring;
use a one-off bench container on the isolated dev DB/sites volume to apply the
commands. Start only the supporting DB/Redis/configurator needed for that work.
Use fresh dev volumes, especially Redis: do not copy a live queue. A disabled
scheduler does not cancel jobs already queued or running.

Run the following **inside the dev bench**, where `<site>` is the restored site
(`frontend` with this compose). Repeat the entire sequence after every restore
and after any migrate that changes settings, before opening the application.
The local precedent applies site-config flags before migrate and DB switches
after it; it starts workers only afterwards (`finance/scripts/copy-stack.sh:166-183,206-220`).
Its `up`/`refresh` wrapper is not the Hetzner procedure: it starts HTTP services
early and targets the local copy. Do not run it for this handoff.

First, with the restored site directory present and before migrate or any app
process, set parsed numeric values in `sites/<site>/site_config.json`:

```sh
bench --site <site> set-config -p pause_scheduler 1
bench --site <site> set-config -p disable_scheduler 1
bench --site <site> set-config -p mute_emails 1
bench --site <site> set-config host_name http://frontend:8080
```

`pause_scheduler` and `mute_emails` follow `finance/scripts/copy-stack.sh:169-172`.
The copy's internal `host_name` replaces the restored public URL; it is not the
Coolify routing hostname. `disable_scheduler` is an extra Frappe site-level
barrier, independent of the DB setting `System Settings.enable_scheduler`.
Frappe v16.51.1 checks both flags and the DB setting in
[frappe/utils/scheduler.py:150-181](https://github.com/frappe/frappe/blob/v16.51.1/frappe/utils/scheduler.py#L150-L181).
`-p` parses the number instead of storing a truthy string; see
[frappe/commands/utils.py, set_config](https://github.com/frappe/frappe/blob/v16.51.1/frappe/commands/utils.py).

After restoring the DB, before starting any consumer, apply:

```sh
bench --site <site> scheduler disable
bench --site <site> execute frappe.db.set_single_value --args '["Wise Settings", "sync_enabled", 0]'
bench --site <site> execute frappe.db.set_single_value --args '["PayPal Settings", "sync_enabled", 0]'
bench --site <site> execute frappe.db.sql --args '["update `tabEmail Account` set enable_outgoing = 0, enable_incoming = 0"]'
bench --site <site> execute frappe.db.sql --args '["update `tabWebhook` set enabled = 0"]'
bench --site <site> execute frappe.db.set_single_value --args '["AI Assistant Settings", "enabled", 0]'
bench --site <site> execute frappe.db.set_single_value --args '["AI Assistant Settings", "actions_enabled", 0]'
bench --site <site> execute frappe.db.set_single_value --args '["Bank Matching Settings", "enabled", 0]'
bench --site <site> execute frappe.db.set_single_value --args '["Bank Matching Settings", "rules_enabled", 0]'
bench --site <site> execute bi_finance.setup.disable_libracore_print_formats
bench --site <site> clear-cache
bench --site <site> scheduler status
```

`scheduler disable` writes `System Settings.enable_scheduler=0`, not an invented
`enable_scheduler` compose variable. Its syntax and commit are in
[frappe/commands/scheduler.py:75-106](https://github.com/frappe/frappe/blob/v16.51.1/frappe/commands/scheduler.py#L75-L106).
`bench execute` commits its DB calls before returning
([frappe/commands/utils.py:269-319](https://github.com/frappe/frappe/blob/v16.51.1/frappe/commands/utils.py#L269-L319)).
The SQL and bank switches match `finance/scripts/copy-stack.sh:177-183`.
Clearing cache matters after direct SQL. Leave scheduler and workers stopped
through the tests; starting them requires a separately reviewed need and keeps
all site/DB off switches in place.

| Integration or job                          | Scope of the switch; evidence and remaining barrier                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Outgoing and incoming mail                  | `mute_emails=1` plus every Email Account's `enable_outgoing=0`, `enable_incoming=0`. Frappe checks muted mail before sending ([email_queue.py:174-180](https://github.com/frappe/frappe/blob/v16.51.1/frappe/email/doctype/email_queue/email_queue.py#L174-L180), [frappe/**init**.py:1455-1456](https://github.com/frappe/frappe/blob/v16.51.1/frappe/__init__.py#L1455-L1456)). Keep workers off and SMTP/HTTPS egress denied, including any default SMTP configuration restored outside Email Account. Do not flush the restored Email Queue. |
| Wise                                        | `Wise Settings.sync_enabled=0` stops only the hourly entrypoint (`finance/apps/bi_finance/bi_finance/wise.py:85-100`). Test connection, Sync now and dry runs still contact Wise (`wise.py:38-83,100-108`); no universal off flag exists. Egress denial must cover those manual paths.                                                                                                                                                                                                                                                           |
| PayPal                                      | `PayPal Settings.sync_enabled=0` stops only the hourly entrypoint (`finance/apps/bi_finance/bi_finance/paypal.py:37-89`). Test connection, Sync now and dry runs still obtain tokens/read PayPal. No universal off flag exists; deny egress.                                                                                                                                                                                                                                                                                                     |
| Exchange rates (Frankfurter)                | No off flag in the rate client. Even a dry run fetches rates (`finance/apps/bi_finance/bi_finance/bank_feed.py:280-296,314-323`). Deny egress; do not exercise external rate lookups on this copy.                                                                                                                                                                                                                                                                                                                                               |
| AI assistant (OpenRouter)                   | `AI Assistant Settings.enabled=0` rejects ask before loading its key or sending ledger context (`finance/apps/bi_finance/bi_finance/assistant.py:144-165`). `actions_enabled=0` also turns off assistant actions (`assistant_actions.py:168-169,220-221`). Keep HTTPS egress denied.                                                                                                                                                                                                                                                             |
| Webhooks                                    | Every restored `Webhook.enabled=0`, as in `finance/scripts/copy-stack.sh:183`; field defined in [Frappe webhook.json:154-159](https://github.com/frappe/frappe/blob/v16.51.1/frappe/integrations/doctype/webhook/webhook.json#L154-L159). This does not cancel an already enqueued webhook. Fresh Redis, stopped workers and denied egress remain necessary.                                                                                                                                                                                     |
| bexio -> ERPNext hourly importer            | Not a `bi_finance` scheduler job. It is a Mac launchd wrapper (`finance/bexio/ch.bi-concepts.bexio-sync.plist:2-10`, `finance/bexio/sync_hourly.sh:1-31`). No site-config off flag exists. Do not install its launchd/cron equivalent on dev, run its wrapper, grant vault access, copy token files or point the existing Mac job at dev. `--dry-run` still contacts bexio and ERPNext (`sync_hourly.sh:44-47`); it is not an off switch. Do not disable the Mac's live sync for this copy.                                                      |
| Other banks / payment files                 | No other bank API is registered in `bi_finance` scheduler hooks (`finance/apps/bi_finance/bi_finance/hooks.py:74-79`). Statement upload and payment XML generation are manual local operations (`bank_statement_upload.py:1`, under `bi_finance/doctype/bank_statement_upload/`; `payment_run.py:1-9`). Do not run external bank launchers or upload generated payment files. Deny egress for integrations from other installed apps too.                                                                                                        |
| Local bank matching and monthly revaluation | `Bank Matching Settings.enabled=0`, `rules_enabled=0` prevent automatic matching/rule booking after imports (`finance/apps/bi_finance/bi_finance/bank_match_job.py:85-89,109-115`). These are local bookkeeping, not bank API kill switches. The monthly draft job is blocked by disabling the scheduler (`hooks.py:77-78`). Manual matching remains possible; do not run it as an isolation check.                                                                                                                                              |
| External Swiss QR rendering                 | `bi_finance.setup.disable_libracore_print_formats` disables inherited print formats that send invoice data to an outside QR renderer (`finance/apps/bi_finance/bi_finance/setup.py:1-49`). The migrate hook repeats it (`hooks.py:47-50`). Use the local BI QR format only; deny renderer egress.                                                                                                                                                                                                                                                |

There is no single existing flag that disables every outside integration.
Follow-up for finance: add one site-config guard, for example
`disable_external_integrations`, checked before all Wise/PayPal manual and
scheduled calls and Frankfurter requests. It is **not implemented yet, see
erp-ksff**; setting it today does nothing. The bexio host job stays off dev;
no host-job change is part of this documentation bead. IT must inventory restored Server Scripts,
other apps' integrations and user/API access privately; the hook list is code
evidence, not a complete inventory of the restored DB. Do not treat tailnet
membership or the restricted API users as a universal integration barrier
(`finance/apps/bi_finance/bi_finance/api_gate.py:1-13,64-70`).

## 3. Backup set, encryption and checksum

Benchi produces a fresh set in the current running backend, using the site's
actual name privately:

```sh
docker exec <current-backend-container> bench --site <site> backup --with-files
```

This is the command in `finance/scripts/copy-stack.sh:139-150` with identifiers
replaced by placeholders; `docs/11-how-to/04-create-backups.md:47-58` also
requires `--with-files` for attachments. Collect exactly one successful backup
invocation's common `<prefix>` from
`/home/frappe/frappe-bench/sites/<site>/private/backups/`:

| Member                             | Purpose                                                                                                   |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `<prefix>-database.sql.gz`         | Database, including audit history and queued mail records.                                                |
| `<prefix>-site_config_backup.json` | Site configuration, including database credentials and `encryption_key`; treat the entire file as secret. |
| `<prefix>-files.tar`               | Public attachments.                                                                                       |
| `<prefix>-private-files.tar`       | Private attachments.                                                                                      |

Names and paths: `finance/scripts/copy-stack.sh:94-95,137-150` and
`finance/docs/archiving.md:15-22`. Fail if any member is missing; do not pick the
newest file independently for each suffix or mix backup generations. Record
the image digest, app versions and code SHA in a private manifest for restore
compatibility, following [the backup software guidance](archiving.md#what-is-kept-today).

**Existing gap:** `copy-stack.sh:139-150` stages these files with directory mode
700, but implements neither archive encryption nor a checksum manifest. The
generic backup recipe's optional restic path names `RESTIC_REPOSITORY`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `RESTIC_PASSWORD`
(`docs/11-how-to/04-create-backups.md:19-30`); it is not configured for this copy.
`overrides/compose.backup-cron.yaml:10-15` only schedules `bench --site all backup`,
without files, encryption or checksums. Do not enable that cron on dev.

For yard-f1hw, the smallest one-time addition is a SHA-256 member manifest inside
an age-encrypted archive, then a checksum of that ciphertext. After staging
only the four selected files in a protected directory, run there with
`BACKUP_PREFIX` set privately to their common prefix. Run the block as one unit;
the subshell stops on any missing member, checksum, archive or encryption failure:

```sh
(
set -eu
: "${BACKUP_PREFIX:?Set BACKUP_PREFIX to the selected backup generation}"
umask 077
shasum -a 256 -- "$BACKUP_PREFIX-database.sql.gz" "$BACKUP_PREFIX-site_config_backup.json" "$BACKUP_PREFIX-files.tar" "$BACKUP_PREFIX-private-files.tar" > SHA256SUMS
shasum -a 256 -c SHA256SUMS
tar -cf finance-dev-backup.tar SHA256SUMS "$BACKUP_PREFIX-database.sql.gz" "$BACKUP_PREFIX-site_config_backup.json" "$BACKUP_PREFIX-files.tar" "$BACKUP_PREFIX-private-files.tar"
age -R <recipient-file> -o finance-dev-backup.tar.age finance-dev-backup.tar
shasum -a 256 finance-dev-backup.tar.age > finance-dev-backup.tar.age.sha256
)
```

The SHA/tar steps are a proposed transfer procedure, not existing repo
automation. Encryption syntax: [age README, Usage](https://github.com/FiloSottile/age#usage).
IT must provision age and the approved recipient file before the copy; no
recipient, key or password value belongs here. Transfer only the ciphertext
and its `.sha256`, over IT's approved authenticated channel, and verify the
ciphertext checksum there. The checksum detects damage; authenticate its
source as well. Decrypt inside protected dev staging with IT's identity file,
extract and verify the inner `SHA256SUMS` before restoring. Keep any private
manifest inside the encrypted bundle as well. The site's `encryption_key`
decrypts stored application passwords; it does **not** encrypt the backup
archive (`finance/docs/archiving.md:47-53`).

Restore the DB plus both file archives as in `finance/scripts/copy-stack.sh:155-163`.
Do not blindly replace dev `site_config.json` with the source backup: retain
dev DB connection details, off switches and dev `host_name`. `bench restore`
does not import the source `encryption_key` automatically
(`finance/docs/archiving.md:50-53`). If an approved test needs it, Benchi/IT handles
it privately under the same exception; it is not needed to prove disabled
integrations. Restored credential decryptability must never be the isolation
barrier.

Before opening dev, verify only off-state flags and counts: scheduler status
disabled, both feed switches zero, assistant/matching switches zero, no enabled
mail accounts or webhooks, no running workers/scheduler, and IT's egress/ingress
checks. Do not test isolation by sending real mail or making a real API call.
After tests, IT removes the dev resource, its volumes and all dev transfer and
staging copies, including plaintext and encrypted bundles; preserve the source
site and its normal backups. Record completion without data or secret values.
