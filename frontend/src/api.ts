import type {
  AttackItem,
  BanState,
  BansPage,
  ChangeFilter,
  ChangeItem,
  HomeAssistant,
  IntegrationsPage,
  LoginFilter,
  LoginItem,
  Overview,
  Page,
  Status,
  SubscribeEvent,
  UnsubscribeFunc,
  UserItem,
} from "./types.ts";

const call = <T>(
  hass: HomeAssistant,
  type: string,
  params: object = {},
): Promise<T> =>
  hass.connection.sendMessagePromise<T>({
    ...params,
    // After the spread, so a filter key can never change the command.
    type: `electric_fence/${type}`,
  });

export const fetchOverview = (hass: HomeAssistant): Promise<Overview> =>
  call(hass, "overview");

export const fetchLogins = (
  hass: HomeAssistant,
  filter: LoginFilter = {},
): Promise<Page<LoginItem>> => call(hass, "logins", filter);

export const fetchChanges = (
  hass: HomeAssistant,
  filter: ChangeFilter = {},
): Promise<Page<ChangeItem>> => call(hass, "changes", filter);

export const fetchUsers = (
  hass: HomeAssistant,
): Promise<{ items: UserItem[] }> => call(hass, "users");

/** Active bans (all of them), or a page of ended ones. */
export const fetchBans = (
  hass: HomeAssistant,
  state: BanState,
  beforeId?: number,
): Promise<BansPage> =>
  call(
    hass,
    "bans",
    beforeId === undefined ? { state } : { state, before_id: beforeId },
  );

export const unban = (
  hass: HomeAssistant,
  ip: string,
): Promise<{ unbanned: boolean }> => call(hass, "unban", { ip });

export const fetchAttacks = (
  hass: HomeAssistant,
  state: BanState,
): Promise<{ items: AttackItem[] }> => call(hass, "attacks", { state });

export const endSlowdown = (
  hass: HomeAssistant,
  username: string,
): Promise<{ ended: boolean }> => call(hass, "end_slowdown", { username });

/** Forget every user's known login countries. */
export const clearCountries = (
  hass: HomeAssistant,
): Promise<{ cleared: number }> => call(hass, "clear_countries");

export const fetchStatus = (hass: HomeAssistant): Promise<Status> =>
  call(hass, "status");

export const fetchIntegrations = (
  hass: HomeAssistant,
): Promise<IntegrationsPage> => call(hass, "integrations");

/** `started` is false when a scan already ran in the last 5 minutes. */
export const scanIntegrations = (
  hass: HomeAssistant,
): Promise<{ started: boolean }> => call(hass, "scan_integrations");

/** Live logins and changes. An `unloaded` event ends the subscription. */
export const subscribe = (
  hass: HomeAssistant,
  callback: (event: SubscribeEvent) => void,
): Promise<UnsubscribeFunc> =>
  hass.connection.subscribeMessage<SubscribeEvent>(callback, {
    type: "electric_fence/subscribe",
  });
