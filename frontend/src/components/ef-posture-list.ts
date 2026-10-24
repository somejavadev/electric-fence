import { LitElement, css, html } from "lit";
import { postureText, strings } from "../strings.ts";
import type { PostureItem } from "../types.ts";

const GLYPH = { ok: "✓", warning: "!", error: "✕" } as const;

/** The posture checklist: one friendly sentence per check. */
export class EfPostureList extends LitElement {
  static override properties = { items: { attribute: false } };

  declare items: PostureItem[];

  static override styles = css`
    ul {
      list-style: none;
      margin: 0;
      padding: 0;
    }
    li {
      display: flex;
      gap: 8px;
      padding: 6px 0;
      font-size: 14px;
      overflow-wrap: anywhere;
    }
    .glyph {
      flex: none;
      width: 1.2em;
      text-align: center;
      font-weight: 700;
    }
    .ok .glyph {
      color: var(--success-color);
    }
    .warning .glyph {
      color: var(--warning-color);
    }
    .error .glyph {
      color: var(--error-color);
    }
  `;

  constructor() {
    super();
    this.items = [];
  }

  protected override render() {
    return html`
      <ul>
        ${this.items.map(
          (item) => html`
            <li class=${item.level}>
              <span class="glyph" aria-label=${strings.level[item.level]}
                >${GLYPH[item.level]}</span
              >
              <bdi class="text">${postureText(item)}</bdi>
            </li>
          `,
        )}
      </ul>
    `;
  }
}

customElements.define("ef-posture-list", EfPostureList);

declare global {
  interface HTMLElementTagNameMap {
    "ef-posture-list": EfPostureList;
  }
}
