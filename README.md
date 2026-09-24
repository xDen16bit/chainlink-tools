
## EA Update tool

Python script that pulls the latest External Adapter (EA) versions from the
[external-adapters-js](https://github.com/smartcontractkit/external-adapters-js)
releases and updates the adapter image tags in a compose-style yaml.

### How it works

For a given release version, the script:

1. Resolves the release tag — when `Latest` is passed, it scrapes the
   [releases page](https://github.com/smartcontractkit/external-adapters-js/releases)
   for the latest stable version.
2. Fetches that tag's `MASTERLIST.md` from GitHub and parses the Name/Version
   table into a map of `<adapter>-adapter -> version`.
3. Reads the `services` block of the target yaml and, for each service image,
   compares the current tag against the latest adapter version.
4. Before proposing an update, verifies that the new image tag actually exists
   in AWS public ECR (`public.ecr.aws/chainlink/adapters/<image>:<version>`),
   authenticating with a bearer token when the registry requires one.

### Two variants

Both scripts take the same arguments and produce the same output; they only
differ in how step 4 talks to the registry:

- `eaupdate.py` — queries the registry v2 API directly with `requests` and
  handles the bearer-token challenge itself. No external binaries needed.
- `eaupdate-skopeo.py` — shells out to `skopeo inspect --raw docker://<image>`
  and lets skopeo deal with auth, retries and manifest negotiation. Requires
  `skopeo` in `PATH` (`apt install skopeo` / `dnf install skopeo` /
  `brew install skopeo`); the script exits early with an install hint when it
  is missing. A tag is treated as absent only when skopeo reports a
  missing-manifest error — any other failure (network, TLS, rate limit) is
  printed as a warning and the image is left untouched.

Each service is then sorted into one of three buckets and reported:

- **Update** — a newer version exists and the ECR image was found; these are
  the images that will be rewritten in the yaml.
- **Skipped** — a newer version exists but the corresponding image was not
  found in ECR, so it is left untouched.
- **Up to date** — image is already current, has no image field, or is not a
  known adapter.

Only images in the **Update** bucket are written back to the yaml.

### Report

The two buckets that need attention are printed as aligned tables; everything
already current is collapsed into a single line, so a 40-service stack reports
in ~15 lines instead of several hundred:

```
UPDATE — 5 services
  SERVICE        ADAPTER                CURRENT     NEW
  ────────────────────────────────────────────────────────
  amberdata      amberdata-adapter      1.8.23   →  1.8.24
  coingecko      coingecko-adapter      2.0.7    →  2.0.8
  coinmarketcap  coinmarketcap-adapter  1.9.0    →  2.0.11

SKIPPED — 3 services, tag not published to ECR
  SERVICE        ADAPTER                CURRENT     NEW
  ────────────────────────────────────────────────────────
  dxfeed         dxfeed-adapter         2.0.4    →  2.0.5

UP TO DATE — 15 services
  coinapi, coinpaprika, cryptocompare, ea-gateway, finage, grafana, nginx
  nomics, openexchangerates, postgres, prometheus, redis (+3 more)

23 services   5 update   3 skip   15 up to date   15.7s
```

In the `NEW` column only the part of the version that actually changed is lit
up in green, so a patch bump (`2.0.` **`8`**), a minor one (`2.` **`1.5`**) and
a major one (**`2.0.11`**) are told apart at a glance. Table columns follow the
terminal width — wide terminals show full names, narrow ones shorten the
longest columns with an ellipsis, and the two tables always share one layout.

### Machine-readable output

`--json` prints the full report to stdout as JSON and moves every human-facing
line (progress, warnings, the prompt) to stderr, so it pipes cleanly:

```bash
./eaupdate.py Latest ea.yaml False --json | jq '.update'
```

The document carries `release`, `yaml_file`, `written`, a `summary` block with
the per-bucket counts, the `update` / `skip` / `retain` maps keyed by service
name, and `replace_strings` (the exact image references that were, or would be,
rewritten).

### Progress output

Registry lookups take a second or two per image, so both scripts report what
they are doing while they run: the release tag being resolved, the number of
adapters parsed out of `MASTERLIST.md`, and then two live lines — the service
being checked right now and an overall percentage bar under it:

```
[12/42] coingecko: looking up coingecko-adapter:2.0.8 in ECR
█████████░░░░░░░░░░░░░░░░░░░░░░░  28.6% (12/42)
```

Both lines are redrawn in place and erased when the run ends, followed by the
elapsed time. Output is colored by meaning: stage messages cyan, the retained /
updatable / skipped summaries blue, green and yellow, warnings yellow, errors
red.

Colors are dropped automatically when the output is not a terminal, when
`TERM=dumb`, or when [`NO_COLOR`](https://no-color.org) is set. Without a
terminal the live lines degrade to one plain line per service
(`[12/42]  28.6% coingecko`), so CI logs keep the full trace. Warnings and
errors never overwrite the status lines.

### Install requirements
```bash
pip install -r requirements.txt
```

### Run the tool

To update for example ea.yaml automatically, run the script `eaupdate.py` with arguments as follows:

```bash
./eaupdate.py Latest ea.yaml Confirm
```

or, using skopeo for the ECR check:

```bash
./eaupdate-skopeo.py Latest ea.yaml Confirm
```

The script expects 2 required arguments and 1 optional argument:

1. Release version of https://github.com/smartcontractkit/external-adapters-js/releases .
   When `Latest` is provided, it will get the latest stable version automatically
   (e.g. `Latest`, `v1.79.0`, `v1.80.0`).
2. Yaml file to update.
3. *(optional)* How to handle the yaml file when updates are found:
   overwrite automatically (`True`), don't overwrite at all (`False`), or ask
   before writing (`Confirm`). Defaults to `Confirm` when not provided.

`--json` may be passed anywhere in the arguments; see *Machine-readable output*.
