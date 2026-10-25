import { css } from "lit";

/** Only Home Assistant CSS custom properties are used for colours. */
export const panelStyles = css`
  :host {
    display: block;
    min-height: 100vh;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-size: var(--ha-font-size-m, 14px);
    line-height: var(--ha-line-height-normal, 1.6);
  }
  header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 16px;
    height: 56px;
    background: var(--card-background-color);
    border-bottom: 1px solid var(--divider-color);
  }
  h1 {
    font-size: var(--ha-font-size-xl, 20px);
    font-weight: 400;
    margin: 0;
  }
  nav {
    display: flex;
    overflow-x: auto;
    background: var(--card-background-color);
    border-bottom: 1px solid var(--divider-color);
  }
  nav button {
    flex: none;
    padding: 12px 16px;
    border: 0;
    border-bottom: 2px solid transparent;
    background: none;
    color: var(--secondary-text-color);
    font: inherit;
    cursor: pointer;
  }
  nav button[aria-selected="true"] {
    color: var(--primary-color);
    border-bottom-color: var(--primary-color);
  }
  main {
    padding: 16px;
    max-width: 1200px;
    margin: 0 auto;
  }
  .error {
    color: var(--error-color);
  }
  .warning {
    color: var(--warning-color);
  }
  .success {
    color: var(--success-color);
  }
`;

/** Shared by the Overview rows. */
export const rowStyles = css`
  :host {
    display: block;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 2px 8px;
    padding: 6px 0;
    font-size: var(--ha-font-size-m, 14px);
  }
  .time,
  .ip,
  .where {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .main {
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  bdi {
    unicode-bidi: isolate;
  }
  .tag {
    padding: 0 8px;
    border-radius: var(--ha-border-radius-xl, 16px);
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
    color: var(--primary-text-color);
    background: color-mix(
      in srgb,
      var(--secondary-text-color) 15%,
      transparent
    );
  }
  /* Tinted like ha-label: status colours are too light for small text. */
  .tag.bad {
    background: color-mix(in srgb, var(--error-color) 20%, transparent);
  }
  .tag.good {
    background: color-mix(in srgb, var(--success-color) 20%, transparent);
  }
  .tag.warn {
    background: color-mix(in srgb, var(--warning-color) 25%, transparent);
  }
`;

/** Shared by the Activity, Audit, Users and Settings tabs. */
export const viewStyles = css`
  :host {
    display: block;
  }
  h2 {
    /* Home Assistant's card header (ha-card .card-header). */
    font-size: var(--ha-card-header-font-size, var(--ha-font-size-2xl, 24px));
    font-weight: var(--ha-font-weight-normal, 400);
    letter-spacing: -0.012em;
    line-height: var(--ha-line-height-condensed, 1.2);
    margin: 0 0 8px;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li + li {
    border-top: 1px solid var(--divider-color);
  }
  .empty,
  .loading,
  .hint {
    margin: 8px 0 0;
    font-size: var(--ha-font-size-m, 14px);
    color: var(--secondary-text-color);
  }
  .notice {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 16px;
    margin-bottom: 16px;
    padding: 12px 16px;
    border: 1px solid var(--warning-color);
    border-radius: var(--ha-card-border-radius, 12px);
    background: var(--card-background-color);
  }
  .notice.error {
    border-color: var(--error-color);
  }
  button {
    padding: 6px 12px;
    border: 1px solid var(--primary-color);
    border-radius: 8px;
    background: none;
    color: var(--primary-color);
    font: inherit;
    cursor: pointer;
  }
  button.toggle {
    border: 0;
    padding: 2px 4px;
    color: var(--primary-text-color);
    text-align: start;
  }
  .more {
    margin-top: 16px;
  }
  bdi {
    unicode-bidi: isolate;
  }
  .card {
    padding: 16px;
    margin-bottom: 16px;
    border-radius: var(--ha-card-border-radius, 12px);
    background: var(--card-background-color);
    border: 1px solid var(--divider-color);
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--ha-font-size-m, 14px);
  }
  th,
  td {
    padding: 6px 8px;
    text-align: start;
    vertical-align: top;
    border-bottom: 1px solid var(--divider-color);
    overflow-wrap: anywhere;
  }
  th {
    color: var(--secondary-text-color);
    font-weight: 500;
  }
  .tag {
    display: inline-block;
    margin-inline-end: 4px;
    padding: 0 8px;
    border-radius: var(--ha-border-radius-xl, 16px);
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
    color: var(--primary-text-color);
    background: color-mix(
      in srgb,
      var(--secondary-text-color) 15%,
      transparent
    );
  }
  /* Tinted like ha-label: status colours are too light for small text. */
  .tag.bad {
    background: color-mix(in srgb, var(--error-color) 20%, transparent);
  }
  .tag.good {
    background: color-mix(in srgb, var(--success-color) 20%, transparent);
  }
  .tag.warn {
    background: color-mix(in srgb, var(--warning-color) 25%, transparent);
  }
  .sub {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .details {
    font-size: var(--ha-font-size-s, 12px);
    color: var(--secondary-text-color);
    padding-bottom: 6px;
  }
`;
