# desktop helpers

Run on a workstation, not in the container:

- `download_export.py` — assistant-export manifest in, zips landed in the vault's `.imports/` via your browser (one-time URLs sit behind a browser check no headless client passes). Set `VAULT_ROOT` to your vault path.
- `Import Claude Export.cmd` — legacy one-click wrapper; superseded by the in-cluster watcher once it ships.

A future desktop app will replace these: it will collect assistant exports (Claude) and local chat stores (e.g. Jan threads) and land them on the vault share in the standard import format.
