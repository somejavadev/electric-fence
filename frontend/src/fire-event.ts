/** Fire a Home Assistant style event: `detail` rides along, nothing else. */
export function fireEvent<T>(
  target: EventTarget,
  type: string,
  detail?: T,
): void {
  target.dispatchEvent(
    new CustomEvent(type, { detail, bubbles: true, composed: true }),
  );
}

/** Go to a path inside Home Assistant, as its own router expects. */
export function navigate(path: string, replace = false): void {
  if (replace) {
    history.replaceState(null, "", path);
  } else {
    history.pushState(null, "", path);
  }
  fireEvent(window, "location-changed", { replace });
}
