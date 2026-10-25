import { LitElement, css, html, nothing } from "lit";
import { formatTime, reasonLabel, truncate } from "../format.ts";
import "./ef-flag.ts";
import { strings } from "../strings.ts";
import { rowStyles } from "../styles.ts";
import type { HomeAssistant, LoginItem } from "../types.ts";

export const MAX_TEXT = 64;

/** Country code; "local" only when the login really came from the local network. */
export function locationLabel(item: LoginItem): string {
  return item.country_code !== null
    ? item.country_code
    : item.source === "local"
      ? strings.local
      : strings.unknownCountry;
}

/**
 * Who the row is about. Failures show the raw typed text (what the admin needs
 * to see); successes and blocked logins name the user, falling back to the
 * typed text, then "unknown user".
 */
export function rowName(item: LoginItem): string {
  if (item.result === "failed") {
    return item.username_typed ?? strings.unknownUser;
  }
  return item.user_name ?? item.username_typed ?? strings.unknownUser;
}

/** Text, isolated and truncated, with the full value in the tooltip. */
const text = (value: string, className = "") => {
  const t = truncate(value, MAX_TEXT);
  return html`<bdi class=${className} title=${t.full}>${t.short}</bdi>`;
};

/** One login attempt. Typed usernames are text, never markup. */
export class EfLoginRow extends LitElement {
  static override properties = {
    hass: { attribute: false },
    item: { attribute: false },
    variant: { type: String },
    detailed: { type: Boolean },
    _open: { state: true },
  };

  declare hass: HomeAssistant;
  declare item: LoginItem | undefined;
  /** "failed" shows the reason; "success" shows the client and MFA. */
  declare variant: "failed" | "success";
  /** Offer an expandable line with the request details. */
  declare detailed: boolean;
  declare _open: boolean;

  static override styles = [
    rowStyles,
    css`
      button.toggle {
        padding: 0 4px;
        border: 0;
        background: none;
        color: var(--primary-color);
        font: inherit;
        font-size: var(--ha-font-size-s, 12px);
        cursor: pointer;
      }
      .details {
        display: grid;
        grid-template-columns: max-content minmax(0, 1fr);
        gap: 2px 12px;
        margin: 0 0 6px;
        font-size: var(--ha-font-size-s, 12px);
        color: var(--secondary-text-color);
      }
      .details dd {
        margin: 0;
        overflow-wrap: anywhere;
      }
    `,
  ];

  constructor() {
    super();
    this.variant = "failed";
    this.detailed = false;
    this._open = false;
  }

  protected override render() {
    const item = this.item;
    if (item === undefined) {
      return nothing;
    }
    const user = truncate(rowName(item), MAX_TEXT);
    const where = locationLabel(item);
    return html`
      <div class="row">
        <span class="time">${formatTime(item.ts, this.hass)}</span>
        <bdi class="main username" title=${user.full}>${user.short}</bdi>
        ${
          item.count > 1
            ? html`<span class="tag">${strings.repeated(item.count)}</span>`
            : nothing
        }
        ${this.variant === "failed" ? this._failed(item) : this._success(item)}
        ${
          item.result === "success" && !item.confirmed
            ? html`<span class="tag warn">${strings.unconfirmed}</span>`
            : nothing
        }
        <ef-flag .code=${item.country_code}></ef-flag>
        <span class="where">${where}</span>
        ${
          this.detailed
            ? html`<button
                type="button"
                class="toggle"
                aria-expanded=${this._open ? "true" : "false"}
                aria-controls="details"
                @click=${this._toggle}
              >
                ${this._open ? strings.details.hide : strings.details.show}
              </button>`
            : nothing
        }
      </div>
      ${this.detailed && this._open ? this._details(item) : nothing}
    `;
  }

  private _toggle = (): void => {
    this._open = !this._open;
  };

  private _failed(item: LoginItem) {
    return html`
      <span class="tag bad">${reasonLabel(item.reason)}</span>
      <bdi class="ip">${item.ip ?? strings.unknownIp}</bdi>
      ${
        item.reason === "invalid_token" && item.url !== null
          ? text(item.url, "url")
          : nothing
      }
    `;
  }

  private _success(item: LoginItem) {
    const client = truncate(
      item.client_name ?? item.client_id ?? strings.unknownClient,
      MAX_TEXT,
    );
    const mfa = item.mfa_module !== null;
    return html`
      <bdi class="client" title=${client.full}>${client.short}</bdi>
      <span class="tag ${mfa ? "good" : "warn"}"
        >${mfa ? strings.mfa : strings.noMfa}</span
      >
    `;
  }

  private _details(item: LoginItem) {
    const d = strings.details;
    const rows: [string, string | null][] = [
      [d.url, item.url],
      [d.hostname, item.hostname],
      [d.userAgent, item.user_agent],
      [
        d.provider,
        item.provider !== null && Object.hasOwn(d.providers, item.provider)
          ? (d.providers[item.provider] ?? item.provider)
          : item.provider,
      ],
      [d.mfaModule, item.mfa_module],
    ];
    return html`
      <dl class="details" id="details">
        ${rows.map(
          ([label, value]) =>
            html`<dt>${label}</dt>
              <dd>${value === null ? d.none : text(value)}</dd>`,
        )}
        <dt>${d.state}</dt>
        <dd>${item.confirmed ? d.confirmed : d.unconfirmed}</dd>
      </dl>
    `;
  }
}

customElements.define("ef-login-row", EfLoginRow);

declare global {
  interface HTMLElementTagNameMap {
    "ef-login-row": EfLoginRow;
  }
}
