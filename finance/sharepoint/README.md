# Finance SharePoint broker

The local broker on `127.0.0.1:8857` lets Yardr read the configured SharePoint
folders and upload work output. Its Graph client holds the certificate key in
memory and pins every Graph write to the configured output folder.

| Area        | Folder                          | Access         |
| ----------- | ------------------------------- | -------------- |
| `out`       | Shared Drive/Finance/ws_finance | read and write |
| other areas | the config's `folders`          | read only      |

Calls are `ping`, `ls`, `get_file`, and `put_file`. `get_file` writes into
`work/inbox/<area>/`. `put_file` reads only files resolved inside
`work/out/`; files up to 4 MiB use a simple upload and larger files use a
10 MiB upload session. Existing output files require `replace=true`. Uploads
are limited to 250 MiB. Office Open XML files may be rewritten by SharePoint;
the answer reports `sharepoint_modified` if its size changes.

## Operator steps (Benchi)

Run these steps from your terminal after IT has supplied the output folder ids.
The builder does not have those ids and has not made a live Graph request.

1. Add the output entry to `/Users/bsaladin/ws_yardr_finance/private/graph-cert/sharepoint.json` using the ids IT mailed: `"out": {"drive_id": "…", "item_id": "…"}`. Keep the file mode 600.
2. Create the broker token with `openssl rand -hex 32`, then save it with `av save --project-directory="$HOME/ws_yardr_finance" FINANCE_SP_BROKER_TOKEN` and type the token into the prompt. Scope both secrets: `~/ws_yardr/integrations/av-yard-scope.sh finance FINANCE_SP_BROKER_TOKEN FINANCE_SP_CERT_KEY`.
3. Bless and endorse the launcher: `av bless --endorse-launcher /Users/bsaladin/ws_yardr_finance/frappe_docker/finance/sharepoint/sp-broker-launch.sh`.
4. Create private work folders: `install -d -m 700 /Users/bsaladin/ws_yardr_finance/work/out /Users/bsaladin/ws_yardr_finance/work/inbox`.
5. Install the LaunchAgent and start it: `cp /Users/bsaladin/ws_yardr_finance/frappe_docker/finance/sharepoint/dev.finance.sp-broker.plist ~/Library/LaunchAgents/` then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.finance.sp-broker.plist`.
6. Write the Yardr source credential file through Vault injection: `av inject +FINANCE_SP_BROKER_TOKEN sh -c 'install -d -m 700 /Users/bsaladin/ws_yardr_finance/.yardr/sources && umask 177 && printf "SP_BROKER_TOKEN=%s\n" "$FINANCE_SP_BROKER_TOKEN" > /Users/bsaladin/ws_yardr_finance/.yardr/sources/sharepoint.env'`.
7. Register the source and grants: `sh /Users/bsaladin/ws_yardr_finance/frappe_docker/finance/sharepoint/install-source.sh`.
8. Check the listener and log with `lsof -nP -iTCP:8857 -sTCP:LISTEN` and `rg -n 'human approval required' /Users/bsaladin/ws_yardr_finance/.yardr/sp-broker.log`, then run `/Users/bsaladin/ws_yardr/yardr --home /Users/bsaladin/ws_yardr_finance/.yardr call sharepoint ping`.

To stop and remove the service: `launchctl bootout gui/$(id -u)/dev.finance.sp-broker && rm ~/Library/LaunchAgents/dev.finance.sp-broker.plist`.

After installation, the yardmaster and Benchi check `ping`, `ls` of `out`, a
`put_file` of a test file placed in `work/out/`, and refusal of `put_file` for
a file outside `work/out/`.

## Port

Port `8857` is assigned locally to the Finance SharePoint broker. IT maintains
the shared port registry.
