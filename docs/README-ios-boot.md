# iOS 内核引导 UEFI：交付与使用说明

> 目标：把中文 UEFI 从「菜单界面」变成「能加载 iOS 内核并交接」的引导器。
> 参考实现：UniXNU（`gUniXNULoaderProtocol` 接缝）+ vermeer-lab（K70 硬件数据）。

---

## 1. 结论：链路是哪一段缺、哪一段已好

整条链是：

```
fastboot boot → BootShim → UEFI(vermeerPkg) → BDS → UniXNU → Loader Protocol → DarwinBoot → XNU
```

逐段核对结果（`tools/vermeer-zh/check_ios_chain.py` 可复现）：

| 环节 | 状态 | 证据 |
|---|---|---|
| BootShim 进入 UEFI | ✅ 实测 | 真机 `fastboot boot` 白条探针成功 |
| 中文菜单显示 | ✅ 已完成 | 宿主渲染预览 + CI 编译通过 |
| `SimpleFbDxe` 发布 GOP | ✅ | `BootVideoFromGop` 要求 BGRX，SimpleFbDxe 满足 |
| UniXNU 的 Loader 协议 | ✅ | Revision 2，`Boot()` 已实现 |
| DarwinBoot 的 kernel 解析 | ✅ | `ParseKernel` 检查 magic/cputype/subtype/filetype |
| **kernel 文件本身** | ⚠️ **本次补齐** | kernelcache 是 MH_FILESET，DarwinBoot 要 MH_EXECUTE |
| **ADT 侧边树** | ⚠️ **本次补齐** | 现已生成并验证 |
| kernel 链接（缺符号） | ❌ 未解决 | 见 §5 |

**关键发现：UEFI 层的代码链路本来就完整**，缺的是「给它的内核文件格式不对」。

---

## 2. 本次交付的东西

```
work/ios-payload/
├── kernel.exec.macho   70,556,719 字节  ← 从 kernelcache 提取的可引导内核
├── kernel.adt              10,324 字节  ← DarwinBoot 走的 Apple 设备树
├── trustcache              67,320 字节  ← 静态信任缓存（isys/trst）
└── payload.json                           ← 各组件校验信息
```

### 2.1 kernel.exec.macho 是怎么来的

原厂 `kernelcache` 是 **MH_FILESET**（filetype 12），内含 241 个条目；而
DarwinBoot 的 `ParseKernel` 只接受 **MH_EXECUTE**（filetype 2）：

```c
if ((Size < 32) || (U32 (Data) != 0xfeedfacf) || (U32 (Data + 4) != 0x100000c) ||
    (U32 (Data + 8) != 0) || (U32 (Data + 12) != 2)) {
  return EFI_UNSUPPORTED;
}
```

从 fileset 里把 `com.apple.kernel` 提取出来，形成独立 MH_EXECUTE：

| 项目 | 值 |
|---|---|
| 嵌套起点 | 文件偏移 `0xC58000` |
| 段数 | 14（`__TEXT`/`__TEXT_EXEC`/`__DATA`/`__LINKEDIT`…） |
| 头长度 | 5,384 字节 |
| 输出大小 | 70,556,719 字节 |
| SHA-256 | `07c813e5…678e8d` |

**两个必须做对的技术细节**：

1. **段偏移是相对容器的绝对偏移**，不是相对嵌套头。
   最初我按"嵌套头 + 相对偏移"计算，导致 `__LINKEDIT` 越过文件末尾。
   实测容器内最后一段结束于 `0x4349C2F`，容器总长 `0x4350000`。

2. **`cpusubtype` 必须写成 0**，不是 arm64e 的 `0xC0000002`。
   两个消费者都要求 0：
   - `DarwinBoot.c`：`U32(Data+8) != 0` 直接拒绝
   - `kits/NativePrelink/native_prelink.py:18`：`subtype != 0` 直接拒绝
   
   sheng 的已验证流程产出的也是 0（`check-sheng-root-release.py` 记录 `native_cpu_subtype: 0`）。

### 2.2 kernel.adt 是怎么生成的

用仓库既有的 `scripts/make-vermeer-adt.py`，带 `--trust-cache`：

```
python make-vermeer-adt.py --output kernel.adt --dram-size 0x80000000 --trust-cache
```

验证（`tools/vermeer-zh/adt_walk.py`，逐节点复刻 DarwinBoot 的 `AdtNode`）：

```
PASS  node walk ends at 0x2854, file is 0x2854
PASS  /chosen/memory-map present
```

DarwinBoot 要求节点遍历**恰好消费整个文件**（`Offset != TreeSize` 即判损坏），这里精确匹配。

---

## 3. 怎么用

### 3.1 把载荷放进可引导卷

DarwinBoot 从**卷**读取 `<kernel 路径>` 和 `<kernel 路径>.adt`。所以需要一个
FAT 卷（UEFI 能枚举的 `SimpleFileSystem`），里面放：

```
/mnt/ios/kernel.macho      ← 即 kernel.exec.macho
/mnt/ios/kernel.macho.adt  ← 即 kernel.adt（注意名字必须是 kernel 路径 + ".adt"）
/mnt/ios/trustcache
```

### 3.2 配一个 xnu 启动项

UniXNU 读 \/EFI\/UniXNU\/unixnu.toml。加一条：

```toml
[[entry]]
id = "ios"
title = "启动 iOS 内核"
type = "xnu"
path = "\\mnt\\ios\\kernel.macho"
args = "-v serial=3 debug=0x14e"
```

**关键约束（踩过）**：`type = "xnu"` 的条目**绝对不能写 `dtb = ...`**。
DarwinBoot 明确拒绝带设备树的请求：

```c
if ((Request->KextCount != 0) || (Request->DeviceTree != NULL) || ...) {
  Print (L"DARWINBOOT: unsupported inputs or exception level; firmware retained\n");
  return EFI_UNSUPPORTED;
}
```

而 `UniXNU` 在 `RunXnu` 里会 `Request.DeviceTree = Fdt`，`Fdt` 只在条目写了
`dtb` 时才非空。所以写了 `dtb` 就等于拒绝启动。

**它要的设备树是 `.adt` 侧边文件，不是配置里的 `dtb`。**

`tools/vermeer-zh/check_xnu_entries.py` 会扫描配置并报出这类冲突。

---

## 4. 已验证 vs 未验证

| 项 | 状态 | 方式 |
|---|---|---|
| kernel 满足 ParseKernel | ✅ | 逐条比对 C 源码条件 |
| ADT 遍历精确匹配 | ✅ | 复刻 AdtNode |
| 协议 Revision 2 | ✅ | 静态检查 |
| BGRX 像素格式 | ✅ | 静态检查 |
| 中文菜单渲染 | ✅ | 宿主逐行翻译渲染 |
| UEFI 固件编译 | ✅ | CI 成功（含缺字修复） |
| **真机引导到内核** | ❌ | 无设备 |
| **kernel 链接** | ❌ | 缺符号，见 §5 |

---

## 5. 仍未解决的：kernel 链接缺符号

最新 CI 日志（run `39890018475`）在链接阶段报缺：

```
vtable for IOBlockStorageDevice
IOBlockStorageDevice::getProperty(OSSymbol const*) const
_verified_root_key / _verified_root_policies / _verified_root_policy_count
_vr_open / _vr_verify_block / _vr_xnu_crypto
_boot_progress_poll（弱符号，不致命）
```

**根因已定位**（`scripts/build-arm-driver.py:77-79`）：

```python
if component == 'NativeUfs':
    command += [..., '-DNATIVE_VERIFIED_BLOCK=1', '-DNATIVE_VERIFIED_BLOCK_MULTI_POLICY=1', ...]
```

`NativeUfs` **无条件**进入 verified-block 模式，而
`NativeVirtioBlock`/`NativeRamBlock` 只在 `--verified-root` 时才进入（第 71 行）。
于是 NativeUfs 引用了 VerifiedRoot 的强符号，但 kit 没被链接。

同时 `vermeer_defconfig` 把这些都关了：

```
CONFIG_XNU_VERIFIED_ROOT_PROBE=n
CONFIG_XNU_OWNER_ROOT_TRUST=n
CONFIG_XNU_BOOT_PROGRESS=n
```

### 两条出路

| 方案 | 做法 | 代价 |
|---|---|---|
| **A. 关闭** | 去掉第 79 行的无条件 `-DNATIVE_VERIFIED_BLOCK=1` | kernel 能链接，但放弃 iOS 卷签名验证 |
| **B. 打开** | 编进 VerifiedRoot kit + 生成 `verified_root_key`（395 字节 RSA-3072 公钥）+ `verified_root_policies` | 需要真实 IPSW 的 manifest 与 trusted key |

**方案 B 的硬约束**：`verified_root_key` 是 Apple 的 RSA-3072 公钥，
`verified_root_policies` 是每个卷的 manifest 摘要，由
`build-verified-root-probe.py` 从真实镜像生成汇编符号。没有对应 IPSW 数据，
它们无法凭空构造——`vr_open()` 也会在运行时失败。

本机现有 `iPhone15,3_16.0_20A362_Restore.ipsw`，**但它不是 `board.toml` 指定的
iPad7,1 17.7.11**，manifest 与 key 不匹配，不能直接用于方案 B。

---

## 6. 工具清单

放在 `tools/vermeer-zh/`：

| 工具 | 作用 |
|---|---|
| `check_ios_chain.py` | 校验整条链的契约（kernel/ADT/协议/video） |
| `make_ios_payload.py` | 从 IPSW 解包生成载荷目录 |
| `extract_fileset_kernel.py` | fileset → MH_EXECUTE，含 subtype 归一 |
| `adt_walk.py` | 复刻 DarwinBoot 的 ADT 遍历 |
| `check_xnu_entries.py` | 扫描 `type="xnu"` 配置里的 `dtb` 冲突 |
| `check_font_coverage.py` | 校验菜单文字都有字形 |
| `pack_boot.py` / `verify_boot_image.py` | 打包与反解验证 boot.img |

---

## 7. 复现命令

```bash
# 1. 从 IPSW 解包出 kernel + trustcache
python tools/vermeer-zh/make_ios_payload.py --extract "D:\ios hack\ipsw_extract" --out work/ios-payload

# 2. fileset -> 可引导 MH_EXECUTE
python tools/vermeer-zh/extract_fileset_kernel.py \
    --kernel work/ios-payload/kernel.macho \
    --out work/ios-payload/kernel.exec.macho

# 3. 生成 ADT
cd src/UniXNU-vermeer
python scripts/make-vermeer-adt.py --output ../../work/ios-payload/kernel.adt \
    --dram-size 0x80000000 --trust-cache

# 4. 验证
python tools/vermeer-zh/adt_walk.py work/ios-payload/kernel.adt
python tools/vermeer-zh/check_ios_chain.py --ipsw "D:\ios hack\ipsw_extract"
```
