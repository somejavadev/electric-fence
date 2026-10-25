import { html, nothing } from "lit";
import { fetchLogins, fetchUsers } from "../api.ts";
import "../components/ef-filter-bar.ts";
import type { FilterChange, FilterField } from "../components/ef-filter-bar.ts";
import "../components/ef-login-row.ts";
import { TIME_RANGES, sinceFor, truncate } from "../format.ts";
import type { TimeRange } from "../format.ts";
import { strings } from "../strings.ts";
import { viewStyles } from "../styles.ts";
import type {
  ChangeItem,
  LoginFilter,
  LoginItem,
  ResultFilter,
  UserItem,
} from "../types.ts";
import { LIVE_CAP, LiveView } from "./live-view.ts";

export const DEFAULT_RANGE: TimeRange = "24h";

const RESULTS: readonly ResultFilter[] = [
  "success",
  "failed",
  "blocked",
  "failed_or_blocked",
];

export interface ActivityFilters {
  result: ResultFilter | "";
  user: string;
  username: string;
  ip: string;
  country: string;
  range: TimeRange;
}

/** SQLite LIKE folds ASCII letters only; so does this. */
const asciiLower = (text: string): string =>
  text.replace(/[A-Z]/g, (c) => c.toLowerCase());

const clean = (value: string | null): string => (value ?? "").trim();

/** Filters from a query string such as `?result=failed`. Bad values drop. */
export function filtersFromQuery(search: string): ActivityFilters {
  const q = new URLSearchParams(search);
  const result = clean(q.get("result"));
  const range = clean(q.get("range"));
  return {
    result: RESULTS.find((r) => r === result) ?? "",
    user: clean(q.get("user")),
    username: clean(q.get("username")),
    ip: clean(q.get("ip")),
    country: clean(q.get("country")).toUpperCase(),
    range: TIME_RANGES.find((r) => r === range) ?? DEFAULT_RANGE,
  };
}

/** The query string for filters; defaults are left out. */
export function queryFromFilters(filters: ActivityFilters): string {
  const q = new URLSearchParams();
  for (const key of ["result", "user", "username", "ip", "country"] as const) {
    if (filters[key] !== "") {
      q.set(key, filters[key]);
    }
  }
  if (filters.range !== DEFAULT_RANGE) {
    q.set("range", filters.range);
  }
  return q.toString();
}

/** The `fetchLogins` filter for the active filters. */
export function loginFilter(
  filters: ActivityFilters,
  now: number = Date.now(),
): LoginFilter {
  const out: LoginFilter = {};
  if (filters.result !== "") {
    out.result = filters.result;
  }
  if (filters.user !== "") {
    out.user_id = filters.user;
  }
  if (filters.username !== "") {
    out.username = filters.username;
  }
  if (filters.ip !== "") {
    out.ip = filters.ip;
  }
  if (filters.country !== "") {
    out.country = filters.country;
  }
  const since = sinceFor(filters.range, now);
  if (since !== undefined) {
    out.since = since;
  }
  return out;
}

/** Whether a pushed login belongs under the active filters (as the server filters). */
export function matchesLogin(
  item: LoginItem,
  filters: ActivityFilters,
  now: number = Date.now(),
): boolean {
  // "failed_or_blocked" is anything but a success.
  if (
    filters.result === "failed_or_blocked"
      ? item.result === "success"
      : filters.result !== "" && item.result !== filters.result
  ) {
    return false;
  }
  if (filters.user !== "" && item.user_id !== filters.user) {
    return false;
  }
  if (
    filters.username !== "" &&
    !asciiLower(item.username_typed ?? "").includes(
      asciiLower(filters.username),
    )
  ) {
    return false;
  }
  if (filters.ip !== "" && item.ip !== filters.ip) {
    return false;
  }
  if (filters.country !== "" && item.country_code !== filters.country) {
    return false;
  }
  const since = sinceFor(filters.range, now);
  return since === undefined || Date.parse(item.ts) >= Date.parse(since);
}

/** Activity tab: every login attempt, filtered, with live additions. */
export class EfActivity extends LiveView {
  static override properties = {
    ...LiveView.properties,
    _filters: { state: true },
    _items: { state: true },
    _live: { state: true },
    _next: { state: true },
    _users: { state: true },
    _loadingMore: { state: true },
  };

  declare _filters: ActivityFilters;
  declare _items: LoginItem[];
  /** Pushed items, newest first. They have no id and are never cursors. */
  declare _live: LoginItem[];
  declare _next: number | null;
  declare _users: UserItem[];
  declare _loadingMore: boolean;

  static override styles = viewStyles;

  constructor() {
    super();
    this._filters = filtersFromQuery("");
    this._items = [];
    this._live = [];
    this._next = null;
    this._users = [];
    this._loadingMore = false;
  }

  override connectedCallback(): void {
    // The Overview links here with `?result=failed_or_blocked&range=24h`.
    this._filters = filtersFromQuery(location.search);
    super.connectedCallback();
  }

  protected override async _fetch(): Promise<() => void> {
    const page = await fetchLogins(this.hass, loginFilter(this._filters));
    return () => {
      this._items = page.items;
      this._next = page.next_before_id;
      this._live = [];
    };
  }

  protected override _loadExtras(generation: number): void {
    void (async () => {
      try {
        const { items } = await fetchUsers(this.hass);
        if (generation === this._generation) {
          this._users = items;
        }
      } catch {
        // Without the list the user dropdown only offers "Any user".
      }
    })();
  }

  protected override _onLive(item: LoginItem | ChangeItem, kind: string): void {
    if (kind !== "login" || this._phase !== "ready") {
      return;
    }
    const login = item as LoginItem;
    if (matchesLogin(login, this._filters)) {
      this._live = [login, ...this._live].slice(0, LIVE_CAP);
    }
  }

  private _onFilter = (event: CustomEvent<FilterChange>): void => {
    const { name, value } = event.detail;
    if (!(name in this._filters)) {
      return;
    }
    const next = { ...this._filters, [name]: value } as ActivityFilters;
    if (name === "country") {
      next.country = value.toUpperCase();
    }
    if (
      name === "range" &&
      !(TIME_RANGES as readonly string[]).includes(value)
    ) {
      return;
    }
    this._filters = next;
    this._resetList();
    const query = queryFromFilters(next);
    history.replaceState(
      null,
      "",
      `${location.pathname}${query === "" ? "" : `?${query}`}`,
    );
    void this._refresh();
  };

  protected override _resetRun(): void {
    this._loadingMore = false;
  }

  /** Drop what the old filters showed, so no cursor or row outlives them. */
  private _resetList(): void {
    this._items = [];
    this._live = [];
    this._next = null;
    this._loadingMore = false;
    this._phase = "loading";
  }

  private async _loadMore(): Promise<void> {
    const before = this._next;
    if (before === null || this._loadingMore) {
      return;
    }
    const generation = this._generation;
    const request = this._requests;
    this._loadingMore = true;
    try {
      const page = await fetchLogins(this.hass, {
        ...loginFilter(this._filters),
        before_id: before,
      });
      if (generation === this._generation && request === this._requests) {
        this._items = [...this._items, ...page.items];
        this._next = page.next_before_id;
      }
    } catch (err) {
      if (generation === this._generation && request === this._requests) {
        this._fail(err);
      }
    } finally {
      // A stale request must not clear the flag of a newer one.
      if (generation === this._generation && request === this._requests) {
        this._loadingMore = false;
      }
    }
  }

  private _fields(): FilterField[] {
    const f = strings.filters;
    return [
      {
        name: "result",
        label: f.result,
        type: "select",
        options: [
          { value: "", label: f.anyResult },
          ...RESULTS.map((r) => ({ value: r, label: f.results[r] })),
        ],
      },
      {
        name: "user",
        label: f.user,
        type: "select",
        options: [
          { value: "", label: f.anyUser },
          ...this._users.map((u) => ({
            value: u.id,
            label: truncate(u.name ?? u.username ?? u.id, 64).short,
          })),
        ],
      },
      { name: "username", label: f.username, type: "text" },
      { name: "ip", label: f.ip, type: "text" },
      { name: "country", label: f.country, type: "text" },
      {
        name: "range",
        label: f.range,
        type: "select",
        options: TIME_RANGES.map((r) => ({ value: r, label: f.ranges[r] })),
      },
    ];
  }

  protected override render() {
    const items = [...this._live, ...this._items];
    return html`
      <h2 class="visually-hidden">${strings.activity.heading}</h2>
      ${this._banners()}
      <ef-filter-bar
        .fields=${this._fields()}
        .values=${this._filters}
        @filter-changed=${this._onFilter}
      ></ef-filter-bar>
      ${
        this._phase === "loading"
          ? html`<p class="loading">${strings.loading}</p>`
          : items.length === 0 && this._phase === "ready"
            ? html`<div class="card">
                <p class="empty">${strings.activity.empty}</p>
              </div>`
            : html`<div class="card">
                <ul>
                  ${items.map(
                    (item) =>
                      html`<li>
                        <ef-login-row
                          .hass=${this.hass}
                          .item=${item}
                          variant=${
                            item.result === "success" ? "success" : "failed"
                          }
                          detailed
                        ></ef-login-row>
                      </li>`,
                  )}
                </ul>
              </div>`
      }
      ${
        this._next !== null
          ? html`<div class="more">
              <button
                type="button"
                ?disabled=${this._loadingMore}
                @click=${() => void this._loadMore()}
              >
                ${strings.loadMore}
              </button>
            </div>`
          : nothing
      }
    `;
  }
}

customElements.define("ef-activity", EfActivity);

declare global {
  interface HTMLElementTagNameMap {
    "ef-activity": EfActivity;
  }
}
