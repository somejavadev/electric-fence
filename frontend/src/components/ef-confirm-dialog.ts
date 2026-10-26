import { LitElement, css, html, nothing } from "lit";
import { strings } from "../strings.ts";
import { dialogStyles, viewStyles } from "../styles.ts";

const TITLE = "confirm-title";
const TEXT = "confirm-text";

/**
 * A yes/no question styled like Home Assistant's confirmation dialog, used
 * instead of `confirm()`. It opens as soon as it renders. The owner listens
 * for `confirm-action` (run the action, set `busy`, then remove this element
 * or set `error`) and `confirm-closed` (dismissed: remove this element).
 */
export class EfConfirmDialog extends LitElement {
  static override properties = {
    heading: { attribute: false },
    text: { type: String },
    action: { type: String },
    error: { type: String },
    busy: { type: Boolean },
  };

  /** The question; a template so values inside it stay text in a bdi. */
  declare heading: unknown;
  declare text: string;
  declare action: string;
  declare error: string;
  declare busy: boolean;

  static override styles = [
    viewStyles,
    dialogStyles,
    css`
      dialog {
        width: min(440px, calc(100vw - 32px));
      }
      .message {
        margin: 16px 0 0;
        color: var(--secondary-text-color);
      }
      .error {
        margin: 16px 0 0;
        color: var(--error-color);
      }
    `,
  ];

  constructor() {
    super();
    this.heading = "";
    this.text = "";
    this.action = "";
    this.error = "";
    this.busy = false;
  }

  protected override firstUpdated(): void {
    this.renderRoot.querySelector("dialog")?.showModal();
  }

  private _confirm = (): void => {
    if (!this.busy) {
      this.dispatchEvent(new CustomEvent("confirm-action"));
    }
  };

  private _cancel = (): void => {
    this.renderRoot.querySelector("dialog")?.close();
  };

  private _onClose = (): void => {
    this.dispatchEvent(new CustomEvent("confirm-closed"));
  };

  protected override render() {
    return html`<dialog
      aria-labelledby=${TITLE}
      aria-describedby=${TEXT}
      @close=${this._onClose}
    >
      <header>
        <div class="title" tabindex="-1" autofocus>
          <h3 id=${TITLE}>${this.heading}</h3>
        </div>
        <button
          type="button"
          class="close"
          aria-label=${strings.confirm.close}
          title=${strings.confirm.close}
          @click=${this._cancel}
        >
          <ha-icon aria-hidden="true" icon="mdi:close"></ha-icon>
        </button>
      </header>
      <p class="message" id=${TEXT}>${this.text}</p>
      ${
        this.error === ""
          ? nothing
          : html`<p class="error" role="alert">${this.error}</p>`
      }
      <div class="actions">
        <button type="button" class="cancel plain" @click=${this._cancel}>
          ${strings.confirm.cancel}
        </button>
        <button
          type="button"
          class="confirm danger"
          ?disabled=${this.busy}
          @click=${this._confirm}
        >
          ${this.action}
        </button>
      </div>
    </dialog>`;
  }
}

customElements.define("ef-confirm-dialog", EfConfirmDialog);

declare global {
  interface HTMLElementTagNameMap {
    "ef-confirm-dialog": EfConfirmDialog;
  }
}
