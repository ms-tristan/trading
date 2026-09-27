import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Guards the file-based metadata assets of the App Router.
 *
 * `icon.svg`, `favicon.ico` and `apple-icon.png` are RESERVED file names of the
 * `app` directory: Next.js 16 emits their `<link>` tags from the files
 * themselves, so `layout.tsx` declares no `icons` entry. These assertions are a
 * pure filesystem check -- no network, no `fetch`, no `next build` -- and every
 * path is resolved from `import.meta.url`, so the suite is cwd-independent.
 */
const APP_DIR = resolveAppDir();

/**
 * The directory of this test file, resolved from `import.meta.url` so the suite
 * is cwd-independent. The URL is rebuilt from its `href` before conversion:
 * newer Node versions ship more than one `URL` class, and `fileURLToPath`
 * rejects an instance that does not belong to its own realm.
 */
function resolveAppDir(): string {
  try {
    return fileURLToPath(new URL(".", import.meta.url));
  } catch {
    return __dirname;
  }
}

const ICON_SVG = path.join(APP_DIR, "icon.svg");
const FAVICON_ICO = path.join(APP_DIR, "favicon.ico");
const APPLE_ICON_PNG = path.join(APP_DIR, "apple-icon.png");

/** Fails with the resolved directory when an asset is missing from the tree. */
function assertAssetExists(filePath: string): void {
  if (!existsSync(filePath)) {
    throw new Error(`brand icon asset not found: ${filePath} (resolved app dir: ${APP_DIR})`);
  }
}

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

function readAsset(filePath: string): Buffer {
  assertAssetExists(filePath);
  return readFileSync(filePath);
}

/** Collects the `{ width, height }` pairs of every 16-byte ICONDIRENTRY. */
function readIcoSizes(ico: Buffer): { width: number; height: number }[] {
  const entryCount = ico.readUInt16LE(4);
  const sizes: { width: number; height: number }[] = [];
  for (let index = 0; index < entryCount; index += 1) {
    const entry = 6 + index * 16;
    const width = ico[entry] === 0 ? 256 : ico[entry]; // a stored 0 encodes 256
    const height = ico[entry + 1] === 0 ? 256 : ico[entry + 1];
    sizes.push({ width, height });
  }
  return sizes;
}

/** Walks the PNG chunk list and returns the four-character chunk types. */
function readPngChunks(png: Buffer): string[] {
  const types: string[] = [];
  let offset = PNG_SIGNATURE.length;
  while (offset + 8 <= png.length) {
    const length = png.readUInt32BE(offset);
    types.push(png.subarray(offset + 4, offset + 8).toString("latin1"));
    offset += 12 + length; // length + type + data + CRC
  }
  return types;
}

describe("brand icon assets", () => {
  it("ships the three file-based metadata assets as non-empty files", () => {
    for (const filePath of [ICON_SVG, FAVICON_ICO, APPLE_ICON_PNG]) {
      assertAssetExists(filePath);
      const stats = statSync(filePath);
      expect(stats.isFile(), `${filePath} is a regular file`).toBe(true);
      expect(stats.size, `${filePath} is not empty`).toBeGreaterThan(0);
    }
  });

  it("keeps icon.svg a self-contained 32x32 shape-only drawing", () => {
    const source = readAsset(ICON_SVG).toString("utf8");
    const document = new DOMParser().parseFromString(source, "image/svg+xml");

    expect(document.querySelector("parsererror")).toBeNull();
    const root = document.documentElement;
    expect(root.tagName.toLowerCase()).toBe("svg");
    expect(root.getAttribute("viewBox")).toBe("0 0 32 32");

    /*
     * The meaning must be carried by the shape alone: a font, a bitmap or an
     * external reference would need a renderer that a 16x16 tab cannot rely on.
     */
    expect(source).not.toContain("<text");
    expect(source).not.toContain("<image");
  });

  it("ships favicon.ico as a real multi-resolution icon", () => {
    const ico = readAsset(FAVICON_ICO);

    expect(ico[0], "reserved byte 0").toBe(0x00);
    expect(ico[1], "reserved byte 1").toBe(0x00);
    expect(ico.readUInt16LE(2), "little-endian icon type").toBe(1);
    const entryCount = ico.readUInt16LE(4);
    expect(entryCount, "directory entry count").toBeGreaterThanOrEqual(3);

    const sizes = readIcoSizes(ico);
    const rendered = sizes.map((size) => `${size.width}x${size.height}`).join(", ");
    expect(sizes.length, `directory entries: ${rendered}`).toBeGreaterThanOrEqual(3);
    expect(sizes, `declared sizes: ${rendered}`).toContainEqual({ width: 16, height: 16 });
    expect(sizes, `declared sizes: ${rendered}`).toContainEqual({ width: 32, height: 32 });
  });

  it("ships apple-icon.png as an opaque 180x180 truecolour image", () => {
    const png = readAsset(APPLE_ICON_PNG);

    expect([...png.subarray(0, 8)], "PNG signature").toEqual(PNG_SIGNATURE);
    const chunks = readPngChunks(png);
    expect(chunks[0], `chunk list: ${chunks.join(", ")}`).toBe("IHDR");

    const width = png.readUInt32BE(16);
    const height = png.readUInt32BE(20);
    const colourType = png[25];
    expect([width, height], "IHDR dimensions").toEqual([180, 180]);
    expect(colourType, "IHDR colour type (2 = truecolour, no alpha)").toBe(2);
    expect(chunks, `chunk list: ${chunks.join(", ")}`).not.toContain("tRNS");
  });
});
