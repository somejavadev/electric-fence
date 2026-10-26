<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/somejavadev/electric-fence/main/custom_components/electric_fence/brand/dark_logo.png">
  <img alt="Electric Fence" src="https://raw.githubusercontent.com/somejavadev/electric-fence/main/custom_components/electric_fence/brand/logo.png" height="64">
</picture>

# Electric Fence

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=somejavadev-github_electric-fence&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=somejavadev-github_electric-fence)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=somejavadev-github_electric-fence&metric=coverage)](https://sonarcloud.io/summary/new_code?id=somejavadev-github_electric-fence)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=somejavadev-github_electric-fence&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=somejavadev-github_electric-fence)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=somejavadev-github_electric-fence&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=somejavadev-github_electric-fence)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=somejavadev-github_electric-fence&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=somejavadev-github_electric-fence)
[![Vulnerabilities](https://sonarcloud.io/api/project_badges/measure?project=somejavadev-github_electric-fence&metric=vulnerabilities)](https://sonarcloud.io/summary/new_code?id=somejavadev-github_electric-fence)

Electric Fence is a Home Assistant integration that records failed and
successful logins, and who changed settings. It bans addresses that keep
failing to log in, slows down guessing of one username, and tells you about
logins from a new country. If something goes wrong inside Electric Fence, Home
Assistant carries on as if it weren't installed.

Screenshots are coming.

## What it does

- Records failed logins, including unknown usernames, wrong passwords, wrong
  two-factor codes, and invalid tokens, with the IP address, country, and host
  name.
- Records successful logins.
- Records who changed settings, when, and from which IP address: integrations,
  users, core settings, backups (settings changes, a deleted backup, and a
  requested restore, which is recorded as "requested" before it runs; see
  [Known limits](#known-limits) for why it rarely survives), HTTP and
  IP ban settings (submitted, withdrawn, and confirmed after the restart),
  network adapters, analytics preferences, log levels, and the recorder
  (disable, enable, purge, and purge entities). A change without a person
  behind it, such as an automation, shows as **System**. Log level and recorder
  actions are recorded only when they succeed.
- Bans an IP address after repeated failed logins, through Home Assistant's own
  IP ban list (see [Protection](#protection)).
- Slows down logins to a username that is being guessed from many addresses.
- Alerts you when a user logs in from a country they haven't logged in from
  before.
- Shows everything in an admin-only panel with Overview, Activity, Users,
  Bans, Audit, and Settings tabs. The panel shows country flags, which are
  bundled with it and loaded from your own Home Assistant.
- Creates persistent notifications, sends messages to your phone, fires events,
  and provides sensors, so you can build your own automations.

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

Electric Fence then appears in the sidebar for admins. Its sidebar icon shows
after the next browser reload, or as soon as you open the panel.

The minimum supported version is Home Assistant 2026.11.0.

## Settings

Select **Configure** on the integration to change these. All of them apply
immediately, without a restart. The dialog has the general settings below, and
the **Bans**, **Username protection**, and **Notifications** sections.

| Setting                            | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **History retention**              | Days of history to keep, from 1 to 3650. The default is 90.                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| **GeoIP provider**                 | Where country lookups come from: DB-IP Lite (no account needed), MaxMind GeoLite2 (needs an account ID and license key), or none. Choosing MaxMind opens a second step for the credentials, which are checked before saving.                                                                                                                                                                                                                                                                         |
| **Reverse DNS lookups**            | Looks up host names for IP addresses. On by default.                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| **Record before and after values** | Stores what each setting was and what it became, with sensitive values hidden (see [Privacy](#privacy)). Turning this off also removes the values already stored. Turning it on doesn't fill in earlier changes. Changing it is itself recorded. Off by default.                                                                                                                                                                                                                                     |
| **Mask IP addresses in the panel** | Shows only the start of each IP address in the Electric Fence panel: `203.x.x.x` for IPv4 and `2001:db8:x:x:x:x:x:x` for IPv6. Host names show as "hidden", because they often contain the address. The address of the Home Assistant a browser logged in through is masked too (`https://hidden/`); the official apps keep their name. Handy for screenshots. Full addresses are still stored and used (for example by Unban); notifications, events and the logbook are unchanged. Off by default. |

**Bans**

| Setting                                | What it does                                                                                                      |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| **Ban automatically**                  | Bans an IP address that fails too many logins. On by default.                                                     |
| **Failed attempts**                    | Failed logins from one address before it's banned, from 1 to 100. The default is 5.                               |
| **Within**                             | The period in which they're counted, from 1 to 1440 minutes. The default is 15.                                   |
| **Ban length**                         | Hours a ban lasts, from 0 to 8760. 0 means it never expires. The default is 24.                                   |
| **Lengthen bans for repeat offenders** | Doubles the ban for each further ban of the same address within 30 days. The fourth is permanent. Off by default. |

**Username protection**

| Setting               | What it does                                                                                  |
| --------------------- | --------------------------------------------------------------------------------------------- |
| **Protect usernames** | Treats many failures against one username from several addresses as an attack. On by default. |
| **Failed attempts**   | Failed logins against one username, from 2 to 1000. The default is 10.                        |
| **From at least**     | Different addresses the failures must come from, from 1 to 100. The default is 3.             |
| **Within**            | The period in which they're counted, from 1 to 1440 minutes. The default is 15.               |

**Notifications**

Turning a notification off stops only the message. Events, the panel, the
audit log, and the entities are unaffected.

| Setting                                     | What it does                                                                                                                        |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Failed login**                            | Creates a persistent notification when a login fails. On by default.                                                                |
| **Home Assistant's own login notification** | **Replace** dismisses Home Assistant's own login notification while failed login notifications are on. **Keep** leaves it in place. |
| **IP address banned**                       | A message when an address is banned. On by default.                                                                                 |
| **Username under attack**                   | A message when a username is being guessed from many addresses. On by default.                                                      |
| **Login from a new country**                | A message when someone logs in from a country not seen before. On by default.                                                       |
| **Ban ended**                               | A message when a ban expires or is lifted. Off by default.                                                                          |
| **Phone notifications**                     | Notify entities, such as phones, that also receive these messages.                                                                  |

## Events

Electric Fence fires these events on the Home Assistant event bus.

| Event                              | Fired when                               |
| ---------------------------------- | ---------------------------------------- |
| `electric_fence_login_failed`      | A login fails.                           |
| `electric_fence_login_succeeded`   | A login succeeds.                        |
| `electric_fence_settings_changed`  | A setting is changed (see `kind` below). |
| `electric_fence_ip_banned`         | Electric Fence bans an IP address.       |
| `electric_fence_username_attack`   | A username attack starts or ends.        |
| `electric_fence_new_country_login` | A user logs in from a new country.       |

A failed login event has these fields: `username`, `username_masked`,
`user_exists`, `user_id`, `reason`, `ip`, `country`, `hostname`, `source`,
`client`, and `count`. `user_id` is set when the typed username belongs to an
existing user (for example a wrong password or a wrong two-factor code), and
is `null` otherwise.

A successful login event has these fields: `user_id`, `username` (the user's
name, never the typed text), `ip`, `country`, `source`, `client`, and
`mfa_used`.

An IP ban event has these fields: `ip`, `country`, `hostname`, `failures`,
`expires_at` (`null` for a permanent ban), and `ban_number` (how many times
Electric Fence has banned this address within 30 days).

A username attack event has these fields: `username`, `username_masked`,
`ips` (up to 20), `ip_count`, `countries`, `started_at`, and `state`, which is
`started` or `ended`. As in a failed login event, `username` is shown in full
only when it belongs to an existing user, and masked otherwise.

A new country login event has these fields: `user_id`, `username` (the user's
name), `ip`, `country`, `hostname`, and `previous_countries_count`.

A settings change event has these fields: `kind`, `action`, `target`,
`target_name`, `changed`, `changed_by_user_id`, `changed_by_name`, and `ip`. A
change without a person behind it has no user and shows as **System** in the
panel.

- `kind` is one of `config_entry`, `user`, `core_config`, `backup`, `http`,
  `network`, `analytics`, `logger`, `recorder`, `ip_ban`, or
  `login_protection`.
- `action` is one of `added`, `removed`, `updated`, `reconfigured`,
  `options_changed`, `enabled`, `disabled`, `password_changed`,
  `restore_requested`, `confirmed`, `purged`, `withdrawn`, `banned`,
  `unbanned`, `expired`, `attack_started`, `attack_ended`, `slowdown_ended`, or
  `countries_cleared`.
- For `login_protection`, `target_name` is the attacked username, masked
  unless it belongs to an existing user.
- `changed` lists the names of the settings that changed, never their values.
  Integration options are namespaced as `options.<key>`, and the panel shows
  them as "Option: ...".

## Sensor

The **Failed logins** sensor (`sensor.electric_fence_failed_logins`) counts
failed and blocked login attempts in the last 24 hours. Its attributes describe
the most recent one: `last_username`, `last_ip`, `last_country`, and
`last_reason`. The reason is one of `no_such_user`, `wrong_password`,
`invalid_credentials`, `wrong_2fa_code`, `invalid_token`,
`blocked_inactive_user`, `blocked_local_only`, or `throttled`.

## Entities

All entities belong to the **Electric Fence** device. Their states are counts,
timestamps, or on and off. The few attributes that hold an IP address, a
country, or a name are kept out of the recorder.

| Entity                                            | State                                                                           |
| ------------------------------------------------- | ------------------------------------------------------------------------------- |
| `sensor.electric_fence_failed_logins`             | Failed and blocked logins in the last 24 hours.                                 |
| `sensor.electric_fence_active_bans`               | Active IP bans. Attributes: `last_ip`, `last_country`.                          |
| `sensor.electric_fence_bans_in_the_last_24_hours` | Bans in the last 24 hours.                                                      |
| `sensor.electric_fence_attacked_usernames`        | Usernames under attack now.                                                     |
| `sensor.electric_fence_last_failed_login`         | When the last failed or blocked login happened.                                 |
| `sensor.electric_fence_last_ban`                  | When the last ban happened.                                                     |
| `sensor.electric_fence_last_new_country_login`    | When the last login from a new country happened. Attributes: `user`, `country`. |
| `sensor.electric_fence_admins_without_mfa`        | Active administrators without two-factor authentication.                        |
| `sensor.electric_fence_geoip_updated`             | The date of the GeoIP database. A diagnostic entity.                            |
| `binary_sensor.electric_fence_login_attack`       | On while a username is under attack.                                            |
| `binary_sensor.electric_fence_security_warnings`  | On while any security check on the Overview tab warns.                          |
| `binary_sensor.electric_fence_auto_ban_available` | On while Electric Fence can ban addresses. A diagnostic entity.                 |

The admin panel is in the sidebar and at `/electric-fence`.

## Protection

### IP bans

When an address fails to log in too many times within the window (5 in 15
minutes by default), Electric Fence adds it to Home Assistant's own ban list
(`ip_bans.yaml`). Home Assistant then refuses every request from it. Bans last
24 hours by default, and expire by themselves. Failures that count are an
unknown username, a wrong password, wrong credentials, a wrong two-factor
code, and a throttled attempt. Invalid tokens and blocked users don't count.

- **Unban.** Open the **Bans** tab to see who is banned, and why, and to lift a
  ban. This works on bans Home Assistant added itself, too. Lifting a ban also
  resets Home Assistant's own failure counter for that address, so one more
  typo doesn't ban it again at once.
- **Never banned.** Local network addresses (loopback, private ranges,
  100.64.0.0/10, link-local, and IPv6 unique-local), the Supervisor, and your
  `trusted_proxies`.
- **Trusted after success.** An address with a successful login within the ban
  length (24 hours if bans never expire) isn't banned automatically. A
  successful login resets that address's count.
- **Repeat offenders.** With **Lengthen bans for repeat offenders** on, each
  further ban of the same address within 30 days doubles the length. The fourth
  is permanent. A ban length of 0 is always permanent.

Bans need Home Assistant's IP ban to be switched on. Add this to your
`configuration.yaml` and restart:

```yaml
http:
  ip_ban_enabled: true
```

It's on unless you turned it off. If it's off, Electric Fence raises a repair
issue and can't ban anything. The **Auto ban available** entity shows whether
it can.

> [!IMPORTANT]
> If Home Assistant runs behind a reverse proxy, set `use_x_forwarded_for` and
> `trusted_proxies` under `http:`. Without them, every visitor appears to come
> from the proxy's address. Proxy addresses are never banned, so bans then do
> nothing. Check the proxy's address is a `trusted_proxies` entry, and not a
> public address you'd lock yourself out of.

### Username protection

When one username fails 10 times from at least 3 addresses within 15 minutes,
Electric Fence treats it as an attack and slows down logins to that username
until it stops. Attempts are handled one at a time. Each failed attempt makes
the next wait longer, starting at 2 seconds, doubling, and capped at 30
seconds. At most 3 attempts wait. Any more are refused with "Too many login
attempts. Try again later." Your own addresses on the local network aren't
slowed down. Attacks show in the panel, in the **Login attack** entity, and in
the `electric_fence_username_attack` event.

### New country alerts

Electric Fence remembers the countries each user has logged in from. The first
successful login from a country a user hasn't used before fires
`electric_fence_new_country_login` and sends a message. The user's first
country is learned silently. Logins with no country (GeoIP off) and logins from the local network are
ignored.

### Notifications

Choose which events send a message in the **Notifications** section of the
settings. Messages appear as persistent notifications. Add notify entities
(such as the Home Assistant companion app on your phone) to **Phone
notifications** to get them there too.

Failed logins and bans reach your phone at most once every 10 minutes each.
The persistent notification always shows the latest. Bans made in between are
summed up in one message, for example "3 addresses banned since the last
message".

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
  sensor, the logbook, and username attack messages) a username that doesn't
  belong to a known user is masked, for example `ad•••`.
- **Network details.** IP addresses, host names (from reverse DNS), URLs, and
  user agents are stored. **Mask IP addresses in the panel** hides addresses,
  host names and login URLs on screen only; it doesn't change what is stored.
- **Setting values.** With **Record before and after values** on, values are
  redacted before they are stored, and a redacted value shows as "changed
  (hidden)". Hidden by name: passwords, passcodes, secrets, tokens, keys,
  credentials, cookies, authorization headers, PINs, codes (such as alarm
  codes, so `country_code` is hidden too), one-time passwords (`otp`), and
  coordinates (latitude, longitude, elevation, radius). Hidden by shape:
  credentials and query values in URLs, `user:password@host` credentials
  without a scheme, authorization values (`Bearer`, `Basic`, `Token`, or
  `Digest` followed by a value), opaque path segments such as webhook IDs, long
  random-looking strings of letters and digits, and keys and certificates. A
  change in which only hidden parts differ shows as "changed (hidden)" as a
  whole. An integration's connection `data` is never stored. A secret that
  looks like plain text under an unrecognisable name can still show.
- **Known countries.** For new country alerts, each user's countries are kept
  as country codes only (for example `DE`), with no IP addresses. They aren't
  removed by the retention period. They're deleted with the user, and you can
  clear all of them with **Clear known countries** on the **Settings** tab.
- **Entities.** Entity states hold counts, timestamps, or on and off only.
  Usernames and IP addresses appear only in a few attributes, which are kept
  out of the recorder.
- **Retention.** Records older than the retention period are deleted. The
  default is 90 days.
- **Removal.** Removing the integration lifts Electric Fence's active bans
  (other bans in `ip_bans.yaml` stay), then deletes
  `.storage/electric_fence.db` and the `.storage/electric_fence/` folder, which
  holds the GeoIP database.
- **Recorder.** The sensor's `last_username`, `last_ip`, and `last_country`
  attributes are excluded from the recorder, so they don't end up in your
  long-term history. To keep the events out of the recorder too, add this to
  your `configuration.yaml`:

  ```yaml
  recorder:
    exclude:
      event_types:
        - electric_fence_login_failed
        - electric_fence_login_succeeded
        - electric_fence_settings_changed
        - electric_fence_ip_banned
        - electric_fence_username_attack
        - electric_fence_new_country_login
  ```

### Attribution

IP geolocation by [DB-IP](https://db-ip.com). The DB-IP Lite country database
is licensed under
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).

## Known limits

- Bans are per IP address only. There are no ranges or countries in v0.2.
- A ban whose record Electric Fence couldn't store in its database still
  works, but after a restart it shows as **Other** in the Bans tab and never
  expires. A ban that couldn't be written to `ip_bans.yaml` disappears when
  Home Assistant restarts.
- Electric Fence can't ban or slow down an address that Home Assistant sees as
  a local or proxy address (see [Protection](#protection)).
- Admin actions that Electric Fence can't tie to a person, and changes an
  integration makes to itself, show as **System**.
- Adding or removing a login (credentials) for an existing user isn't
  recorded.
- A full restore of Home Assistant from a backup replaces the whole
  configuration directory, including `.storage/electric_fence.db`, with the
  backup's copy. The "restore requested" record, and everything recorded since
  that backup was made, is then gone. The record survives only a restore that
  fails, or one that restores just the database or add-ons.
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
| `scripts/build_brand`    | Renders the brand images from the SVG sources in `brand-src`.   |

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
