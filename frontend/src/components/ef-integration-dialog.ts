import { LitElement, css, html, nothing } from "lit";
import { formatTime } from "../format.ts";
import { integrationFiles, integrationHealth, strings } from "../strings.ts";
import { dialogStyles, viewStyles } from "../styles.ts";
import { bdiText } from "../text.ts";
import type { FileReport, HomeAssistant, IntegrationItem } from "../types.ts";

const TITLE = "integration-title";

const KINDS = [
  "changed",
  "missing",
  "added",
  "unreadable",
  "symlinks",
] as const;

/**
 * What a check found for one custom integration. Show only: the owner
 * removes this element when it emits `dialog-closed`. It opens as soon as
 * it renders.
 */
export class EfIntegrationDialog extends LitElement {
  static override properties = {
    hass: { attribute: false },
    item: { attribute: false },
  };

  declare hass: HomeAssistant;
  declare item: IntegrationItem | undefined;

  static override styles = [
    viewStyles,
    dialogStyles,
    css`
      dl {
        margin: 16px 0 0;
      }
      dt {
        margin-top: 12px;
        color: var(--secondary-text-color);
        font-size: var(--ha-font-size-s, 12px);
      }
      dd {
        margin: 0;
        overflow-wrap: anywhere;
      }
      ul.files {
        padding-inline-start: 20px;
        list-style: disc;
        margin: 0;
      }
      ul.files li + li {
        border-top: 0;
      }
    `,
  ];

  protected override firstUpdated(): void {
    this.renderRoot.querySelector("dialog")?.showModal();
  }

  private _close = (): void => {
    this.renderRoot.querySelector("dialog")?.close();
  };

  private _onClose = (): void => {
    this.dispatchEvent(new CustomEvent("dialog-closed"));
  };

  private _files(report: FileReport) {
    const t = strings.integrations;
    const lists = KINDS.filter((kind) => report[kind].length > 0);
    if (lists.length === 0) {
      return html`<p class="empty">${t.noFiles}</p>`;
    }
    return html`${lists.map(
      (kind) =>
        html`<h4>${t.headings[kind]}</h4>
          <ul class="files">
            ${report[kind].map((path) => html`<li>${bdiText(path, 256)}</li>`)}
          </ul>`,
    )}`;
  }

  protected override render() {
    const item = this.item;
    if (item === undefined) {
      return nothing;
    }
    const t = strings.integrations;
    const files = item.files;
    const reason = files?.status === "unchecked" ? files.reason : null;
    return html`<dialog aria-labelledby=${TITLE} @close=${this._onClose}>
      <header>
        <div class="title" tabindex="-1" autofocus>
          <h3 id=${TITLE}>${bdiText(item.name)}</h3>
        </div>
        <button
          type="button"
          class="close"
          aria-label=${t.close}
          title=${t.close}
          @click=${this._close}
        >
          <ha-icon aria-hidden="true" icon="mdi:close"></ha-icon>
        </button>
      </header>
      <dl>
        <dt>${t.status}</dt>
        <dd>${integrationFiles(item)}</dd>
        ${
          reason === null
            ? nothing
            : html`<dt>${t.why}</dt>
                <dd>
                  ${Object.hasOwn(t.reasons, reason) ? t.reasons[reason] : bdiText(reason)}
                </dd>`
        }
        ${
          item.repository === null
            ? nothing
            : html`<dt>${t.repository}</dt>
                <dd>${bdiText(item.repository, 256)}</dd>`
        }
        ${
          files?.ref == null
            ? nothing
            : html`<dt>${t.ref}</dt>
                <dd>${bdiText(files.ref)}</dd>`
        }
        <dt>${t.healthLabel}</dt>
        <dd>${integrationHealth(item)}</dd>
        ${
          item.health.reason === null
            ? nothing
            : html`<dt>${t.healthReason}</dt>
                <dd>${bdiText(item.health.reason, 256)}</dd>`
        }
        ${
          item.health.last_updated === null
            ? nothing
            : html`<dt>${t.lastUpdated}</dt>
                <dd>${formatTime(item.health.last_updated, this.hass)}</dd>`
        }
      </dl>
      ${files?.report == null ? nothing : this._files(files.report)}
      ${item.truncated ? html`<p class="hint">${t.truncated}</p>` : nothing}
    </dialog>`;
  }
}

customElements.define("ef-integration-dialog", EfIntegrationDialog);

declare global {
  interface HTMLElementTagNameMap {
    "ef-integration-dialog": EfIntegrationDialog;
  }
}
