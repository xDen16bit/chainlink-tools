#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
import json
import yaml
import sys
import re
import os
import time
import shutil
import subprocess

REQUEST_TIMEOUT = 10
ECR_REGISTRY = "public.ecr.aws"
ECR_REPOSITORY_PREFIX = "chainlink/adapters"

SKOPEO_BINARY = "skopeo"
SKOPEO_TIMEOUT = 60
SKOPEO_RETRY_TIMES = 2

# skopeo exits non-zero both for "image is not there" and for real errors
# (network, tls, rate limit), so the stderr text is what tells them apart.
IMAGE_NOT_FOUND_MARKERS = (
    "manifest unknown",
    "manifest for",
    "name unknown",
    "repository name not known",
    "was not found",
    "404 (not found)",
    "requested access to the resource is denied",
)

MIN_OUTPUT_WIDTH = 80
MIN_COLUMN_WIDTH = 8
COLUMN_GAP = 2


# --- output plumbing ---------------------------------------------------------

# Everything meant for a human goes to STREAM. With --json that is stderr, so
# stdout carries nothing but the report and stays pipeable into jq.
STREAM = sys.stdout
COLOR_ENABLED = False

BOLD, DIM, RED, GREEN, YELLOW, BLUE, CYAN = "1", "2", "31", "32", "33", "34", "36"
CLEAR_LINE = "\033[K"
CURSOR_UP = "\033[A"

# Replaced with the nicer glyphs by configure_output() when the encoding allows.
BAR_FULL, BAR_EMPTY, ARROW, RULE, DASH, ELLIPSIS = "#", "-", "->", "-", "-", "..."


def configure_output(json_mode):
    """Decide where human-readable output goes, and how fancy it may look."""
    global STREAM, COLOR_ENABLED
    global BAR_FULL, BAR_EMPTY, ARROW, RULE, DASH, ELLIPSIS

    STREAM = sys.stderr if json_mode else sys.stdout
    COLOR_ENABLED = (
        STREAM.isatty()
        and os.environ.get("TERM") != "dumb"
        and not os.environ.get("NO_COLOR")
    )

    try:
        "█░→─—…".encode(STREAM.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return

    BAR_FULL, BAR_EMPTY = "█", "░"
    ARROW, RULE, DASH, ELLIPSIS = "→", "─", "—", "…"


def say(message="", flush=False):
    print(message, file=STREAM, flush=flush)


def paint(text, *styles):
    if not COLOR_ENABLED or not styles:
        return str(text)
    return f"\033[{';'.join(styles)}m{text}\033[0m"


def output_width():
    if not STREAM.isatty():
        return MIN_OUTPUT_WIDTH
    columns = shutil.get_terminal_size((MIN_OUTPUT_WIDTH, 20)).columns - 1
    return max(MIN_OUTPUT_WIDTH, columns)


def plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def truncate(text, width):
    if len(text) <= width:
        return text
    if width <= len(ELLIPSIS):
        return text[:width]
    return text[:width - len(ELLIPSIS)] + ELLIPSIS


# --- progress ----------------------------------------------------------------

class Progress:
    """
    Shows what the script is doing while it works.

    On a terminal it keeps two live lines - the service being worked on right
    now and an overall percentage bar under it - redrawn in place. When the
    output is piped (CI, `| tee`) it falls back to one plain line per step,
    percentage included, so that nothing is lost in a log file.
    """

    def __init__(self, total):
        self.total = total
        self.current = 0
        self.interactive = STREAM.isatty() and os.environ.get("TERM") != "dumb"
        self._label = ""
        self._started = time.monotonic()
        self._live = False

    @property
    def percent(self):
        if not self.total:
            return 100.0
        return 100.0 * self.current / self.total

    def _bar(self, width):
        bar_width = max(10, min(32, width - 24))
        filled = int(round(bar_width * self.percent / 100.0))
        return (
            paint(BAR_FULL * filled, GREEN)
            + paint(BAR_EMPTY * (bar_width - filled), DIM)
            + paint(f" {self.percent:5.1f}%", BOLD)
            + paint(f" ({self.current}/{self.total})", DIM)
        )

    def _render(self):
        width = output_width()
        prefix = f"[{self.current}/{self.total}] "
        label = truncate(self._label, max(0, width - len(prefix)))

        if self._live:
            STREAM.write(CURSOR_UP)
        STREAM.write(
            "\r" + CLEAR_LINE + paint(prefix, DIM) + paint(label, BOLD)
            + "\n\r" + CLEAR_LINE + self._bar(width)
        )
        STREAM.flush()
        self._live = True

    def _clear(self):
        if self._live:
            STREAM.write("\r" + CLEAR_LINE + CURSOR_UP + "\r" + CLEAR_LINE)
            STREAM.flush()
            self._live = False

    def step(self, label):
        """Advance the counter and show what is being worked on."""
        self.current += 1
        self._label = label
        if self.interactive:
            self._render()
        else:
            say(f"[{self.current}/{self.total}] {self.percent:5.1f}% {label}", flush=True)

    def update(self, label):
        """Refine the current step's label without advancing the counter."""
        if self.interactive:
            self._label = label
            self._render()

    def log(self, message):
        """Print a message without leaving it mangled by the live lines."""
        self._clear()
        say(message, flush=True)

    def done(self):
        """Erase the live lines and report how long the run took."""
        self._clear()
        return time.monotonic() - self._started


# --- tables ------------------------------------------------------------------

def cell(text, *styles):
    """A table cell: its plain text (for widths) plus how it should look."""
    return (str(text), paint(text, *styles))


def version_cell(current, new):
    """Dim the part of the new version that did not change, light up the rest."""
    common = 0
    for old_part, new_part in zip(re.split(r"([.+-])", current), re.split(r"([.+-])", new)):
        if old_part != new_part:
            break
        common += len(old_part)

    unchanged = paint(new[:common], DIM) if common else ""
    return (new, unchanged + paint(new[common:], GREEN, BOLD))


def compute_widths(columns, rows, width, indent="  "):
    """
    Column widths that fit `width`, shrinking the flexible columns first.
    Shared by several tables so that they line up with each other.
    """
    widths = [
        max([len(title)] + [len(row[i][0]) for row in rows])
        for i, (title, _) in enumerate(columns)
    ]

    available = width - len(indent) - COLUMN_GAP * (len(columns) - 1)
    shrinkable = [i for i, (_, flexible) in enumerate(columns) if flexible]
    while sum(widths) > available and shrinkable:
        widest = max(shrinkable, key=lambda i: widths[i])
        if widths[widest] <= MIN_COLUMN_WIDTH:
            break
        widths[widest] -= 1

    return widths


def render_table(columns, rows, width, widths=None, indent="  "):
    """
    columns: (title, shrinkable) pairs; shrinkable ones give up room first.
    rows:    lists of cells as built by cell() / version_cell().
    """
    if widths is None:
        widths = compute_widths(columns, rows, width, indent)

    titles = [(truncate(title, w), truncate(title, w)) for (title, _), w in zip(columns, widths)]
    rule = min(sum(widths) + COLUMN_GAP * (len(widths) - 1), width - len(indent))

    say(indent + paint(_join(titles, widths), DIM, BOLD))
    say(indent + paint(RULE * rule, DIM))

    for row in rows:
        say(indent + _join(row, widths))


def _join(row, widths):
    parts = []
    for index, ((plain, styled), width) in enumerate(zip(row, widths)):
        if len(plain) > width:
            # Dropping the styling is the price of making an over-long value fit.
            plain = truncate(plain, width)
            styled = plain
        last = index == len(widths) - 1
        parts.append(styled if last else styled + " " * (width - len(plain)))
    return (" " * COLUMN_GAP).join(parts).rstrip()


def _wrap_names(names, width):
    lines, current = [], ""
    for name in names:
        candidate = name if not current else f"{current}, {name}"
        if len(candidate) > width and current:
            lines.append(current)
            current = name
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def format_name_list(names, width, max_lines=2):
    """
    Pack names onto at most max_lines lines, ending with '(+N more)'.

    Names are dropped whole rather than cut in half, so the tail never reads
    like a typo.
    """
    names = list(names)

    for shown in range(len(names), -1, -1):
        hidden = len(names) - shown
        suffix = f" (+{hidden} more)" if hidden else ""
        lines = _wrap_names(names[:shown], width)

        if len(lines) > max_lines:
            continue
        if not lines:
            return [suffix.strip()] if suffix else []
        if len(lines[-1]) + len(suffix) <= width:
            lines[-1] += suffix
            return lines

    return []


# --- release and adapter versions --------------------------------------------

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
        say(paint(f"Failed to fetch {url}", RED))
        sys.exit(1)

    md_text = BeautifulSoup(page.content, "html.parser").get_text()

    pattern = r'\|\s*Name\s*\|\s*Version'
    match = re.search(pattern, md_text, re.IGNORECASE)

    if not match:
        say(paint(f"Unable to find the expected Markdown table in {url}", RED))
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
                say(paint(f"Unable to parse the header from the markdown table at {url}", RED))
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


# --- ECR ---------------------------------------------------------------------

def ensure_skopeo_available():
    binary = shutil.which(SKOPEO_BINARY)
    if binary is None:
        sys.exit(
            f"'{SKOPEO_BINARY}' was not found in PATH. Install it first, for example:\n"
            "  Debian/Ubuntu: sudo apt install skopeo\n"
            "  RHEL/Fedora:   sudo dnf install skopeo\n"
            "  macOS:         brew install skopeo"
        )

    try:
        version = subprocess.run(
            [SKOPEO_BINARY, "--version"],
            capture_output=True,
            text=True,
            timeout=SKOPEO_TIMEOUT,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        version = ""

    say(paint(f"Using {version or binary}", DIM))


def skopeo_inspect(image_reference):
    """
    Runs `skopeo inspect --raw docker://<image>`.
    Returns (exit_code, stdout, stderr); exit code is None when skopeo timed out.
    """
    command = [
        SKOPEO_BINARY,
        "inspect",
        "--raw",
        "--retry-times", str(SKOPEO_RETRY_TIMES),
        f"docker://{image_reference}",
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SKOPEO_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"skopeo inspect timed out after {SKOPEO_TIMEOUT}s"

    return result.returncode, result.stdout, result.stderr


def ecr_manifest_exists(repository, tag, log=say):
    """
    Checks if an image tag exists in public ECR:
      public.ecr.aws/<repository>:<tag>
    """
    image_reference = f"{ECR_REGISTRY}/{repository}:{tag}"
    exit_code, stdout, stderr = skopeo_inspect(image_reference)

    if exit_code == 0 and stdout.strip():
        return True

    error = " ".join(stderr.split()).lower()

    if any(marker in error for marker in IMAGE_NOT_FOUND_MARKERS):
        return False

    log(paint(
        f"Warning: unexpected skopeo result while checking "
        f"{image_reference} -> exit {exit_code}: {stderr.strip() or 'no stderr output'}",
        YELLOW,
    ))
    return False


def build_ecr_image(image_name, version):
    return f"{ECR_REGISTRY}/{ECR_REPOSITORY_PREFIX}/{image_name}:{version}"


# --- the actual comparison ---------------------------------------------------

def get_updates(yaml_file, adapter_versions):
    response = {
        "replace_strings": {},
        "update": {},
        "skip": {},
        "retain": {},
        "elapsed": 0.0,
    }

    with open(yaml_file, "r") as file:
        data = yaml.safe_load(file)

    services = data.get("services", {})
    if not isinstance(services, dict):
        say(paint(f"Unable to find 'services' in {yaml_file}", RED))
        sys.exit(1)

    say(paint(f"Checking {plural(len(services), 'service')} from '{yaml_file}'", CYAN), flush=True)
    progress = Progress(len(services))

    for service_name, service in services.items():
        progress.step(service_name)

        image = service.get("image")
        if not image:
            response["retain"][service_name] = {
                "adapter": None, "current": None, "reason": "no-image-field",
            }
            continue

        try:
            parsed = parse_image_reference(image)
        except ValueError as exc:
            progress.log(paint(f"Skipping service '{service_name}': {exc}", YELLOW))
            response["retain"][service_name] = {
                "adapter": None, "current": None, "reason": "invalid-image",
            }
            continue

        image_name = parsed["image_name"]
        image_version = parsed["tag"]

        if image_name not in adapter_versions:
            response["retain"][service_name] = {
                "adapter": image_name, "current": image_version, "reason": "not-an-adapter",
            }
            continue

        new_version = adapter_versions[image_name]

        if new_version == image_version:
            response["retain"][service_name] = {
                "adapter": image_name, "current": image_version, "reason": "up-to-date",
            }
            continue

        ecr_repository = f"{ECR_REPOSITORY_PREFIX}/{image_name}"
        progress.update(f"{service_name}: looking up {image_name}:{new_version} in ECR")

        if not ecr_manifest_exists(ecr_repository, new_version, log=progress.log):
            response["skip"][service_name] = {
                "adapter": image_name,
                "current": image_version,
                "new": new_version,
                "expected_ecr_image": build_ecr_image(image_name, new_version),
            }
            continue

        new_image = build_ecr_image(image_name, new_version)
        response["replace_strings"][image] = new_image
        response["update"][service_name] = {
            "adapter": image_name,
            "current": image_version,
            "new": new_version,
            "new_image": new_image,
        }

    response["elapsed"] = progress.done()

    return response


# --- reporting ---------------------------------------------------------------

VERSION_COLUMNS = [
    ("SERVICE", True),
    ("ADAPTER", True),
    ("CURRENT", False),
    ("", False),
    ("NEW", False),
]


def _version_rows(bucket):
    return [
        [
            cell(service),
            cell(info["adapter"], DIM),
            cell(info["current"], DIM),
            cell(ARROW, DIM),
            version_cell(info["current"], info["new"]),
        ]
        for service, info in sorted(bucket.items())
    ]


def print_report(response):
    width = output_width()
    update, skip, retain = response["update"], response["skip"], response["retain"]

    update_rows = _version_rows(update)
    skip_rows = _version_rows(skip)
    # One set of widths for both tables, so the columns line up across sections.
    widths = compute_widths(VERSION_COLUMNS, update_rows + skip_rows, width)

    if update:
        say()
        say(paint(f"UPDATE {DASH} {plural(len(update), 'service')}", GREEN, BOLD))
        render_table(VERSION_COLUMNS, update_rows, width, widths)

    if skip:
        say()
        say(paint(
            f"SKIPPED {DASH} {plural(len(skip), 'service')}, tag not published to ECR",
            YELLOW, BOLD,
        ))
        render_table(VERSION_COLUMNS, skip_rows, width, widths)

    if retain:
        say()
        say(paint(f"UP TO DATE {DASH} {plural(len(retain), 'service')}", BLUE, BOLD))
        for line in format_name_list(sorted(retain), width - 2):
            say("  " + paint(line, DIM))

    total = len(update) + len(skip) + len(retain)
    say()
    say(
        f"{paint(total, BOLD)} services"
        + "   " + paint(f"{len(update)} update", GREEN)
        + "   " + paint(f"{len(skip)} skip", YELLOW)
        + "   " + paint(f"{len(retain)} up to date", DIM)
        + "   " + paint(f"{response['elapsed']:.1f}s", DIM)
    )


def build_json_report(response, release, yaml_file, written):
    return {
        "release": release,
        "yaml_file": yaml_file,
        "written": written,
        "summary": {
            "services": len(response["update"]) + len(response["skip"]) + len(response["retain"]),
            "update": len(response["update"]),
            "skip": len(response["skip"]),
            "retain": len(response["retain"]),
            "elapsed_seconds": round(response["elapsed"], 1),
        },
        "update": response["update"],
        "skip": response["skip"],
        "retain": response["retain"],
        "replace_strings": response["replace_strings"],
    }


# --- writing back ------------------------------------------------------------

def confirm_update(yaml_file):
    while True:
        print(
            paint(f"Do you want to update the stack '{yaml_file}' file? (yes/no) ", BOLD),
            end="", file=STREAM, flush=True,
        )
        do_update = input().strip()
        if do_update in {"yes", "no"}:
            return do_update == "yes"


def save_updated_yaml(yaml_file, replace_strings):
    with open(yaml_file, "r") as f:
        content = f.read()

    for target, replacement in replace_strings.items():
        content = content.replace(target, replacement)

    with open(yaml_file, "w") as f:
        f.write(content)

    say(paint(f"'{yaml_file}' file updated", GREEN, BOLD))


def usage():
    say(paint("Missing expected arguments.", RED))
    say(
        "Usage: ./eaupdate-skopeo.py "
        "[version:Latest/v1.79.0/v1.80.0] "
        "[yaml-file-to-update:ea-rpc-composite-por.yml/ea-source-adapters.yml] "
        "[(optional) update-stack-file:True/False/Confirm] "
        "[(optional) --json]"
    )


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--json"]
    json_mode = "--json" in sys.argv[1:]
    configure_output(json_mode)

    if not (2 <= len(args) <= 3):
        usage()
        sys.exit(1)

    tag_version = args[0]
    yaml_file = args[1]
    update_file = args[2] if len(args) == 3 else "Confirm"

    if update_file not in {"True", "False", "Confirm"}:
        say(paint("Argument for update stack file must be either True, False, or Confirm", RED))
        sys.exit(1)

    ensure_skopeo_available()

    if tag_version == "Latest":
        say(paint("Resolving the latest adapter release...", CYAN), flush=True)
        tag_version = get_latest_tag_version()
        say(f"Using adapter release version {paint(tag_version, BOLD)}")

    say(paint(f"Fetching MASTERLIST.md for {tag_version}...", CYAN), flush=True)
    adapter_versions = get_adapter_versions(tag_version)
    say(f"Found {paint(len(adapter_versions), BOLD)} adapters in MASTERLIST.md")

    response = get_updates(yaml_file, adapter_versions)

    if not json_mode:
        print_report(response)

    if response["update"] and update_file == "Confirm":
        update_file = "True" if confirm_update(yaml_file) else "False"

    written = False
    if response["update"] and update_file == "True":
        save_updated_yaml(yaml_file, response["replace_strings"])
        written = True

    if json_mode:
        json.dump(
            build_json_report(response, tag_version, yaml_file, written),
            sys.stdout, indent=2, sort_keys=True,
        )
        sys.stdout.write("\n")
