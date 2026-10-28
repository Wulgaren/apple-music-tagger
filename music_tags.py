#!/usr/bin/env python3
"""Interactive Apple Music title / track-number editor for the current selection."""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Literal

PREFIX_RE = re.compile(r"^\s*(\d+)\s*(?:\.|-)\s*")
TRAILING_ID_RE = re.compile(r"\s+\d{8,}$")
SPACE_RE = re.compile(r" {2,}")
ENTRY_NUMBER_ONLY_RE = re.compile(r"^\s*(\d{1,3})\s*[.)]?\s*$")
ENTRY_INLINE_RE = re.compile(r"^\s*(\d{1,3})\s*(?:[.)]|[-–—])\s+(.+?)\s*$")
DURATION_RE = re.compile(r"\s*[(\[]?\d{1,3}:\d{2}(?::\d{2})?[)\]]?\s*$")
WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")

TITLE_CASE_SMALL_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "but",
        "or",
        "nor",
        "for",
        "so",
        "yet",
        "as",
        "at",
        "by",
        "etc",
        "in",
        "n",
        "of",
        "off",
        "on",
        "out",
        "per",
        "to",
        "up",
        "via",
    }
)
TITLE_BREAK_CHARS = frozenset(":;?!—–")
APOSTROPHE_NAME_PREFIXES = frozenset({"d", "l", "o"})


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


POLICY_BY_CHOICE = {
    "o": ConflictPolicy.OVERWRITE,
    "k": ConflictPolicy.KEEP,
    "s": ConflictPolicy.SKIP,
}


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
    # \b only helps next to word chars. A trailing "-" (as in "Grimes -") is
    # non-word, so a final \b never matches before the following space.
    left = r"\b" if re.match(r"\w", word[0]) else ""
    right = r"\b" if re.match(r"\w", word[-1]) else ""
    pattern = re.compile(rf"{left}{re.escape(word)}{right}", flags=re.IGNORECASE)
    return clean_spaces(pattern.sub("", name))


def find_replace(name: str, find: str, replace: str) -> str:
    if not find:
        raise ValueError("find must not be empty")
    pattern = re.compile(re.escape(find), flags=re.IGNORECASE)
    return pattern.sub(replace, name)


def strip_trailing_download_id(name: str) -> str:
    return clean_spaces(TRAILING_ID_RE.sub("", name))


def _is_protected_word(word: str) -> bool:
    """Keep acronyms and deliberate mixed case (DJ, R&B, iPhone, McDonald)."""
    letters = [char for char in word if char.isalpha()]
    if len(letters) < 2 or word.casefold() in TITLE_CASE_SMALL_WORDS:
        return False
    if all(char.isupper() for char in letters):
        return len(letters) <= 4
    return any(char.isupper() for char in letters[1:])


def _capitalize_word(word: str) -> str:
    lowered = word.lower()
    if not lowered[0].isalpha():
        return lowered
    if len(lowered) >= 3 and lowered[0] in APOSTROPHE_NAME_PREFIXES:
        if lowered[1] in "'’":
            return lowered[0].upper() + lowered[1] + lowered[2].upper() + lowered[3:]
    return lowered[0].upper() + lowered[1:]


def _title_case_word(word: str, *, capitalize: bool) -> str:
    if not capitalize and word.casefold() in TITLE_CASE_SMALL_WORDS:
        return word.lower()
    if _is_protected_word(word):
        return word
    return _capitalize_word(word)


def _follows_title_break(text: str, start: int) -> bool:
    index = start - 1
    while index >= 0 and text[index].isspace():
        index -= 1
    return index >= 0 and text[index] in TITLE_BREAK_CHARS


def title_case(text: str) -> str:
    """Chicago/AP-style title case, preserving acronyms and mixed-case styling."""
    matches = list(WORD_RE.finditer(text))
    if not matches:
        return clean_spaces(text)
    pieces: list[str] = []
    cursor = 0
    last_index = len(matches) - 1
    for index, match in enumerate(matches):
        pieces.append(text[cursor : match.start()])
        pieces.append(
            _title_case_word(
                match.group(),
                capitalize=(
                    index == 0
                    or index == last_index
                    or _follows_title_break(text, match.start())
                ),
            )
        )
        cursor = match.end()
    pieces.append(text[cursor:])
    return clean_spaces("".join(pieces))


@dataclass(frozen=True)
class TracklistEntry:
    number: int
    title: str


def strip_duration(title: str) -> str:
    return clean_spaces(DURATION_RE.sub("", title))


def parse_tracklist(raw: str) -> list[TracklistEntry]:
    """Parse pasted tracklists like "1.\\nTitle 03:36" or "1. Title 03:36"."""
    entries: list[TracklistEntry] = []
    pending_number: int | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        number_only = ENTRY_NUMBER_ONLY_RE.match(line)
        if number_only is not None:
            if pending_number is not None:
                raise ValueError(f"track number {pending_number} has no title")
            pending_number = int(number_only.group(1))
            continue
        inline = ENTRY_INLINE_RE.match(line)
        if inline is not None:
            if pending_number is not None:
                raise ValueError(f"track number {pending_number} has no title")
            entries.append(
                TracklistEntry(
                    number=int(inline.group(1)),
                    title=strip_duration(inline.group(2)),
                )
            )
            continue
        if pending_number is None:
            raise ValueError(f"unexpected line in tracklist: {line!r}")
        entries.append(
            TracklistEntry(number=pending_number, title=strip_duration(line))
        )
        pending_number = None
    if pending_number is not None:
        raise ValueError(f"track number {pending_number} has no title")
    return entries


def normalize_title(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).casefold()
    return re.sub(r"[^a-z0-9]+", "", folded)


def match_tracklist(
    tracks: list[Track], entries: list[TracklistEntry]
) -> tuple[
    list[tuple[TracklistEntry, Track]],
    list[TracklistEntry],
    list[tuple[TracklistEntry, list[Track]]],
]:
    by_title: dict[str, list[Track]] = {}
    for track in tracks:
        key = normalize_title(track.name)
        if key:
            by_title.setdefault(key, []).append(track)
    matches: list[tuple[TracklistEntry, Track]] = []
    missing: list[TracklistEntry] = []
    ambiguous: list[tuple[TracklistEntry, list[Track]]] = []
    used: set[str] = set()
    for entry in entries:
        candidates = [
            track
            for track in by_title.get(normalize_title(entry.title), [])
            if track.persistent_id not in used
        ]
        if not candidates:
            missing.append(entry)
        elif len(candidates) > 1:
            ambiguous.append((entry, candidates))
        else:
            used.add(candidates[0].persistent_id)
            matches.append((entry, candidates[0]))
    return matches, missing, ambiguous


def plan_tracklist(
    matches: list[tuple[TracklistEntry, Track]],
    *,
    policy: ConflictPolicy,
) -> list[PlannedChange]:
    planned: list[PlannedChange] = []
    for entry, track in matches:
        if track.track_number > 0 and policy is not ConflictPolicy.OVERWRITE:
            continue
        planned.append(
            PlannedChange(
                track=track, new_name=track.name, new_track_number=entry.number
            )
        )
    return planned


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


def plan_strip_trailing_id(tracks: list[Track]) -> list[PlannedChange]:
    planned: list[PlannedChange] = []
    for track in tracks:
        new_name = strip_trailing_download_id(track.name)
        if new_name == track.name:
            continue
        planned.append(
            PlannedChange(track=track, new_name=new_name, new_track_number=None)
        )
    return planned


def plan_title_case(tracks: list[Track]) -> list[PlannedChange]:
    planned: list[PlannedChange] = []
    for track in tracks:
        new_name = title_case(track.name)
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
        # Avoid Music dictionary terms (e.g. "matched" → eClSkMat, read-only).
        lines.append("  set foundTrack to false")
        lines.append("  repeat with t in sel")
        lines.append(f"    if persistent ID of t is {pid_lit} then")
        lines.append(f"      set name of t to {name_lit}")
        if change.new_track_number is not None:
            lines.append(f"      set track number of t to {change.new_track_number}")
        lines.append("      set foundTrack to true")
        lines.append("      exit repeat")
        lines.append("    end if")
        lines.append("  end repeat")
        lines.append(
            '  if foundTrack is false then error "Track no longer in selection"'
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


def read_tracklist_block() -> str:
    print("Paste the tracklist, then press Enter on an empty line to finish.")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


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
        if after == before:
            print(f"  [{change.track.index}] {before!r}  ({tn_bit})")
        else:
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
    return POLICY_BY_CHOICE[choice]


def ask_tracklist_conflict_policy(
    conflicts: list[tuple[TracklistEntry, Track]],
) -> ConflictPolicy:
    print(f"\n{len(conflicts)} track(s) already have a track number set:")
    for entry, track in conflicts[:10]:
        print(
            f"  [{track.index}] track# {track.track_number}, "
            f"tracklist wants {entry.number}: {track.name!r}"
        )
    if len(conflicts) > 10:
        print(f"  … and {len(conflicts) - 10} more")
    choice = prompt_choice(
        "For those tracks",
        {
            "o": "overwrite with the tracklist number",
            "k": "keep the existing track number",
            "s": "skip those tracks",
        },
    )
    return POLICY_BY_CHOICE[choice]


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


def run_strip_trailing_id(tracks: list[Track]) -> None:
    changes = plan_strip_trailing_id(tracks)
    print_preview(changes)
    if not changes:
        return
    if not confirm("Apply"):
        print("Cancelled.")
        return
    apply_changes(changes)
    print("Done.")


def run_title_case(tracks: list[Track]) -> None:
    changes = plan_title_case(tracks)
    print_preview(changes)
    if not changes:
        return
    if not confirm("Apply"):
        print("Cancelled.")
        return
    apply_changes(changes)
    print("Done.")


def run_set_track_numbers(tracks: list[Track]) -> None:
    raw = read_tracklist_block()
    try:
        entries = parse_tracklist(raw)
    except ValueError as exc:
        print(f"Could not read tracklist: {exc}")
        return
    if not entries:
        print("No tracklist entries found.")
        return
    matches, missing, ambiguous = match_tracklist(tracks, entries)
    print(f"\nMatched {len(matches)} of {len(entries)} tracklist entries.")
    for entry in missing:
        print(f"  ! no selected track matches {entry.title!r} (#{entry.number})")
    for entry, candidates in ambiguous:
        names = ", ".join(repr(track.name) for track in candidates)
        print(f"  ! {entry.title!r} matches several selected tracks: {names}")
    matched_ids = {track.persistent_id for _, track in matches}
    for track in tracks:
        if track.persistent_id not in matched_ids:
            print(f"  ! selected track not in tracklist: {track.name!r}")
    if not matches:
        return
    conflicts = [(entry, track) for entry, track in matches if track.track_number > 0]
    policy = ConflictPolicy.OVERWRITE
    if conflicts:
        policy = ask_tracklist_conflict_policy(conflicts)
    changes = plan_tracklist(matches, policy=policy)
    print_preview(changes)
    if not changes:
        return
    if not confirm("Apply"):
        print("Cancelled.")
        return
    apply_changes(changes)
    print("Done.")


MenuAction = Literal["1", "2", "3", "4", "5", "6", "q"]


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
    print("4) Strip trailing download ID from titles")
    print("5) Set track numbers from a pasted tracklist")
    print("6) Title-case titles")
    print("q) Quit")
    action = prompt_choice(
        "Action",
        {
            "1": "strip",
            "2": "remove",
            "3": "replace",
            "4": "strip-id",
            "5": "tracklist",
            "6": "title-case",
            "q": "quit",
        },
    )
    if action == "q":
        return
    if action == "1":
        run_strip(tracks)
    elif action == "2":
        run_remove_word(tracks)
    elif action == "3":
        run_find_replace(tracks)
    elif action == "4":
        run_strip_trailing_id(tracks)
    elif action == "5":
        run_set_track_numbers(tracks)
    else:
        run_title_case(tracks)


if __name__ == "__main__":
    main()
