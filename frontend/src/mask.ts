/**
 * The "mask IP addresses" option. Only what the panel shows is masked: the
 * server still stores and returns full addresses, and actions such as unban
 * keep using them.
 */

import { visible } from "./format.ts";

/** Stands in for a host name, or for an address that cannot be parsed. */
export const HIDDEN = "hidden";

let masking = false;

/** Set from the integration's options before any tab renders. */
export function setMaskIps(on: boolean): void {
  masking = on;
}

export const maskingIps = (): boolean => masking;

const IPV4 = /^(\d{1,3})(?:\.\d{1,3}){3}$/;

/** The eight groups of an IPv6 address, or null when it is not one. */
function ipv6Groups(ip: string): string[] | null {
  const halves = ip.split("::");
  if (halves.length > 2) {
    return null;
  }
  const [first = "", second = ""] = halves;
  const head = first === "" ? [] : first.split(":");
  const tail = second === "" ? [] : second.split(":");
  const missing = 8 - head.length - tail.length;
  if (halves.length === 1 ? missing !== 0 : missing < 1) {
    return null;
  }
  const groups = [...head, ...new Array<string>(missing).fill("0"), ...tail];
  return groups.every((g) => /^[\da-f]{1,4}$/i.test(g)) ? groups : null;
}

/**
 * IPv4 keeps its first octet (203.x.x.x); IPv6 keeps its first two groups
 * (2001:db8:x:x:x:x:x:x). An IPv4-mapped IPv6 address is masked as IPv4.
 * Anything else becomes "hidden".
 */
export function maskIp(ip: string): string {
  const [bare = ""] = ip.split("%");
  const v4 = IPV4.exec(bare);
  if (v4 !== null) {
    return `${v4[1] ?? ""}.x.x.x`;
  }
  const mapped = /^::ffff:(\d.*)$/i.exec(bare)?.[1];
  if (mapped !== undefined && IPV4.test(mapped)) {
    return maskIp(mapped);
  }
  const groups = ipv6Groups(bare);
  if (groups === null) {
    return HIDDEN;
  }
  return [...groups.slice(0, 2), ...new Array<string>(6).fill("x")].join(":");
}

/** An address as the panel shows it. */
export const showIp = (ip: string): string =>
  masking ? maskIp(ip) : visible(ip);

/** A reverse DNS host name as the panel shows it: often it spells the IP. */
export const showHost = (host: string): string =>
  masking ? HIDDEN : visible(host);

/** Client IDs of the official apps name no one's server, so they stay. */
const PUBLIC_CLIENT_HOST = /(^|\.)home-assistant\.io$/i;

/**
 * A login's client ID as the panel shows it. A browser login's client ID is
 * the URL of the Home Assistant it used, so its host and path are hidden (a
 * reverse proxy's path can name the server too): https://ha.example.com/x/
 * shows as https://hidden/.
 */
export function showClient(clientId: string): string {
  if (!masking) {
    return visible(clientId);
  }
  let url: URL;
  try {
    url = new URL(clientId);
  } catch {
    return HIDDEN;
  }
  if (PUBLIC_CLIENT_HOST.test(url.hostname)) {
    return visible(clientId);
  }
  return `${url.protocol}//${HIDDEN}/`;
}
