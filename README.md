# apple-music-tagger

Batch-edit titles on tracks selected in the macOS Music app. Music already lets you change genre, year, and the like in bulk; titles are the awkward one. This is a small interactive terminal tool for that.

## Requirements

- macOS with the Music app
- Python 3.11+
- Tracks in your local library (files you own / matched downloads). Streaming-only catalogue tracks often won't keep metadata edits.

## Usage

1. Select one or more tracks in Music.
2. Run:

```bash
python3 music_tags.py
```

3. Pick an action, check the before → after preview, confirm.

### Actions

**Strip leading number → track number field**  
Pulls prefixes like `01.`, `1.`, `01 -`, or `1 -` off the title and writes the number into Music's track number field. Leftover spaces are trimmed. If some tracks already have a track number, you're asked once for the whole batch: overwrite, keep the existing number (still strip the title), or skip those tracks.

**Remove whole word/phrase**  
Case-insensitive. `live` removes the word `Live`, not the letters inside `Alive`.

**Find/replace**  
Case-insensitive substring replace across selected titles.

Edits are matched by each track's persistent ID, so changing the selection mid-run won't silently retarget the wrong songs.

## License

MIT. See [LICENSE](LICENSE).
