#!/usr/bin/env python3
"""Regenerate k0s autopilot update channels from k0sproject/k0s GitHub releases.

Channels written (relative to repo root):
  stable/vX.Y/index.yaml   newest GA patch of each minor >= MIN_MINOR
  stable/index.yaml        newest GA release overall
  latest/index.yaml        newest GA release overall
  unstable/index.yaml      newest release overall, pre-releases included

Checksums come from the sha256sums.txt asset that k0s' release.yml attaches
to every release. Only published (non-draft) releases are considered.
Existing eolDate values are preserved.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

SOURCE_REPO = os.environ.get("SOURCE_REPO", "k0sproject/k0s")
MIN_MINOR = tuple(int(x) for x in os.environ.get("MIN_MINOR", "1.27").split("."))
ARCHES = ["amd64", "arm64", "arm"]
ROOT = Path(os.environ.get("CHANNELS_ROOT", Path(__file__).resolve().parent.parent))

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?\+k0s\.(\d+)$")


def http_get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "k0s-update-channels"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def list_releases():
    out, page = [], 1
    while True:
        batch = json.loads(http_get(
            f"https://api.github.com/repos/{SOURCE_REPO}/releases?per_page=100&page={page}"))
        if not batch:
            return out
        out += [r for r in batch if not r["draft"]]
        page += 1


def sort_key(m):
    major, minor, patch, pre, k0s = m.groups()
    # GA sorts after its pre-releases; pre-release identifiers compared semver-style
    pre_key = (1,) if pre is None else (0,) + tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (int(major), int(minor), int(patch), pre_key, int(k0s))


def fetch_checksums(release):
    asset = next((a for a in release["assets"] if a["name"] == "sha256sums.txt"), None)
    if asset is None:
        return None
    sums = {}
    for line in http_get(asset["browser_download_url"], accept="application/octet-stream").decode().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip("*")] = digest
    return sums


def render(channel, tag, sums, eol=None):
    base = f"https://github.com/{SOURCE_REPO}/releases/download/{quote(tag, safe='')}"
    lines = [f"channel: {channel}"]
    if eol:
        lines.append(f'eolDate: "{eol}"')
    lines += [f"version: {tag[1:]}", "downloadURLs:"]
    for arch in ARCHES:
        binary = f"k0s-{tag}-{arch}"
        # Newer releases: ...-linux-<arch>.tar, older ones: ...-<arch>
        airgap = next((n for n in (f"k0s-airgap-bundle-{tag}-linux-{arch}.tar",
                                   f"k0s-airgap-bundle-{tag}-{arch}") if n in sums), None)
        if binary not in sums:
            raise RuntimeError(f"{tag}: {binary} missing from sha256sums.txt")
        lines += [f"- arch: {arch}", "  os: linux",
                  f"  k0s: {base}/{binary}", f"  k0sSha256: {sums[binary]}"]
        if airgap:
            lines += [f"  airgapBundle: {base}/{airgap}", f"  airgapSha256: {sums[airgap]}"]
    return "\n".join(lines) + "\n"


def existing_eol(path, channel):
    """Keep a hand-maintained eolDate, but only while the file still serves the same channel."""
    if not path.exists():
        return None
    text = path.read_text()
    if not re.search(rf"^channel:\s*{re.escape(channel)}\s*$", text, re.M):
        return None
    m = re.search(r'^eolDate:\s*"?([^"\n]+)"?', text, re.M)
    return m.group(1).strip() if m else None


def main():
    releases = {}
    for r in list_releases():
        m = TAG_RE.match(r["tag_name"])
        if m:
            releases[r["tag_name"]] = (m, r)
    if not releases:
        sys.exit("no k0s releases found")

    ordered = sorted(releases.values(), key=lambda mr: sort_key(mr[0]))
    ga = [mr for mr in ordered if mr[0].group(4) is None]

    targets = {}  # path -> (channel name, (match, release))
    newest_per_minor = {}
    for m, r in ga:
        minor = (int(m.group(1)), int(m.group(2)))
        if minor >= MIN_MINOR:
            newest_per_minor[minor] = (m, r)
    for (maj, mi), mr in newest_per_minor.items():
        targets[f"stable/v{maj}.{mi}/index.yaml"] = (f"v{maj}.{mi}", mr)
    newest_ga = ga[-1]
    ga_channel = f"v{newest_ga[0].group(1)}.{newest_ga[0].group(2)}"
    targets["stable/index.yaml"] = (ga_channel, newest_ga)
    targets["latest/index.yaml"] = (ga_channel, newest_ga)
    targets["unstable/index.yaml"] = ("latest-unstable", ordered[-1])

    cache = {}
    for path, (channel, (m, r)) in sorted(targets.items()):
        tag = r["tag_name"]
        if tag not in cache:
            cache[tag] = fetch_checksums(r)
        if cache[tag] is None:
            print(f"skip {path}: {tag} has no sha256sums.txt", file=sys.stderr)
            continue
        out = ROOT / path
        content = render(channel, tag, cache[tag], existing_eol(out, channel))
        if not out.exists() or out.read_text() != content:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content)
            print(f"updated {path} -> {tag}")


if __name__ == "__main__":
    main()
