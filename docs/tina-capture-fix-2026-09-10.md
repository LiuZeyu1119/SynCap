# Tina 采集计时与 0 字节文件修复

## 范围与环境

- 设备：Allwinner Tina，通过 USB ADB 调试（公开记录省略设备序列号）。
- SD：`/dev/mmcblk0p1`，FAT32，挂载于 `/rom/mnt/SDCARD`，测试前可用约 2.1 GiB。
- 未连接手机。设备测试通过 ADB 转发调用与 App 相同的 HTTP 接口；前端通过自动化验证，不等同于手机真机验收。
- 未修改编码、分辨率、帧率、预览源或采集完整性阈值，未格式化或删除用户数据。

## 根因与修复

### 计时跨时钟域

设备日历为 1970 年，`deviceTimeNs` 和采集开始纳秒为开机后的单调时间。旧 UI 用手机 `Date.now()` 减设备 `startedAt`，测试复现 `496850:30:31`。

服务现在返回单调时长 `capture.elapsedMs` 和起点 `startedAtDeviceTimeNs`；App 优先采用设备时长，再以同域纳秒差兼容，使用本地 `performance.now()` 补时。轮询、重连和开始响应丢失不再用跨设备日历差计时。

### FAT32 改名后文件元数据没有立即落盘

旧流程为临时文件写入 → 文件 fsync → close → rename → 父目录 fsync。FAT rename 新建目录项并迁移文件 inode，子文件 inode 的 size/start cluster 仍需同步；只同步父目录不足。

实测对照（没有执行 sync 后再读盘）：

| 版本 / 会话 | 停止返回 | 停止后 O_DIRECT 检查 | 磁盘目录结果 |
|---|---|---|---|
| 旧版 `ses_19700105_051119_e71004` | completed，5,200,200 字节 | 约 231 ms | 全部文件 size=0、start_cluster=0 |
| 修复版 `ses_19700105_051341_3113d3` | completed，5,435,863 字节 | 约 236 ms | MCAP、侧车、session/export 均为正确非零大小与簇号 |

修复为临时文件 fsync → rename → 对同一未关闭文件 fd 再次 fsync → 父目录 fsync → close。只有持久化成功才回收 tmpfs 原片段。保存清单采用相同顺序，提交失败不返回采集成功。

依据与实测一致的 Linux 实现：[FAT rename](https://raw.githubusercontent.com/torvalds/linux/v5.4/fs/fat/namei_vfat.c)、[FAT fsync](https://raw.githubusercontent.com/torvalds/linux/v5.4/fs/fat/file.c)、[generic fsync](https://raw.githubusercontent.com/torvalds/linux/v5.4/fs/libfs.c)。本次有直接读卡验证，没有进行强制断电破坏测试。

### 损坏会话被普通空数据处理

空 session.json 原先返回空名称、0 B、空状态；空导出清单也返回 HTTP 200。现在损坏会话保留列表项，标记失败并提供 `failureReason`、`exportAvailable:false`，App 显示“清单损坏”或“文件损坏”，禁用导出。有效的失败会话残片仍可导出。

原用户会话 `ses_19700116_202251_01e182` 的 12 个文件确实全部为 0，O_DIRECT 确认文件目录 size/start cluster 都为零。原文件未改动；本次修复不会自动恢复其丢失内容。

## 验证结果

重启后连续三次采集，一次开始和一次停止均成功：

| 会话后缀 | 服务时长 | 数据字节 | 左 / 右帧数 | IMU 条数 | 音频消息 |
|---|---:|---:|---:|---:|---:|
| `051646_9f4bc3` | 2,946 ms | 2,686,384 | 60 / 60 | 1,619 | 31 |
| `051650_74afc7` | 8,939 ms | 11,009,369 | 240 / 240 | 6,504 | 128 |
| `051702_b95d05` | 31,765 ms | 41,832,598 | 900 / 900 | 24,391 | 478 |

- 每组停止后约 241–346 ms，直接读 FAT32 目录确认文件非零且大小符合清单。
- 所有 MCAP 通过非帧对齐 HTTP Range 分段下载，拼接后尺寸与 SHA-256 全部匹配，使用 MCAP reader 遍历全部消息并确认双目、IMU、音频存在。
- `ses_19700105_051341_3113d3` 重启前后全部文件逐字节一致；服务自动启动为 0.2.1。
- 设备协议 / 会话完整性 42 项、持久化顺序和错误注入 6 项、前端计时单测 11 项、采集 / Tina UI 22 项通过。
- TypeScript、28 个保护运行时文件校验、Android APK 构建通过。
- 最后补充落盘失败清理时保留原始 errno，重新构建、部署，并核对设备二进制一致。最终二进制再次实测 3,095 ms / 2,588,160 字节，左右目各 60 帧、IMU 1,624 条；停止后 302 ms 直接读盘、Range 下载、SHA-256 均通过。最终设备测试 48 项再次全部通过。

验证不代表原厂 qgapp 的长时间稳定性问题已全部解决，也未验证手机实际安装、网络或蓝牙操作。

## 交付

- APK：`artifacts/2026-09-10-capture-fix/SynCap-Android-capture-fix-20260910.apk`。
- APK SHA-256：`ecef79a22826a5be91b6d104cc52fb08efeb73f430a716a45c0db7e269833528`。
- 服务 0.2.1 已部署：`/opt/syncap/syncap-tina-service`。
- 服务 SHA-256：`8c725dd12a0350ad4ad6080e9689fe27d893adf12005caf28591b15c3b92e0c9`。
- 旧服务备份：`artifacts/2026-09-10-capture-fix/tina-service-before`。
- 临时调试材料：`/tmp/syncap-capture-fix.yRMK0X`，FAT 只读脚本 `/tmp/syncap-fat-readonly.py`。临时路径可能被系统清理；测试采集仍留在 SD 的 `SynCap` 目录并以 `QA` 命名。

收尾时采集为 idle，服务和原厂生产者运行，未留下正在采集的任务。
