# 公开发布检查

源码通过 Git 提交，APK、SDK ZIP、CLI 通过 GitHub Release 发布；不要用 Git LFS 或 `git add -f` 上传录像、原厂固件、私钥或编译缓存。

## 发布前

1. 核对本次改动、版本、测试结果与已知限制；保留用户工作区中无关内容。更新 `sdk/RELEASE_2026_09_13.md` 或新的日期报告，不把历史失败改成通过。
2. 按模块明确暂存，然后执行 `node scripts/check-publication.mjs`、`git diff --cached --check`。脚本读取 Git 暂存区，检查禁止路径、大文件和常见令牌/私钥，不代替人工审核。
3. 另外人工检查真实 Wi-Fi 密码、设备序列号、蓝牙地址、私人路径、日志和二进制资产；fixture 用虚构值，不能把真实凭据当测试常量。
4. 用干净检出或独立 SDK 解压目录运行构建/测试。明确哪些是主机、模拟器和真机结果。首次构建需要工具链与依赖，不能承诺没有缓存也能离线构建。

## 产物

App 构建见 [DEVELOPMENT](DEVELOPMENT.md)，保留已实测 APK，不为了改文件名重编译未验证版本。SDK 先完成对应平台构建，再运行：

```sh
bash sdk/scripts/package-mobile.sh artifacts/release-candidate
```

该脚本要求所有平台二进制存在。Android 控制/蓝牙、Apple 和 Rust 为 0.1.0；Android 媒体为 0.1.1。已发布 Maven 坐标不可偷偷替换同版本代码。只改文档的重打包需在 Release 注明，不能混淆为新 ABI。

Release 至少包含 App APK、开发者示例 APK、所交付 SDK 包、接入入口、已知限制和 `SHA256SUMS`。SHA-256 覆盖每个实际上传文件。不要上传测试 instrumentation APK、调试签名私钥、本机 `package.*` 工作区或录制数据。

使用 `gh release create --draft` 创建草稿，上传、检查附件后再公开发布。不要覆盖已发布附件或移动标签；应新建修订版本。发布前确认账号、仓库名称、公开性和准确 commit。

## 用户须知

- APK 是 Debug 开发体验包，不宣称商店签名、生产后台可靠性或所有手机兼容。
- SDK 示例 APK 与完整 App 是两个独立包名，不把示例界面当完整采集 App。
- SDK 包的媒体运行库较大且可选；只用控制不要引入媒体依赖。
- 保留第三方许可及来源，不分发原厂闭源固件、厂商工具链或没有明确来源的二进制。
- 总体项目许可尚待所有者选择；公开仓库本身不代表已选定 MIT/Apache 许可。
