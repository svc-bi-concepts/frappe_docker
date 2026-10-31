# Wise transfer preparation

`prepare_wise_transfer.py` prepares one unfunded transfer to an existing, active recipient in the single business profile. It refuses unknown recipients, a recipient without completed transfer history, duplicate invoice references, unsupported details, and, with `--yes`, dates outside the allowed window. It never funds, cancels, creates or changes recipients, or writes to bexio or ERPNext.

Run a GET-only preview first:

```sh
finance/wise/prepare_wise_transfer.py --recipient-id 123 --invoice INV-2026-01 --amount 1081.00 --target-currency EUR --planned-date 2026-11-10
```

After comparing the printed recipient name and account summary with the invoice, the yardmaster may add `--yes` on or after the planned date. The script creates a recipient-first quote using BALANCE pricing, checks transfer requirements, creates one transfer, and reads it back. `--detail KEY=VALUE` supplies the supported conditional purpose fields; `--attempt 2` gives a cancelled earlier invoice a new idempotency key.

`--yes` requires a planned date from today through 14 days ahead and refuses past dates; the preview prints these rules without creating a quote. The yardmaster waits until the planned day before the live create. A transfer is never retried within a run, including after a lost connection. A read-back failure prints the transfer id so it can be checked in the UI before rerunning.

An unfunded transfer's rate is held for about 24–48 hours and then floats; the transfer is cancelled automatically after about 14 days. Benchi alone funds it in the Wise web UI. No live Wise request is part of the tests.
