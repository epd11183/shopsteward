# Contributing to ShopSteward

Thanks for the interest! This is a nights-and-weekends project being built in
the open. A few ground rules keep it moving:

- **Read `docs/PRD_v2.md` first.** The three-gates principle and the
  architecture rules in `CLAUDE.md` are load-bearing; PRs that add operator
  touchpoints or bypass the adapter layer will be asked to redesign.
- **One milestone per PR.** Check the milestone table (PRD §10) and keep scope
  tight.
- **No live API calls in tests.** Adapters are tested against scrubbed,
  recorded fixtures. Never commit a raw API response or any real shop data.
- **Run `uv run ruff check .` and `uv run pytest`** before opening a PR — CI
  enforces both, plus secret scanning (gitleaks).
- **New POD provider or channel?** Open an issue first. Providers must fit the
  existing adapter interface; if the interface needs to grow, that's a design
  discussion, not a drive-by PR.

Using Claude Code on this repo? `CLAUDE.md` and `.claude/settings.json` are
checked in and will configure your session automatically. Put personal
overrides in `.claude/settings.local.json` (gitignored).
