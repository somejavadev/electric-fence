import { html, nothing } from "lit";
import { fetchUsers } from "../api.ts";
import { locationLabel, MAX_TEXT } from "../components/ef-login-row.ts";
import { formatTime, truncate } from "../format.ts";
import { strings } from "../strings.ts";
import { viewStyles } from "../styles.ts";
import type { SessionItem, UserItem } from "../types.ts";
import { LiveView } from "./live-view.ts";

const text = (value: string) => {
  const t = truncate(value, MAX_TEXT);
  return html`<bdi title=${t.full}>${t.short}</bdi>`;
};

/** Users tab: who exists, how they sign in, and their sessions. Show only. */
export class EfUsers extends LiveView {
  static override properties = {
    ...LiveView.properties,
    _users: { state: true },
    _open: { state: true },
  };

  declare _users: UserItem[] | undefined;
  declare _open: ReadonlySet<string>;

  static override styles = viewStyles;

  constructor() {
    super();
    this._open = new Set();
  }

  protected override async _fetch(): Promise<() => void> {
    const { items } = await fetchUsers(this.hass);
    return () => {
      this._users = items;
    };
  }

  private _toggle(id: string): void {
    const open = new Set(this._open);
    if (!open.delete(id)) {
      open.add(id);
    }
    this._open = open;
  }

  private _roles(user: UserItem) {
    const u = strings.users;
    return html`
      ${user.is_admin ? html`<span class="tag">${u.admin}</span>` : nothing}
      ${user.is_owner ? html`<span class="tag">${u.owner}</span>` : nothing}
      <span class="tag ${user.is_active ? "good" : "warn"}"
        >${user.is_active ? u.active : u.inactive}</span
      >
      ${user.local_only ? html`<span class="tag warn">${u.localOnly}</span>` : nothing}
    `;
  }

  private _lastLogin(user: UserItem) {
    const login = user.last_login;
    if (login === null) {
      return strings.users.never;
    }
    return html`${formatTime(login.ts, this.hass)}
      <div class="sub">
        <bdi>${login.ip ?? strings.unknownIp}</bdi> · ${locationLabel(login)}
      </div>`;
  }

  private _sessions(sessions: SessionItem[]) {
    const u = strings.users;
    if (sessions.length === 0) {
      return html`<p class="empty">${u.noSessions}</p>`;
    }
    return html`<table>
      <thead>
        <tr>
          <th scope="col">${u.client}</th>
          <th scope="col">${u.tokenType}</th>
          <th scope="col">${u.created}</th>
          <th scope="col">${u.lastUsed}</th>
          <th scope="col">${u.lastIp}</th>
        </tr>
      </thead>
      <tbody>
        ${sessions.map(
          (s) =>
            html`<tr>
              <th scope="row">
                ${text(s.client_name ?? s.client_id ?? strings.unknownClient)}
              </th>
              <td>${text(s.token_type)}</td>
              <td>${formatTime(s.created_at, this.hass)}</td>
              <td>
                ${s.last_used_at === null ? u.never : formatTime(s.last_used_at, this.hass)}
              </td>
              <td><bdi>${s.last_used_ip ?? strings.unknownIp}</bdi></td>
            </tr>`,
        )}
      </tbody>
    </table>`;
  }

  private _row(user: UserItem) {
    const open = this._open.has(user.id);
    const panelId = `sessions-${user.id}`;
    return html`<tr>
        <th scope="row">
          <button
            type="button"
            class="toggle"
            aria-expanded=${open ? "true" : "false"}
            aria-controls=${panelId}
            title=${open ? strings.users.collapse : strings.users.expand}
            @click=${() => this._toggle(user.id)}
          >
            ${open ? "▾" : "▸"}
            ${text(user.name ?? user.username ?? strings.unknownUser)}
          </button>
        </th>
        <td>${user.username === null ? nothing : text(user.username)}</td>
        <td>${this._roles(user)}</td>
        <td>${this._lastLogin(user)}</td>
        <td>${user.sessions.length}</td>
        <td>
          <span class="tag ${user.mfa_modules.length > 0 ? "good" : "warn"}"
            >${user.mfa_modules.length > 0 ? strings.mfa : strings.noMfa}</span
          >
        </td>
      </tr>
      ${
        open
          ? html`<tr id=${panelId}>
              <td colspan="6">${this._sessions(user.sessions)}</td>
            </tr>`
          : nothing
      }`;
  }

  protected override render() {
    const u = strings.users;
    const users = this._users;
    return html`
      <h2>${u.heading}</h2>
      ${this._banners()}
      ${
        users === undefined
          ? this._phase === "error" || this._phase === "unloaded"
            ? nothing
            : html`<p class="loading">${strings.loading}</p>`
          : users.length === 0
            ? html`<p class="empty">${u.empty}</p>`
            : html`<div class="card">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">${u.name}</th>
                      <th scope="col">${u.username}</th>
                      <th scope="col">${u.roles}</th>
                      <th scope="col">${u.lastLogin}</th>
                      <th scope="col">${u.sessions}</th>
                      <th scope="col">${u.twoFactor}</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${users.map((user) => this._row(user))}
                  </tbody>
                </table>
              </div>`
      }
    `;
  }
}

customElements.define("ef-users", EfUsers);

declare global {
  interface HTMLElementTagNameMap {
    "ef-users": EfUsers;
  }
}
