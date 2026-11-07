#!/bin/sh
# Shared image-test manifest. Sourcing sets the runner's positional arguments.
set -- \
  bi_finance.test_payment_run \
  bi_finance.test_treasury \
  bi_finance.test_bank_list \
  bi_finance.test_bank_statement_upload \
  bi_finance.test_cash_flow_forecast \
  bi_finance.test_cash_position \
  bi_finance.test_paypal \
  bi_finance.test_paypal_sync \
  bi_finance.test_external_integrations \
  bi_finance.test_qrbill \
  bi_finance.test_setup \
  bi_finance.test_wise \
  bi_finance.test_hrms_accrual_filter
