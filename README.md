
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

Each service is then sorted into one of three buckets and reported:

- **Retain** — image is already up to date, has no image field, or is not a
  known adapter.
- **Update** — a newer version exists and the ECR image was found; these are
  the images that will be rewritten in the yaml.
- **Skip (no ECR image)** — a newer version exists but the corresponding image
  was not found in ECR, so it is left untouched.

Only images in the **Update** bucket are written back to the yaml.

### Install requirements
```bash
pip install -r requirements.txt
```

### Run the tool

To update for example ea.yaml automatically, run the script `eaupdate.py` with arguments as follows:

```bash
./eaupdate.py Latest ea.yaml Confirm
```

The script expects 2 required arguments and 1 optional argument:

1. Release version of https://github.com/smartcontractkit/external-adapters-js/releases .
   When `Latest` is provided, it will get the latest stable version automatically
   (e.g. `Latest`, `v1.79.0`, `v1.80.0`).
2. Yaml file to update.
3. *(optional)* How to handle the yaml file when updates are found:
   overwrite automatically (`True`), don't overwrite at all (`False`), or ask
   before writing (`Confirm`). Defaults to `Confirm` when not provided.
