# Contributing to Electric Fence

Thanks for helping. Bug reports, fixes, documentation and ideas are all
welcome. This page covers how to get set up and what a change needs before it
can be merged.

By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Security problems** go through a
  [private report](https://github.com/somejavadev/electric-fence/security/advisories/new),
  never a public issue. See the [security policy](SECURITY.md).
- **Questions** go to
  [Discussions](https://github.com/somejavadev/electric-fence/discussions).
- **Bugs and feature requests** go to
  [Issues](https://github.com/somejavadev/electric-fence/issues/new/choose).
  Search first; someone may have reported it already.
- **Bigger changes** (a new feature, a new setting, anything that touches how
  logins are handled): open an issue first, so we can agree on the approach
  before you spend time on it.

## Ground rules

Electric Fence sits in Home Assistant's login path. A few rules follow from
that, and every change is reviewed against them:

- **Never break or slow down logins.** Work that isn't needed to answer the
  login request happens afterwards, in the background.
- **Fail open.** If something inside Electric Fence goes wrong, Home Assistant
  carries on as if it weren't installed. Errors are caught, logged, and shown
  as a repair or posture warning, never raised into Home Assistant.
- **Fail early on upstream changes.** Electric Fence relies on some of Home
  Assistant Core's internals. Each one has a contract test and a runtime
  check, so a change in Core switches the affected part off and says so,
  instead of misbehaving. Add both when you rely on something new in Core,
  and add its Core path to `WATCHED` in `scripts/upstream_watch.sh` so the
  daily upstream watch reports changes to it.
- **Keep data local.** Nothing leaves the Home Assistant system unless the
  user turned it on (GeoIP downloads, reverse DNS, log forwarding). Don't add
  calls to third-party services.
- **Follow Home Assistant's own guidelines** for integrations and frontend
  code, so the project stays acceptable for the Marketplace and for upstream
  contributions.

## Setting up

The quickest way is the dev container: open the repository in VS Code and
choose **Dev Containers: Reopen in Container**. It runs `scripts/setup`, which
installs Home Assistant, the test tooling, and the panel build.

[Development](README.md#development) in the README covers running Home
Assistant locally, the scripts, and how Home Assistant is installed.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Make your change, with tests:
   - Python tests live in `tests/` and run with `pytest`.
   - Panel tests live in `frontend/test/` and run with `vitest`.
   - A bug fix comes with a test that fails without the fix.
3. Run the checks:

   ```bash
   scripts/test   # Python and frontend tests
   scripts/lint   # ruff, mypy, ESLint, and Prettier
   ```

4. Update the README when behaviour, settings or events change.
5. Open a pull request against `main` and fill in the template.

Pull requests are squash-merged, so the pull request title becomes the commit
message. Write it in the imperative, with a type prefix, for example
`fix: keep the integrations card current after scheduled scans`. Use `feat`,
`fix`, `docs`, `test`, `refactor` or `chore`.

The tests, frontend, hassfest and HACS checks must pass before a pull request
can be merged.

## Screenshots and logs

Screenshots, logs and diagnostics from a real system contain IP addresses,
usernames and host names. Turn on **Mask IP addresses in the panel** before
taking screenshots, and remove anything personal from logs before posting
them.

## AI-assisted contributions

You may use AI tools to help write code or documentation. You are responsible
for what you submit: you must understand it, have tested it, and be able to
explain it in review.

## Licence

Electric Fence is released under the [Apache License 2.0](LICENSE). Unless you
say otherwise, anything you contribute is licensed under the same terms, as
section 5 of the licence describes. There is no separate contributor
agreement to sign.
