#!/usr/local/bin/av inject +FINANCE_SP_CERT_KEY +FINANCE_SP_BROKER_TOKEN /bin/sh
# shellcheck shell=sh
# shellcheck disable=SC2096
# Changes to this blessed launcher require a new av blessing.
exec /usr/bin/python3 -I /Users/bsaladin/ws_yardr_finance/frappe_docker/finance/sharepoint/broker.py
