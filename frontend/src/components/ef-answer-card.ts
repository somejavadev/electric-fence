import { LitElement, css, html, nothing } from "lit";
import { navigate } from "../fire-event.ts";

/** A card that answers one question; selecting it opens the matching tab. */
export class EfAnswerCard extends LitElement {
  static override properties = {
    heading: { type: String },
    count: { attribute: false },
    note: { type: String },
    path: { type: String },
  };

  declare heading: string;
  declare count: number | undefined;
  declare note: string;
  declare path: string;

  static override styles = css`
    :host {
      display: block;
    }
    article {
      height: 100%;
      box-sizing: border-box;
      padding: 16px;
      cursor: pointer;
      background: var(--ha-card-background, var(--card-background-color));
      border: 1px solid var(--ha-card-border-color, var(--divider-color));
      border-radius: var(--ha-card-border-radius, 12px);
      box-shadow: var(--ha-card-box-shadow, none);
      color: var(--primary-text-color);
    }
    article:hover {
      border-color: var(--primary-color);
    }
    header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }
    button {
      padding: 0;
      border: 0;
      background: none;
      color: var(--primary-text-color);
      font: inherit;
      /* Home Assistant's card header (ha-card .card-header). */
      font-size: var(--ha-card-header-font-size, var(--ha-font-size-2xl, 24px));
      font-weight: var(--ha-font-weight-normal, 400);
      letter-spacing: -0.012em;
      line-height: var(--ha-line-height-condensed, 1.2);
      text-align: start;
      cursor: pointer;
    }
    .count {
      font-size: var(--ha-font-size-3xl, 28px);
      line-height: var(--ha-line-height-condensed, 1.2);
      color: var(--primary-text-color);
    }
    .note {
      font-size: var(--ha-font-size-m, 14px);
      color: var(--secondary-text-color);
    }
  `;

  constructor() {
    super();
    this.heading = "";
    this.note = "";
    this.path = "";
  }

  protected override render() {
    return html`
      <article @click=${this._open}>
        <header>
          <button type="button">${this.heading}</button>
          ${
            this.count === undefined
              ? nothing
              : html`<span class="count">${this.count}</span>`
          }
          ${
            this.note === ""
              ? nothing
              : html`<span class="note">${this.note}</span>`
          }
        </header>
        <slot></slot>
      </article>
    `;
  }

  private _open = (): void => {
    if (this.path !== "") {
      navigate(this.path);
    }
  };
}

customElements.define("ef-answer-card", EfAnswerCard);

declare global {
  interface HTMLElementTagNameMap {
    "ef-answer-card": EfAnswerCard;
  }
}
