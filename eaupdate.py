#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
import json
import yaml
import sys
import re
from urllib.parse import urlparse, parse_qs

REQUEST_TIMEOUT = 10
ECR_REGISTRY = "public.ecr.aws"
ECR_REPOSITORY_PREFIX = "chainlink/adapters"


def extract_name(name):
    match = re.match(r'\[([^\]]+)\]\([^)]+\)', name)
    if match:
        return match.group(1)
    return name.strip()


def extract_version(version):
    match = re.match(r'`([^`]+)`', version)
    if match:
        return match.group(1)
    return version.strip()


def get_latest_tag_version():
    url = "https://github.com/smartcontractkit/external-adapters-js/releases"
    page = requests.get(url, timeout=REQUEST_TIMEOUT)
    page.raise_for_status()

    soup = BeautifulSoup(page.content, "html.parser")

    for section in soup.select("div#repo-content-pjax-container section"):
        section_title_el = section.select_one("h2.sr-only")
        if not section_title_el:
            continue

        section_title = section_title_el.text.strip().split(" ")[1]
        is_latest = section.select("span.Label.Label--success.Label--large")
        if len(is_latest) > 0:
            return section_title

    sys.exit(
        "Could not find the latest version of adapters on the release page: "
        "https://github.com/smartcontractkit/external-adapters-js/releases"
    )


def get_adapter_versions(tag):
    url = f"https://raw.githubusercontent.com/smartcontractkit/external-adapters-js/{tag}/MASTERLIST.md"
    page = requests.get(url, timeout=REQUEST_TIMEOUT)

    if page.status_code != 200:
        print("Failed to fetch", url)
        sys.exit(1)

    md_text = BeautifulSoup(page.content, "html.parser").get_text()

    pattern = r'\|\s*Name\s*\|\s*Version'
    match = re.search(pattern, md_text, re.IGNORECASE)

    if not match:
        print("Unable to find the expected Markdown table in", url)
        sys.exit(1)

    md_table = md_text[match.start():]

    json_table = []
    header = []

    for n, line in enumerate(md_table.splitlines()):
        if not line.strip().startswith("|"):
            continue

        if n == 0:
            header = [t.strip() for t in line.split('|') if t.strip()]
            if not header:
                print("Unable to parse the header from the markdown table at", url)
                sys.exit(1)
            continue

        if re.match(r'^\|\s*-+', line):
            continue

        values = [t.strip() for t in line.split('|')[1:-1]]
        if len(values) != len(header):
            continue

        row = {}
        for col, value in zip(header, values):
            row[col] = value
        json_table.append(row)

    adapter_versions = {}
    for row in json_table:
        name = extract_name(row.get("Name", ""))
        version = extract_version(row.get("Version", ""))

        if name and version:
            adapter_versions[f"{name}-adapter"] = version

    return adapter_versions


def parse_image_reference(image):
    """
    Parse docker image reference:
      repo/image:tag
      registry/repo/image:tag
    Returns:
      {
        "full": original,
        "registry": optional registry or None,
        "repository": repository path without tag,
        "image_name": last repository segment,
        "tag": tag
      }
    """
    if ":" not in image:
        raise ValueError(f"Image '{image}' does not contain a tag")

    repository, tag = image.rsplit(":", 1)
    image_name = repository.split("/")[-1]

    registry = None
    first_segment = repository.split("/")[0]
    if "." in first_segment or ":" in first_segment:
        registry = first_segment

    return {
        "full": image,
        "registry": registry,
        "repository": repository,
        "image_name": image_name,
        "tag": tag,
    }


def get_bearer_token_from_www_authenticate(www_authenticate):
    """
    Parse header like:
    Bearer realm="https://public.ecr.aws/token/",service="public.ecr.aws",scope="aws"
    """
    if not www_authenticate or not www_authenticate.startswith("Bearer "):
        return None

    auth_fields = {}
    for item in www_authenticate[len("Bearer "):].split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        auth_fields[key.strip()] = value.strip().strip('"')

    realm = auth_fields.get("realm")
    if not realm:
        return None

    params = {k: v for k, v in auth_fields.items() if k != "realm"}
    resp = requests.get(realm, params=params, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return None

    data = resp.json()
    return data.get("token") or data.get("access_token")


def ecr_manifest_exists(repository, tag):
    """
    Checks if an image tag exists in public ECR:
      public.ecr.aws/<repository>:<tag>
    """
    manifest_url = f"https://{ECR_REGISTRY}/v2/{repository}/manifests/{tag}"
    headers = {
        "Accept": ",".join([
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        ])
    }

    response = requests.get(manifest_url, headers=headers, timeout=REQUEST_TIMEOUT)

    if response.status_code == 200:
        return True

    if response.status_code == 401:
        token = get_bearer_token_from_www_authenticate(
            response.headers.get("WWW-Authenticate")
        )
        if token:
            headers["Authorization"] = f"Bearer {token}"
            retry = requests.get(manifest_url, headers=headers, timeout=REQUEST_TIMEOUT)
            return retry.status_code == 200

    if response.status_code == 404:
        return False

    print(
        f"Warning: unexpected response while checking "
        f"{ECR_REGISTRY}/{repository}:{tag} -> HTTP {response.status_code}"
    )
    return False


def build_ecr_image(image_name, version):
    return f"{ECR_REGISTRY}/{ECR_REPOSITORY_PREFIX}/{image_name}:{version}"


def get_updates(yaml_file, adapter_versions):
    response = {
        "replace_strings": {},
        "to_update_image_versions": {},
        "to_retain_image_versions": {},
        "skipped_no_ecr_image": {},
        "services": {
            "retain": {},
            "update": {},
            "skip_no_ecr_image": {},
        },
    }

    with open(yaml_file, "r") as file:
        data = yaml.safe_load(file)

    services = data.get("services", {})
    if not isinstance(services, dict):
        print(f"Unable to find 'services' in {yaml_file}")
        sys.exit(1)

    for service_name, service in services.items():
        image = service.get("image")
        if not image:
            response["services"]["retain"][service_name] = "no-image-field"
            continue

        try:
            parsed = parse_image_reference(image)
        except ValueError as exc:
            print(f"Skipping service '{service_name}': {exc}")
            response["services"]["retain"][service_name] = "invalid-image"
            continue

        image_name = parsed["image_name"]
        image_version = parsed["tag"]

        if image_name not in adapter_versions:
            response["to_retain_image_versions"][image_name] = image_version
            response["services"]["retain"][service_name] = image_version
            continue

        new_version = adapter_versions[image_name]

        if new_version == image_version:
            response["to_retain_image_versions"][image_name] = image_version
            response["services"]["retain"][service_name] = image_version
            continue

        ecr_repository = f"{ECR_REPOSITORY_PREFIX}/{image_name}"
        ecr_exists = ecr_manifest_exists(ecr_repository, new_version)

        if not ecr_exists:
            response["skipped_no_ecr_image"][image_name] = {
                "current": image_version,
                "new": new_version,
                "expected_ecr_image": build_ecr_image(image_name, new_version),
            }
            response["services"]["skip_no_ecr_image"][service_name] = {
                "current": image_version,
                "new": new_version,
            }
            continue

        new_image = build_ecr_image(image_name, new_version)

        response["to_update_image_versions"][image_name] = {
            "current": image_version,
            "new": new_version,
            "new_image": new_image,
        }
        response["replace_strings"][image] = new_image
        response["services"]["update"][service_name] = {
            "current": image_version,
            "new": new_version,
            "new_image": new_image,
        }

    return response


def confirm_update(yaml_file):
    while True:
        do_update = input(f"Do you want to update the stack '{yaml_file}' file? (yes/no) ").strip()
        if do_update in {"yes", "no"}:
            return do_update == "yes"


def save_updated_yaml(yaml_file, replace_strings):
    with open(yaml_file, "r") as f:
        content = f.read()

    for target, replacement in replace_strings.items():
        content = content.replace(target, replacement)

    with open(yaml_file, "w") as f:
        f.write(content)

    print(f"'{yaml_file}' file updated")


if __name__ == "__main__":
    if not (3 <= len(sys.argv) <= 4):
        print("Missing expected arguments.")
        print(
            "Usage: ./eaupdate.py "
            "[version:Latest/v1.79.0/v1.80.0] "
            "[yaml-file-to-update:ea-rpc-composite-por.yml/ea-source-adapters.yml] "
            "[(optional) update-stack-file:True/False/Confirm]"
        )
        sys.exit(1)

    tag_version = sys.argv[1]
    yaml_file = sys.argv[2]
    update_file = sys.argv[3] if len(sys.argv) == 4 else "Confirm"

    if tag_version == "Latest":
        tag_version = get_latest_tag_version()
        print(f"Using adapter release version {tag_version}")

    adapter_versions = get_adapter_versions(tag_version)

    if update_file not in {"True", "False", "Confirm"}:
        print("Argument for update stack file must be either True, False, or Confirm")
        sys.exit(1)

    response = get_updates(yaml_file, adapter_versions)

    total_services = (
        len(response["services"]["update"])
        + len(response["services"]["retain"])
        + len(response["services"]["skip_no_ecr_image"])
    )

    print(f"Retained images: {len(response['to_retain_image_versions'])} from {total_services} services")
    print(json.dumps(response["to_retain_image_versions"], indent=4, sort_keys=True))

    print(f"Updatable images in AWS ECR: {len(response['to_update_image_versions'])} from {total_services} services")
    print(json.dumps(response["to_update_image_versions"], indent=4, sort_keys=True))

    print(f"Skipped because image/tag not found in AWS ECR: {len(response['skipped_no_ecr_image'])}")
    print(json.dumps(response["skipped_no_ecr_image"], indent=4, sort_keys=True))

    if len(response["to_update_image_versions"]) > 0 and update_file == "Confirm":
        if confirm_update(yaml_file):
            update_file = "True"
        else:
            update_file = "False"

    if len(response["to_update_image_versions"]) > 0 and update_file == "True":
        save_updated_yaml(yaml_file, response["replace_strings"])
