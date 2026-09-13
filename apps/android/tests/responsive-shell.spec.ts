import { expect, test } from "@playwright/test";

test("one app shell switches between wide desktop and compact mobile layouts", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?shell=desktop");

  await expect(page.locator("html")).toHaveAttribute("data-desktop-shell", "true");
  const wideNav = await page.locator(".bottom-navigation").boundingBox();
  const wideScroll = await page.locator(".syncap-scroll").boundingBox();
  expect(wideNav?.width).toBeCloseTo(214, 0);
  expect(wideScroll?.x).toBeCloseTo(214, 0);

  const wideTiles = await page.locator(".camera-tile").evaluateAll((tiles) =>
    tiles.map((tile) => ({ x: tile.getBoundingClientRect().x, y: tile.getBoundingClientRect().y })),
  );
  expect(wideTiles).toHaveLength(4);
  expect(new Set(wideTiles.map((tile) => Math.round(tile.y))).size).toBe(1);

  await page.setViewportSize({ width: 700, height: 1000 });
  const compactNav = await page.locator(".bottom-navigation").boundingBox();
  const compactTiles = await page.locator(".camera-tile").evaluateAll((tiles) =>
    tiles.map((tile) => ({ x: tile.getBoundingClientRect().x, y: tile.getBoundingClientRect().y })),
  );
  expect(compactNav?.width).toBeCloseTo(700, 0);
  expect(compactNav?.y).toBeGreaterThan(900);
  expect(new Set(compactTiles.map((tile) => Math.round(tile.y))).size).toBe(2);
  await expect(page.locator(".camera-tile img")).toHaveCount(0);
});
