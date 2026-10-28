import type {
  PropertyValues,
  ReactiveController,
  ReactiveControllerHost,
} from "lit";
import { LitElement, css, html, nothing } from "lit";
import { fireEvent } from "../fire-event.ts";
import { strings } from "../strings.ts";

export interface FilterOption {
  value: string;
  label: string;
}

/** One labelled control. A checkbox's value is "1" when on, else "". */
export interface FilterField {
  name: string;
  label: string;
  type: "select" | "text" | "checkbox" | "date";
  options?: FilterOption[];
  /** Show the field only while another field has this value. */
  when?: { field: string; equals: string };
}

export interface FilterChange {
  name: string;
  value: string;
}

/** Wait this long after the last keystroke before searching. */
export const SEARCH_DELAY_MS = 300;

/** Home Assistant's breakpoint for a phone-sized layout. */
const NARROW_QUERY = "(max-width: 870px)";

/** Tracks whether the window is narrow, and re-renders its host on change. */
export class NarrowController implements ReactiveController {
  narrow = false;
  private _query: MediaQueryList | undefined;

  constructor(private readonly _host: ReactiveControllerHost) {
    _host.addController(this);
  }

  private _onChange = (event: MediaQueryListEvent): void => {
    this.narrow = event.matches;
    this._host.requestUpdate();
  };

  hostConnected(): void {
    this._query = window.matchMedia?.(NARROW_QUERY);
    this.narrow = this._query?.matches ?? false;
    this._query?.addEventListener("change", this._onChange);
  }

  hostDisconnected(): void {
    this._query?.removeEventListener("change", this._onChange);
  }
}

/**
 * Home Assistant style filters: a header with a Filters chip and a search
 * box, a 280 px column (a bottom sheet when `narrow`) with one collapsible
 * section per field, and the list in the default slot.
 *
 * Events: `filter-changed` `{name, value}` (text on change, search 300 ms
 * after the last keystroke), `filters-cleared` and `open-changed` `{open}`.
 * The parent owns the values; this element only reports changes.
 * Slots: default (the list) and `actions` (the end of the header row).
 */
export class EfFilterPane extends LitElement {
  static override properties = {
    fields: { attribute: false },
    values: { attribute: false },
    defaults: { attribute: false },
    open: { type: Boolean, reflect: true },
    narrow: { type: Boolean, reflect: true },
  };

  declare fields: FilterField[];
  declare values: Record<string, string>;
  /** What counts as "not filtering" for a field; missing means "". */
  declare defaults: Record<string, string>;
  declare open: boolean;
  declare narrow: boolean;

  private _timer: ReturnType<typeof setTimeout> | undefined;

  static override styles = css`
    :host {
      display: block;
    }
    .header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
    }
    .chip {
      position: relative;
      display: inline-flex;
      align-items: center;
      flex: none;
      box-sizing: border-box;
      height: 32px;
      padding: 0 16px;
      font: inherit;
      font-size: var(--ha-font-size-m, 14px);
      color: var(--primary-text-color);
      background: var(--card-background-color);
      border: 1px solid var(--outline-color, var(--divider-color));
      border-radius: 10px;
      cursor: pointer;
    }
    .chip[aria-expanded="true"] {
      border-color: var(--primary-color);
      color: var(--primary-color);
    }
    .badge {
      position: absolute;
      top: -4px;
      inset-inline-end: -4px;
      box-sizing: border-box;
      min-width: 16px;
      padding: 0 2px;
      font-size: var(--ha-font-size-xs, 10px);
      line-height: var(--ha-line-height-normal, 1.5);
      text-align: center;
      color: var(--text-primary-color);
      background-color: var(--primary-color);
      border-radius: var(--ha-border-radius-circle, 50%);
      pointer-events: none;
    }
    .search {
      flex: 1;
      min-width: 0;
    }
    input,
    select {
      box-sizing: border-box;
      width: 100%;
      height: 40px;
      padding: 0 12px;
      font: inherit;
      font-size: var(--ha-font-size-m, 14px);
      color: var(--primary-text-color);
      background: var(--card-background-color);
      border: 1px solid var(--outline-color, var(--divider-color));
      border-radius: var(--ha-border-radius-sm, 4px);
    }
    input:hover,
    select:hover {
      border-color: var(--primary-text-color);
    }
    input:focus-visible,
    select:focus-visible,
    .chip:focus-visible,
    button.text:focus-visible,
    summary:focus-visible {
      outline: 2px solid var(--primary-color);
      outline-offset: -2px;
    }
    input[type="checkbox"] {
      width: 18px;
      height: 18px;
      margin: 0;
      accent-color: var(--primary-color);
    }
    .layout {
      display: flex;
      align-items: flex-start;
      gap: 16px;
    }
    .main {
      flex: 1;
      min-width: 0;
    }
    .pane,
    .sheet {
      display: flex;
      flex-direction: column;
      box-sizing: border-box;
      background: var(--card-background-color);
      border: 1px solid var(--divider-color);
      border-radius: var(--ha-border-radius-lg, 12px);
      overflow-y: auto;
    }
    .pane {
      flex: none;
      width: 280px;
    }
    .sheet {
      position: fixed;
      inset-inline: 0;
      bottom: 0;
      z-index: 10;
      max-height: 70vh;
      border-radius: var(--ha-border-radius-lg, 12px)
        var(--ha-border-radius-lg, 12px) 0 0;
      box-shadow: 0 -4px 16px rgb(0 0 0 / 24%);
    }
    .pane-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 16px;
      border-bottom: 1px solid var(--divider-color);
      font-weight: var(--ha-font-weight-medium, 500);
    }
    button.text {
      font: inherit;
      font-size: var(--ha-font-size-m, 14px);
      color: var(--primary-color);
      background: none;
      border: 0;
      padding: 4px 8px;
      border-radius: var(--ha-border-radius-sm, 4px);
      cursor: pointer;
    }
    details {
      border-bottom: 1px solid var(--divider-color);
    }
    details:last-child {
      border-bottom: 0;
    }
    summary {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 8px 16px;
      cursor: pointer;
    }
    .label {
      color: var(--primary-text-color);
    }
    .value {
      font-size: var(--ha-font-size-s, 12px);
      color: var(--secondary-text-color);
      overflow-wrap: anywhere;
    }
    .control {
      padding: 0 16px 12px;
    }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: var(--ha-font-size-m, 14px);
      color: var(--primary-text-color);
    }
    @media (prefers-reduced-motion: no-preference) {
      .chip {
        transition: border-color 0.15s;
      }
    }
  `;

  constructor() {
    super();
    this.fields = [];
    this.values = {};
    this.defaults = {};
    this.open = false;
    this.narrow = false;
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    clearTimeout(this._timer);
  }

  private _value(name: string): string {
    return this.values[name] ?? this.defaults[name] ?? "";
  }

  private _visible(): FilterField[] {
    return this.fields.filter(
      (f) =>
        f.when === undefined || this._value(f.when.field) === f.when.equals,
    );
  }

  private _active(): number {
    return this._visible().filter(
      (f) => this._value(f.name) !== (this.defaults[f.name] ?? ""),
    ).length;
  }

  /** Options render after the select, so its value is set once they exist. */
  protected override updated(changed: PropertyValues<this>): void {
    // Typed text is emitted trimmed; write the box only when `search` itself
    // changed, never because a re-render found it ahead of `values`.
    const search =
      this.renderRoot.querySelector<HTMLInputElement>("input[name=search]");
    const wanted = this.values["search"] ?? "";
    const before =
      (changed.get("values") as Record<string, string> | undefined)?.[
        "search"
      ] ?? "";
    if (
      search !== null &&
      changed.has("values") &&
      before !== wanted &&
      search.value.trim() !== wanted
    ) {
      search.value = wanted;
    }
    for (const select of this.renderRoot.querySelectorAll("select")) {
      select.value = this._value(select.name);
    }
  }

  private _emit(name: string, value: string): void {
    fireEvent<FilterChange>(this, "filter-changed", { name, value });
  }

  private _toggle = (): void => {
    this.open = !this.open;
    fireEvent(this, "open-changed", { open: this.open });
  };

  private _onSearch = (event: Event): void => {
    const input = event.target as HTMLInputElement;
    clearTimeout(this._timer);
    this._timer = setTimeout(
      () => this._emit("search", input.value.trim()),
      SEARCH_DELAY_MS,
    );
  };

  private _clear = (): void => {
    clearTimeout(this._timer);
    const search =
      this.renderRoot.querySelector<HTMLInputElement>("input[name=search]");
    if (search !== null) {
      search.value = "";
    }
    fireEvent(this, "filters-cleared");
  };

  private _shown(field: FilterField): string {
    const value = this._value(field.name);
    if (field.type === "checkbox") {
      return value === "1" ? strings.filters.on : strings.filters.off;
    }
    if (field.type === "select") {
      const option = field.options?.find((o) => o.value === value);
      return option?.label ?? (value === "" ? strings.filters.any : value);
    }
    return value === "" ? strings.filters.any : value;
  }

  private _control(field: FilterField) {
    const value = this._value(field.name);
    switch (field.type) {
      case "checkbox":
        return html`<label class="check"
          ><input
            type="checkbox"
            name=${field.name}
            .checked=${value === "1"}
            @change=${(e: Event) =>
              this._emit(
                field.name,
                (e.target as HTMLInputElement).checked ? "1" : "",
              )}
          />${field.label}</label
        >`;
      case "select":
        return html`<select
          name=${field.name}
          aria-label=${field.label}
          @change=${(e: Event) =>
            this._emit(field.name, (e.target as HTMLSelectElement).value)}
        >
          ${(field.options ?? []).map(
            (option) =>
              html`<option value=${option.value}>${option.label}</option>`,
          )}
        </select>`;
      case "date":
        return html`<input
          type="date"
          name=${field.name}
          aria-label=${field.label}
          .value=${value}
          @change=${(e: Event) =>
            this._emit(field.name, (e.target as HTMLInputElement).value)}
        />`;
      default:
        return html`<input
          type="text"
          name=${field.name}
          aria-label=${field.label}
          autocomplete="off"
          .value=${value}
          @change=${(e: Event) =>
            this._emit(field.name, (e.target as HTMLInputElement).value.trim())}
        />`;
    }
  }

  private _section(field: FilterField) {
    return html`<details>
      <summary>
        <span class="label">${field.label}</span>
        <span class="value"><bdi>${this._shown(field)}</bdi></span>
      </summary>
      <div class="control">${this._control(field)}</div>
    </details>`;
  }

  private _filters() {
    return html`<div class="pane-header">
        <span>${strings.filters.filters}</span>
        <span>
          <button type="button" class="text clear" @click=${this._clear}>
            ${strings.filters.clear}
          </button>
          ${
            this.narrow
              ? html`<button type="button" class="text" @click=${this._toggle}>
                  ${strings.filters.done}
                </button>`
              : nothing
          }
        </span>
      </div>
      ${this._visible().map((field) => this._section(field))}`;
  }

  protected override render() {
    const active = this._active();
    return html`
      <div class="header">
        <button
          type="button"
          class="chip"
          aria-expanded=${this.open ? "true" : "false"}
          @click=${this._toggle}
        >
          ${strings.filters.filters}
          ${active > 0 ? html`<span class="badge">${active}</span>` : nothing}
        </button>
        <div class="search">
          <input
            type="text"
            name="search"
            maxlength="200"
            autocomplete="off"
            placeholder=${strings.filters.searchPlaceholder}
            aria-label=${strings.filters.search}
            @input=${this._onSearch}
          />
        </div>
        <slot name="actions"></slot>
      </div>
      <div class="layout">
        ${
          this.open && !this.narrow
            ? html`<div class="pane">${this._filters()}</div>`
            : nothing
        }
        <div class="main"><slot></slot></div>
      </div>
      ${
        this.open && this.narrow
          ? html`<div class="sheet">${this._filters()}</div>`
          : nothing
      }
    `;
  }
}

customElements.define("ef-filter-pane", EfFilterPane);

declare global {
  interface HTMLElementTagNameMap {
    "ef-filter-pane": EfFilterPane;
  }
}
