import { css, html, nothing } from "lit";
import { fetchBans, unban } from "../api.ts";
import "../components/ef-confirm-dialog.ts";
import "../components/ef-filter-bar.ts";
import type { FilterChange, FilterField } from "../components/ef-filter-bar.ts";
import "../components/ef-flag.ts";
import { formatTime } from "../format.ts";
import { banExpiry, strings } from "../strings.ts";
import { dialogStyles, viewStyles } from "../styles.ts";
import { bdiText, fill } from "../text.ts";
import type { BanItem, BanSource, BanState, BansPage } from "../types.ts";
import { LiveView } from "./live-view.ts";

const DIALOG_TITLE = "ban-dialog-title";
const STATES: readonly BanState[] = ["active", "history"];
const SOURCES: readonly BanSource[] = ["electric_fence", "other"];

/** History can hold the same IP more than once. */
const banKey = (ban: BanItem): string => `${ban.ip} ${ban.banned_at}`;

/**
 * Bans tab: every IP ban, as a list like the Users tab; a ban's details and
 * its Unban button open in a dialog, and Unban asks first.
 */
export class EfBans extends LiveView {
  static override properties = {
    ...LiveView.properties,
    _state: { state: true },
    _source: { state: true },
    _page: { state: true },
    _items: { state: true },
    _loadingMore: { state: true },
    _selected: { state: true },
    _confirming: { state: true },
    _busy: { state: true },
    _error: { state: true },
  };

  declare _state: BanState;
  /** Applied here, not by the server: "" shows every source. */
  declare _source: BanSource | "";
  declare _page: BansPage | undefined;
  /** The fetched page plus any loaded with Load more. */
  declare _items: BanItem[];
  declare _loadingMore: boolean;
  declare _selected: string | undefined;
  declare _confirming: boolean;
  declare _busy: boolean;
  declare _error: string;

  static override styles = [
    viewStyles,
    dialogStyles,
    css`
      ul.bans > li + li {
        border-top: 1px solid var(--divider-color);
      }
      button.ban {
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
      button.ban:hover {
        background: color-mix(
          in srgb,
          var(--primary-text-color) 4%,
          transparent
        );
      }
      .primary {
        grid-area: primary;
        display: flex;
        align-items: center;
        gap: 8px;
        min-width: 0;
        font-weight: var(--ha-font-weight-medium, 500);
        overflow-wrap: anywhere;
      }
      .secondary {
        grid-area: secondary;
        color: var(--secondary-text-color);
        font-size: var(--ha-font-size-s, 12px);
        overflow-wrap: anywhere;
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
      button.ban ha-icon {
        grid-area: icon;
        color: var(--secondary-text-color);
      }
      @media (max-width: 600px) {
        button.ban {
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
      dialog .secondary {
        display: block;
        margin-top: 4px;
      }
      .facts {
        display: grid;
        grid-template-columns: max-content minmax(0, 1fr);
        gap: 8px 16px;
        margin: 24px 0 0;
      }
      .facts dt {
        color: var(--secondary-text-color);
      }
      .facts dd {
        margin: 0;
        overflow-wrap: anywhere;
      }
    `,
  ];

  constructor() {
    super();
    this._state = "active";
    this._source = "";
    this._items = [];
    this._loadingMore = false;
    this._confirming = false;
    this._busy = false;
    this._error = "";
  }

  protected override async _fetch(): Promise<() => void> {
    const page = await fetchBans(this.hass, this._state);
    return () => {
      this._page = page;
      this._items = page.items;
    };
  }

  protected override _resetRun(): void {
    this._loadingMore = false;
  }

  private _onFilter = (event: CustomEvent<FilterChange>): void => {
    const { name, value } = event.detail;
    if (name === "source") {
      this._source = SOURCES.find((s) => s === value) ?? "";
      return;
    }
    const state = STATES.find((s) => s === value);
    if (name !== "state" || state === undefined || state === this._state) {
      return;
    }
    this._state = state;
    this._page = undefined;
    this._items = [];
    this._loadingMore = false;
    this._phase = "loading";
    void this._refresh();
  };

  private async _loadMore(): Promise<void> {
    const before = this._page?.next_before_id ?? null;
    if (before === null || this._loadingMore) {
      return;
    }
    const generation = this._generation;
    const request = this._requests;
    this._loadingMore = true;
    try {
      const page = await fetchBans(this.hass, this._state, before);
      if (generation === this._generation && request === this._requests) {
        this._page = page;
        this._items = [...this._items, ...page.items];
      }
    } catch (err) {
      if (generation === this._generation && request === this._requests) {
        this._fail(err);
      }
    } finally {
      if (generation === this._generation && request === this._requests) {
        this._loadingMore = false;
      }
    }
  }

  private _ban(): BanItem | undefined {
    return this._items.find((b) => banKey(b) === this._selected);
  }

  /** Opens the dialog once its content renders; closes it if the ban is gone. */
  protected override updated(): void {
    const dialog = this.renderRoot.querySelector("dialog");
    if (dialog === null) {
      return;
    }
    const ban = this._ban();
    if (ban !== undefined && !dialog.open) {
      dialog.showModal();
    } else if (ban === undefined && dialog.open) {
      dialog.close();
    }
  }

  private _close = (): void => {
    this.renderRoot.querySelector("dialog")?.close();
  };

  private _onDialogClose = (): void => {
    this._selected = undefined;
    this._confirming = false;
  };

  private _askUnban = (): void => {
    this._error = "";
    this._busy = false;
    this._confirming = true;
  };

  private _onConfirmClosed = (): void => {
    this._confirming = false;
  };

  private _onConfirm = async (): Promise<void> => {
    if (this._busy) {
      return;
    }
    const ban = this._ban();
    if (ban === undefined) {
      this._confirming = false;
      return;
    }
    this._busy = true;
    this._error = "";
    try {
      // `unbanned: false` means it was already gone; the refresh shows that.
      await unban(this.hass, ban.ip);
    } catch {
      this._busy = false;
      this._error = strings.bans.unbanError;
      return;
    }
    this._busy = false;
    this._confirming = false;
    await this._refresh();
  };

  private _country(ban: BanItem) {
    return ban.country_code ?? strings.countryUnknown;
  }

  private _tag(ban: BanItem) {
    return html`<span class="tags"
      ><span class="tag">${strings.bans.sources[ban.source]}</span></span
    >`;
  }

  private _row(ban: BanItem) {
    return html`<li>
      <button
        type="button"
        class="ban"
        aria-haspopup="dialog"
        title=${strings.bans.open}
        @click=${() => {
          this._selected = banKey(ban);
        }}
      >
        <span class="primary"
          ><ef-flag .code=${ban.country_code}></ef-flag>${bdiText(ban.ip)}</span
        >
        <span class="secondary">${this._country(ban)} · ${banExpiry(ban)}</span>
        ${this._tag(ban)}
        <ha-icon aria-hidden="true" icon="mdi:chevron-right"></ha-icon>
      </button>
    </li>`;
  }

  private _ended(ban: BanItem) {
    const b = strings.bans;
    if (ban.end_reason === "unbanned" && ban.ended_by_name !== null) {
      return fill(b.unbannedBy, ban.ended_by_name);
    }
    return ban.end_reason === null ? b.unknown : b.endReasons[ban.end_reason];
  }

  private _facts(ban: BanItem) {
    const b = strings.bans;
    const count = (n: number | null) => (n === null ? b.unknown : String(n));
    return html`<dl class="facts">
      <dt>${strings.country}</dt>
      <dd>
        <ef-flag .code=${ban.country_code}></ef-flag> ${this._country(ban)}
      </dd>
      <dt>${b.hostname}</dt>
      <dd>${ban.hostname === null ? b.unknown : bdiText(ban.hostname)}</dd>
      <dt>${b.failures}</dt>
      <dd>${count(ban.failures)}</dd>
      <dt>${b.banNumber}</dt>
      <dd>${count(ban.ban_number)}</dd>
      <dt>${b.bannedAt}</dt>
      <dd>${formatTime(ban.banned_at, this.hass)}</dd>
      <dt>${b.expires}</dt>
      <dd>
        ${ban.expires_at === null ? b.never : formatTime(ban.expires_at, this.hass)}
      </dd>
      ${
        ban.ended_at === null
          ? nothing
          : html`<dt>${b.endedAt}</dt>
              <dd>${formatTime(ban.ended_at, this.hass)}</dd>
              <dt>${b.how}</dt>
              <dd>${this._ended(ban)}</dd>`
      }
    </dl>`;
  }

  private _dialog() {
    const b = strings.bans;
    const ban = this._ban();
    return html`<dialog
      aria-labelledby=${DIALOG_TITLE}
      @close=${this._onDialogClose}
    >
      ${
        ban === undefined
          ? nothing
          : html`
              <header>
                <div class="title" tabindex="-1" autofocus>
                  <h3 id=${DIALOG_TITLE}>${bdiText(ban.ip)}</h3>
                  <span class="secondary">${banExpiry(ban)}</span>
                </div>
                <button
                  type="button"
                  class="close"
                  aria-label=${strings.confirm.close}
                  title=${strings.confirm.close}
                  @click=${this._close}
                >
                  <ha-icon aria-hidden="true" icon="mdi:close"></ha-icon>
                </button>
              </header>
              ${this._tag(ban)} ${this._facts(ban)}
              ${
                // Unban needs the ban manager, like banning does.
                ban.ended_at === null && this._page?.available === true
                  ? html`<div class="actions">
                      <button
                        type="button"
                        class="unban danger"
                        aria-haspopup="dialog"
                        @click=${this._askUnban}
                      >
                        ${b.unban}
                      </button>
                    </div>`
                  : nothing
              }
              ${
                this._confirming
                  ? html`<ef-confirm-dialog
                      .heading=${fill(b.unbanTitle, ban.ip)}
                      text=${b.unbanText}
                      action=${b.unban}
                      error=${this._error}
                      ?busy=${this._busy}
                      @confirm-action=${this._onConfirm}
                      @confirm-closed=${this._onConfirmClosed}
                    ></ef-confirm-dialog>`
                  : nothing
              }
            `
      }
    </dialog>`;
  }

  private _fields(): FilterField[] {
    const b = strings.bans;
    return [
      {
        name: "state",
        label: b.state,
        type: "select",
        options: STATES.map((s) => ({ value: s, label: b.states[s] })),
      },
      {
        name: "source",
        label: b.source,
        type: "select",
        options: [
          { value: "", label: b.anySource },
          ...SOURCES.map((s) => ({ value: s, label: b.sources[s] })),
        ],
      },
    ];
  }

  private _list() {
    const b = strings.bans;
    const page = this._page;
    if (page === undefined) {
      return this._phase === "loading"
        ? html`<p class="loading">${strings.loading}</p>`
        : nothing;
    }
    const unavailable = page.available
      ? nothing
      : html`<div class="card">
          <p class="empty">
            ${page.reason === "ip_ban_disabled" || page.reason === null ? b.unavailable.ip_ban_disabled : b.unavailable.other}
          </p>
        </div>`;
    const items = this._items.filter(
      (ban) => this._source === "" || ban.source === this._source,
    );
    if (items.length === 0) {
      if (!page.available && this._items.length === 0) {
        return unavailable;
      }
      const empty =
        this._items.length > 0
          ? b.emptyFiltered
          : this._state === "active"
            ? b.empty
            : b.emptyHistory;
      return html`${unavailable}
        <div class="card"><p class="empty">${empty}</p></div>`;
    }
    return html`${unavailable}
      <div class="card">
        <ul class="bans">
          ${items.map((ban) => this._row(ban))}
        </ul>
      </div>`;
  }

  protected override render() {
    return html`
      <h2 class="visually-hidden">${strings.bans.heading}</h2>
      ${this._banners()}
      <ef-filter-bar
        .fields=${this._fields()}
        .values=${{ state: this._state, source: this._source }}
        @filter-changed=${this._onFilter}
      ></ef-filter-bar>
      ${this._list()}
      ${
        (this._page?.next_before_id ?? null) !== null
          ? html`<div class="more">
              <button
                type="button"
                ?disabled=${this._loadingMore}
                @click=${() => void this._loadMore()}
              >
                ${strings.loadMore}
              </button>
            </div>`
          : nothing
      }
      ${this._dialog()}
    `;
  }
}

customElements.define("ef-bans", EfBans);

declare global {
  interface HTMLElementTagNameMap {
    "ef-bans": EfBans;
  }
}
