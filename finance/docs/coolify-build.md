# Finance image build and gate on Coolify dev

Build the PR head with repository-root context, Dockerfile
`images/custom/Containerfile`, and target `finance`. This target includes the
patched Frappe/ERPNext, erpnextswiss and HRMS base plus bi_finance and bi_payroll.
The default target remains the upstream base image. Frappe defaults to
`v16.51.1`; `NODE_OPTIONS` defaults to `--max-old-space-size=900`.

Coolify builds read `finance/apps.json` from the repository by default.
To override it, provide raw apps-list JSON as a **BuildKit secret named `APPS_JSON`**.
A normal Docker build argument or runtime environment variable is insufficient;
never declare `ARG APPS_JSON` or pass its value as `--build-arg`. In Coolify, add `APPS_JSON` with Build time available and Runtime unavailable,
then select **Build secrets > Docker BuildKit secrets**. BuildKit must be
available; its fallback to build arguments is not suitable here. See
[Coolify's build variable settings](https://coolify.io/docs/applications/configuration/environment-variables).
Configure the builder to forward that secret, and disable layer-cache reuse for every
run: BuildKit secrets do not invalidate the app installation layer's cache.
The legacy `apps_json` secret name remains supported for existing callers.

Equivalent build command on the dev builder (not the mini):

```sh
docker buildx build --no-cache --load \
  --secret id=APPS_JSON,src=finance/apps.json \
  --target finance --file images/custom/Containerfile \
  --tag finance-gate:pr-head .
```

`finance/apps.json` contains only public repository URLs, tags, and a revision
pin, with no credentials or company data. When `APPS_JSON` is absent or empty,
the legacy `apps_json` secret is used if nonempty, then the repository file.
The app list is never passed through a build argument or environment variable.
The fetched pinned app revision is verified before patches are applied. Avoid credential-bearing URLs: fetching apps inherently retains
source code and package metadata in the resulting image.

Run the baked gate with no checkout mount, database, site or published port:

```sh
docker run --rm --network none --entrypoint sh finance-gate:pr-head \
  /home/frappe/frappe-bench/finance/scripts/gate-tests.sh
```

The entrypoint runs the modules in `finance/scripts/gate-modules.sh` with the
bench virtualenv, prints unittest counts to stderr and a short PASS/FAIL summary to stdout, and
returns the failing status. `.yardr/check` uses the same runner and manifest,
mounted alongside its app checkout so older gate images still work. Its Docker
step remains mandatory until IT's replacement PR check is live.

The local `finance/scripts/build-image.sh live|copy <tag>` delegates to the same
`finance` target, retaining the capped-builder and existing-tag guards.
No GitHub Actions workflow is added or required for this build.

Finance's handoff is bead **erp-ve60**. IT's **yard-x1sn** starts after merge
(initial coordination: **yard-7yt7**); notify yardmaster-chris@it on merge. IT owns
the Coolify resource, per-PR-head trigger and SHA-bound result check. No image
build runs on the mini; static tests and `.yardr/check-host.sh` cover this
change there. IT's first Coolify dev run must prove the full image build and
baked test run before enabling the replacement check. Production deployment
and image pinning belong to erp-mnkv.

## Three results in one dev gate run (erp-s4c6 / yard-x1sn)

IT runs the second entrypoint on the isolated **dev gate runner**, from the
exact PR-head checkout used to build the image. It runs all three checks,
including those after a failure. It does not install tools into the `finance`
production image. No GitHub Actions enablement or production deployment is
involved.

Prerequisites match the workflow contracts:

- `python3` uses the Python version and `jinja2` dependency selected in
  `.github/workflows/finance-tests.yml`. The checkout includes Git metadata
  (the host gate uses `git grep`) and all tracked files.
- `pre-commit` is installed in a separate environment using the Python version
  selected in `.github/workflows/lint.yml`, with its executable on PATH. Keep `python` and
  `python3` on PATH for system hooks and the host tests, respectively.
- Git, OpenSSL, Go 1.x, Docker CLI with Compose v2, and access to the dev
  runner's existing rootless daemon are available. Pre-commit needs network
  access to install the hooks from `.pre-commit-config.yaml` and fetch the
  configured hadolint image. All hooks run unchanged; none are skipped.
- The runner has its own writable HOME, pre-commit cache and scratch directory.
  No production credentials, private exports, site volumes, published ports,
  privileged containers or Docker socket mounts are needed. Host Compose
  checks only render configurations; they start no stack.

After building target `finance` from that checkout, resolve its immutable image
digest and run on the dev runner (paths and digest are supplied by IT):

```sh
python3 finance/scripts/dev-gate.py \
  --image 'registry.example.test/finance@sha256:<built-image-digest>' \
  --results /run-specific-scratch/gate-results.json
```

Replace the example digest before executing the command. The results
path belongs to this run; a previous file is cleared before any check starts.
Relative result paths resolve from the caller's directory. Checks always run
from the entrypoint's checkout root. Do not run this inside the finance image:
the host checks and Docker-backed lint belong on the dev runner, without
mounting a daemon socket into a container.

| Result name    | Command                                                                                                          | Exit code                  |
| -------------- | ---------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `frappe-tests` | `docker run --rm --network none --entrypoint sh <image> /home/frappe/frappe-bench/finance/scripts/gate-tests.sh` | Baked runner's exit status |
| `host-tests`   | `sh .yardr/check-host.sh`                                                                                        | Host gate's exit status    |
| `lint`         | `pre-commit run --color=always --all-files` with `GO111MODULE=on`                                                | Pre-commit's exit status   |

Each result is an object with `name`, integer `exit_code` and a short `summary`
(`PASS (exit 0)` or `FAIL (exit N)` for completed commands;
`FAIL (<OS error>)` when a command cannot be launched). The JSON file is an array, updated after
each completed check. The same object is printed as a `dev-gate-result: ` JSON
line in stdout. Test counts and hook details stream to the run log. Missing
executables report 127, other launch errors 126, and signals use 128 plus the
signal number. The entrypoint exits **0 only when all three pass**, otherwise
**1**; invocation/output errors also fail the command. A missing or incomplete
array, failed build, interrupted run or missing result is never success. Lint
clears an inherited `SKIP` so an operator environment cannot omit hooks.

IT's poster must validate exactly these three unique names, require all exit
codes to be zero, and bind the checkout SHA and built image digest to the PR
head. One green `frappe-tests` result cannot stand in for either other result.
The existing `finance/frappe-gate` status and the two bridge rows remain in
place until IT proves the same-SHA comparison and switches their posting.
This bead supplies execution/results, not the Coolify resource or poster.
No new artifacts are published: retain the JSON and combined run log with
the existing private gate evidence, under IT's retention policy.

The separate local `private/check-real-amounts.sh` remains required before
push; it is a privacy check, not one of these workflow jobs. Its private data
is never copied to the dev runner. A real hosted build and three-job run are
verified by IT; local regression tests exercise the entrypoint with isolated
CLI stand-ins.
