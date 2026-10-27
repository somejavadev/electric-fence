import { LitElement, css, html } from "lit";
import { postureText, strings } from "../strings.ts";
import type { PostureItem } from "../types.ts";

const ICON = {
  ok: "mdi:check-circle-outline",
  info: "mdi:information-outline",
  warning: "mdi:alert-circle-outline",
  error: "mdi:close-circle-outline",
  critical: "mdi:alert-octagon-outline",
} as const;

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
      align-items: flex-start;
      gap: 8px;
      padding: 6px 0;
      font-size: var(--ha-font-size-m, 14px);
      overflow-wrap: anywhere;
    }
    ha-icon {
      flex: none;
      --mdc-icon-size: 20px;
    }
    .ok ha-icon {
      color: var(--success-color);
    }
    .info ha-icon {
      color: var(--info-color, var(--primary-color));
    }
    .warning ha-icon {
      color: var(--warning-color);
    }
    .error ha-icon,
    .critical ha-icon {
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
              <ha-icon
                icon=${ICON[item.level]}
                role="img"
                aria-label=${strings.level[item.level]}
              ></ha-icon>
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
