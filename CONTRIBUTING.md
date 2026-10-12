# Contributing to vettarr

Thanks for helping. A few rules keep vettarr a general tool rather than one person's setup.

## What belongs in the code

- **No tracker-specific presets, fixtures or names.** No tracker, group or site is named anywhere in the tree: not in code, comments, tests, docs or screenshots. A tracker in an example or a test is `tracker-a.example`; a group's post wording is a word list in `config.yaml`, never a pattern in code.
- **Software-level presets only.** `config.example.yaml` carries commented templates per tracker software (xbtit, NexusPHP, UNIT3D) and per service (Prowlarr, qBittorrent, Radarr, Sonarr, Seerr). A preset for one particular tracker does not go in, even commented out.
- **Policy words are the user's.** Keyword, genre and subject lists live in each user's own config. Tests use neutral stand-ins (`list-a-word`), and the demo data uses invented titles.
- **Screenshots show demo data only.** Take them from `vettarr demo-seed` as [docs/DEV.md](docs/DEV.md) describes.

## Before a pull request

- Run the tests: `python -m unittest discover -s tests`.
- `config.yaml` is a contract ([docs/CONFIG.md](docs/CONFIG.md)): add optional fields and blocks, never rename or remove one.
- Database migrations only add things: new tables, nullable columns. No renames.
- A change to the UI runs the browser checks and the accessibility check in [docs/DEV.md](docs/DEV.md).
- Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, ...) and say why.

## Before a release: run the leak gate

```sh
scripts/leak-gate.sh --self-test <words-file>
scripts/leak-gate.sh <words-file>
```

`<words-file>` is a private list, one case-insensitive Perl regular expression per line, of the names and words that must never be published: hosts, trackers, groups, people and personal lists. Keep it outside the repository. A line that is one plain word matches whole words only; anything else matches as written. The gate searches every tracked file and every commit message, prints each hit as `path:line` with the words masked, and exits 1 on any hit. `--self-test` first proves the gate can fail: it plants one canary per pattern in a scratch clone and passes only if every one is caught. Release only when both pass.

`git archive` leaves out the agents' and the issue tracker's plumbing (`.gitattributes`), so an archive of a clean commit is what gets published.

## Licence

By contributing you agree that your contribution is licensed under the GNU Affero General Public License, version 3 or later, like the rest of vettarr.
