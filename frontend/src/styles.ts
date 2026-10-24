import { css } from "lit";

/** Only Home Assistant CSS custom properties are used for colours. */
export const panelStyles = css`
  :host {
    display: block;
    min-height: 100vh;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
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
    font-size: 20px;
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
    font-size: 14px;
  }
  .time,
  .ip,
  .where {
    color: var(--secondary-text-color);
    font-size: 12px;
  }
  .main {
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  bdi {
    unicode-bidi: isolate;
  }
  .tag {
    padding: 0 6px;
    border-radius: 4px;
    font-size: 12px;
    border: 1px solid var(--divider-color);
    color: var(--secondary-text-color);
  }
  .tag.bad {
    color: var(--error-color);
    border-color: var(--error-color);
  }
  .tag.good {
    color: var(--success-color);
    border-color: var(--success-color);
  }
  .tag.warn {
    color: var(--warning-color);
    border-color: var(--warning-color);
  }
`;
