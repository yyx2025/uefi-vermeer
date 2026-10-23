# 红米 K70（vermeer / SM8550）中文 UEFI 研究镜像

> 一个可以**临时启动**的中文 UEFI 界面，用 `fastboot boot` 直接加载，**不改动任何分区**。
> 重启即回到原生安卓；如需彻底回退，见文末「恢复原系统」。

---

## 1. 这是什么

- **平台**：红米 K70 / K70 Pro 系列（代号 `vermeer`，SoC 骁龙 8 Gen 2 / SM8550）
- **形态**：`boot.img`（Android boot image，header v0，BootShim + UEFI 固件 + 设备树）
- **界面**：简体中文启动菜单，16×16 点阵字模（GNU Unifont 子集，175 个汉字）
- **启动方式**：`fastboot boot` —— **只加载到内存运行，不写 flash**

镜像内部结构：

```
boot.img
├─ kernel = gzip( BootShim.bin(192B) + SILICIUM_UEFI.fd(3MiB) ) + vermeer-splash.dtb
│                                        └─ FVMAIN_COMPACT ── LZMA ──> FVMAIN(55 个模块)
│                                                                     ├─ 中文界面应用 (31 KB)
│                                                                     └─ 中文界面注册器 (26 KB)
└─ ramdisk = 空
```

---

## 2. 前置条件

| 项目 | 要求 |
|---|---|
| Bootloader | **必须已解锁**（`fastboot oem device-info` 显示 `unlocked: true`） |
| 平台工具 | `fastboot`（Android platform-tools），能识别设备为 `fastboot` 模式 |
| 电量 | 建议 30% 以上 |
| 风险认知 | 这是**实验性研究固件**，不是成品系统；GPU/触摸/音频等不具备 |

---

## 3. 刷入（临时启动，不写分区）

```bash
# 1) 关机，按住「音量下 + 电源」进 fastboot 模式
# 2) 确认设备被识别
fastboot devices

# 3) 临时启动（关键：boot 子命令，不是 flash）
fastboot boot vermeer-zh-boot.img
```

设备会立即重启并加载镜像。屏幕上应出现中文菜单：

```
红米 K70 中文启动器
维米尔 UEFI 研究固件
────────────────
  继续启动        ← 默认，10 秒倒计时
  重新启动
  关闭电源
按音量键选择 按电源键确认
此固件不会修改任何分区
```

按键操作：

| 操作 | 按键 |
|---|---|
| 上移 / 下移 | 音量上 / 音量下 |
| 确认 | 电源键 |
| 直接选第 N 项 | 数字键 1–3（有 USB 键盘时） |

> **为什么用 `boot` 而不是 `flash`**：`fastboot boot` 把镜像放进内存由 bootloader 直接执行，
> boot 分区上的原系统镜像**一个字节都不会被改写**。因此这一步不需要备份，也不需要刷回。

---

## 4. 退出与恢复

### 4.1 回到原生安卓（最常见）

任选其一：

1. 菜单里选 **「重新启动」**（冷复位），或
2. 长按电源键约 10 秒强制关机，再正常开机。

因为固件从未落盘，**重启后就是原来的 ColorOS / HyperOS**。

### 4.2 如果你额外做了 `fastboot flash boot`

本说明的流程**不需要**这一步。但若你之前手动刷过 boot 分区（例如早期版本），
用原厂 boot 镜像刷回即可：

```bash
# 原厂 boot 镜像可从官方 OTA 包中取：images/boot.img（或 boot_a.img / boot_b.img）
fastboot flash boot_a boot_a.img      # A/B 设备按当前 slot 选择
fastboot flash boot_b boot_b.img      # 或两个槽都刷
fastboot reboot
```

也可以走官方恢复：用 ColorOS/HyperOS 官方完整包（`.zip` 内含 `images/`）执行
`fastboot flashall` 或官方刷机工具，能恢复全部分区到出厂状态。

> **注意**：本项目的镜像本身**从不写 flash**，所以「通过 fastboot 重新刷原 boot 回原系统」
> 在标准流程里是**不需要**的动作。保留原厂 boot 镜像仍是个好习惯，用于应对误操作。

---

## 5. 已知限制（请先读完）

这是**研究性首启固件**，以下能力**不具备**：

| 能力 | 状态 | 原因 |
|---|---|---|
| 中文菜单显示 | ✅ 已实现 | SimpleFbDxe 在 ABL 留下的帧缓冲上出画 |
| 音量/电源键导航 | ✅ 已实现 | `VermeerKeyInputDxe` 通过 SPMI 读 PMIC 按键 |
| 重启 / 关机 | ✅ 已实现 | 标准 `ResetSystem` |
| 触摸 | ❌ | 需 Novatek 触摸固件与 I²C 通路 |
| GPU / 显示加速 | ❌ | 无 Adreno 驱动；仅线性帧缓冲 |
| 音频 / 蜂窝 / Wi-Fi / 蓝牙 | ❌ | 无对应驱动 |
| 启动到安卓或其它 OS | ❌ | 这是引导器 + 诊断界面，不是系统 |

菜单里的「继续启动」只是**交还控制权给固件 BDS**，不代表能进入可用系统。

---

## 6. 它是怎么工作的（技术说明）

### 6.1 为什么能绕开 iBoot

iOS 的引导链（BootROM → LLB → iBoot）要求 Apple 按设备 ECID 签发的 **IM4M/APTicket** 个人化，
离开 Apple 服务器无法完成。K70 的方案是**完全不碰那条链**：用 bootloader 自己的
`fastboot boot` 路径加载自建 UEFI 固件，由 BootShim 把 FD 搬到 `0xA7100000` 再跳入 SEC。

### 6.2 中文界面的执行时机（一个真实踩过的坑）

`BdsDxe` 的 DEPEX 是 `TRUE`，DXE 调度器**不保证**任何"也依赖 GOP"的驱动在 BDS 之前执行。
如果只写一个依赖 `gEfiGraphicsOutputProtocolGuid` 的 DXE 驱动，界面可能永不显示或与 BDS 抢屏。

因此采用**应用 + 注册器**（复用 `UniXNUDxe` 在同平台验证过的做法）：

```
VermeerChineseUiDxe (DXE 驱动, DEPEX 等 Variable)
        │  写 Boot####（设备路径指向本 FV 的应用文件）
        │  并把该项排到 BootOrder 首位
        ▼
BDS 启动 VermeerChineseUi (UEFI_APPLICATION)
        │  画中文菜单、等按键、超时或选择后返回
        ▼
返回 EFI_SUCCESS → BDS 继续原有引导顺序
```

### 6.3 splash_region 为什么必须存在

原厂 ABL 在交接前点亮面板并持续扫描输出帧缓冲；如果设备树里没有
`/reserved-memory/splash_region` 节点，**ABL 会把面板断电**，屏幕全黑。

`vermeer.dtb` 原本缺少该节点，本项目用纯 Python FDT 工具补上：

```
/reserved-memory/splash_region
    label = "cont_splash_region"
    reg   = <0 0xb8000000 0 0x2b00000>
```

（地址与 K70 原厂 `uefiplat.cfg` 的 `Display Reserved` 一致。）

### 6.4 字模从哪里来

GNU Unifont 15.1.05（**GNU Unifont License v2，含字体嵌入例外**，可合法编入固件）。
只收录界面真正显示的 175 个汉字 + 95 个 ASCII，固件体积增加约 30 KB，而不是整个 57k 字形。

---

## 7. 自验证

镜像出厂前做了完整自校验，可随时复现：

```bash
# 从 boot.img 反解，确认 FD 里确有中文界面两个模块 + DTB 有 splash_region
python tools/ipsw/verify_boot_image.py vermeer-zh-boot.img \
    --expect-sha256 a49d0f97888aad002099300be92b982b603e14258eef373b3b9661d28bc1febc
```

预期输出包含：

```
firmware   3,145,728 bytes
dtb        510,680 bytes, splash_region present
中文界面应用   : present
中文界面注册器 : present
```

打包链的每一步都做了断言，不是"看起来对"：

| 检查项 | 内容 |
|---|---|
| BootShim ARM64 头 | `text_offset == 0xA7100000`，`image_size == 192 + 0x300000` |
| `_Payload` 位置 | 必须等于 shim 长度（否则拷贝循环会错位） |
| gzip 往返 | 解压结果必须逐字节等于 `shim + FD` |
| 附加 DTB | 解压后的 `unused_data` 必须逐字节等于输入 DTB |
| boot.img 头 | `kernel_addr = 0x8000`、`tags = 0x1e00000`、`page = 4096`、`v0`、ramdisk = 0 |
| FD 体积 | 必须精确等于 `0x300000` |

---

## 8. 构建（想自己改的话）

固件需要在 Linux/macOS 上用 EDK II 工具链编译（Windows 本机没有 gmake/VS 构建环境），
当前通过 GitHub Actions 完成：

```
仓库  yyx2025/uefi-vermeer         分支 vermeer   ← 平台包 vermeerPkg
仓库  yyx2025/UniXNU-vermeer       分支 main      ← 板级配置 + CI
```

改动界面文字后必须同步重新生成字库，否则缺字会渲染成方框：

```bash
python tools/gen-cjk-font.py --hex unifont-15.1.05.hex.gz \
    --extra-file cjk-charset.txt \
    --chars "0123456789:.-/%()[]<>" \
    --out Platforms/Xiaomi/vermeerPkg/Drivers/VermeerChineseUi/VermeerCjkFontData.c
```

编译开关（`vermeer.dsc`）：

```
VERMEER_CHINESE_UI = 1     # 1 = 编入中文界面；0 = 排除（排查 GOP 问题时用）
VERMEER_VENDOR_HW  = 0     # 0 = 只用 Mu/Silicium 驱动（首启路径）
```

---

## 9. 许可与合规

- 平台包与中文界面代码：**BSD-2-Clause-Patent**（与 Project Mu / Silicium 一致）
- 字模：GPL-2.0-or-later **WITH Font-exception-2.0**（允许嵌入固件）
- 基线：Mu-Silicium / Project Silicium（其自身许可见上游）

> **合规提示**：iOS/macOS 的许可协议不允许在非 Apple 硬件上运行。
> 本项目是 **UEFI 固件与引导研究**，不包含、不分发任何 Apple 代码或系统镜像。
