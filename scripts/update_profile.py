#!/usr/bin/env python3
"""Refresh a public-only profile snapshot using the Python standard library.

Only the activity SVG and the PUBLIC-SIGNALS block in README.md are updated.
API failures leave the previous files intact. Tokens and raw API responses are
never printed or written to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


API_BASE = "https://api.github.com"
USER = "zbtzbtzbt"
PROFILE_REPO = f"{USER}/{USER}"
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")
CODE_TYPES = frozenset(
    {"PushEvent", "PullRequestEvent", "IssuesEvent", "IssueCommentEvent"}
)
START = "<!-- PUBLIC-SIGNALS:START -->"
END = "<!-- PUBLIC-SIGNALS:END -->"
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ProfileError(Exception):
    """A safe, human-readable failure without credentials or response bodies."""


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProfileError("GitHub returned a missing or invalid timestamp.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProfileError("GitHub returned an invalid timestamp.") from error
    if result.tzinfo is None:
        raise ProfileError("GitHub returned a timestamp without a timezone.")
    return result.astimezone(timezone.utc)


def local_date(value: datetime) -> str:
    return value.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d")


def repo_name(value: object) -> str:
    if not isinstance(value, str) or not REPO_PATTERN.fullmatch(value):
        raise ProfileError("GitHub returned an invalid repository identifier.")
    return value


def github_url(value: object, expected_path: str) -> str:
    if not isinstance(value, str):
        raise ProfileError("GitHub returned an invalid public URL.")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path.rstrip("/") != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ProfileError("GitHub returned an unexpected public URL.")
    return value


def markdown_text(value: str) -> str:
    # A public issue title is content, never README markup or an instruction.
    text = html.escape(" ".join(value.split()), quote=False)
    text = text.replace("\\", "\\\\")
    for character in "[]*_`|~":
        text = text.replace(character, "\\" + character)
    return text


class GitHub:
    def __init__(self) -> None:
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def get(
        self,
        path: str,
        params: dict[str, object] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> object:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ProfileError("Invalid GitHub API request path.")
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": accept,
            "User-Agent": "zbt-public-profile-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                if status in {429, 502, 503, 504} and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise ProfileError(
                    f"GitHub API request failed with HTTP {status}; snapshot preserved."
                ) from None
            except (urllib.error.URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise ProfileError(
                    "GitHub API network request failed; snapshot preserved."
                ) from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ProfileError(
                    "GitHub returned invalid JSON; snapshot preserved."
                ) from None
        raise ProfileError("GitHub API request did not complete.")


@dataclass(frozen=True)
class Star:
    name: str
    url: str
    at: datetime


@dataclass(frozen=True)
class PublicEvent:
    identifier: str
    at: datetime


@dataclass(frozen=True)
class PullRequest:
    title: str
    url: str
    at: datetime


@dataclass(frozen=True)
class Snapshot:
    stars: tuple[Star, ...]
    events: tuple[PublicEvent, ...]
    prs: tuple[PullRequest, ...]
    events_limited: bool
    now: datetime


def collect_stars(api: GitHub, cutoff: datetime, now: datetime) -> tuple[Star, ...]:
    found: dict[str, Star] = {}
    page = 1
    while True:
        entries = api.get(
            f"/users/{USER}/starred",
            {"sort": "created", "direction": "desc", "per_page": 100, "page": page},
            accept="application/vnd.github.star+json",
        )
        if not isinstance(entries, list):
            raise ProfileError("GitHub returned an invalid starred-project list.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ProfileError("GitHub returned an invalid starred-project entry.")
            at = timestamp(entry.get("starred_at"))
            repo = entry.get("repo")
            if not isinstance(repo, dict):
                raise ProfileError("GitHub returned invalid starred-project metadata.")
            # Test visibility before reading or rendering any repository identifier.
            if repo.get("private") is not False or not cutoff <= at <= now:
                continue
            name = repo_name(repo.get("full_name"))
            url = github_url(repo.get("html_url"), "/" + name)
            found[name] = Star(name, url, at)
        if len(entries) < 100 or timestamp(entries[-1].get("starred_at")) < cutoff:
            break
        page += 1
    return tuple(sorted(found.values(), key=lambda star: (star.at, star.name), reverse=True))


def event_pull_request(
    event: dict[str, object], name: str, cutoff: datetime, now: datetime
) -> PullRequest | None:
    """Read an authored PR link from a verified public event, never search.

    The public event feed is a sample, not a complete PR history. An action on
    someone else's PR can be a discussion event but is not an authored PR link.
    """
    if event.get("type") != "PullRequestEvent":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        return None
    author = pr.get("user")
    if not isinstance(author, dict):
        return None
    login = author.get("login")
    if (
        not isinstance(login, str)
        or login.casefold() != USER.casefold()
        or login.casefold().endswith("[bot]")
        or author.get("type") == "Bot"
    ):
        return None
    base = pr.get("base")
    if isinstance(base, dict):
        base_repo = base.get("repo")
        if isinstance(base_repo, dict) and base_repo.get("private") is True:
            return None
    at = timestamp(pr.get("created_at"))
    if not cutoff <= at <= now:
        return None
    title = pr.get("title")
    url = pr.get("html_url")
    if not isinstance(title, str) or not isinstance(url, str):
        raise ProfileError("GitHub returned invalid public PR text.")
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    if not re.fullmatch(re.escape("/" + name) + r"/pull/[0-9]+", path):
        raise ProfileError("GitHub PR URL does not match its public event repository.")
    github_url(url, path)
    return PullRequest(title, "https://github.com" + path, at)


def collect_events(
    api: GitHub, cutoff: datetime, now: datetime
) -> tuple[tuple[PublicEvent, ...], tuple[PullRequest, ...], bool]:
    found: dict[str, PublicEvent] = {}
    authored_prs: dict[str, PullRequest] = {}
    repo_metadata: dict[str, dict[str, object]] = {}
    limited = False
    for page in range(1, 4):
        entries = api.get(
            f"/users/{USER}/events/public", {"per_page": 100, "page": page}
        )
        if not isinstance(entries, list):
            raise ProfileError("GitHub returned an invalid public-event list.")
        for event in entries:
            if not isinstance(event, dict):
                raise ProfileError("GitHub returned an invalid public-event entry.")
            actor = event.get("actor")
            if not isinstance(actor, dict):
                raise ProfileError("GitHub returned invalid event actor metadata.")
            login = actor.get("login")
            if (
                event.get("public") is not True
                or not isinstance(login, str)
                or login.casefold() != USER.casefold()
                or login.casefold().endswith("[bot]")
                or actor.get("type") == "Bot"
                or event.get("type") not in CODE_TYPES
            ):
                continue
            at = timestamp(event.get("created_at"))
            if not cutoff <= at <= now:
                continue
            repo = event.get("repo")
            if not isinstance(repo, dict):
                raise ProfileError("GitHub returned invalid event repository metadata.")
            name = repo_name(repo.get("name"))
            if name.casefold() == PROFILE_REPO.casefold():
                continue
            if event.get("type") == "PushEvent":
                # A fork's push may only mirror upstream. Public PRs and discussions
                # remain eligible, but fork pushes are conservatively excluded.
                if name not in repo_metadata:
                    metadata = api.get("/repos/" + name)
                    if not isinstance(metadata, dict):
                        raise ProfileError("GitHub returned invalid push repository metadata.")
                    repo_metadata[name] = metadata
                metadata = repo_metadata[name]
                if metadata.get("private") is not False or metadata.get("fork") is not False:
                    continue
            identifier = event.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ProfileError("GitHub returned an invalid public-event identifier.")
            found[identifier] = PublicEvent(identifier, at)
            pr = event_pull_request(event, name, cutoff, now)
            if pr is not None:
                # The newest event is encountered first. Open/close/reopen actions
                # on one PR yield a single link with the newest available title.
                authored_prs.setdefault(pr.url, pr)
        if len(entries) < 100:
            break
        if timestamp(entries[-1].get("created_at")) < cutoff:
            break
        if page == 3:
            limited = True
    return (
        tuple(sorted(found.values(), key=lambda event: event.at, reverse=True)),
        tuple(sorted(authored_prs.values(), key=lambda pr: (pr.at, pr.url), reverse=True)[:3]),
        limited,
    )


def collect(api: GitHub, now: datetime) -> Snapshot:
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    cutoff = now - timedelta(days=90)
    stars = collect_stars(api, cutoff, now)
    events, prs, limited = collect_events(api, cutoff, now)
    return Snapshot(stars, events, prs, limited, now)


def signals_markdown(snapshot: Snapshot) -> str:
    version = hashlib.sha256(activity_svg(snapshot).encode("utf-8")).hexdigest()[:12]
    image = (
        f'<img src="assets/activity.svg?v={version}" '
        'alt="Training systems focus and recent public updates" width="100%" />'
    )
    if not snapshot.stars and not snapshot.events and not snapshot.prs:
        return "\n".join(
            [
                "### Building forward / 接下来",
                "",
                image,
                "",
                "继续深入训练系统，开展 GPU 性能实验，整理 LLM / VLM 论文解析；用可复核的实现与实验记录持续进展。",
            ]
        )
    lines = ["### Recent signals / 近 90 日公开动态", "", image]
    if snapshot.stars:
        lines.extend(["", "**最近关注 · Recent stars**", ""])
        for star in snapshot.stars[:3]:
            lines.append(f"- [{markdown_text(star.name)}]({star.url}) · {local_date(star.at)}")
    if snapshot.prs:
        lines.extend(["", "**近期公开 PR · Recent public PRs**", ""])
        for pr in snapshot.prs:
            title = " ".join(pr.title.split())
            if len(title) > 140:
                title = title[:137] + "…"
            lines.append(f"- [{markdown_text(title)}]({pr.url}) · {local_date(pr.at)}")
    return "\n".join(lines)


def update_readme(readme: str, snapshot: Snapshot) -> str:
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ProfileError("README must contain exactly one PUBLIC-SIGNALS marker pair.")
    begin = readme.index(START) + len(START)
    end = readme.index(END)
    if end < begin:
        raise ProfileError("README PUBLIC-SIGNALS markers are in the wrong order.")
    return readme[:begin] + "\n\n" + signals_markdown(snapshot) + "\n\n" + readme[end:]


def weekly_counts(snapshot: Snapshot) -> tuple[list[int], list[int]]:
    stars = [0] * 13
    events = [0] * 13
    for entries, counts in ((snapshot.stars, stars), (snapshot.events, events)):
        for entry in entries:
            age = (snapshot.now - entry.at).total_seconds()
            # Thirteen seven-day bins cover the 90-day window; the oldest bin is
            # partial. WatchEvent is never counted again as a code event.
            index = 12 - min(int(age // (7 * 24 * 60 * 60)), 12)
            counts[index] += 1
    return stars, events


def building_forward_svg() -> str:
    """A direction card for an empty public snapshot, without invented activity."""
    return '''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="200" viewBox="0 0 1280 200" role="img" aria-labelledby="title description">
  <title id="title">Building forward — training systems focus</title>
  <desc id="description">Training systems. Measured progress. Focus on distributed training, GPU performance, and LLM / VLM.</desc>
  <defs>
    <linearGradient id="background" x2="1" y2="1"><stop stop-color="#080d18"/><stop offset="1" stop-color="#10172a"/></linearGradient>
    <radialGradient id="cyanHalo"><stop stop-color="#28c8d8" stop-opacity=".13"/><stop offset="1" stop-color="#28c8d8" stop-opacity="0"/></radialGradient>
    <radialGradient id="violetHalo"><stop stop-color="#9e7ae8" stop-opacity=".11"/><stop offset="1" stop-color="#9e7ae8" stop-opacity="0"/></radialGradient>
    <linearGradient id="accent" x2="0" y2="1"><stop stop-color="#67e3ed"/><stop offset="1" stop-color="#a48bf2"/></linearGradient>
    <clipPath id="bounds"><rect width="1280" height="200" rx="18"/></clipPath>
  </defs>
  <g clip-path="url(#bounds)">
    <rect width="1280" height="200" fill="url(#background)"/>
    <ellipse cx="1120" cy="20" rx="270" ry="185" fill="url(#cyanHalo)"/>
    <ellipse cx="1240" cy="205" rx="310" ry="190" fill="url(#violetHalo)"/>
    <path d="M1105-40C1000 30 1000 170 1105 240M1160-40C1055 30 1055 170 1160 240M1215-40C1110 30 1110 170 1215 240" fill="none" stroke="#82b5d1" stroke-opacity=".07" stroke-width="1"/>
    <rect x="1220" y="40" width="3" height="120" rx="1.5" fill="url(#accent)" opacity=".65"/>
    <g font-family="Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif">
      <text x="64" y="37" fill="#80e5ed" font-size="13" font-weight="600" letter-spacing="2.5">BUILDING FORWARD</text>
      <text x="63" y="95" fill="#edf4ff" font-size="41" font-weight="600" letter-spacing="-.75">Training systems. Measured progress.</text>
      <g font-size="15" font-weight="500">
        <rect x="64" y="126" width="223" height="38" rx="8" fill="#102333" stroke="#3d7488" stroke-opacity=".5"/>
        <text x="83" y="151" fill="#8ce5ed">Distributed training</text>
        <rect x="301" y="126" width="206" height="38" rx="8" fill="#182139" stroke="#66658e" stroke-opacity=".45"/>
        <text x="320" y="151" fill="#c8bcf7">GPU performance</text>
        <rect x="521" y="126" width="155" height="38" rx="8" fill="#131f32" stroke="#526987" stroke-opacity=".45"/>
        <text x="540" y="151" fill="#a9bfdb">LLM / VLM</text>
      </g>
    </g>
    <rect x=".5" y=".5" width="1279" height="199" rx="17.5" fill="none" stroke="#52617d" stroke-opacity=".26"/>
  </g>
</svg>
'''


def activity_svg(snapshot: Snapshot) -> str:
    if not snapshot.stars and not snapshot.events and not snapshot.prs:
        return building_forward_svg()
    stars, events = weekly_counts(snapshot)
    largest = max([s + e for s, e in zip(stars, events)] + [1])
    bars: list[str] = []
    for index, (star_count, event_count) in enumerate(zip(stars, events)):
        x = 730 + index * 35
        star_height = 70 * star_count / largest
        event_height = 70 * event_count / largest
        title = html.escape(
            f"Rolling week {index + 1}: {star_count} stars, {event_count} code/discussion events",
            quote=True,
        )
        bars.append(f'<g><title>{title}</title>')
        if star_count + event_count == 0:
            bars.append(f'<rect x="{x}" y="142" width="23" height="2" rx="1" fill="#27364c"/>')
        if star_count:
            bars.append(
                f'<rect x="{x}" y="{144-star_height:.2f}" width="23" '
                f'height="{star_height:.2f}" rx="2" fill="#66dce9"/>'
            )
        if event_count:
            bars.append(
                f'<rect x="{x}" y="{144-star_height-event_height:.2f}" width="23" '
                f'height="{event_height:.2f}" rx="2" fill="#a78bfa"/>'
            )
        bars.append("</g>")
    signal_times = [star.at for star in snapshot.stars] + [event.at for event in snapshot.events]
    latest = local_date(max(signal_times)) if signal_times else "No recent public signals"
    latest = html.escape(latest, quote=True)
    event_word = "event" if len(snapshot.events) == 1 else "events"
    star_label_x = 64 + max(75, len(str(len(snapshot.stars))) * 30) + 22
    event_label_x = 64 + max(27, len(str(len(snapshot.events))) * 19) + 16
    if snapshot.events:
        event_line = (
            f'<text x="64" y="143" fill="#bca7f7" font-size="30" font-weight="600">{len(snapshot.events)}</text>'
            f'<text x="{event_label_x}" y="138" fill="#aebed4" font-size="19">public code/discussion {event_word}</text>'
        )
    else:
        event_line = '<text x="64" y="138" fill="#aebed4" font-size="19">Training systems · GPU performance · RL</text>'
    footer = "Public event feed · profile updates &amp; fork syncs excluded"
    if snapshot.events_limited:
        footer += " · event feed capped at 300"
    graph = "\n    ".join(bars)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="200" viewBox="0 0 1280 200" role="img" aria-labelledby="title description">
  <title id="title">Recent public GitHub signals</title>
  <desc id="description">Last 90 days: {len(snapshot.stars)} recently starred projects and {len(snapshot.events)} observed code or discussion {event_word} from the public event feed. Latest public signal: {latest}. A 13-bin graph shows stars and observed code or discussion events separately. The public event feed is a sample of up to 300 events. Profile updates and fork pushes are excluded.</desc>
  <defs>
    <linearGradient id="background" x2="1" y2="1"><stop stop-color="#080d18"/><stop offset="1" stop-color="#10172a"/></linearGradient>
  </defs>
  <rect width="1280" height="200" rx="18" fill="url(#background)"/>
  <rect x=".5" y=".5" width="1279" height="199" rx="17.5" fill="none" stroke="#52617d" stroke-opacity=".26"/>
  <path d="M677 32V166" stroke="#455570" stroke-opacity=".35"/>
  <g font-family="Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif">
    <text x="64" y="35" fill="#94a9c4" font-size="12" font-weight="600" letter-spacing="1.8">PUBLIC SIGNALS / LAST 90 DAYS</text>
    <text x="64" y="100" fill="#80e5ef" font-size="52" font-weight="650">{len(snapshot.stars)}</text>
    <text x="{star_label_x}" y="86" fill="#dbe8f8" font-size="23">recently starred projects</text>
    {event_line}
    <text x="64" y="176" fill="#7d91ad" font-size="13">Latest public signal · {latest}</text>
    <text x="730" y="35" fill="#c9d7e9" font-size="16" font-weight="500">Stars + code/discussion events</text>
    <rect x="730" y="51" width="8" height="8" rx="2" fill="#66dce9"/>
    <text x="747" y="59" fill="#94a9c4" font-size="12">Stars</text>
    <rect x="813" y="51" width="8" height="8" rx="2" fill="#a78bfa"/>
    <text x="830" y="59" fill="#94a9c4" font-size="12">Code / discussion</text>
    <text x="1173" y="59" text-anchor="end" fill="#7d91ad" font-size="11">13 rolling weeks</text>
    <path d="M730 145H1173" stroke="#324058" stroke-opacity=".7"/>
    {graph}
    <text x="730" y="165" fill="#7d91ad" font-size="11">Earlier</text>
    <text x="1173" y="165" text-anchor="end" fill="#7d91ad" font-size="11">Recent</text>
    <text x="730" y="184" fill="#7185a1" font-size="10">{footer}</text>
  </g>
</svg>
'''


def write_changes(changes: dict[Path, str]) -> list[str]:
    # Stage every changed file before replacing any destination. API collection,
    # SVG generation and README validation have all completed by this point.
    prepared: list[tuple[Path, str]] = []
    try:
        for path, content in changes.items():
            encoded = content.encode("utf-8")
            if path.exists() and path.read_bytes() == encoded:
                continue
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix="." + path.name + ".", dir=path.parent, delete=False
            ) as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = temporary.name
            os.chmod(temporary_path, 0o644)
            prepared.append((path, temporary_path))
        for path, temporary_path in prepared:
            os.replace(temporary_path, path)
        return [path.name for path, _ in prepared]
    finally:
        for _, temporary_path in prepared:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Collect and validate without writing files.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    readme_path = root / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ProfileError("README must contain exactly one PUBLIC-SIGNALS marker pair.")
    snapshot = collect(GitHub(), datetime.now(timezone.utc))
    updated_readme = update_readme(readme, snapshot)
    svg = activity_svg(snapshot)
    changes = {root / "assets" / "activity.svg": svg, readme_path: updated_readme}
    print(
        f"Public snapshot: {len(snapshot.stars)} recent stars, "
        f"{len(snapshot.events)} observed code/discussion events, "
        f"{len(snapshot.prs)} public PR links in the event feed."
    )
    if args.dry_run:
        print("Dry run complete; no files written.")
    else:
        written = write_changes(changes)
        print("Updated: " + ", ".join(written) if written else "Snapshot unchanged; no files written.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProfileError as error:
        print(f"Profile update failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except OSError:
        print("Profile update failed: local file operation failed.", file=sys.stderr)
        raise SystemExit(1) from None
