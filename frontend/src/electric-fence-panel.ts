import { LitElement, html } from "lit";
import "./views/ef-overview.ts";
import { navigate } from "./fire-event.ts";
import { panelStyles } from "./styles.ts";
import type { HomeAssistant, Route } from "./types.ts";

export const TABS = [
  { id: "overview", label: "Overview" },
  { id: "activity", label: "Activity" },
  { id: "users", label: "Users" },
  { id: "audit", label: "Audit" },
  { id: "settings", label: "Settings" },
] as const;

export type TabId = (typeof TABS)[number]["id"];

/** `/audit/anything` selects "audit"; unknown or empty paths give overview. */
export function tabFromPath(path: string | undefined): TabId {
  const segment = (path ?? "").split("/").find((part) => part !== "");
  return TABS.find((tab) => tab.id === segment)?.id ?? "overview";
}

export class ElectricFencePanel extends LitElement {
  static override properties = {
    hass: { attribute: false },
    narrow: { type: Boolean },
    route: { attribute: false },
    panel: { attribute: false },
  };

  declare hass: HomeAssistant;
  declare narrow: boolean;
  declare route: Route | undefined;
  declare panel: unknown;

  static override styles = panelStyles;

  constructor() {
    super();
    this.narrow = false;
  }

  protected override render() {
    const current = tabFromPath(this.route?.path);
    return html`
      <header>
        <ha-menu-button
          .hass=${this.hass}
          .narrow=${this.narrow}
        ></ha-menu-button>
        <h1>Electric Fence</h1>
      </header>
      <nav role="tablist">
        ${TABS.map(
          (tab) => html`
            <button
              role="tab"
              id="tab-${tab.id}"
              aria-selected=${tab.id === current ? "true" : "false"}
              @click=${() => this._select(tab.id)}
            >
              ${tab.label}
            </button>
          `,
        )}
      </nav>
      <main role="tabpanel" aria-labelledby="tab-${current}">
        ${this._renderTab(current)}
      </main>
    `;
  }

  private _renderTab(tab: TabId) {
    if (tab === "overview") {
      return html`<ef-overview .hass=${this.hass}></ef-overview>`;
    }
    // Placeholders until the other tab bodies are built.
    const label = TABS.find((t) => t.id === tab)?.label ?? "";
    return html`<p class="placeholder">${label}</p>`;
  }

  private _select(tab: TabId): void {
    const prefix = this.route?.prefix ?? "/electric-fence";
    navigate(`${prefix}/${tab}`);
  }
}

customElements.define("electric-fence-panel", ElectricFencePanel);

declare global {
  interface HTMLElementTagNameMap {
    "electric-fence-panel": ElectricFencePanel;
  }
}
