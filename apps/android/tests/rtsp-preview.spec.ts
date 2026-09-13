import { expect, test } from "@playwright/test";

test("desktop RTSP page accepts public and HiSi stream presets", async ({ page }) => {
  await page.goto("/?shell=desktop");

  await expect(page.getByTestId("nav-rtsp")).toBeVisible();
  await page.getByTestId("nav-rtsp").click();
  await expect(page.getByTestId("rtsp-preview-page")).toContainText("RTSP 预览");

  await page.getByTestId("rtsp-preset-public").click();
  await expect(page.getByTestId("rtsp-url-0")).toHaveValue(/^rtsp:\/\/.+wowza\.com:/);
  await expect(page.getByTestId("rtsp-url-1")).toHaveValue("");

  await page.getByTestId("rtsp-preset-hisi").click();
  await expect(page.getByTestId("rtsp-url-0")).toHaveValue("rtsp://192.168.100.100:8554/cam0");
  await expect(page.getByTestId("rtsp-url-3")).toHaveValue("rtsp://192.168.100.100:8554/cam3");

  await page.getByTestId("rtsp-url-0").fill("https://example.com/not-rtsp");
  await page.getByTestId("rtsp-url-1").fill("");
  await page.getByTestId("rtsp-url-2").fill("");
  await page.getByTestId("rtsp-url-3").fill("");
  await page.getByTestId("rtsp-connect").click();
  await expect(page.getByRole("status")).toContainText("rtsp://");
});

test("RTSP page stays out of the mobile navigation", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("nav-rtsp")).toHaveCount(0);
});
