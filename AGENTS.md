# Finance repo: local operations

- **Colima sleeps** after about 10 idle minutes; its Docker socket exists only while it runs.
  Before Docker, run `/Users/bsaladin/ws_yardr/integrations/bin/colima-wake --hold <min> --reason <what>`;
  hold longer than the job, then `export DOCKER_HOST=unix:///Users/bsaladin/.colima/default/docker.sock`.
  A missing `/var/run/docker.sock` is not an outage. Never stop or restart Colima.
  Until erp-xfem lands, `.yardr/check` also needs this explicit wake and socket setup.
- **CI is local; GitHub Actions is off on purpose.** IT's ci-bridge runs via launchd every 5 minutes.
  Its log is `/Users/bsaladin/ws_yardr/integrations/state/ci-bridge.log`.
  It posts `finance tests / host-tests` and `Lint / lint` commit statuses on main and open PRs.
  Read them with `gh pr checks <n>`. Empty means pending, never a pass.
  If nothing appears after about 30 minutes, record the last log lines and hold the bead.
  Repeated `another bridge run is active` with no bridge process is IT's problem:
  report it; do not touch the lock.
- **`finance/frappe-gate` is separate.** IT posts it from Coolify dev using
  `/Users/bsaladin/ws_yardr/integrations/bin/finance-gate-run`, only on request.
  It is not a required check yet.
- **The real-amounts guard runs before every push:**
  `sh /Users/bsaladin/ws_yardr_finance/private/check-real-amounts.sh`.
  It is already part of the depot test command.
