import { LitElement, css, html } from "lit";
import { fireEvent } from "../fire-event.ts";

export interface FilterOption {
  value: string;
  label: string;
}

/** One labelled control. A checkbox's value is "1" when on, else "". */
export interface FilterField {
  name: string;
  label: string;
  type: "select" | "text" | "checkbox";
  options?: FilterOption[];
}

export interface FilterChange {
  name: string;
  value: string;
}

/**
 * A row of labelled filter controls. Emits `filter-changed` with
 * `{name, value}`; text fields report on `change` (Enter or blur).
 */
export class EfFilterBar extends LitElement {
  static override properties = {
    fields: { attribute: false },
    values: { attribute: false },
  };

  declare fields: FilterField[];
  declare values: Record<string, string>;

  static override styles = css`
    :host {
      display: block;
    }
    form {
      display: flex;
      flex-wrap: wrap;
      align-items: end;
      gap: 8px 12px;
      margin-bottom: 16px;
    }
    label {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: var(--ha-font-size-s, 12px);
      color: var(--secondary-text-color);
    }
    label.check {
      flex-direction: row;
      align-items: center;
      font-size: var(--ha-font-size-m, 14px);
      color: var(--primary-text-color);
    }
    /* Home Assistant's outlined field look, at a dense height. */
    select,
    input[type="text"] {
      box-sizing: border-box;
      width: 150px;
      height: 40px;
      padding: 0 12px;
      font: inherit;
      font-size: var(--ha-font-size-m, 14px);
      color: var(--primary-text-color);
      background: var(--card-background-color);
      border: 1px solid var(--outline-color, var(--divider-color));
      border-radius: var(--ha-border-radius-sm, 4px);
    }
    select:hover,
    input[type="text"]:hover {
      border-color: var(--primary-text-color);
    }
    select:focus-visible,
    input[type="text"]:focus-visible {
      outline: 2px solid var(--primary-color);
      outline-offset: -2px;
    }
    input[type="checkbox"] {
      width: 18px;
      height: 18px;
      margin: 0;
      accent-color: var(--primary-color);
    }
  `;

  constructor() {
    super();
    this.fields = [];
    this.values = {};
  }

  protected override render() {
    return html`<form @submit=${(e: Event) => e.preventDefault()}>
      ${this.fields.map((field) => this._control(field))}
    </form>`;
  }

  /** Options render after the select, so its value is set once they exist. */
  protected override updated(): void {
    for (const select of this.renderRoot.querySelectorAll("select")) {
      select.value = this.values[select.name] ?? "";
    }
  }

  private _emit(name: string, value: string): void {
    fireEvent<FilterChange>(this, "filter-changed", { name, value });
  }

  private _control(field: FilterField) {
    const value = this.values[field.name] ?? "";
    if (field.type === "checkbox") {
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
    }
    if (field.type === "select") {
      return html`<label
        >${field.label}
        <select
          name=${field.name}
          @change=${(e: Event) =>
            this._emit(field.name, (e.target as HTMLSelectElement).value)}
        >
          ${(field.options ?? []).map(
            (option) =>
              html`<option value=${option.value}>${option.label}</option>`,
          )}
        </select></label
      >`;
    }
    return html`<label
      >${field.label}
      <input
        type="text"
        name=${field.name}
        autocomplete="off"
        .value=${value}
        @change=${(e: Event) =>
          this._emit(field.name, (e.target as HTMLInputElement).value.trim())}
    /></label>`;
  }
}

customElements.define("ef-filter-bar", EfFilterBar);

declare global {
  interface HTMLElementTagNameMap {
    "ef-filter-bar": EfFilterBar;
  }
}
