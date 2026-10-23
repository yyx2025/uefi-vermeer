# K70 触摸支持：可行性评估与技术路线

> 结论先行：**当前固件形态下"能加"但"不能保证生效"**。
> 触摸需要三件东西同时到位：硬件识别、总线驱动、触摸协议。三者中只有第 2 项在
> `VERMEER_VENDOR_HW = 0` 的首启路径上现成可用，而第 1 项受限于 ABL 交接的数据。

---

## 1. 已确证的硬件事实（实测 K70 原厂 `dtbo.img`）

用 `tools/vermeer-zh/dtbo_tool.py` 解析 `vermeer_extract/images/dtbo.img`
（**DTBO v0，57 个 overlay**），触摸相关结论如下。

**K70 实际只有一套触摸方案：Goodix GT9916S，走 SPI。**
`dtbo.img` 里 57 个 overlay 中有 30+ 个带 `goodix-berlin@0` 节点，
而 `novatek` 只出现在**字符串表残留**里（通用 Kalama 模板带下来的），
**没有任何 overlay 真的启用 Novatek 节点**。

### 1.1 节点完整属性（overlay 19 实测，27 条）

```
/soc/qcom,qupv3_1_geni_se@ac0000/spi@a90000/goodix-berlin@0
    compatible             'goodix,gt9916S'          ← 末尾的 S = SPI 版本
    reg                    0x0                       ← SPI 片选索引，不是 I²C 地址
    spi-max-frequency      0xf4240  = 1 MHz
    interrupts             0x19 0x2008               ← GPIO 25，下降沿
    goodix,reset-gpio      <&tlmm 0x18 0x0>          ← GPIO 24
    goodix,irq-gpio        <&tlmm 0x19 0x2008>       ← GPIO 25
    goodix,irq-flags       0x2                       ← FALLING
    goodix,panel-max-x     0x438  = 1080
    goodix,panel-max-y     0x960  = 2400
    goodix,panel-max-w     0xff
    goodix,panel-max-p     0x1000
    goodix,firmware-name   'goodix_firmware_spi.bin'      ← SPI 固件
    goodix,config-name     'goodix_cfg_group_spi.bin'
    goodix,avdd-name       'avdd' / iovdd-name 'iovdd'
    goodix,touch-type      'primary'
    goodix,qts_en          (空)
    qts,trusted-touch-mode 'vm_mode'
    qts,touch-environment  'pvm'
    qts,trusted-touch-spi-irq 0x239
    qts,trusted-touch-io-bases 0xa90000
    qts,trusted-touch-io-sizes 0x1000
```

### 1.2 一个必须留意的原厂命名陷阱

`/__fixups__` 里写的是：

```
qupv3_se4_i2c  ->  /fragment@19:target:0
```

`__symbols__` 把它解析到 `i2c@a90000`，`fragment@19/__overlay__` 也自称
`qcom,i2c-touch-active = "goodix,gt9916"`。

**但这三处命名都是继承模板的遗留，实际是 SPI**，证据：

| 证据 | 说明 |
|---|---|
| `spi-max-frequency` | 只有 SPI 才有这个属性 |
| `goodix_firmware_spi.bin` | 固件名带 `_spi` |
| `qts,trusted-touch-spi-irq` | QTS 明确写 SPI |
| `compatible = goodix,gt9916S` | 末尾 `S` 是 SPI 变体 |
| `reg = <0x0>` | 是 CS 索引，不是 I²C 从地址 |

同一 SE4 下 `i2c@a90000` 与 `spi@a90000` 并存，触摸挂在 **`spi@a90000`**。

如果按字面去读 `i2c@a90000` + 地址 0x5D，会**完全找不到设备**。
这是本项目踩到并纠正的第一个实质错误。

### 1.3 触摸分辨率 ≠ 面板分辨率

| | X | Y |
|---|---|---|
| 触摸面板 `panel-max` | **1080** | **2400** |
| K70 显示面板 | **1440** | **3200** |
| 比例 | **0.75** | **0.75** |

触摸感应区比显示区小（比例恰好 0.75），且带 `invert_xy`。
**坐标映射不能简单按比例缩放**，必须按玻璃盖板实际边界标定。

### 1.4 Qualcomm Trusted Touch（QTS）

```
qts,trusted-touch-mode    'vm_mode'
qts,touch-environment     'pvm'
qts,trusted-touch-spi-irq 0x239
```

原厂把触摸跑在**受信任虚拟机（PVM）**里，走 SPI + QTS 通道。
对 UEFI 而言这既是障碍也是机会：**UEFI 阶段 QTS/VM 尚未启动**，
触摸芯片理论上处于未托管状态，可以直接访问。
这一点**必须真机验证**，无法离线推断。


```
qts,trusted-touch-mode    'vm_mode'
qts,touch-environment     'pvm'
qts,trusted-touch-spi-irq 0x239
qts,trusted-touch-io-bases 0xa90000
```

原厂把触摸跑在一个**受信任虚拟机（PVM）**里，走 SPI + QTS 通道，而不是普通 Linux I²C 驱动。
这解释了两件事：

1. 为什么触摸节点挂在 `fragment@19` 这种独立 fragment 上——它是安全侧配置的一部分；
2. 为什么"直接访问 SPI 片选 0"可能拿不到数据——**触摸芯片在正常运行时由 QTS 托管**，
   AP 侧直接访问会被拒绝或读到空。

对 UEFI 而言这既是障碍也是机会：UEFI 阶段 QTS/VM 尚未启动，**理论上**触摸芯片处于
未托管状态，可以直接通过 SPI 访问。这一点**必须真机验证**，无法离线推断。

---

## 2. 为什么现在的镜像点不亮触摸

三个必要条件逐一对照：

### 2.1 硬件识别：❌ 缺

现有 `vermeer.dtb` 是 **Kalama v2 SoC 参考骨架**：

```
model     = "Qualcomm Technologies, Inc. Kalama v2 SoC"
compatible= "qcom,kalama"
qcom,board-id = 0x0 0x0        ← 通用板，不是 K70
```

核查结果：**3405 个节点里没有任何触摸节点**；所有 I²C/SPI 控制器（共 40+ 个）
**子设备为空**，唯一的例外是 `fsa4480@42`（音频开关）。

原因：真正的整机外设在 `dtbo.img` 的 overlay 里，而这份 `vermeer.dtb` 是从基础
DTB 提取的，overlay 没有被合入。`board.toml` 目前也只注入了 `splash_region` 一个节点。

### 2.2 总线驱动：⚠️ 有条件可用

| 总线 | UEFI 侧现状 |
|---|---|
| I²C | `I2CDxe` 存在，但**只在 `VERMEER_VENDOR_HW = 1` 时编入**（`Binaries/vermeer/QcomPkg/Drivers/I2CDxe`） |
| SPI | `SPIDxe` 同样只在 `VERMEER_VENDOR_HW = 1` 时编入 |
| TLMM（GPIO） | `TLMMDxe` 同样只在 `VERMEER_VENDOR_HW = 1` 时编入 |

而 `VERMEER_VENDOR_HW = 1` 这条路径在本平台**已知会把面板点亮状态破坏掉**
（PORTING.md 记录：厂商栈初始化后屏幕变黑、日志丢失）。也就是说：

> **首启能显示中文菜单的前提，正好是"不加载 I²C/SPI/GPIO 驱动"。**
> 要触摸就得打开厂商栈，而打开厂商栈目前会黑屏。这是一个必须解决的耦合。

### 2.3 触摸协议：❌ 缺，但目标已明确

即使总线通了，还需要 Goodix GT9916S 的 SPI 协议（寄存器、握手、坐标包格式）。

**已确认的协议参数**（来自 DT，不用猜）：

| 参数 | 值 |
|---|---|
| 总线 / 片选 | SPI，CS 0（`spi@a90000`） |
| 复位引脚 | TLMM GPIO **24** |
| 中断引脚 | TLMM GPIO **25**，下降沿 |
| 坐标包最大 X/Y | 1080 / 2400 |
| 触点权重/压力 | `max-w 0xFF`、`max-p 0x1000` |
| 固件/配置文件名 | `goodix_firmware_i2c.bin` / `goodix_cfg_group_i2c.bin` |
| 坐标轴 | 需要交换（`invert_xy`） |

GT9916 有公开的 Linux 驱动（`goodix_berlin` / `gt9916`）可作协议参考。
所谓"写驱动"更多是**移植这套已知协议**，而不是从零逆向。

**注意**：`NativeNovatekTouch`（仓库里那份）是为 sheng 的 **Novatek NT36532E + SPI** 写的，
**与 K70 的 Goodix GT9916S + SPI 完全不兼容**，不能复用，只能作结构参考。

---

## 3. 结论：分三个层次给答案

### 3.1 能不能做到"菜单里点按选择"？

**短期内不现实**，因为要同时打通：厂商 SPI/I²C 栈 + 不黑屏 + Goodix 协议 + 坐标校准。
每一步都需要真机调试，而当前**没有连接设备**（`fastboot devices` 为空）。

### 3.2 有没有更稳妥的中间方案？

**有，而且我已经实现了**：**音量键 + 电源键导航**。

`VermeerKeyInputDxe` 通过 **SPMI 直接读 PMIC 的 PON 键和音量键**，不经过 TLMM/I²C 厂商栈，
因此在 `VERMEER_VENDOR_HW = 0` 下可用。这已是完整可用的交互方案——触摸对"三选一菜单"
并不是必需的。

### 3.3 如果一定要触摸，正确的顺序是什么？

```
第 1 步  合入 overlay（已可离线完成，且节点数据齐全）
         fragment@19 挂到 /soc/qcom,qupv3_1_geni_se@ac0000/i2c@a90000，
         __overlay__ 里加 goodix-berlin@5d：
             compatible = "goodix,gt9916S"
             reg = <0x0>   /* SPI 片选索引 */
             interrupts / goodix,reset-gpio / goodix,irq-gpio（GPIO 24/25）
             goodix,panel-max-x = <0x438>(1080)、panel-max-y = <0x960>(2400)
             invert_xy
         工具：tools/vermeer-zh/dtbo_tool.py（解析）+ fdt_tool.py（合并/校验）

第 2 步  解决"厂商栈黑屏"这个前置问题
         现在 VERMEER_VENDOR_HW=1 会黑屏（vermeer.dsc:32 与 PORTING.md:72
         都有记录：厂商栈初始化后屏幕变黑、背光仍亮、日志丢失）。
         而 I2CDxe / SPIDxe / TLMMDxe 恰好**只在这个开关下编入**。
         在这一点解决前，任何触摸工作都无法在屏幕上验证。

第 3 步  写驱动
         SPI 访问 spi@a90000、CS 0，按 GT9916S 协议解析坐标包。
         需先确认：UEFI 阶段 QTS 未启动时，芯片是否可直接访问。
         发布 EFI_SIMPLE_POINTER_PROTOCOL，让中文界面消费。

第 4 步  真机标定
         触摸 1080x2400 vs 面板 1440x3200 的映射 + invert_xy。
         这一步没有捷径，必须在设备上用坐标打点法测。

第 5 步  真机验证
         串口日志（-v serial=3）是唯一可靠的调试通道。
```

---

## 4. 现在就能做的一步：把触摸节点合进 DTB

这是唯一**不依赖真机、可以立即验证正确性**的部分。我可以：

1. 从 `dtbo.img` 解析出 overlay fragment（需要处理 Android DTBO 表头 + FDT overlay 的
   `fragment@N` / `target` / `__overlay__` 结构）
2. 提取 `goodix-berlin@5d` 与 `novatek@0` 节点的完整属性
3. 合并到 `vermeer-splash.dtb`，生成 `vermeer-touch.dtb`
4. 自校验：节点存在、父节点正确、`reg`/中断/GPIO 属性齐全

**但**：即使 DTB 里有节点，没有对应驱动也不会生效。这属于"为第 4 步铺路"，
不是"触摸可用"。

---

## 5. 我的建议

按优先级：

| 优先级 | 动作 | 依赖 |
|---|---|---|
| **P0** | 用现有的音量/电源键方案，先把镜像在真机上跑起来 | 需要设备 |
| **P1** | 解决 `VERMEER_VENDOR_HW=1` 黑屏问题 | 需要设备 + 串口日志 |
| **P2** | 合入触摸 overlay 到 DTB（可离线做） | 无 |
| **P3** | 实现 Goodix 或 Novatek 驱动 | P1 + P2 + 真机 |

**不做 P1 就直接做 P3 是没有意义的**——驱动写好了也点不亮。

---

## 6. 可以直接回答你的问题

- **"能加触摸吗？"** → 能，但需要真机调试，且要先解决厂商栈黑屏。
- **"现在加有用吗？"** → 没有。触摸节点进 DTB 是必要但远远不充分的条件。
- **"那菜单怎么操作？"** → 已经可用：音量键选择、电源键确认，走 SPMI 直读 PMIC，不依赖触摸。
- **"要我继续做哪一步？"** → 建议先 P2（离线合 overlay，纯代码，可验证），
  等你设备接上后再推进 P1/P3。
