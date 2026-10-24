import { LitElement, html, nothing } from "lit";
import { countryFlag, formatTime, reasonLabel, truncate } from "../format.ts";
import { strings } from "../strings.ts";
import { rowStyles } from "../styles.ts";
import type { HomeAssistant, LoginItem } from "../types.ts";

export const MAX_TEXT = 64;

/** One login attempt. Typed usernames are text, never markup. */
export class EfLoginRow extends LitElement {
  static override properties = {
    hass: { attribute: false },
    item: { attribute: false },
    variant: { type: String },
  };

  declare hass: HomeAssistant;
  declare item: LoginItem | undefined;
  /** "failed" shows the reason; "success" shows the client and MFA. */
  declare variant: "failed" | "success";

  static override styles = rowStyles;

  constructor() {
    super();
    this.variant = "failed";
  }

  protected override render() {
    const item = this.item;
    if (item === undefined) {
      return nothing;
    }
    const user = truncate(item.username_typed ?? strings.unknownUser, MAX_TEXT);
    const where =
      item.country_code === null
        ? strings.local
        : `${countryFlag(item.country_code)} ${item.country_code}`.trim();
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
        <span class="where">${where}</span>
      </div>
    `;
  }

  private _failed(item: LoginItem) {
    return html`
      <span class="tag bad">${reasonLabel(item.reason)}</span>
      <span class="ip">${item.ip ?? strings.unknownIp}</span>
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
}

customElements.define("ef-login-row", EfLoginRow);

declare global {
  interface HTMLElementTagNameMap {
    "ef-login-row": EfLoginRow;
  }
}
