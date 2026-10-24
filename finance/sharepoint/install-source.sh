#!/bin/sh
set -e
Y="/Users/bsaladin/ws_yardr/yardr --home /Users/bsaladin/ws_yardr_finance/.yardr"
D=/Users/bsaladin/ws_yardr_finance/frappe_docker/finance/sharepoint
$Y source add "$D/sharepoint.source.yaml"
$Y source set sharepoint env_file=/Users/bsaladin/ws_yardr_finance/.yardr/sources/sharepoint.env
$Y source check sharepoint
$Y source grant sharepoint read --group erp-builders
$Y source grant sharepoint put_file --group erp-builders
$Y source grant sharepoint read --group erp-reviewers
$Y source grant sharepoint read --crew yardmaster
$Y source grant sharepoint put_file --crew yardmaster
echo "sharepoint source ready"
