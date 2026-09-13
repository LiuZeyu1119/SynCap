import { expect, test } from "@playwright/test";

test("disconnected devices never render demo frames or an online state", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByTestId("capture-button")).toBeDisabled();
  await expect(page.getByTestId("capture-button")).toContainText("设备未连接");
  await expect(page.locator(".camera-tile-state", { hasText: "未连接" })).toHaveCount(4);
  await expect(page.locator(".camera-tile img")).toHaveCount(0);
  await expect(page.locator(".metric-strip")).toContainText("相机--设备未连接");
  await expect(page.getByText("全部在线")).toHaveCount(0);
});

test("device and data pages contain only observed state", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("nav-device").click();

  await expect(page.getByText("连接成功后显示相机、存储和传感器状态")).toBeVisible();
  await expect(page.getByRole("button", { name: /相机位置标定/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /相机位置标定/ })).toBeDisabled();
  await expect(page.getByRole("button", { name: /图像方向/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /同步状态/ })).toBeVisible();
  await expect(page.getByText("Lab-5G")).toHaveCount(0);
  await expect(page.getByText("SynCap HeadRing 02")).toHaveCount(0);
  await expect(page.getByText("工厂标定有效")).toHaveCount(0);

  await page.getByTestId("nav-data").click();
  await expect(page.getByText("还没有采集数据")).toBeVisible();
  await expect(page.locator(".session-list article")).toHaveCount(0);
});
