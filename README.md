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
- Records changes to apps on Supervisor installs: installs, updates, starts,
  stops, restarts, rebuilds, and changes to their settings (see
  [App changes](#app-changes-supervisor-installs)).
- Checks the files of your custom integrations against the official copy, and
  warns about integrations that are archived, removed from the Marketplace, or
  no longer maintained (see [Custom integration checks](#custom-integration-checks)).
- Sends every event to a SIEM such as Wazuh (see
  [Send events to a SIEM](#send-events-to-a-siem)).
- Lets you filter the Audit and Activity tabs and download what you see (see
  [Filter and download](#filter-and-download)).
- Shows everything in an admin-only panel with Overview, Activity, Users,
  Bans, Audit, and Settings tabs. The panel shows country flags, which are
  bundled with it and loaded from your own Home Assistant.
- Creates persistent notifications, sends messages to your phone, fires events,
  and provides sensors, so you can build your own automations.

## Keep Home Assistant off the internet

Electric Fence is defence in depth. It is not a reason to expose Home
Assistant to the internet. The safest Home Assistant is one the internet can't
reach at all. For remote access, in order of preference:

1. **A VPN**, such as WireGuard or NetBird. Your phone and laptop join your
   home network, and Home Assistant stays private. Nothing is exposed.
2. **Home Assistant Cloud** by Nabu Casa. Remote access through an encrypted
   connection, without opening a port on your router.
3. **Exposing Home Assistant yourself**, with port forwarding or a reverse
   proxy, only if neither of the above works for you. Then use TLS, give every
   user two-factor authentication, keep Home Assistant up to date, and set up
   the reverse proxy settings (see [IP bans](#ip-bans)).

Electric Fence is useful in every case: it records who logs in and who changes
what, and warns about admins without two-factor authentication. Over a VPN,
logins come from local network addresses. Those are banned and slowed down
like any other, unless you exempt local addresses (see [IP bans](#ip-bans)).

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

Go to **Settings** > **Devices & services** > **Electric Fence**, and select
the cog (**Configure**) to change these. All of them apply
immediately, without a restart. The dialog has the general settings below, and
the **Bans**, **Username protection**, **Notifications**, **Integration
checks**, and **Log forwarding** sections.

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

**Integration checks**

| Setting                | What it does                                                                                                                                                                                    |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Verify files**       | Compares installed custom integration files with the official copy, or with a local baseline for hand-copied ones (see [Custom integration checks](#custom-integration-checks)). On by default. |
| **Create repairs**     | Raises a repair when an integration check finds a problem. Off leaves the audit log, events and forwarding unchanged. On by default.                                                            |
| **Unmaintained after** | Months without a release before an integration counts as unmaintained, from 1 to 36. The default is 4.                                                                                          |
| **First scan delay**   | Minutes to wait after startup before the first scan, from 1 to 1440. The default is 30.                                                                                                         |
| **Ignored files**      | Files the check skips, one per line as `<integration folder>/<path>`, for example `my_integration/data.json`.                                                                                   |

**Log forwarding**

| Setting            | What it does                                                                                               |
| ------------------ | ---------------------------------------------------------------------------------------------------------- |
| **Forward events** | Sends each event to your collector as a syslog message. Off by default.                                    |
| **Host**           | Host name or IP address of the collector. Required when forwarding is on.                                  |
| **Port**           | From 1 to 65535. The default is 514.                                                                       |
| **Protocol**       | `tcp` (the default), `udp`, or `tls`. Wazuh's own listener accepts TCP and UDP only.                       |
| **CA certificate** | For TLS: PEM text of a self-signed or private certificate, or of the certificate authority that signed it. |

## App changes (Supervisor installs)

On Home Assistant OS and Supervised installs, Electric Fence records changes
to apps (add-ons): installs, uninstalls, updates, starts, stops, restarts,
rebuilds, changes to an app's options, ports and other settings (boot,
watchdog, auto update, ingress panel, protection mode), and apps repositories
added or removed. A record holds the app's name and slug, the action, the
versions before and after an update, and which settings changed. The values
are stored only when **Record before and after values** is on, redacted as
described in [Privacy](#privacy).

- **Where it listens.** Changes are recorded from the Supervisor WebSocket
  commands, the app actions (`hassio.addon_start` and friends), and the app
  update entities, with the user and IP address behind them. A change is
  recorded only once Supervisor reports it succeeded. The `/api/hassio` REST
  proxy can't make app changes: Home Assistant only lets admins reach backups,
  logs, the changelog and the docs there.
- **Integrations that manage their own app.** Z-Wave JS, Matter, OpenThread
  Border Router, the Home Assistant hardware integrations and others install,
  start, update and configure their app through Home Assistant's app manager.
  Those changes are recorded as **System**, with the option names (and values
  when **Record before and after values** is on).
- **Outside Home Assistant.** A change made from the command line, on the host
  or in Supervisor itself shows as **Outside Home Assistant**. Electric Fence
  sees these through Supervisor's events and a comparison of the app list
  every 15 minutes. Starts and stops in a burst, such as after a reboot,
  collapse into one record that lists the apps. Options changed outside are
  recorded without values, as a hash only, whatever **Record before and after
  values** says. A change already recorded from Home Assistant isn't recorded
  twice, and neither are the steps Supervisor reports while it carries the
  change out: an update or restart stops and starts the app, and those stops
  and starts aren't recorded as outside changes while Home Assistant's call
  runs or in the two minutes after it succeeds.
- **System.** Supervisor's automatic updates of apps with auto update on show
  as **System**. Watchdog restarts show as **Outside Home Assistant**, because
  Supervisor doesn't tell Home Assistant they were the watchdog.
- **Background installs and updates** are recorded when Supervisor accepts
  them.
- **Not available** on Container and Core installs. The Settings tab shows
  "Not available on this install type".
- **Turned off** when Home Assistant changed in a way Electric Fence doesn't
  expect. The Settings tab then shows "App auditing is turned off because Home
  Assistant changed (see Repairs)", and a repair says why. If only the app
  update entities or the app manager changed, only that source is skipped,
  with a repair of its own, and the rest keep recording.

## Custom integration checks

Two checks look at the integrations in your `custom_components` folder. Both
run in the background and never stop Home Assistant from starting.

**File verification** spots an integration whose files no longer match what its
author published, which may mean someone altered the code.

- For integrations installed from the Marketplace, Electric Fence builds the
  official file set for the installed version, the way the Marketplace installs
  it, and compares file by file: matching, changed, missing, or added. After a
  Marketplace install or update the new files are checked against the new
  version, so an update isn't flagged, and an install still in progress isn't
  reported as changed.
- **Unverified** means Electric Fence has no official copy to compare with,
  because the integration was copied by hand. The first time it sees one, it
  records a local baseline (file hashes), and flags any file that changes
  afterwards. When the integration's `manifest.json` version changes, the
  baseline is renewed and the audit log says so.
- **Couldn't check** means the official copy wasn't reachable: no release
  asset, a 404, an archive over the size limit, a timeout, an install at a
  commit, or the Marketplace not being set up. It is retried on the next daily
  scan and never treated as altered.
- A symlink inside an integration folder is flagged and never followed.
  Unreadable files are reported as unreadable, not as changed. Bytecode in
  `__pycache__` isn't hashed, since Python rewrites it. Instead, every file
  there that Python would load in place of its source is compiled from that
  source and compared; one that differs is flagged as changed. A `*.pyc`
  outside `__pycache__` can be imported on its own, so it is compared like any
  other file. Use **Ignored files** for integrations that write their own
  files into their folder.

**Integration health** warns about Marketplace integrations whose repository is
archived, removed from the Marketplace, or not updated in more than 4 months
(**Unmaintained after**). It uses what the Marketplace already stores, with no
extra requests. The Marketplace raises its own repair for repositories it has
removed, so Electric Fence records, forwards and shows those, but doesn't
raise a second repair.

Findings go to the audit log (when one appears and when it goes away), to the
events and the SIEM, to the posture warnings on the Overview tab, and, with
**Create repairs** on, to a repair issue that names the integration and its
files. Open an integration on the Overview tab to see the status of each file.

**What is sent to GitHub.** Only plain downloads of the public release zip or
the repository archive for the installed version. That tells GitHub which
integrations you run, as the Marketplace already does. Nothing about your
system is sent, and the REST API isn't used. Turn off **Verify files** to stop
it, which also stops the local baseline.

**Resource use.** The check runs one integration at a time, once a day and once
after the **First scan delay** following startup (and after a Marketplace
install or update). Files are hashed as a stream in a background thread. The
official file hashes are cached per repository and version, so a download only
happens when the installed version changes. Each archive is limited to 25 MB
and a 60 second download, and is read in memory without being extracted into
`custom_components`; Home Assistant's own limits of 50,000 files and 100 MB
expanded also apply.

## Send events to a SIEM

Electric Fence can forward every event it records to a syslog collector or
SIEM, starting with [Wazuh](docs/wazuh/README.md). Turn it on in the **Log
forwarding** section of the settings.

- **What is sent.** Logins (successful and failed), bans, attacks, new-country
  logins, every audit change with the user and IP behind it, app changes,
  integration check findings, and forwarding health. Each event is one JSON
  object (`event_type`, `ts`, and the fields of the event) in a syslog message
  with an RFC 3164 header (`<PRI>Mmm dd hh:mm:ss HOST electric_fence: {json}`).
  The syslog time is UTC without a zone, so use the `ts` field in the JSON.
  The header is RFC 3164 because Wazuh's pre-decoder handles it reliably.
- **Unmasked.** Usernames and IP addresses go out exactly as recorded, to the
  collector you choose. **Mask IP addresses in the panel** only affects the
  panel. Before and after values are included only when **Record before and
  after values** is on, redacted as in the audit log. Passwords, tokens, keys
  and coordinates are never sent.
- **Details.** A change event carries a `details` object: the record's
  details (file lists, versions, the apps of a burst, and so on) without the
  values, with fields named like a secret or a location hidden. Every list in
  it holds at most 50 entries and every text in a list at most 200 characters;
  a cut list has `<name>_truncated: true` beside it. The changed names or files
  are in `changed` at the top level of the event, not repeated in `details`. A
  line never exceeds 8 KB (one UDP datagram): the longest lists are halved,
  and flagged, until it fits.
- **Protocol.** TCP by default, or UDP, or TLS for a collector in front of
  Wazuh such as rsyslog (Wazuh's own listener has no TLS). With TLS, the
  collector's certificate and name are always checked, because events go out
  unmasked. For a self-signed or private certificate, paste it, or the
  certificate authority that signed it, into **CA certificate**, and set
  **Host** to the name or address the certificate is for. TCP and UDP aren't
  encrypted, so use them only on a network you trust. Over TCP and TLS each
  message ends with a newline, with no length prefix, which is what Wazuh and
  Splunk's raw TCP input expect. Over UDP each message is one datagram.
- **Reliability.** Forwarding never makes a login wait. If the collector is
  down or its name doesn't resolve, events queue up to 1,000 and the oldest
  are dropped first. Reconnects back off from 1 second to 5 minutes, and the
  queued events are sent in order once it's back. A forwarding sensor and a
  security warning show the failure.

The [Wazuh page](docs/wazuh/README.md) has the `ossec.conf` listener and a
sample rules file.

## Filter and download

The **Audit** and **Activity** tabs have a **Filters** button with a count of
active filters, and a search box. Filtering happens on the server, so large
histories stay fast.

- **Audit** filters by kind, action, user, IP address, time, and whether to
  show System changes. Changes made outside Home Assistant are always shown.
  **Activity** filters by result (success or failed), user or submitted
  username, IP address, country, and time.
- **Search** matches the target, IP address, username and details.
- Under the menu (**&vellip;**), **Download data** saves the filtered records as
  CSV or JSON. It exports everything the filters match, not just what's on
  screen, up to 100,000 rows. The file is built in your browser and contains
  full, unmasked values, which the file name says (for example
  `electric-fence-audit-2026-10-26-unmasked.csv`). In the CSV, values that
  start with `=`, `+`, `-`, `@`, a tab or a carriage return are neutralised so
  a spreadsheet doesn't run them as formulas, and the file opens as UTF-8.
  The audit CSV has a `details` column with each record's details as JSON;
  before and after values are only in the `values` column.

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
  `network`, `analytics`, `logger`, `recorder`, `ip_ban`, `login_protection`,
  `app`, `files`, `integration_health`, or `forwarding`.
- `action` is one of `added`, `removed`, `updated`, `reconfigured`,
  `options_changed`, `enabled`, `disabled`, `password_changed`,
  `restore_requested`, `confirmed`, `purged`, `withdrawn`, `banned`,
  `unbanned`, `expired`, `attack_started`, `attack_ended`, `slowdown_ended`,
  `countries_cleared`, `installed`, `uninstalled`, `started`, `stopped`,
  `restarted`, `rebuilt`, `settings_changed`, `repository_added`,
  `repository_removed`, `files_changed`, `files_match`, `baseline_recorded`,
  `baseline_renewed`, `archived`, `removed_from_store`, `unmaintained`,
  `recovered`, `forwarding_failed`, or `forwarding_recovered`.
- For `login_protection`, `target_name` is the attacked username, masked
  unless it belongs to an existing user.
- `changed` lists the names of the settings that changed, never their values.
  Integration options are namespaced as `options.<key>`, and the panel shows
  them as "Option: ...". It holds at most 50 names; a longer list (such as the
  files of an integration check) is cut and `changed_truncated` is `true`.

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
24 hours by default, and expire by themselves.

IPv6 is counted per prefix, a /64 by default (**IPv6 prefix length**). One
IPv6 host usually holds a whole /64, so changing address doesn't escape the
count. While an address is banned, logins from the rest of its prefix are
refused too, recorded as `banned`. Those refusals don't count toward new bans.
Electric Fence keeps at most 500 active bans. Past that, it raises a repair
issue and stops banning until bans expire or are lifted; failed logins are
still recorded, and username protection still works. Failures that count are an
unknown username, a wrong password, wrong credentials, a wrong two-factor
code, and a throttled attempt. Invalid tokens and blocked users don't count.

- **Unban.** Open the **Bans** tab to see who is banned, and why, and to lift a
  ban. This works on bans Home Assistant added itself, too. Lifting a ban also
  resets Home Assistant's own failure counter for that address, so one more
  typo doesn't ban it again at once.
- **Never banned.** This machine (loopback), the Supervisor, and your trusted
  proxies.
- **Local addresses.** Private ranges, 100.64.0.0/10 (including Tailscale),
  link-local and IPv6 unique-local addresses are counted and banned like any
  other: a compromised device on your network is an attacker too. Turn on
  **Never ban or slow down local addresses** to exempt them. Do that if Home
  Assistant runs in Docker without host networking, where every device on
  your network can appear as the same address.
- **Trusted after success.** A successful login trusts its address for that
  username, within the ban length (24 hours if bans never expire). The address
  isn't banned for that user's own typos, but failures for any other username
  still ban it. A successful login resets the count of its own username's
  failures only.
- **Repeat offenders.** With **Lengthen bans for repeat offenders** on, each
  further ban of the same address within 30 days doubles the length. The fourth
  is permanent. A ban length of 0 is always permanent.

Bans need Home Assistant's IP ban to be switched on. It's on unless you
turned it off. To check, go to **Settings** > **System** > **Network** >
**HTTP server** > **IP banning**, and make sure **Enable IP banning** is on.
Saving restarts Home Assistant. If it's off, Electric Fence raises a repair
issue and can't ban anything. The **Auto ban available** entity shows whether
it can.

Home Assistant keeps these HTTP server settings itself and no longer reads an
`http:` block in `configuration.yaml`. An existing block was imported once,
and Home Assistant raises a repair issue while it's still there.

> [!IMPORTANT]
> If Home Assistant runs behind a reverse proxy, turn on **Trust
> X-Forwarded-For** and add the proxy to **Trusted proxies**, in the **Reverse
> proxy** section of the same **HTTP server** settings. Without them, every
> visitor appears to come from the proxy's address. Proxy addresses are never
> banned, so bans then do nothing. Check the proxy's address is a trusted
> proxy, and not a public address you'd lock yourself out of.

### Username protection

When one username fails 10 times from at least 3 addresses within 15 minutes,
Electric Fence treats it as an attack and slows down logins to that username
until it stops. Attempts are handled one at a time. Each failed attempt makes
the next wait longer, starting at 2 seconds, doubling, and capped at 30
seconds. At most 3 attempts wait. Any more are refused with "Too many login
attempts. Try again later." A known device isn't slowed down or refused: an
address (for IPv6, its prefix) that logged in successfully as that username
within the last 30 days. An attacker can't lock you out by filling the queue.
Attacks show in the panel, in the **Login attack** entity, and in
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
nothing anywhere, apart from downloading the GeoIP database, doing reverse DNS
lookups, downloading public release files from GitHub for the integration
checks (see [Custom integration checks](#custom-integration-checks)), and
forwarding events to the SIEM you set up. Forwarding sends usernames and IP
addresses unmasked to that SIEM. Reverse DNS is on by default. It sends each
IP address to the DNS resolver your system is configured to use. You can turn
it off in the settings.

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

- Bans are per IP address only. There are no ranges or countries yet.
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
- App auditing needs Supervisor, so it isn't available on Container and Core
  installs or in the dev container.
- Watchdog restarts of apps show as **Outside Home Assistant**, since
  Supervisor doesn't identify them to Home Assistant.

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

## Contributing

Contributions are welcome. [Contributing](CONTRIBUTING.md) explains how to get
set up and what a pull request needs, and everyone taking part follows the
[code of conduct](CODE_OF_CONDUCT.md). For help, see [Support](SUPPORT.md);
for security problems, see the [security policy](SECURITY.md).
[Responsible use](RESPONSIBLE_USE.md) covers what Electric Fence is for and
how to handle the data it records.

## License

Electric Fence is released under the [Apache License 2.0](LICENSE). Copyright
and third-party notices are in [NOTICE](NOTICE).
