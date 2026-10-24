import { LitElement, html, nothing } from "lit";
import { formatTime, truncate } from "../format.ts";
import { changeSentence, strings } from "../strings.ts";
import { rowStyles } from "../styles.ts";
import type { ChangeItem, HomeAssistant } from "../types.ts";
import { MAX_TEXT } from "./ef-login-row.ts";

/** One settings change: who did what, and from where. */
export class EfChangeRow extends LitElement {
  static override properties = {
    hass: { attribute: false },
    item: { attribute: false },
  };

  declare hass: HomeAssistant;
  declare item: ChangeItem | undefined;

  static override styles = rowStyles;

  protected override render() {
    const item = this.item;
    if (item === undefined) {
      return nothing;
    }
    const actor = truncate(item.actor_name ?? strings.system, MAX_TEXT);
    const sentence = truncate(changeSentence(item), MAX_TEXT * 2);
    return html`
      <div class="row">
        <span class="time">${formatTime(item.ts, this.hass)}</span>
        <bdi class="main actor" title=${actor.full}>${actor.short}</bdi>
        <bdi class="what" title=${sentence.full}>${sentence.short}</bdi>
        <bdi class="ip">${item.actor_ip ?? strings.unknownIp}</bdi>
      </div>
    `;
  }
}

customElements.define("ef-change-row", EfChangeRow);

declare global {
  interface HTMLElementTagNameMap {
    "ef-change-row": EfChangeRow;
  }
}
