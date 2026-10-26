import { css, html, nothing } from "lit";
import { fetchUsers } from "../api.ts";
import "../components/ef-flag.ts";
import { locationLabel } from "../components/ef-login-row.ts";
import { formatTime } from "../format.ts";
import { strings } from "../strings.ts";
import { dialogStyles, viewStyles } from "../styles.ts";
import { bdiText } from "../text.ts";
import type { SessionItem, UserItem } from "../types.ts";
import { LiveView } from "./live-view.ts";
import { showClient, showIp } from "../mask.ts";

const DIALOG_TITLE = "user-dialog-title";

/**
 * Users tab: who exists and how they sign in, as a list like Home
 * Assistant's users page; a user's sessions open in a details dialog.
 * Show only: the dialog has no actions besides closing it.
 */
export class EfUsers extends LiveView {
  static override properties = {
    ...LiveView.properties,
    _users: { state: true },
    _selected: { state: true },
  };

  declare _users: UserItem[] | undefined;
  declare _selected: string | undefined;

  static override styles = [
    viewStyles,
    dialogStyles,
    css`
      ul.users > li + li {
        border-top: 1px solid var(--divider-color);
      }
      button.user {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto 24px;
        grid-template-areas:
          "primary tags icon"
          "secondary tags icon";
        align-items: center;
        gap: 2px 16px;
        width: 100%;
        min-height: 0;
        padding: 12px 8px;
        border: 0;
        border-radius: var(--ha-border-radius-md, 8px);
        color: var(--primary-text-color);
        font-weight: inherit;
        text-align: start;
      }
      button.user:hover {
        background: color-mix(
          in srgb,
          var(--primary-text-color) 4%,
          transparent
        );
      }
      .primary {
        grid-area: primary;
        font-weight: var(--ha-font-weight-medium, 500);
        overflow-wrap: break-word;
      }
      .secondary {
        grid-area: secondary;
        color: var(--secondary-text-color);
        font-size: var(--ha-font-size-s, 12px);
        overflow-wrap: break-word;
      }
      .tags {
        grid-area: tags;
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-end;
        gap: 4px;
      }
      .tags .tag {
        margin: 0;
      }
      button.user ha-icon {
        grid-area: icon;
        color: var(--secondary-text-color);
      }
      @media (max-width: 600px) {
        button.user {
          grid-template-columns: minmax(0, 1fr) 24px;
          grid-template-areas:
            "primary icon"
            "secondary icon"
            "tags icon";
        }
        .tags {
          justify-content: flex-start;
          margin-top: 4px;
        }
      }

      dialog .tags {
        justify-content: flex-start;
        margin-top: 12px;
      }
      li.session {
        display: flex;
        flex-direction: column;
        gap: 2px;
        padding: 8px 0;
      }
    `,
  ];

  protected override async _fetch(): Promise<() => void> {
    const { items } = await fetchUsers(this.hass);
    return () => {
      this._users = items;
    };
  }

  private _user(): UserItem | undefined {
    return this._users?.find((u) => u.id === this._selected);
  }

  /** Opens the dialog once its content renders; closes it if the user is gone. */
  protected override updated(): void {
    const dialog = this.renderRoot.querySelector("dialog");
    if (dialog === null) {
      return;
    }
    const user = this._user();
    if (user !== undefined && !dialog.open) {
      dialog.showModal();
    } else if (user === undefined && dialog.open) {
      dialog.close();
    }
  }

  private _close = (): void => {
    this.renderRoot.querySelector("dialog")?.close();
  };

  private _onDialogClose = (): void => {
    this._selected = undefined;
  };

  private _tags(user: UserItem) {
    const u = strings.users;
    const mfa = user.mfa_modules.length > 0;
    return html`<span class="tags">
      ${user.is_admin ? html`<span class="tag">${u.admin}</span>` : nothing}
      ${user.is_owner ? html`<span class="tag">${u.owner}</span>` : nothing}
      <span class="tag ${user.is_active ? "good" : "warn"}"
        >${user.is_active ? u.active : u.inactive}</span
      >
      ${user.local_only ? html`<span class="tag warn">${u.localOnly}</span>` : nothing}
      <span class="tag ${mfa ? "good" : "warn"}"
        >${mfa ? strings.mfa : strings.noMfa}</span
      >
    </span>`;
  }

  /** In the list the line says "Last login …"; in the dialog its heading does. */
  private _lastLogin(user: UserItem, labelled: boolean) {
    const login = user.last_login;
    if (login === null) {
      return strings.users.neverLoggedIn;
    }
    const when = formatTime(login.ts, this.hass);
    // The list stays compact; the dialog shows the country's flag.
    return html`${labelled ? strings.users.lastLoginAt(when) : when} ·
      <bdi>${login.ip === null ? strings.unknownIp : showIp(login.ip)}</bdi> ·
      ${labelled ? nothing : html`<ef-flag .code=${login.country_code}></ef-flag>`}
      ${locationLabel(login)}`;
  }

  private _row(user: UserItem) {
    return html`<li>
      <button
        type="button"
        class="user"
        aria-haspopup="dialog"
        title=${strings.users.open}
        @click=${() => {
          this._selected = user.id;
        }}
      >
        <span class="primary"
          >${bdiText(user.name ?? user.username ?? strings.unknownUser)}</span
        >
        <span class="secondary">
          ${user.username === null ? nothing : html`${bdiText(user.username)} · `}
          ${this._lastLogin(user, true)}
        </span>
        ${this._tags(user)}
        <ha-icon aria-hidden="true" icon="mdi:chevron-right"></ha-icon>
      </button>
    </li>`;
  }

  private _session(s: SessionItem) {
    const u = strings.users;
    const type = Object.hasOwn(u.tokenTypes, s.token_type)
      ? u.tokenTypes[s.token_type]
      : undefined;
    return html`<li class="session">
      <span class="primary"
        >${bdiText(
          s.client_name ??
            (s.client_id === null
              ? strings.unknownClient
              : showClient(s.client_id)),
        )}</span
      >
      <span class="secondary">
        ${type ?? bdiText(s.token_type)} ·
        ${
          s.last_used_at === null
            ? u.neverUsed
            : u.lastUsedAt(formatTime(s.last_used_at, this.hass))
        }
        ·
        <bdi
          >${s.last_used_ip === null ? strings.unknownIp : showIp(s.last_used_ip)}</bdi
        >
        · ${u.createdAt(formatTime(s.created_at, this.hass))}
      </span>
    </li>`;
  }

  private _dialog() {
    const u = strings.users;
    const user = this._user();
    return html`<dialog
      aria-labelledby=${DIALOG_TITLE}
      @close=${this._onDialogClose}
    >
      ${
        user === undefined
          ? nothing
          : html`
              <header>
                <div class="title" tabindex="-1" autofocus>
                  <h3 id=${DIALOG_TITLE}>
                    ${bdiText(user.name ?? user.username ?? strings.unknownUser)}
                  </h3>
                  ${
                    user.username === null
                      ? nothing
                      : html`<span class="secondary"
                          >${bdiText(user.username)}</span
                        >`
                  }
                </div>
                <button
                  type="button"
                  class="close"
                  aria-label=${u.close}
                  title=${u.close}
                  @click=${this._close}
                >
                  <ha-icon aria-hidden="true" icon="mdi:close"></ha-icon>
                </button>
              </header>
              ${this._tags(user)}
              <h4>${u.lastLoginHeading}</h4>
              <p class="secondary last-login">
                ${this._lastLogin(user, false)}
              </p>
              <h4>${u.sessionsHeading(user.sessions.length)}</h4>
              ${
                user.sessions.length === 0
                  ? html`<p class="empty">${u.noSessions}</p>`
                  : html`<ul class="sessions">
                      ${user.sessions.map((s) => this._session(s))}
                    </ul>`
              }
            `
      }
    </dialog>`;
  }

  protected override render() {
    const u = strings.users;
    const users = this._users;
    return html`
      <h2 class="visually-hidden">${u.heading}</h2>
      ${this._banners()}
      ${
        users === undefined
          ? this._phase === "error" || this._phase === "unloaded"
            ? nothing
            : html`<p class="loading">${strings.loading}</p>`
          : users.length === 0
            ? html`<p class="empty">${u.empty}</p>`
            : html`<div class="card">
                <ul class="users">
                  ${users.map((user) => this._row(user))}
                </ul>
              </div>`
      }
      ${this._dialog()}
    `;
  }
}

customElements.define("ef-users", EfUsers);

declare global {
  interface HTMLElementTagNameMap {
    "ef-users": EfUsers;
  }
}
