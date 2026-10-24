import type {
  ChangeFilter,
  ChangeItem,
  HomeAssistant,
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

export const fetchStatus = (hass: HomeAssistant): Promise<Status> =>
  call(hass, "status");

/** Live logins and changes. An `unloaded` event ends the subscription. */
export const subscribe = (
  hass: HomeAssistant,
  callback: (event: SubscribeEvent) => void,
): Promise<UnsubscribeFunc> =>
  hass.connection.subscribeMessage<SubscribeEvent>(callback, {
    type: "electric_fence/subscribe",
  });
