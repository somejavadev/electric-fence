# Responsible use

Electric Fence records who logs in to Home Assistant, from where, and what
they change. That is useful for spotting attacks, and it is also personal
data about the people who use your Home Assistant. This page sets out what
Electric Fence is for, and what to keep in mind when you run it.

## What it's for

Electric Fence is for protecting a Home Assistant system you own or
administer: noticing failed logins, slowing down guessing, banning addresses
that keep failing, and keeping an audit trail of changes.

It is not a tool for monitoring people. Don't use it to track the
whereabouts or habits of household members, guests, tenants or employees.

## The people it records

Everyone who logs in, or tries to, is recorded, including your household. For
each attempt Electric Fence stores the IP address, the country, the host name
(with reverse DNS on), the browser or app, and the username that was typed.
For each change it stores who made it and, with **Record before and after
values** on, what changed. [Privacy](README.md#privacy) in the README lists
exactly what is stored and what is hidden.

- **Tell the people who use your Home Assistant** that logins and changes are
  recorded, and for how long.
- **Keep only what you need.** The retention period defaults to 90 days.
  Shorten it if you don't need that much history.
- **Know your local rules.** If other people's data ends up in your records,
  privacy law where you live (for example the GDPR in the EU and UK) may give
  them rights over it, especially in a shared house, a rental or a workplace.

## Treat the records as sensitive

- **Typed usernames can contain passwords.** People sometimes type a password
  into the username field. Electric Fence stores what was typed, so the
  audit log can hold a password by accident.
- **Downloads are unmasked.** CSV and JSON downloads hold full IP addresses
  and usernames, whatever the panel's masking setting. Store and share them
  with care, and delete them when you're done.
- **Forwarded events are unmasked.** With log forwarding on, every event goes
  in full to the collector you configured. Use TLS when it leaves your
  network, and make sure the collector's own access control and retention
  are as careful as your Home Assistant's.
- **Screenshots.** Turn on **Mask IP addresses in the panel** before taking
  screenshots to share.

## Know its limits

Electric Fence adds a layer of defence. It doesn't make it safe to expose Home
Assistant to the internet. [Keep Home Assistant off the
internet](README.md#keep-home-assistant-off-the-internet) explains safer ways
to reach it from outside, and [Known limits](README.md#known-limits) lists what
Electric Fence can't see or stop.

Electric Fence comes with no warranty, as the [licence](LICENSE) states.
