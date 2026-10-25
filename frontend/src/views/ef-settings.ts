import { html, nothing } from "lit";
import { clearCountries, fetchStatus } from "../api.ts";
import "../components/ef-confirm-dialog.ts";
import { navigate } from "../fire-event.ts";
import { formatDate } from "../format.ts";
import { strings } from "../strings.ts";
import { viewStyles } from "../styles.ts";
import { bdiText } from "../text.ts";
import type { Status } from "../types.ts";
import { LiveView } from "./live-view.ts";

export const SETTINGS_PATH = "/config/integrations/integration/electric_fence";

/** Settings tab: read-only status; options live in Home Assistant. */
export class EfSettings extends LiveView {
  static override properties = {
    ...LiveView.properties,
    _status: { state: true },
    _clearing: { state: true },
    _busy: { state: true },
    _error: { state: true },
    _cleared: { state: true },
  };

  declare _status: Status | undefined;
  declare _clearing: boolean;
  declare _busy: boolean;
  declare _error: string;
  declare _cleared: boolean;

  static override styles = viewStyles;

  constructor() {
    super();
    this._clearing = false;
    this._busy = false;
    this._error = "";
    this._cleared = false;
  }

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
            <td>${s.providers[geo.provider] ?? bdiText(geo.provider)}</td>
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
              ${geo.last_error === null ? s.none : html`<span class="geo-error">${bdiText(geo.last_error, 256)}</span>`}
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

  private _askClear = (): void => {
    this._error = "";
    this._busy = false;
    this._cleared = false;
    this._clearing = true;
  };

  private _onClearClosed = (): void => {
    this._clearing = false;
  };

  private _onClear = async (): Promise<void> => {
    if (this._busy) {
      return;
    }
    this._busy = true;
    this._error = "";
    try {
      await clearCountries(this.hass);
    } catch {
      this._busy = false;
      this._error = strings.settings.clearError;
      return;
    }
    this._busy = false;
    this._clearing = false;
    this._cleared = true;
  };

  /** Read-only label/value rows. */
  private _table(rows: [string, unknown][]) {
    return html`<table>
      <tbody>
        ${rows.map(
          ([label, value]) =>
            html`<tr>
              <th scope="row">${label}</th>
              <td>${value}</td>
            </tr>`,
        )}
      </tbody>
    </table>`;
  }

  private _protection(status: Status) {
    const s = strings.settings;
    const v = status.settings;
    const onOff = (on: boolean) => (on ? s.on : s.off);
    const targets = v.notify_targets;
    return html`
      <section class="card">
        <h2>${s.bans}</h2>
        ${this._table([
          [s.autoBan, onOff(v.auto_ban)],
          [s.banAfter, s.banRule(v.ban_threshold, v.ban_window_minutes)],
          [s.banLength, v.ban_hours === 0 ? s.permanent : s.hours(v.ban_hours)],
          [s.repeatOffenders, onOff(v.ban_repeat_offenders)],
        ])}
      </section>
      <section class="card">
        <h2>${s.usernameProtection}</h2>
        ${this._table([
          [s.protectUsernames, onOff(v.username_protection)],
          [
            s.slowDownAfter,
            s.attackRule(
              v.attack_threshold,
              v.attack_min_ips,
              v.attack_window_minutes,
            ),
          ],
        ])}
      </section>
      <section class="card">
        <h2>${s.notifications}</h2>
        ${this._table([
          [s.notifyFailedLogin, onOff(v.notify_failed_login)],
          [s.notifyIpBanned, onOff(v.notify_ip_banned)],
          [s.notifyUsernameAttack, onOff(v.notify_username_attack)],
          [s.notifyNewCountry, onOff(v.notify_new_country)],
          [s.notifyBanEnded, onOff(v.notify_ban_ended)],
          [
            s.notifyTargets,
            targets.length === 0
              ? s.none
              : targets.map(
                  (t, i) => html`${i > 0 ? ", " : nothing}${bdiText(t)}`,
                ),
          ],
        ])}
      </section>
      <section class="card">
        <h2>${s.knownCountries}</h2>
        <p class="hint">${s.knownCountriesHint}</p>
        <div class="more">
          <button type="button" aria-haspopup="dialog" @click=${this._askClear}>
            ${s.clearCountries}
          </button>
        </div>
        ${
          this._cleared
            ? html`<p class="hint" role="status">${s.cleared}</p>`
            : nothing
        }
        ${
          this._clearing
            ? html`<ef-confirm-dialog
                .heading=${s.clearTitle}
                text=${s.clearText}
                action=${s.clearAction}
                error=${this._error}
                ?busy=${this._busy}
                @confirm-action=${this._onClear}
                @confirm-closed=${this._onClearClosed}
              ></ef-confirm-dialog>`
            : nothing
        }
      </section>
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
              <th scope="row">
                ${Object.hasOwn(s.collectorNames, c.name) ? s.collectorNames[c.name] : bdiText(c.name)}
              </th>
              <td>
                <span class="tag ${c.enabled ? "good" : "warn"}"
                  >${c.enabled ? s.on : s.off}</span
                >
              </td>
              <td>
                ${c.reason.map((r, i) => html`${i > 0 ? ", " : nothing}${bdiText(r)}`)}
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
      <h2 class="visually-hidden">${s.heading}</h2>
      ${this._banners()}
      ${
        status === undefined
          ? this._phase === "error" || this._phase === "unloaded"
            ? nothing
            : html`<p class="loading">${strings.loading}</p>`
          : html`
              ${this._protection(status)}
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
