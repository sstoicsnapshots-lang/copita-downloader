# Contributing to Copita

Thanks for helping out.

## Setup

```bash
git clone https://github.com/sstoicsnapshots-lang/copita-downloader.git
cd copita-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Before opening a pull request

- Run the test suite: `python -m unittest discover -s tests` — it should be
  green (a couple of `libtorrent` / `playwright` import errors are expected
  if those optional extras aren't installed).
- Match the style of the file you're editing.
- Keep user‑facing strings plain and jargon‑free. Copita is meant for people
  who don't know or care what a codec or a signature challenge is — if
  something breaks, the app should handle it, not explain it.
- One focused change per PR.

## Reporting a bug

Open an issue with the link you used (or a similar public one), what you
expected, and the exact error text from the task's **Technical details**.

## License

By contributing you agree that your work is licensed under the
[GPL‑3.0](LICENSE), the same as the rest of the project.
