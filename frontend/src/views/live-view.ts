import { LitElement, html, nothing } from "lit";
import { subscribe } from "../api.ts";
import { strings } from "../strings.ts";
import type {
  ChangeItem,
  HomeAssistant,
  LoginItem,
  SubscribeEvent,
  UnsubscribeFunc,
} from "../types.ts";

/** Wait this long after a push before refetching, to merge bursts. */
export const REFRESH_DELAY_MS = 1000;

/** Most live items kept above the fetched pages. */
export const LIVE_CAP = 200;

export type Phase = "loading" | "ready" | "error" | "unloaded";

export const isNotLoaded = (err: unknown): boolean =>
  typeof err === "object" &&
  err !== null &&
  "code" in err &&
  err.code === "not_loaded";

/**
 * Shared lifecycle of the data tabs: load, follow live pushes, and stop for
 * good once the integration is unloaded (only a retry or a reconnect while
 * unloaded starts again, never a new `hass`).
 */
export abstract class LiveView extends LitElement {
  static override properties = {
    hass: { attribute: false },
    _phase: { state: true },
    _liveFailed: { state: true },
  };

  declare hass: HomeAssistant;
  declare _phase: Phase;
  declare _liveFailed: boolean;

  protected _generation = 0;
  protected _requests = 0;
  private _started = false;
  private _unloaded = false;
  private _unsub: UnsubscribeFunc | undefined;
  private _timer: ReturnType<typeof setTimeout> | undefined;

  constructor() {
    super();
    this._phase = "loading";
    this._liveFailed = false;
  }

  /** Fetch the data; the returned function applies it if still current. */
  protected abstract _fetch(): Promise<() => void>;

  /** A live push. By default the data is refetched, throttled. */
  protected _onLive(item: LoginItem | ChangeItem, kind: "login" | "change") {
    void item;
    void kind;
    this._scheduleRefresh();
  }

  /** Extra loads that start with the view; failures must be swallowed. */
  protected _loadExtras(generation: number): void {
    void generation;
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
    if (
      changed.has("hass") &&
      this.hass !== undefined &&
      this.isConnected &&
      !this._unloaded
    ) {
      this._start();
    }
  }

  private _start(): void {
    if (this._started) {
      return;
    }
    this._started = true;
    this._unloaded = false;
    this._liveFailed = false;
    const generation = ++this._generation;
    this._phase = "loading";
    void this._refresh();
    void this._subscribe(generation);
    this._loadExtras(generation);
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
    this._onLive(event.item, event.type);
  }

  /** Refetch after a push; a timer already running covers later pushes. */
  protected _scheduleRefresh(): void {
    if (this._timer === undefined) {
      this._timer = setTimeout(() => {
        this._timer = undefined;
        void this._refresh();
      }, REFRESH_DELAY_MS);
    }
  }

  private _markUnloaded(): void {
    this._clearTimer();
    this._dropSubscription();
    this._started = false;
    this._unloaded = true;
    this._generation += 1;
    this._phase = "unloaded";
  }

  protected _fail(err: unknown): void {
    if (isNotLoaded(err)) {
      this._markUnloaded();
    } else {
      this._phase = "error";
    }
  }

  protected async _refresh(): Promise<void> {
    const generation = this._generation;
    const request = ++this._requests;
    try {
      const apply = await this._fetch();
      // Ignore answers that are stale or from a stopped view.
      if (generation !== this._generation || request !== this._requests) {
        return;
      }
      apply();
      this._phase = "ready";
    } catch (err) {
      if (generation === this._generation && request === this._requests) {
        this._fail(err);
      }
    }
  }

  protected _retry = (): void => {
    this._stop();
    this._start();
  };

  private _onVisibility = (): void => {
    if (document.visibilityState === "visible" && this._phase === "unloaded") {
      this._retry();
    }
  };

  /** Unloaded, live-failed and load-error banners (null when none). */
  protected _banners() {
    if (this._phase === "unloaded") {
      return html`
        <div class="notice" role="status">
          <strong>${strings.unloaded}</strong>
          <span class="hint">${strings.unloadedHint}</span>
          <button type="button" @click=${this._retry}>${strings.retry}</button>
        </div>
      `;
    }
    return html`
      ${
        this._liveFailed
          ? html`<div class="notice" role="status">
              <strong>${strings.liveUnavailable}</strong>
              <button type="button" @click=${this._retry}>
                ${strings.retry}
              </button>
            </div>`
          : nothing
      }
      ${
        this._phase === "error"
          ? html`<div class="notice error" role="alert">
              <strong>${strings.loadError}</strong>
              <button type="button" @click=${this._retry}>
                ${strings.retry}
              </button>
            </div>`
          : nothing
      }
    `;
  }
}
