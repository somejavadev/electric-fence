# Electric Fence and Wazuh

Electric Fence can send every event it records to Wazuh as a syslog message.
This page sets up the receiving side.

## 1. Open a syslog listener

On the Wazuh manager, add a `<remote>` block to `/var/ossec/etc/ossec.conf`.
Replace the address with that of your Home Assistant:

```xml
<remote>
  <connection>syslog</connection>
  <port>514</port>
  <protocol>tcp</protocol>
  <allowed-ips>192.168.1.10</allowed-ips>
</remote>
```

Wazuh's own listener accepts TCP and UDP, not TLS. If you want TLS, put a
collector such as rsyslog in front of Wazuh and choose **TLS** in Electric
Fence.

## 2. Add the rules

Copy [`electric_fence_rules.xml`](electric_fence_rules.xml) to
`/var/ossec/etc/rules/` on the manager and restart it
(`systemctl restart wazuh-manager`). The rules use IDs 100500 to 100520 and
the group `electric_fence`. If those IDs clash with your own rules, change
them; nothing else refers to them.

| Rule   | Level | Fires on                                             |
| ------ | ----- | ---------------------------------------------------- |
| 100501 | 5     | A failed login.                                      |
| 100502 | 10    | 8 failed logins from one address within 120 seconds. |
| 100503 | 8     | A login from a new country.                          |
| 100504 | 7     | An IP ban.                                           |
| 100505 | 8     | A change to a Home Assistant user.                   |
| 100506 | 8     | A change made outside Home Assistant.                |
| 100507 | 8     | An app installed.                                    |
| 100508 | 10    | An app repository added.                             |
| 100509 | 12    | Files of a custom integration changed.               |
| 100510 | 10    | A custom integration removed from the Marketplace.   |
| 100511 | 6     | A custom integration's repository archived.          |
| 100512 | 3     | Forwarding recovered after an outage.                |

The first matching rule wins, so the specific rules are listed first in the
file and the generic user and outside rules (100505, 100506) come last. Keep
that order if you edit the file. The rules match the action on the JSON text
(`"action":"installed"`) because `action` is also a static Wazuh field name.

## 3. Turn on forwarding

In Home Assistant, go to **Settings** > **Devices & services** > **Electric
Fence** > **Configure** > **Log forwarding**. Turn on **Forward events**, enter
the manager's address and port 514, and keep the protocol on **tcp**.

## The message format

Each line looks like this:

```text
<132>Oct 26 09:15:02 homeassistant electric_fence: {"event_type":"login_failed","ts":"2026-10-26T09:15:02Z",...}
```

Over TCP each message ends with a newline (no length prefix), and over UDP
each message is one datagram. The JSON never contains a raw newline.

The header is RFC 3164, not RFC 5424. Wazuh's pre-decoder reads the 3164 form
(`Mmm dd hh:mm:ss host program:`) reliably and hands the JSON to its stock JSON
decoder, so no custom decoder is needed. The rules match the program name
`electric_fence` and then the JSON fields `event_type`, `kind`, `action` and
`actor_kind`. A change event also has `target`, `target_name`, `actor_name`,
`actor_ip`, `changed` and a `details` object (for example the changed,
missing and added files of an integration check, at most 50 of each, with
`<name>_truncated: true` when a list was cut).

The syslog time is UTC with no zone. When you need the time of an event, use
the `ts` field in the JSON, which is ISO 8601 with a `Z`.

Events are sent as recorded: usernames and IP addresses are not masked, and
before and after values are included only when **Record before and after
values** is on.
