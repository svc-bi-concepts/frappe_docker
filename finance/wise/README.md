# Wise transfer preparation

`prepare_wise_transfer.py` lists recipients matching `--find-recipient NAME` and/or `--find-iban IBAN`, or prepares one unfunded transfer to an existing, active recipient in the single business profile. Search reads the profile's account list, so a recipient Benchi has just added in the Wise UI is found before its first payment; it prints the numeric id, name, IBAN and the count of completed transfers. Transfer preparation refuses unknown recipients, a recipient without completed transfer history unless verified with `--first-payment`, duplicate invoice references, unsupported details, and, with `--yes`, dates outside the allowed window. It never funds, cancels, creates or changes recipients, or writes to bexio or ERPNext.

For a recipient with no transfer history, use `--first-payment` with `--expect-iban` and `--expect-name` copied from the invoice, plus `--source-currency`. The script compares the Wise account details with those invoice values and refuses any mismatch; this mode also refuses recipients that already have a completed transfer. Benchi adds the recipient in the Wise UI first. The usual preview and `--yes` safeguards still apply.

Find a recipient with a partial name match (case and repeated whitespace are ignored), or by its exact IBAN (spaces and case ignored); both options together must both match:

```sh
finance/wise/prepare_wise_transfer.py --find-recipient "Example Ltd"
finance/wise/prepare_wise_transfer.py --find-iban "ZZ00 TEST 0000 0000 0000 1"
```

`--recipient-id` takes the numeric account id or the UUID from the recipient URL of the Wise web app (`wise.com/recipients/<UUID>`). A UUID is resolved through the profile's account list to the numeric id: only a top-level `uuid`, `publicId` or `id` field of an account counts, never a nested one, which the script prints and uses; one that matches no account, or more than one, is refused with a pointer to `--find-recipient` and `--find-iban`.

Run a GET-only preview first:

```sh
finance/wise/prepare_wise_transfer.py --recipient-id 123 --invoice INV-2026-01 --amount 1081.00 --target-currency EUR --planned-date 2026-11-10
```

After comparing the printed recipient name and account summary with the invoice, the yardmaster may add `--yes` on or after the planned date. The script creates a recipient-first quote using BALANCE pricing, checks transfer requirements, creates one transfer, and reads it back. `--detail KEY=VALUE` supplies the supported conditional purpose fields; `--attempt 2` gives a cancelled earlier invoice a new idempotency key.

`--yes` requires a planned date from today through 14 days ahead and refuses past dates; the preview prints these rules without creating a quote. The yardmaster waits until the planned day before the live create. A transfer is never retried within a run, including after a lost connection. A read-back failure prints the transfer id so it can be checked in the UI before rerunning.

An unfunded transfer's rate is held for about 24–48 hours and then floats; the transfer is cancelled automatically after about 14 days. Benchi alone funds it in the Wise web UI. No live Wise request is part of the tests.
