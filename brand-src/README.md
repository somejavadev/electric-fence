# Brand sources

The SVG sources of the Electric Fence logo. `scripts/build_brand` renders them
into the eight PNGs in `custom_components/electric_fence/brand`, which Home
Assistant shows on the integrations page and in HACS. Rebuild after editing a
source, and commit the PNGs.

- `icon.svg` and `dark_icon.svg`: the fenced house on a 128 grid. They differ
  only in the fence colour (`#37474F` light, `#B0BEC5` dark).
- `sidebar-glyph.svg`: the one-colour sidebar icon, a single path on a 24 grid.
  `frontend/src/electric-fence-icons.ts` holds a copy of its path.
- The shapes are fills only, because ImageMagick's built-in SVG renderer draws
  strokes wrongly.

The wordmark uses `NotoSans-Bold.ttf`, Copyright Google Inc. and Google LLC,
licensed under the SIL Open Font License 1.1 (`OFL.txt`).
