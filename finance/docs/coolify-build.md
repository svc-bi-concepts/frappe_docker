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

## Isolated dev resource

The image build and baked gate above are owned by erp-ve60 / IT yard-x1sn.
The dev resource configuration does not build or publish an image.

For the dev resource, IT must build **main containing both the finance target
and `391fadb5`**, the `disable_external_integrations` implementation. Verify
source ancestry before building:

```sh
git merge-base --is-ancestor 391fadb5 <main-build-source-sha>
```

Run the baked offline gate as that build's instructions specify. Publish the
successful image to IT's registry and obtain the **registry manifest digest**
from the build/push result (or `docker buildx imagetools inspect <registry-image>:<build-tag>`).
Do not use the local Docker image ID. Record source SHA plus digest privately.
Set `FINANCE_DEV_IMAGE=<registry-image>@sha256:<64 hex digits>` in Coolify;
a tag-only value is rejected by dev init. The runtime host must be able to pull
the image before isolated containers start. No image is built on the mini.

Workers (`scheduler`, `queue-long`, `queue-short`) stay off by default: they sit
behind the dev overlay's `workers` profile. Do not enable it as part of the
image step; it needs Benchi's yes first.

After the image is available, follow the
[dev handoff](hetzner-dev-handoff.md#1-compose-branch-and-image) for the standalone
Compose selection and isolation requirements, and its
[startup contract](hetzner-dev-handoff.md#2-off-switches-before-first-application-start)
for required runtime environment values, init ordering and redeployment after
restores. The handoff also owns volume preparation, restore authorization,
additional DB off switches and cleanup.
