// Registers the Electric Fence icon set, so the sidebar can show
// `electric-fence:logo`. Loaded on every page, so it only draws: no network,
// no storage, and no DOM writes beyond asking icons to reload.

export const ICON_SET = "electric-fence";

// Copy of the path in brand-src/sidebar-glyph.svg (a test keeps them equal).
export const GLYPH_PATH =
  "M11.48 2.67Q12.00 2.20 12.52 2.67L20.88 10.13Q21.40 10.60 20.70 10.60L19.80 10.60Q19.10 10.60 19.10 11.30L19.10 12.90Q19.10 13.60 18.40 13.60L5.60 13.60Q4.90 13.60 4.90 12.90L4.90 11.30Q4.90 10.60 4.20 10.60L3.30 10.60Q2.60 10.60 3.12 10.13ZM1.60 14.00Q1.60 13.10 2.50 13.10L2.50 13.10Q3.40 13.10 3.40 14.00L3.40 22.00Q3.40 22.90 2.50 22.90L2.50 22.90Q1.60 22.90 1.60 22.00ZM20.60 14.00Q20.60 13.10 21.50 13.10L21.50 13.10Q22.40 13.10 22.40 14.00L22.40 22.00Q22.40 22.90 21.50 22.90L21.50 22.90Q20.60 22.90 20.60 22.00ZM0.90 16.50Q0.90 15.90 1.50 15.90L9.00 15.90Q9.60 15.90 9.60 16.50L9.60 16.50Q9.60 17.10 9.00 17.10L1.50 17.10Q0.90 17.10 0.90 16.50ZM14.40 16.50Q14.40 15.90 15.00 15.90L22.50 15.90Q23.10 15.90 23.10 16.50L23.10 16.50Q23.10 17.10 22.50 17.10L15.00 17.10Q14.40 17.10 14.40 16.50ZM0.90 20.00Q0.90 19.40 1.50 19.40L9.50 19.40Q10.10 19.40 10.10 20.00L10.10 20.00Q10.10 20.60 9.50 20.60L1.50 20.60Q0.90 20.60 0.90 20.00ZM14.40 20.00Q14.40 19.40 15.00 19.40L22.50 19.40Q23.10 19.40 23.10 20.00L23.10 20.00Q23.10 20.60 22.50 20.60L15.00 20.60Q14.40 20.60 14.40 20.00ZM13.14 13.79Q13.20 13.50 13.03 13.75L10.37 17.75Q10.20 18.00 10.50 18.00L11.70 18.00Q12.00 18.00 11.93 18.29L11.07 22.21Q11.00 22.50 11.16 22.24L14.04 17.46Q14.20 17.20 13.90 17.20L12.70 17.20Q12.40 17.20 12.46 16.91Z";

type CustomIcon = { path: string; viewBox: string };
type IconSet = { getIcon: (name: string) => Promise<CustomIcon | undefined> };
type IconsWindow = Window & { customIcons?: Record<string, IconSet> };
type HaIcon = Element & {
  icon?: unknown;
  _legacy?: boolean;
  requestUpdate?: (name: string, oldValue: unknown) => void;
};

const ICONS = new Map<string, CustomIcon>([
  ["logo", { path: GLYPH_PATH, viewBox: "0 0 24 24" }],
]);

export function getIcon(name: string): Promise<CustomIcon | undefined> {
  return Promise.resolve(ICONS.get(name));
}

// An ha-icon that looked the set up before it existed caches the miss (it sets
// `_legacy` and never retries), so reset that and make it look again.
export function refreshRenderedIcons(
  root: Document | ShadowRoot = document,
): number {
  let refreshed = 0;
  for (const el of root.querySelectorAll<HaIcon>("*")) {
    if (el.shadowRoot) {
      refreshed += refreshRenderedIcons(el.shadowRoot);
    }
    if (
      el.localName !== "ha-icon" ||
      typeof el.icon !== "string" ||
      !el.icon.startsWith(`${ICON_SET}:`)
    ) {
      continue;
    }
    if ("_legacy" in el) {
      el._legacy = false;
    }
    if (typeof el.requestUpdate === "function") {
      el.requestUpdate("icon", undefined);
    }
    refreshed += 1;
  }
  return refreshed;
}

const w = window as IconsWindow;
w.customIcons ??= {};
w.customIcons[ICON_SET] = { getIcon };
refreshRenderedIcons();
