# Contributing to cc-discord-kit

Thanks for considering a contribution.

## Before you start

- Open an issue first for anything bigger than a bug fix or doc tweak.
- One logical change per PR. Bundling unrelated changes makes review slow and rollbacks painful.

## Workflow

```bash
git clone https://github.com/<you>/cc-discord-kit.git
cd cc-discord-kit
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# run tests if a tests/ directory exists
pytest 2>/dev/null || true
```

## Sample data and personal info

This kit wires Claude Code into private channels and a personal memory store, so
it's easy to paste real data into an example or fixture by accident. Don't.
Anything that ships in the repo — test fixtures, docstring examples, README
samples, mock payloads, commit subjects — must use generic placeholders:

- Names: `alice`, `bob`, `carol`
- Emails: `user@example.com`
- Addresses / cities: `123 Main St`, `Anytown`
- Companies / tickers: `Acme Corp`, `AAPL`, `MSFT`
- IDs / hosts / tokens: obvious fakes (`123456789012345678`, `example.host`, `<token>`)

Never commit real Discord IDs or channel names, hostnames, home-directory file
paths, addresses, or anything you wouldn't post publicly. Commit subjects are
public surface area too — don't name a private project you forked this from.
When in doubt, grep the diff before you push.

## Commit messages

Conventional commits, one line under ~70 chars:

- `feat: …` new user-visible behavior
- `fix: …` bug fix
- `refactor: …` no behavior change
- `docs: …` documentation only
- `test: …` tests only
- `chore: …` build / deps / CI / housekeeping
- `release: …` version bumps

Body in the imperative; explain the *why* not the *what*. Keep one logical change per commit so `git bisect` stays useful.
