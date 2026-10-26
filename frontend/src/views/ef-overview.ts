import { LitElement, css, html, nothing } from "lit";
import {
  endSlowdown,
  fetchIntegrations,
  fetchOverview,
  fetchStatus,
  scanIntegrations,
  subscribe,
} from "../api.ts";
import "../components/ef-answer-card.ts";
import "../components/ef-change-row.ts";
import "../components/ef-confirm-dialog.ts";
import "../components/ef-flag.ts";
import "../components/ef-integration-dialog.ts";
import "../components/ef-login-row.ts";
import "../components/ef-posture-list.ts";
import { formatTime } from "../format.ts";
import { integrationFiles, integrationHealth, strings } from "../strings.ts";
import { bdiText, fill } from "../text.ts";
import type {
  AttackItem,
  HomeAssistant,
  IntegrationItem,
  IntegrationsPage,
  Overview,
  Status,
  SubscribeEvent,
  UnsubscribeFunc,
} from "../types.ts";

/** Wait this long after a push before refetching, to merge bursts. */
export const REFRESH_DELAY_MS = 1000;

/** Where the cards lead: the same rows the card counts, over the same range. */
export const FAILED_LINK =
  "/electric-fence/activity?result=failed_or_blocked&range=24h";
export const SUCCESS_LINK = "/electric-fence/activity?result=success&range=all";
export const BANS_LINK = "/electric-fence/bans";

/** After Check now, look this often (and at most this many times) for the end. */
export const SCAN_POLL_MS = 2000;
/** Later polls are further apart; give up on a scan after this long. */
export const SCAN_SLOW_POLL_MS = 10_000;
export const SCAN_GIVE_UP_MS = 5 * 60_000;

type Phase = "loading" | "ready" | "error" | "unloaded";

const isNotLoaded = (err: unknown): boolean =>
  typeof err === "object" &&
  err !== null &&
  "code" in err &&
  err.code === "not_loaded";

/**
 * Overview tab (layout A): cards answering the first questions, with any
 * usernames under attack above them.
 */
export class EfOverview extends LitElement {
  static override properties = {
    hass: { attribute: false },
    _overview: { state: true },
    _status: { state: true },
    _phase: { state: true },
    _liveFailed: { state: true },
    _ending: { state: true },
    _busy: { state: true },
    _error: { state: true },
    _integrations: { state: true },
    _selected: { state: true },
    _scanning: { state: true },
    _scanMessage: { state: true },
    _integrationsFailed: { state: true },
  };

  declare hass: HomeAssistant;
  /** The username whose slow-down the confirmation asks to end. */
  declare _ending: string | undefined;
  declare _busy: boolean;
  declare _error: string;
  declare _overview: Overview | undefined;
  declare _status: Status | undefined;
  declare _phase: Phase;
  declare _liveFailed: boolean;
  declare _integrations: IntegrationsPage | undefined;
  /** The domain whose details dialog is open. */
  declare _selected: string | undefined;
  declare _scanning: boolean;
  declare _scanMessage: string;
  declare _integrationsFailed: boolean;

  private _generation = 0;
  private _requests = 0;
  private _started = false;
  /** Set by an unload; only a retry clears it, never a new `hass`. */
  private _unloaded = false;
  private _unsub: UnsubscribeFunc | undefined;
  private _timer: ReturnType<typeof setTimeout> | undefined;
  private _scanTimer: ReturnType<typeof setTimeout> | undefined;

  static override styles = css`
    :host {
      display: block;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
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
    .loading {
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
    .notice button {
      padding: 6px 12px;
      border: 1px solid var(--primary-color);
      border-radius: 8px;
      background: none;
      color: var(--primary-color);
      font: inherit;
      cursor: pointer;
    }
    .hint {
      color: var(--secondary-text-color);
      font-size: var(--ha-font-size-m, 14px);
    }
    .attribution {
      margin: 16px 0 0;
      font-size: var(--ha-font-size-s, 12px);
      color: var(--secondary-text-color);
    }
    /* Laid out like the answer cards, without opening a tab on click. */
    .attacks {
      margin-bottom: 16px;
      padding: 16px;
      background: var(--ha-card-background, var(--card-background-color));
      border: 1px solid var(--ha-card-border-color, var(--divider-color));
      border-radius: var(--ha-card-border-radius, 12px);
      box-shadow: var(--ha-card-box-shadow, none);
    }
    .integrations {
      margin-top: 16px;
      padding: 16px;
      background: var(--ha-card-background, var(--card-background-color));
      border: 1px solid var(--ha-card-border-color, var(--divider-color));
      border-radius: var(--ha-card-border-radius, 12px);
      box-shadow: var(--ha-card-box-shadow, none);
    }
    .integrations header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px 16px;
    }
    .integrations button.item {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 2px 8px;
      width: 100%;
      min-height: 40px;
      padding: 8px 0;
      border: 0;
      background: none;
      color: inherit;
      font: inherit;
      text-align: start;
      cursor: pointer;
    }
    .integrations .name {
      font-weight: var(--ha-font-weight-medium, 500);
      overflow-wrap: anywhere;
    }
    .integrations .sub {
      font-size: var(--ha-font-size-s, 12px);
      color: var(--secondary-text-color);
      overflow-wrap: anywhere;
    }
    .integrations .tag {
      padding: 0 8px;
      border-radius: var(--ha-border-radius-xl, 16px);
      font-size: var(--ha-font-size-s, 12px);
      background: color-mix(
        in srgb,
        var(--secondary-text-color) 15%,
        transparent
      );
    }
    .integrations .tag.bad {
      background: color-mix(in srgb, var(--error-color) 20%, transparent);
    }
    .integrations .tag.good {
      background: color-mix(in srgb, var(--success-color) 20%, transparent);
    }
    .integrations .tag.warn {
      background: color-mix(in srgb, var(--warning-color) 25%, transparent);
    }
    .integrations button:focus-visible {
      outline: 2px solid var(--primary-color);
      outline-offset: 2px;
    }
    .integrations button.check {
      min-height: 40px;
      padding: 0 16px;
      border: 1px solid var(--outline-color, var(--divider-color));
      border-radius: var(--ha-border-radius-pill, 9999px);
      background: none;
      color: var(--primary-color);
      font: inherit;
      font-weight: var(--ha-font-weight-medium, 500);
      cursor: pointer;
    }
    .integrations button:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .integrations h2 {
      margin: 0;
      font-size: var(--ha-card-header-font-size, var(--ha-font-size-2xl, 24px));
      font-weight: var(--ha-font-weight-normal, 400);
      letter-spacing: -0.012em;
      line-height: var(--ha-line-height-condensed, 1.2);
    }
    .attacks h2 {
      margin: 0 0 8px;
      font-size: var(--ha-card-header-font-size, var(--ha-font-size-2xl, 24px));
      font-weight: var(--ha-font-weight-normal, 400);
      letter-spacing: -0.012em;
      line-height: var(--ha-line-height-condensed, 1.2);
    }
    .attacks li {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px 16px;
      padding: 8px 0;
    }
    .attacks .who {
      display: flex;
      flex-direction: column;
      min-width: 0;
    }
    .attacks .name {
      font-weight: var(--ha-font-weight-medium, 500);
      overflow-wrap: anywhere;
    }
    .attacks .sub {
      font-size: var(--ha-font-size-s, 12px);
      color: var(--secondary-text-color);
      overflow-wrap: anywhere;
    }
    .attacks ef-flag {
      margin-inline-start: 4px;
    }
    /* Home Assistant's outlined button (ha-button). */
    .attacks button {
      min-height: 40px;
      padding: 0 16px;
      border: 1px solid var(--outline-color, var(--divider-color));
      border-radius: var(--ha-border-radius-pill, 9999px);
      background: none;
      color: var(--primary-color);
      font: inherit;
      font-weight: var(--ha-font-weight-medium, 500);
      cursor: pointer;
    }
    .attacks button:focus-visible {
      outline: 2px solid var(--primary-color);
      outline-offset: 2px;
    }
  `;

  constructor() {
    super();
    this._phase = "loading";
    this._liveFailed = false;
    this._busy = false;
    this._error = "";
    this._scanning = false;
    this._scanMessage = "";
    this._integrationsFailed = false;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    document.addEventListener("visibilitychange", this._onVisibility);
    if (this.hass !== undefined) {
      this._start();
    }
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stop();
  }

  protected override willUpdate(changed: Map<PropertyKey, unknown>): void {
    // `hass` can arrive after the element is connected.
    if (
      changed.has("hass") &&
      this.hass !== undefined &&
      this.isConnected &&
      !this._unloaded
    ) {
      this._start();
    }
  }

  /** Begin loading and live updates. Does nothing when already running. */
  private _start(): void {
    if (this._started) {
      return;
    }
    this._started = true;
    this._scanning = false;
    this._unloaded = false;
    this._liveFailed = false;
    const generation = ++this._generation;
    this._phase = "loading";
    void this._refresh(generation);
    void this._subscribe(generation);
    void this._loadStatus(generation);
    void this._watchIntegrations(generation);
  }

  /** Only the attribution needs the status; failing here costs nothing. */
  private async _loadStatus(generation: number): Promise<void> {
    try {
      const status = await fetchStatus(this.hass);
      if (generation === this._generation) {
        this._status = status;
      }
    } catch {
      // No attribution line is the only effect.
    }
  }

  private async _loadIntegrations(generation: number): Promise<void> {
    try {
      const page = await fetchIntegrations(this.hass);
      if (generation === this._generation) {
        // An answer without a page counts as a failure.
        this._integrations = page ?? this._integrations;
        this._integrationsFailed = page == null;
      }
    } catch {
      if (generation === this._generation) {
        this._integrationsFailed = true;
      }
    }
  }

  /** Load, and follow a scan that is already running (a schedule, a tab). */
  private async _watchIntegrations(generation: number): Promise<void> {
    await this._loadIntegrations(generation);
    if (
      generation === this._generation &&
      this._integrations?.running === true &&
      this._scanTimer === undefined
    ) {
      this._pollScan(generation, 0);
    }
  }

  private _retryIntegrations = (): void => {
    void this._watchIntegrations(this._generation);
  };

  private _checkNow = async (): Promise<void> => {
    const page = this._integrations;
    if (
      this._scanning ||
      page === undefined ||
      !page.available ||
      page.running
    ) {
      return;
    }
    const generation = this._generation;
    this._scanning = true;
    this._scanMessage = "";
    try {
      const { started } = await scanIntegrations(this.hass);
      if (generation !== this._generation) {
        return;
      }
      this._scanMessage = started
        ? strings.integrations.started
        : strings.integrations.recent;
      if (started) {
        this._pollScan(generation, 0);
        return;
      }
    } catch {
      if (generation === this._generation) {
        this._scanMessage = strings.integrations.scanError;
      }
    }
    this._scanning = false;
  };

  /** Refetch until the scan ends; `elapsed` is the time waited so far. */
  private _pollScan(generation: number, elapsed: number): void {
    const delay = elapsed === 0 ? SCAN_POLL_MS : SCAN_SLOW_POLL_MS;
    this._scanTimer = setTimeout(() => {
      this._scanTimer = undefined;
      void (async () => {
        await this._loadIntegrations(generation);
        if (generation !== this._generation) {
          return;
        }
        const page = this._integrations;
        if (page?.running !== true) {
          this._scanning = false;
        } else if (elapsed + delay < SCAN_GIVE_UP_MS) {
          this._pollScan(generation, elapsed + delay);
        } else {
          // Give up waiting: do not leave the button stuck on stale state.
          this._integrations = { ...page, running: false };
          this._scanning = false;
        }
      })();
    }, delay);
  }

  private _stop(): void {
    this._started = false;
    this._generation += 1;
    this._clearTimer();
    this._dropSubscription();
  }

  private _clearTimer(): void {
    if (this._scanTimer !== undefined) {
      clearTimeout(this._scanTimer);
      this._scanTimer = undefined;
      this._scanning = false;
    }
    if (this._timer !== undefined) {
      clearTimeout(this._timer);
      this._timer = undefined;
    }
  }

  private _dropSubscription(): void {
    const unsub = this._unsub;
    this._unsub = undefined;
    try {
      unsub?.();
    } catch {
      // The server may already have ended it.
    }
  }

  private async _subscribe(generation: number): Promise<void> {
    try {
      const unsub = await subscribe(this.hass, (event) =>
        this._onEvent(event, generation),
      );
      if (generation !== this._generation) {
        unsub();
        return;
      }
      this._unsub = unsub;
    } catch (err) {
      if (generation !== this._generation) {
        return;
      }
      if (isNotLoaded(err)) {
        this._markUnloaded();
      } else {
        this._liveFailed = true;
      }
    }
  }

  private _onEvent(event: SubscribeEvent, generation: number): void {
    if (generation !== this._generation) {
      return;
    }
    if (event.type === "unloaded") {
      this._markUnloaded();
      return;
    }
    // Pushes carry `id: null`; the refetch replaces the lists anyway. A
    // timer already running covers every push that arrives before it fires.
    this._timer ??= setTimeout(() => {
      this._timer = undefined;
      void this._refresh(this._generation);
    }, REFRESH_DELAY_MS);
  }

  private _markUnloaded(): void {
    this._clearTimer();
    this._dropSubscription();
    this._started = false;
    this._unloaded = true;
    this._generation += 1;
    this._phase = "unloaded";
  }

  private _fail(err: unknown): void {
    if (isNotLoaded(err)) {
      this._markUnloaded();
    } else {
      this._phase = "error";
    }
  }

  private async _refresh(generation: number): Promise<void> {
    const request = ++this._requests;
    try {
      const overview = await fetchOverview(this.hass);
      // Ignore answers that are stale or from a stopped panel.
      if (generation !== this._generation || request !== this._requests) {
        return;
      }
      this._overview = overview;
      this._phase = "ready";
    } catch (err) {
      if (generation === this._generation && request === this._requests) {
        this._fail(err);
      }
    }
  }

  private _askEnd(attack: AttackItem): void {
    this._error = "";
    this._busy = false;
    this._ending = attack.username;
  }

  private _onConfirmClosed = (): void => {
    this._ending = undefined;
  };

  private _onConfirmEnd = async (): Promise<void> => {
    const username = this._ending;
    if (username === undefined || this._busy) {
      return;
    }
    this._busy = true;
    this._error = "";
    try {
      await endSlowdown(this.hass, username);
    } catch {
      this._busy = false;
      this._error = strings.attacks.endError;
      return;
    }
    this._busy = false;
    this._ending = undefined;
    await this._refresh(this._generation);
  };

  /** Hidden while nothing is under attack. */
  private _attacks(attacks: AttackItem[] | undefined) {
    const a = strings.attacks;
    if (attacks === undefined || attacks.length === 0) {
      return nothing;
    }
    return html`<section class="attacks">
      <h2>${a.heading}</h2>
      <ul>
        ${attacks.map(
          (attack) =>
            html`<li>
              <div class="who">
                <span class="name">${bdiText(attack.username)}</span>
                <span class="sub"
                  >${a.failures(attack.failures)} ·
                  ${a.addresses(attack.ip_count)}${attack.countries.map(
                    (code) =>
                      html`<ef-flag .code=${code}></ef-flag> ${bdiText(code)}`,
                  )}</span
                >
              </div>
              <button
                type="button"
                aria-haspopup="dialog"
                @click=${() => this._askEnd(attack)}
              >
                ${a.endSlowdown}
              </button>
            </li>`,
        )}
      </ul>
    </section>`;
  }

  private _tag(text: string, level: "good" | "warn" | "bad" | "") {
    return html`<span class="tag ${level}">${text}</span>`;
  }

  /** The custom integrations and what the last check found. */
  private _integrationsCard() {
    const t = strings.integrations;
    const page = this._integrations;
    if (page === undefined && !this._integrationsFailed) {
      return nothing;
    }
    const available = page?.available === true;
    const disabled = !available || this._scanning || page?.running === true;
    return html`<section class="integrations">
      <header>
        <h2>${strings.cards.integrations}</h2>
        <button
          type="button"
          class="check"
          ?disabled=${disabled}
          @click=${this._checkNow}
        >
          ${t.checkNow}
        </button>
      </header>
      ${
        this._integrationsFailed
          ? html`<p class="sub" role="alert">
              ${strings.loadError}
              <button
                type="button"
                class="check"
                @click=${this._retryIntegrations}
              >
                ${strings.retry}
              </button>
            </p>`
          : nothing
      }
      ${
        page === undefined || page.scanned_at === null
          ? nothing
          : html`<p class="sub">
              ${t.scannedAt(formatTime(page.scanned_at, this.hass))}
            </p>`
      }
      ${
        this._scanMessage === ""
          ? nothing
          : html`<p class="sub" role="status">${this._scanMessage}</p>`
      }
      ${
        page === undefined
          ? nothing
          : !available && page.items.length === 0
            ? html`<p class="empty">${t.unavailable}</p>`
            : page.items.length === 0
              ? html`<p class="empty">${t.empty}</p>`
              : html`<ul>
                  ${page.items.map((item) => this._integrationRow(item))}
                </ul>`
      }
    </section>`;
  }

  private _integrationRow(item: IntegrationItem) {
    const t = strings.integrations;
    const files = item.files?.status;
    const filesLevel =
      files === "changed" ? "bad" : files === "match" ? "good" : "";
    const health = item.health.status;
    const healthLevel =
      health === "ok"
        ? "good"
        : health === "removed"
          ? "bad"
          : health === "unknown"
            ? ""
            : "warn";
    return html`<li>
      <button
        type="button"
        class="item"
        aria-haspopup="dialog"
        @click=${() => {
          this._selected = item.domain;
        }}
      >
        <span class="name">${bdiText(item.name)}</span>
        ${this._tag(Object.hasOwn(t.sources, item.source) ? (t.sources[item.source] ?? item.source) : item.source, "")}
        ${
          item.version === null
            ? nothing
            : html`<span class="sub"
                >${bdiText(t.version(item.version), 32)}</span
              >`
        }
        ${this._tag(integrationFiles(item), filesLevel)}
        ${this._tag(integrationHealth(item), healthLevel)}
      </button>
    </li>`;
  }

  private _integrationDialog() {
    const item = this._integrations?.items.find(
      (i) => i.domain === this._selected,
    );
    if (item === undefined) {
      return nothing;
    }
    return html`<ef-integration-dialog
      .hass=${this.hass}
      .item=${item}
      @dialog-closed=${() => {
        this._selected = undefined;
      }}
    ></ef-integration-dialog>`;
  }

  private _bans() {
    const protection = this._overview?.protection;
    if (protection === undefined) {
      return this._phase === "error"
        ? html`<p class="empty">${strings.loadError}</p>`
        : html`<p class="loading">${strings.loading}</p>`;
    }
    return html`<p class="empty">
      ${
        protection.available
          ? strings.attacks.bans24h(protection.bans_24h)
          : strings.attacks.unavailable
      }
    </p>`;
  }

  private _confirm() {
    const username = this._ending;
    if (username === undefined) {
      return nothing;
    }
    const a = strings.attacks;
    return html`<ef-confirm-dialog
      .heading=${fill(a.endTitle, username)}
      text=${a.endText}
      action=${a.endSlowdown}
      error=${this._error}
      ?busy=${this._busy}
      @confirm-action=${this._onConfirmEnd}
      @confirm-closed=${this._onConfirmClosed}
    ></ef-confirm-dialog>`;
  }

  private _retry = (): void => {
    this._stop();
    this._start();
  };

  private _onVisibility = (): void => {
    if (document.visibilityState === "visible" && this._phase === "unloaded") {
      this._retry();
    }
  };

  protected override render() {
    if (this._phase === "unloaded") {
      return html`
        <div class="notice" role="status">
          <strong>${strings.unloaded}</strong>
          <span class="hint">${strings.unloadedHint}</span>
          <button type="button" @click=${this._retry}>${strings.retry}</button>
        </div>
      `;
    }
    const overview = this._overview;
    const problems =
      overview?.posture.filter((item) => item.level !== "ok").length ?? 0;
    const attribution = this._status?.geo?.attribution;
    return html`
      ${
        this._liveFailed
          ? html`
              <div class="notice" role="status">
                <strong>${strings.liveUnavailable}</strong>
                <button type="button" @click=${this._retry}>
                  ${strings.retry}
                </button>
              </div>
            `
          : nothing
      }
      ${
        this._phase === "error"
          ? html`
              <div class="notice error" role="alert">
                <strong>${strings.loadError}</strong>
                <button type="button" @click=${this._retry}>
                  ${strings.retry}
                </button>
              </div>
            `
          : nothing
      }
      ${this._attacks(overview?.protection.attacks)}
      <div class="grid">
        <ef-answer-card
          heading=${strings.cards.failed}
          path=${FAILED_LINK}
          .count=${overview?.failed_24h.count}
        >
          ${this._list(
            overview?.failed_24h.items,
            strings.empty.failed,
            (item) =>
              html`<ef-login-row
                .hass=${this.hass}
                .item=${item}
                variant="failed"
              ></ef-login-row>`,
          )}
        </ef-answer-card>
        <ef-answer-card
          heading=${strings.attacks.activeBans}
          path=${BANS_LINK}
          .count=${overview?.protection.active_bans}
        >
          ${this._bans()}
        </ef-answer-card>
        <ef-answer-card
          heading=${strings.cards.changes}
          path="/electric-fence/audit"
          .count=${overview?.changes_7d.count}
        >
          ${this._list(
            overview?.changes_7d.items,
            strings.empty.changes,
            (item) =>
              html`<ef-change-row
                .hass=${this.hass}
                .item=${item}
                details
              ></ef-change-row>`,
          )}
        </ef-answer-card>
        <ef-answer-card heading=${strings.cards.success} path=${SUCCESS_LINK}>
          ${this._list(
            overview?.recent_success.items,
            strings.empty.success,
            (item) =>
              html`<ef-login-row
                .hass=${this.hass}
                .item=${item}
                variant="success"
              ></ef-login-row>`,
          )}
        </ef-answer-card>
        <ef-answer-card
          heading=${strings.cards.posture}
          path="/electric-fence/settings"
          note=${overview === undefined ? "" : strings.postureProblems(problems)}
        >
          ${this._list(
            overview?.posture,
            strings.empty.posture,
            undefined,
            (items) =>
              html`<ef-posture-list .items=${items}></ef-posture-list>`,
          )}
        </ef-answer-card>
      </div>
      ${this._integrationsCard()}
      ${
        typeof attribution === "string" && attribution !== ""
          ? html`<p class="attribution"><bdi>${attribution}</bdi></p>`
          : nothing
      }
      ${this._confirm()} ${this._integrationDialog()}
    `;
  }

  /** Rows, or the loading, error or empty text when there are none. */
  private _list<T>(
    items: T[] | undefined,
    emptyText: string,
    row?: (item: T) => unknown,
    whole?: (items: T[]) => unknown,
  ) {
    if (items === undefined) {
      return this._phase === "error"
        ? html`<p class="empty">${strings.loadError}</p>`
        : html`<p class="loading">${strings.loading}</p>`;
    }
    if (items.length === 0) {
      return html`<p class="empty">${emptyText}</p>`;
    }
    if (whole !== undefined) {
      return whole(items);
    }
    return html`<ul>
      ${items.map((item) => html`<li>${row?.(item)}</li>`)}
    </ul>`;
  }
}

customElements.define("ef-overview", EfOverview);

declare global {
  interface HTMLElementTagNameMap {
    "ef-overview": EfOverview;
  }
}
