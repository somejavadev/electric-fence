# Electric Fence

Electric Fence is a Home Assistant integration that records failed and
successful logins, and who changed settings. It never affects logins: if
something goes wrong inside Electric Fence, Home Assistant carries on as if it
weren't installed.

Screenshots are coming.

## What it does

- Records failed logins, including unknown usernames, wrong passwords, wrong
  two-factor codes, and invalid tokens, with the IP address, country, and host
  name.
- Records successful logins.
- Records who added, changed, or removed users, integrations, and core
  settings.
- Shows everything in an admin-only panel with Overview, Activity, Users,
  Audit, and Settings tabs.
- Creates a persistent notification, fires events, and provides a sensor, so
  you can build your own automations.

## Install

Install Electric Fence from the Marketplace built into Home Assistant 2026.11
and newer, or from the Home Assistant Community Store (HACS). Until Electric
Fence is in the default catalog, it's a custom repository. You can also copy
`custom_components/electric_fence` into the `custom_components` folder of your
configuration directory.

1. Add this repository as a custom repository of type **Integration** in the
   Marketplace or in HACS, then download **Electric Fence**.
2. Restart Home Assistant.
3. Add the integration:

   [![Add the integration to your Home Assistant instance.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=electric_fence)

The minimum supported version is Home Assistant 2026.11.0.

## Settings

Select **Configure** on the integration to change these. All of them apply
immediately, without a restart.

| Setting                                     | What it does                                                                                                                                                                                                                 |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Notify on failed login**                  | Creates a persistent notification when a login fails.                                                                                                                                                                        |
| **Home Assistant's own login notification** | **Replace** dismisses Home Assistant's own login notification while notifications are on. **Keep** leaves it in place.                                                                                                       |
| **History retention**                       | Days of history to keep, from 1 to 3650. The default is 90.                                                                                                                                                                  |
| **GeoIP provider**                          | Where country lookups come from: DB-IP Lite (no account needed), MaxMind GeoLite2 (needs an account ID and license key), or none. Choosing MaxMind opens a second step for the credentials, which are checked before saving. |
| **Reverse DNS lookups**                     | Looks up host names for IP addresses. On by default.                                                                                                                                                                         |

## Events

Electric Fence fires these events on the Home Assistant event bus.

| Event                             | Fired when                                       |
| --------------------------------- | ------------------------------------------------ |
| `electric_fence_login_failed`     | A login fails.                                   |
| `electric_fence_login_succeeded`  | A login succeeds.                                |
| `electric_fence_settings_changed` | A user, integration, or core setting is changed. |

A failed login event has these fields: `username`, `username_masked`,
`user_exists`, `user_id`, `reason`, `ip`, `country`, `hostname`, `source`,
`client`, and `count`. `user_id` is set when the typed username belongs to an
existing user (for example a wrong password or a wrong two-factor code), and
is `null` otherwise.

A successful login event has these fields: `user_id`, `username` (the user's
name, never the typed text), `ip`, `country`, `source`, `client`, and
`mfa_used`.

A settings change event has these fields: `kind`, `action`, `target`,
`target_name`, `changed_by_user_id`, `changed_by_name`, and `ip`. A change
without a person behind it has no user and shows as **System** in the panel.

- `kind` is one of `config_entry`, `user`, or `core_config`.
- `action` is one of `added`, `removed`, `updated`, `reconfigured`,
  `options_changed`, `enabled`, `disabled`, or `password_changed`.

## Sensor

The **Failed logins** sensor (`sensor.electric_fence_failed_logins`) counts
failed and blocked login attempts in the last 24 hours. Its attributes describe
the most recent one: `last_username`, `last_ip`, `last_country`, and
`last_reason`. The reason is one of `no_such_user`, `wrong_password`,
`invalid_credentials`, `wrong_2fa_code`, `invalid_token`,
`blocked_inactive_user`, or `blocked_local_only`.

The admin panel is in the sidebar and at `/electric-fence`.

> [!WARNING]
> Sensor attributes and event fields contain text typed by whoever tried to
> log in, which means an attacker controls it. Never render it as HTML in a
> template or a card. Electric Fence shortens it and strips line breaks, but
> treat it as untrusted text anyway.

## Privacy

Electric Fence stores data on your Home Assistant system only. It sends
nothing anywhere, apart from downloading the GeoIP database and doing reverse
DNS lookups. Reverse DNS is on by default. It sends each IP address to the DNS
resolver your system is configured to use. You can turn it off in the settings.

- **Usernames.** The username someone typed is stored, and shown in full only
  in the admin panel. Everywhere else (the notification, the events, the
  sensor, and the logbook) a username that doesn't belong to a known user is
  masked, for example `ad•••`.
- **Network details.** IP addresses, host names (from reverse DNS), URLs, and
  user agents are stored.
- **Retention.** Records older than the retention period are deleted. The
  default is 90 days.
- **Removal.** Removing the integration deletes `.storage/electric_fence.db`
  and the `.storage/electric_fence/` folder, which holds the GeoIP database.
- **Recorder.** The sensor's `last_username` and `last_ip` attributes are
  excluded from the recorder, so they don't end up in your long-term history.
  To keep the events out of the recorder too, add this to your
  `configuration.yaml`:

  ```yaml
  recorder:
    exclude:
      event_types:
        - electric_fence_login_failed
        - electric_fence_login_succeeded
        - electric_fence_settings_changed
  ```

### Attribution

IP geolocation by [DB-IP](https://db-ip.com). The DB-IP Lite country database
is licensed under
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).

## Known limits

- Electric Fence doesn't block anything in v0.1. It records and tells you.
- Admin actions that Electric Fence can't tie to a person, and changes an
  integration makes to itself, show as **System**.
- Adding or removing a login (credentials) for an existing user isn't
  recorded.
- The minimum supported version is Home Assistant 2026.11.0. The integration
  refuses to load on betas and development builds below it, including
  `2026.11.0.dev0`.
- Supervisor features aren't available in the dev container.

## Development

### Try it

> [!NOTE]
> Until Home Assistant 2026.11.0 is released, the dev container installs a
> development build (`2026.11.0.dev0`), which is below the minimum version, so
> the integration refuses to load there (there is deliberately no bypass).
> Once a 2026.11 tag exists, set `HA_REF` to it and rerun `scripts/setup` (see
> [How Home Assistant is installed](#how-home-assistant-is-installed)). Until
> then, use the test suite (`scripts/test`), which runs Electric Fence against
> the development build with the version check pinned to 2026.11.0.

1. Open the repository in the dev container (VS Code: **Dev Containers: Reopen
   in Container**). It installs everything with `scripts/setup`.
2. Run `scripts/develop`, then open `http://localhost:8123`.
3. Finish onboarding, and create your admin user.
4. Go to **Settings** > **Devices & services**, and add **Electric Fence**.
5. Create a second user in **Settings** > **People**.
6. Sign out, and try to log in with a username that doesn't exist.
7. Sign back in, and open **Electric Fence** in the sidebar to watch the
   Overview.
8. Optional: to see Home Assistant's own IP ban as well, set a login attempts
   threshold in **Settings** > **System** > **Network**, and confirm the new
   configuration after the restart.

The dev configuration has no `http:` block on purpose. Since 2026.11, Home
Assistant imports a YAML `http:` block as a pending configuration that has to
be confirmed within 5 minutes. Unconfirmed, it reverts and restarts, and
without Supervisor nothing starts Home Assistant again.

### Scripts

| Script                   | What it does                                                    |
| ------------------------ | --------------------------------------------------------------- |
| `scripts/setup`          | Installs Home Assistant, the test tooling, and the panel build. |
| `scripts/develop`        | Runs Home Assistant with `.devcontainer/config`.                |
| `scripts/frontend-watch` | Rebuilds the panel when files change.                           |
| `scripts/test`           | Runs the Python and frontend tests.                             |
| `scripts/lint`           | Runs ruff, mypy, ESLint, and Prettier.                          |

Build the panel before Home Assistant starts. The static path is registered
once, with the folder as it exists at startup, so a panel built afterward only
shows up after a restart. `scripts/develop` builds the panel if
`frontend_dist` is missing. After that, `scripts/frontend-watch` rebuilds on
changes, and a browser reload picks them up.

### How Home Assistant is installed

No build of `pytest-homeassistant-custom-component` exists on PyPI for Home
Assistant 2026.11. So `scripts/setup` runs `scripts/ci_install_ha.sh`, which
clones Home Assistant Core and generates the test plugin from it. It uses the
`dev` ref by default until 2026.11.0 is released. Set `HA_REF` to use another
tag or branch:

```bash
HA_REF=2026.11.0 scripts/setup
```

`requirements_test.txt` only lists tooling and extra test requirements. Once
the plugin supports 2026.11, the dependency will switch back to PyPI (or to
Core's `pytest-home-assistant` package) and the script can retire.

## License

Electric Fence is released under the [Apache License 2.0](LICENSE).
