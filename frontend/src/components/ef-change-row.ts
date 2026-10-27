import { LitElement, html, nothing } from "lit";
import { changeLines, isProtectionChange } from "../change-lines.ts";
import { MAX_TEXT, formatTime, truncate } from "../format.ts";
import { actorLabel, changeSentence, strings } from "../strings.ts";
import { rowStyles } from "../styles.ts";
import type { ChangeItem, HomeAssistant } from "../types.ts";
import { showIp } from "../mask.ts";

/** One settings change: who did what, and from where. */
export class EfChangeRow extends LitElement {
  static override properties = {
    hass: { attribute: false },
    item: { attribute: false },
    details: { type: Boolean },
  };

  declare hass: HomeAssistant;
  declare item: ChangeItem | undefined;
  /** Also show the changed settings under the row, at most three lines. */
  declare details: boolean;

  static override styles = rowStyles;

  protected override render() {
    const item = this.item;
    if (item === undefined) {
      return nothing;
    }
    const actor = truncate(actorLabel(item), MAX_TEXT);
    const sentence = truncate(changeSentence(item), MAX_TEXT * 2);
    return html`
      <div class="row">
        <span class="time">${formatTime(item.ts, this.hass)}</span>
        <bdi class="main actor" title=${actor.full}>${actor.short}</bdi>
        <bdi class="what" title=${sentence.full}>${sentence.short}</bdi>
        ${
          // Made over the panel's WebSocket or by Electric Fence: no IP.
          item.actor_ip === null && isProtectionChange(item)
            ? nothing
            : html`<bdi class="ip"
                >${item.actor_ip === null ? strings.unknownIp : showIp(item.actor_ip)}</bdi
              >`
        }
      </div>
      ${this.details ? changeLines(item, this.hass, 3) : nothing}
    `;
  }
}

customElements.define("ef-change-row", EfChangeRow);

declare global {
  interface HTMLElementTagNameMap {
    "ef-change-row": EfChangeRow;
  }
}
