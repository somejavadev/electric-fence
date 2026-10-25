import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";

const MAX_BYTES = 2 * 1024 * 1024;
const dir = fileURLToPath(
  new URL(
    "../../custom_components/electric_fence/frontend_dist/flags/",
    import.meta.url,
  ),
);

if (!existsSync(`${dir}nl.svg`)) {
  console.error(`Missing ${dir}nl.svg; did the build copy the flags?`);
  process.exit(1);
}

const panel = readFileSync(`${dir}../electric-fence-panel.js`, "utf8");
if (/data:image\/svg|flags\/[a-z-]+\.svg/.test(panel)) {
  console.error("The panel bundle inlines or hard-codes a flag asset.");
  process.exit(1);
}

let total = 0;
for (const name of readdirSync(dir)) {
  if (name !== "LICENSE" && !/^[a-z]{2}\.svg$/.test(name)) {
    console.error(`Unexpected file in flags: ${name}`);
    process.exit(1);
  }
  total += statSync(`${dir}${name}`).size;
}
if (total >= MAX_BYTES) {
  console.error(`Flags take ${total} bytes; the limit is ${MAX_BYTES}.`);
  process.exit(1);
}
console.log(`Flags OK: ${total} bytes`);
