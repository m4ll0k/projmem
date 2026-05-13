# Screenshots

This directory holds the images referenced from the project README and USAGE doc. Drop each capture here under the exact filename below — the docs already link to these paths, so the images render on GitHub as soon as the files are committed.

## Filenames the docs reference

| Filename | What it shows | Source view (in `projmem ui`) |
|---|---|---|
| `ui-overview.png`    | Full-window screenshot — activity feed (left) + tree/graph/schema (center) + inspector (right). Best taken with a non-trivial repo (this one works) so the graph has nodes. | Default landing view, any center mode |
| `graph.png`          | Force-directed graph view, with at least one node selected so the spotlight halo is visible | `projmem ui` → click **graph** in the top tabs |
| `tree.png`           | Hierarchical directory tree view; show one expanded subtree | `projmem ui` → click **tree** |
| `schema.png`         | Horizontal arborist schema view | `projmem ui` → click **schema** |
| `inspector-code.png` | Inspector's **code** tab showing the line-annotation menu open on a line, plus a gutter dot or two | Select a file → click **code** tab → click any gutter line number |
| `inspector-notes.png`| Inspector's **notes** + **guidance** tab with at least one note pinned, including a `@ line N` badge | Select a file with notes attached |
| `help-modal.png`     | The `? help` popup open, showing the orientation sections | Click the **`? help`** button in the top bar |
| `activity-feed.png`  | The left rail with a mix of `leased`/`edited`/`released`/`note_added` events | Any moment after some agent activity |

## Capture tips

- **Window size**: 1600×1000 looks good in both light and dark mode. The UI is fully responsive, but that ratio matches the markdown layout the docs assume.
- **Theme**: capture in *dark mode* by default (clean against the GitHub repo header). Capture matching pairs (`*-light.png`) if you want side-by-side comparisons later.
- **DPI**: 2× retina captures are welcome — the docs use `<img width="…">` to scale.
- **Sensitive content**: the file tree and graph reveal directory structure. If you're capturing against a private repo, run the captures against a sample repo first.

## License

All screenshots committed here are licensed under the same terms as the rest of the repository (PolyForm Noncommercial 1.0.0).
