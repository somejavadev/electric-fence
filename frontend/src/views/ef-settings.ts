import { html, nothing } from "lit";
import { fetchStatus } from "../api.ts";
import { MAX_TEXT } from "../components/ef-login-row.ts";
import { navigate } from "../fire-event.ts";
import { formatDate, truncate } from "../format.ts";
import { strings } from "../strings.ts";
import { viewStyles } from "../styles.ts";
import type { Status } from "../types.ts";
import { LiveView } from "./live-view.ts";

export const SETTINGS_PATH = "/config/integrations/integration/electric_fence";

const text = (value: string, max = MAX_TEXT) => {
  const t = truncate(value, max);
  return html`<bdi title=${t.full}>${t.short}</bdi>`;
};

/** Settings tab: read-only status; options live in Home Assistant. */
export class EfSettings extends LiveView {
  static override properties = {
    ...LiveView.properties,
    _status: { state: true },
  };

  declare _status: Status | undefined;

  static override styles = viewStyles;

  protected override async _fetch(): Promise<() => void> {
    const status = await fetchStatus(this.hass);
    return () => {
      this._status = status;
    };
  }

  private _open = (): void => {
    navigate(SETTINGS_PATH);
  };

  private _geo(status: Status) {
    const s = strings.settings;
    const geo = status.geo;
    if (geo === null) {
      return html`<p class="empty">${s.notConfigured}</p>`;
    }
    return html`
      <table>
        <tbody>
          <tr>
            <th scope="row">${s.provider}</th>
            <td>${s.providers[geo.provider] ?? text(geo.provider)}</td>
          </tr>
          <tr>
            <th scope="row">${s.databaseDate}</th>
            <td>
              ${geo.db_date === null ? s.unknown : formatDate(geo.db_date, this.hass)}
            </td>
          </tr>
          <tr>
            <th scope="row">${s.lastError}</th>
            <td>
              ${geo.last_error === null ? s.none : html`<span class="geo-error">${text(geo.last_error, 256)}</span>`}
            </td>
          </tr>
        </tbody>
      </table>
      ${
        geo.attribution !== null && geo.attribution !== ""
          ? html`<p class="sub attribution"><bdi>${geo.attribution}</bdi></p>`
          : nothing
      }
    `;
  }

  private _collectors(status: Status) {
    const s = strings.settings;
    return html`<table>
      <thead>
        <tr>
          <th scope="col">${s.collector}</th>
          <th scope="col">${s.state}</th>
          <th scope="col">${s.reasons}</th>
        </tr>
      </thead>
      <tbody>
        ${status.collectors.map(
          (c) =>
            html`<tr>
              <th scope="row">${text(c.name)}</th>
              <td>
                <span class="tag ${c.enabled ? "good" : "warn"}"
                  >${c.enabled ? s.on : s.off}</span
                >
              </td>
              <td>
                ${c.reason.map((r, i) => html`${i > 0 ? ", " : nothing}${text(r)}`)}
              </td>
            </tr>`,
        )}
      </tbody>
    </table>`;
  }

  protected override render() {
    const s = strings.settings;
    const status = this._status;
    return html`
      <h2>${s.heading}</h2>
      ${this._banners()}
      ${
        status === undefined
          ? this._phase === "error" || this._phase === "unloaded"
            ? nothing
            : html`<p class="loading">${strings.loading}</p>`
          : html`
              <section class="card">
                <h2>${s.geoip}</h2>
                ${this._geo(status)}
              </section>
              <section class="card">
                <h2>${s.collectors}</h2>
                ${this._collectors(status)}
              </section>
              <section class="card">
                <h2>${s.worker}</h2>
                <table>
                  <tbody>
                    <tr>
                      <th scope="row">${s.queued}</th>
                      <td>${status.worker.queued}</td>
                    </tr>
                    <tr>
                      <th scope="row">${s.dropped}</th>
                      <td>${status.worker.dropped}</td>
                    </tr>
                    <tr>
                      <th scope="row">${s.storageErrors}</th>
                      <td>${status.worker.storage_errors}</td>
                    </tr>
                  </tbody>
                </table>
              </section>
              <p class="hint">${s.openHint}</p>
              <button type="button" @click=${this._open}>${s.open}</button>
            `
      }
    `;
  }
}

customElements.define("ef-settings", EfSettings);

declare global {
  interface HTMLElementTagNameMap {
    "ef-settings": EfSettings;
  }
}
