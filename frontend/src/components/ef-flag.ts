import { LitElement, css, html, nothing } from "lit";

const CODE = /^[A-Za-z]{2}$/;
const FLAGS_DIR = "flags/";

/**
 * Small country flag from the bundled flag-icons SVGs. Decorative: the code or
 * name stays as text next to it. The URL is built at run time so Vite leaves
 * it alone; the build copies the SVGs next to this module.
 */
export class EfFlag extends LitElement {
  static override properties = {
    code: { attribute: false },
  };

  declare code: string | null | undefined;

  static override styles = css`
    :host([hidden]) {
      display: none;
    }
    img {
      vertical-align: -2px;
      border-radius: 2px;
      box-shadow: 0 0 0 1px var(--divider-color);
    }
  `;

  protected override willUpdate(changed: Map<string, unknown>): void {
    if (changed.has("code")) {
      this.hidden = false;
    }
  }

  protected override render() {
    const code = this.code;
    if (typeof code !== "string" || !CODE.test(code)) {
      return nothing;
    }
    const file = `./${FLAGS_DIR}${code.toLowerCase()}.svg`;
    const src = new URL(file, import.meta.url).href;
    return html`<img
      src=${src}
      alt=""
      width="20"
      height="15"
      loading="lazy"
      decoding="async"
      @error=${this._onError}
    />`;
  }

  private _onError = (): void => {
    this.hidden = true;
  };
}

customElements.define("ef-flag", EfFlag);

declare global {
  interface HTMLElementTagNameMap {
    "ef-flag": EfFlag;
  }
}
