import { html } from "lit";
import { MAX_TEXT, truncate } from "./format.ts";

/** Text, isolated and truncated, with the full value in the tooltip. */
export const bdiText = (value: string, max = MAX_TEXT) => {
  const t = truncate(value, max);
  return html`<bdi title=${t.full}>${t.short}</bdi>`;
};

/** A sentence from strings.ts with `value` (as text) where `{}` stands. */
export function fill(sentence: string, value: string) {
  const at = sentence.indexOf("{}");
  if (at < 0) {
    return sentence;
  }
  return html`${sentence.slice(0, at)}${bdiText(value)}${sentence.slice(at + 2)}`;
}
