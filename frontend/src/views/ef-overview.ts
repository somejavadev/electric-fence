import { LitElement, css, html, nothing } from "lit";
import { fetchOverview, fetchStatus, subscribe } from "../api.ts";
import "../components/ef-answer-card.ts";
import "../components/ef-change-row.ts";
import "../components/ef-login-row.ts";
import "../components/ef-posture-list.ts";
import { strings } from "../strings.ts";
import type {
  HomeAssistant,
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

type Phase = "loading" | "ready" | "error" | "unloaded";

const isNotLoaded = (err: unknown): boolean =>
  typeof err === "object" &&
  err !== null &&
  "code" in err &&
  err.code === "not_loaded";

/** Overview tab (layout A): four cards answering the first questions. */
export class EfOverview extends LitElement {
  static override properties = {
    hass: { attribute: false },
    _overview: { state: true },
    _status: { state: true },
    _phase: { state: true },
    _liveFailed: { state: true },
  };

  declare hass: HomeAssistant;
  declare _overview: Overview | undefined;
  declare _status: Status | undefined;
  declare _phase: Phase;
  declare _liveFailed: boolean;

  private _generation = 0;
  private _requests = 0;
  private _started = false;
  /** Set by an unload; only a retry clears it, never a new `hass`. */
  private _unloaded = false;
  private _unsub: UnsubscribeFunc | undefined;
  private _timer: ReturnType<typeof setTimeout> | undefined;

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
  `;

  constructor() {
    super();
    this._phase = "loading";
    this._liveFailed = false;
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
    this._unloaded = false;
    this._liveFailed = false;
    const generation = ++this._generation;
    this._phase = "loading";
    void this._refresh(generation);
    void this._subscribe(generation);
    void this._loadStatus(generation);
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

  private _stop(): void {
    this._started = false;
    this._generation += 1;
    this._clearTimer();
    this._dropSubscription();
  }

  private _clearTimer(): void {
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
      ${
        typeof attribution === "string" && attribution !== ""
          ? html`<p class="attribution"><bdi>${attribution}</bdi></p>`
          : nothing
      }
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
