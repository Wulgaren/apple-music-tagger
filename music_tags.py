#!/usr/bin/env python3
"""Interactive Apple Music title / track-number editor for the current selection."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Literal

PREFIX_RE = re.compile(r"^\s*(\d+)\s*(?:\.|-)\s*")
SPACE_RE = re.compile(r" {2,}")


@dataclass(frozen=True)
class Track:
    index: int  # 1-based display order from selection
    persistent_id: str
    name: str
    track_number: int  # 0 means unset / missing


@dataclass(frozen=True)
class PlannedChange:
    track: Track
    new_name: str
    new_track_number: int | None  # None = leave track number alone


class ConflictPolicy(Enum):
    OVERWRITE = "overwrite"
    KEEP = "keep"
    SKIP = "skip"


def clean_spaces(text: str) -> str:
    return SPACE_RE.sub(" ", text).strip()


def parse_prefix(name: str) -> tuple[int, str] | None:
    match = PREFIX_RE.match(name)
    if match is None:
        return None
    number = int(match.group(1))
    rest = clean_spaces(name[match.end() :])
    return number, rest


def remove_whole_word(name: str, word: str) -> str:
    if not word:
        raise ValueError("word must not be empty")
    pattern = re.compile(rf"\b{re.escape(word)}\b", flags=re.IGNORECASE)
    return clean_spaces(pattern.sub("", name))


def find_replace(name: str, find: str, replace: str) -> str:
    if not find:
        raise ValueError("find must not be empty")
    pattern = re.compile(re.escape(find), flags=re.IGNORECASE)
    return pattern.sub(replace, name)


def plan_strip(
    tracks: list[Track],
    *,
    policy: ConflictPolicy,
) -> list[PlannedChange]:
    planned: list[PlannedChange] = []
    for track in tracks:
        parsed = parse_prefix(track.name)
        if parsed is None:
            continue
        number, new_name = parsed
        has_existing = track.track_number > 0
        if has_existing:
            if policy is ConflictPolicy.SKIP:
                continue
            if policy is ConflictPolicy.KEEP:
                planned.append(
                    PlannedChange(track=track, new_name=new_name, new_track_number=None)
                )
                continue
        planned.append(
            PlannedChange(track=track, new_name=new_name, new_track_number=number)
        )
    return planned


def plan_remove_word(tracks: list[Track], word: str) -> list[PlannedChange]:
    planned: list[PlannedChange] = []
    for track in tracks:
        new_name = remove_whole_word(track.name, word)
        if new_name == track.name:
            continue
        planned.append(
            PlannedChange(track=track, new_name=new_name, new_track_number=None)
        )
    return planned


def plan_find_replace(
    tracks: list[Track], find: str, replace: str
) -> list[PlannedChange]:
    planned: list[PlannedChange] = []
    for track in tracks:
        new_name = find_replace(track.name, find, replace)
        if new_name == track.name:
            continue
        planned.append(
            PlannedChange(track=track, new_name=new_name, new_track_number=None)
        )
    return planned


def run_osascript(source: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", source],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "osascript failed").strip()
        raise RuntimeError(err)
    return result.stdout


def fetch_selection() -> list[Track]:
    script = r"""
tell application "Music"
  set sel to selection
  if sel is {} then
    return ""
  end if
  set out to ""
  set i to 1
  repeat with t in sel
    set trackName to name of t
    set pid to persistent ID of t
    try
      set tn to track number of t
      if tn is missing value then
        set tnText to "0"
      else
        set tnText to (tn as text)
      end if
    on error
      set tnText to "0"
    end try
    set out to out & i & tab & pid & tab & trackName & tab & tnText & linefeed
    set i to i + 1
  end repeat
  return out
end tell
"""
    raw = run_osascript(script)
    tracks: list[Track] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise RuntimeError(f"unexpected Music row: {line!r}")
        index_s, persistent_id, name, tn_s = parts
        tracks.append(
            Track(
                index=int(index_s),
                persistent_id=persistent_id,
                name=name,
                track_number=int(tn_s),
            )
        )
    return tracks


def apply_changes(changes: list[PlannedChange]) -> None:
    if not changes:
        return
    lines: list[str] = [
        'tell application "Music"',
        "  set sel to selection",
        '  if sel is {} then error "Nothing selected"',
    ]
    for change in changes:
        pid_lit = _as_literal(change.track.persistent_id)
        name_lit = _as_literal(change.new_name)
        lines.append("  set matched to false")
        lines.append("  repeat with t in sel")
        lines.append(f"    if persistent ID of t is {pid_lit} then")
        lines.append(f"      set name of t to {name_lit}")
        if change.new_track_number is not None:
            lines.append(f"      set track number of t to {change.new_track_number}")
        lines.append("      set matched to true")
        lines.append("      exit repeat")
        lines.append("    end if")
        lines.append("  end repeat")
        lines.append(
            '  if matched is false then error "Track no longer in selection"'
        )
    lines.append("end tell")
    run_osascript("\n".join(lines))


def _as_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def prompt(message: str) -> str:
    try:
        return input(message)
    except EOFError as exc:
        raise SystemExit(1) from exc


def prompt_choice(
    message: str, options: dict[str, str]
) -> str:
    keys = "/".join(options)
    while True:
        raw = prompt(f"{message} [{keys}]: ").strip().lower()
        if raw in options:
            return raw
        print(f"Pick one of: {keys}")


def confirm(message: str) -> bool:
    return prompt_choice(message, {"y": "yes", "n": "no"}) == "y"


def print_preview(changes: list[PlannedChange]) -> None:
    if not changes:
        print("No changes.")
        return
    print(f"\n{len(changes)} change(s):")
    for change in changes:
        before = change.track.name
        after = change.new_name
        tn_before = change.track.track_number
        if change.new_track_number is None:
            tn_bit = f"track# {tn_before} (unchanged)"
        else:
            tn_bit = f"track# {tn_before} → {change.new_track_number}"
        print(f"  [{change.track.index}] {before!r}")
        print(f"       → {after!r}  ({tn_bit})")
    print()


def conflict_tracks(tracks: list[Track]) -> list[Track]:
    out: list[Track] = []
    for track in tracks:
        parsed = parse_prefix(track.name)
        if parsed is None:
            continue
        if track.track_number > 0:
            out.append(track)
    return out


def ask_conflict_policy(conflicts: list[Track]) -> ConflictPolicy:
    print(f"\n{len(conflicts)} track(s) already have a track number set:")
    for track in conflicts[:10]:
        parsed = parse_prefix(track.name)
        assert parsed is not None
        number, _ = parsed
        print(
            f"  [{track.index}] track# {track.track_number}, "
            f"title wants {number}: {track.name!r}"
        )
    if len(conflicts) > 10:
        print(f"  … and {len(conflicts) - 10} more")
    choice = prompt_choice(
        "For those tracks",
        {
            "o": "overwrite track number from title",
            "k": "keep existing track number, still strip title",
            "s": "skip those tracks entirely",
        },
    )
    return {
        "o": ConflictPolicy.OVERWRITE,
        "k": ConflictPolicy.KEEP,
        "s": ConflictPolicy.SKIP,
    }[choice]


def run_strip(tracks: list[Track]) -> None:
    conflicts = conflict_tracks(tracks)
    policy = ConflictPolicy.OVERWRITE
    if conflicts:
        policy = ask_conflict_policy(conflicts)
    changes = plan_strip(tracks, policy=policy)
    print_preview(changes)
    if not changes:
        return
    if not confirm("Apply"):
        print("Cancelled.")
        return
    apply_changes(changes)
    print("Done.")


def run_remove_word(tracks: list[Track]) -> None:
    word = prompt("Word/phrase to remove: ").strip()
    if not word:
        print("Empty input, cancelled.")
        return
    changes = plan_remove_word(tracks, word)
    print_preview(changes)
    if not changes:
        return
    if not confirm("Apply"):
        print("Cancelled.")
        return
    apply_changes(changes)
    print("Done.")


def run_find_replace(tracks: list[Track]) -> None:
    find = prompt("Find: ")
    if not find:
        print("Empty find, cancelled.")
        return
    replace = prompt("Replace with: ")
    changes = plan_find_replace(tracks, find, replace)
    print_preview(changes)
    if not changes:
        return
    if not confirm("Apply"):
        print("Cancelled.")
        return
    apply_changes(changes)
    print("Done.")


MenuAction = Literal["1", "2", "3", "q"]


def main() -> None:
    print("music-tags — select tracks in Music, then pick an action.\n")
    try:
        tracks = fetch_selection()
    except RuntimeError as exc:
        print(f"Music error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not tracks:
        print("Nothing selected in Music.")
        raise SystemExit(1)
    print(f"Loaded {len(tracks)} selected track(s).\n")
    for track in tracks[:5]:
        tn = track.track_number if track.track_number > 0 else "-"
        print(f"  [{track.index}] #{tn} {track.name}")
    if len(tracks) > 5:
        print(f"  … and {len(tracks) - 5} more")
    print()
    print("1) Strip leading number from title → track number field")
    print("2) Remove whole word/phrase from titles")
    print("3) Find/replace in titles")
    print("q) Quit")
    action = prompt_choice("Action", {"1": "strip", "2": "remove", "3": "replace", "q": "quit"})
    if action == "q":
        return
    if action == "1":
        run_strip(tracks)
    elif action == "2":
        run_remove_word(tracks)
    else:
        run_find_replace(tracks)


if __name__ == "__main__":
    main()
