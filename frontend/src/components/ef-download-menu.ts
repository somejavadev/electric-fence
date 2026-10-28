import { LitElement, css, html, nothing } from "lit";
import type { ExportFormat } from "../export.ts";
import { maskingIps } from "../mask.ts";
import { strings } from "../strings.ts";

/**
 * A ⋮ button with a small popup of two downloads.
 *
 * Event: `download-requested` `{format}`. While `busy` the button is disabled
 * and shows `Preparing… n rows`. Escape, an outside click and a choice close
 * the popup; the arrow keys move between the items.
 */
export class EfDownloadMenu extends LitElement {
  static override properties = {
    busy: { type: Boolean },
    rows: { type: Number },
    _open: { state: true },
  };

  declare busy: boolean;
  declare rows: number;
  declare _open: boolean;

  static override styles = css`
    :host {
      position: relative;
      display: inline-block;
      flex: none;
    }
    button {
      box-sizing: border-box;
      min-height: 32px;
      min-width: 32px;
      padding: 0 8px;
      font: inherit;
      font-size: var(--ha-font-size-m, 14px);
      color: var(--primary-text-color);
      background: var(--card-background-color);
      border: 1px solid var(--outline-color, var(--divider-color));
      border-radius: 10px;
      cursor: pointer;
    }
    button:disabled {
      opacity: 0.6;
      cursor: default;
    }
    button:focus-visible {
      outline: 2px solid var(--primary-color);
      outline-offset: 2px;
    }
    .menu {
      position: absolute;
      inset-inline-end: 0;
      top: calc(100% + 4px);
      z-index: 10;
      display: flex;
      flex-direction: column;
      min-width: 220px;
      padding: 4px 0;
      background: var(--card-background-color);
      border: 1px solid var(--divider-color);
      border-radius: var(--ha-card-border-radius, 12px);
      box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0, 0, 0, 0.2));
    }
    .menu button {
      border: 0;
      border-radius: 0;
      min-height: 40px;
      padding: 0 16px;
      text-align: start;
      background: none;
    }
    .menu button:hover,
    .menu button:focus-visible {
      background: var(--secondary-background-color);
      outline-offset: -2px;
    }
  `;

  constructor() {
    super();
    this.busy = false;
    this.rows = 0;
    this._open = false;
  }

  protected override updated(changed: Map<PropertyKey, unknown>): void {
    if (changed.get("busy") === true && !this.busy) {
      this.shadowRoot?.querySelector<HTMLElement>(".trigger")?.focus();
    }
  }

  override connectedCallback(): void {
    super.connectedCallback();
    this.addEventListener("keydown", this._onHostKey);
    this.addEventListener("focusout", this._onFocusOut);
  }

  private _onHostKey = (event: KeyboardEvent): void => {
    if (event.key === "Escape" && this._open) {
      event.stopPropagation();
      this._close(true);
    }
  };

  private _onFocusOut = (event: FocusEvent): void => {
    const to = event.relatedTarget;
    if (this._open && to instanceof Node && !this.contains(to)) {
      // relatedTarget is retargeted at a shadow host; inside it stays ours.
      this._close(false);
    }
  };

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.removeEventListener("keydown", this._onHostKey);
    this.removeEventListener("focusout", this._onFocusOut);
    document.removeEventListener("click", this._onOutside, true);
  }

  private _onOutside = (event: Event): void => {
    if (!event.composedPath().includes(this)) {
      this._close(false);
    }
  };

  private _toggle(): void {
    if (this._open) {
      this._close(true);
      return;
    }
    this._open = true;
    document.addEventListener("click", this._onOutside, true);
    void this.updateComplete.then(() => this._items()[0]?.focus());
  }

  private _close(refocus: boolean): void {
    document.removeEventListener("click", this._onOutside, true);
    this._open = false;
    if (refocus) {
      this.shadowRoot?.querySelector<HTMLElement>(".trigger")?.focus();
    }
  }

  private _items(): HTMLButtonElement[] {
    return [
      ...(this.shadowRoot?.querySelectorAll<HTMLButtonElement>(
        "[role=menuitem]",
      ) ?? []),
    ];
  }

  private _onKey(event: KeyboardEvent): void {
    const step =
      event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
    if (step === 0) {
      return;
    }
    event.preventDefault();
    const items = this._items();
    const at = items.indexOf(
      this.shadowRoot?.activeElement as HTMLButtonElement,
    );
    items[(at + step + items.length) % items.length]?.focus();
  }

  private _choose(format: ExportFormat): void {
    this._close(true);
    this.dispatchEvent(
      new CustomEvent("download-requested", {
        detail: { format },
        bubbles: true,
        composed: true,
      }),
    );
  }

  protected override render() {
    const d = strings.download;
    const masking = maskingIps();
    return html`
      <button
        type="button"
        class="trigger"
        aria-haspopup="menu"
        aria-expanded=${this._open ? "true" : "false"}
        aria-label=${strings.download.menu}
        ?disabled=${this.busy}
        @click=${this._toggle}
      >
        ${this.busy ? strings.download.preparing(this.rows) : "⋮"}
      </button>
      ${
        this._open && !this.busy
          ? html`<div class="menu" role="menu" @keydown=${this._onKey}>
              <button
                type="button"
                role="menuitem"
                @click=${() => this._choose("csv")}
              >
                ${masking ? d.csvUnmasked : d.csv}
              </button>
              <button
                type="button"
                role="menuitem"
                @click=${() => this._choose("json")}
              >
                ${masking ? d.jsonUnmasked : d.json}
              </button>
            </div>`
          : nothing
      }
    `;
  }
}

customElements.define("ef-download-menu", EfDownloadMenu);

declare global {
  interface HTMLElementTagNameMap {
    "ef-download-menu": EfDownloadMenu;
  }
}
