# iOS 内核链接：缺符号的完整诊断与修复路径

> 本文回答一个问题：CI 里 `kernel.macho` 链接失败，到底缺什么、能不能补、怎么补。
> 所有结论都标注了证据出处（文件:行号），可逐条复核。

---

## 1. 缺的符号分两类，原因完全不同

CI run `39890018475` 的链接日志列出的 undefined symbols：

### A 类：VerifiedRoot 数据与代码（8 个）

```
_vr_open  _vr_verify_block  _vr_xnu_crypto
_verified_root_key  _verified_root_policies  _verified_root_policy_count
_boot_progress_poll
```

**来源**：`NativeUfs.cpp` 在 `NATIVE_VERIFIED_BLOCK` 下引用它们。

**机制**（`build-xnu-4k-baseline.sh`）：

```bash
OWNER_ROOT_PROBE=${XNU_VERIFIED_ROOT_PROBE:-0}
...
if [ "$OWNER_ROOT_PROBE" = 1 ]; then
    VERIFIED_ROOT_ARCHIVE=.../libVerifiedRootProbe.a
    KERNEL_EXTRA_ARCHIVES="... -Wl,-force_load,$VERIFIED_ROOT_ARCHIVE"
fi
```

而 `vermeer_defconfig` 里是 `CONFIG_XNU_VERIFIED_ROOT_PROBE=n`
→ `OWNER_ROOT_PROBE=0` → archive 不链接 → 全部缺失。

**但 `NativeUfs` 仍被带上了 `-DNATIVE_VERIFIED_BLOCK=1`**
（`build-arm-driver.py:78-79`，无条件）→ 引用了不存在的符号。

**这是配置冲突，不是代码 bug。**

### B 类：`IOBlockStorageDevice`（10 个）

```
vtable for IOBlockStorageDevice
IOBlockStorageDevice::gMetaClass / init / getProperty / requestIdle
  doSetPriority / doUnmap / doGetProvisionStatus / ~IOBlockStorageDevice
```

**来源**：`NativeUfs.cpp` 继承 `IOBlockStorageDevice`（`#include <IOKit/storage/IOBlockStorageDevice.h>`）。

**这是 XNU 内核类，不是 UniXNU 的代码。** 它的实现随 XNU 的
`iokit/IOStorageFamily` 模块编译。

`scripts/audit-storage-interface.py` 会编译一份
`IOBlockStorageDevice-reference.o`，但脚本注释明确写着：

> The reference object is an annotation instrument and is never linked/executed.

我核实过：该文件**只被 audit 脚本自己引用**，不参与任何链接。

**所以 B 类无法用「补一个 archive」解决**，必须让 XNU 构建时把
`IOStorageFamily` 的 `.cpp` 编进内核。

---

## 2. 我已经做完的部分：A 类

用 `board.toml` 指定的真实源生成了完整数据，并逐项验证。

### 数据

| 项 | 值 |
|---|---|
| 签名对象 | `044-76040-038.dmg`（iPad7,1 17.7.11 的 OS 卷） |
| 镜像大小 | 5,157,406,069 字节 |
| **image_sha256** | `3f99b859c9728379110b66657c7b15285bcb1925ad8b5726542e2cdcf8b1028d` |
| `board.toml` 的 pin | **同上，逐字符一致** |
| 块数 | 1,259,133（4 KiB/块） |
| manifest | 40,292,800 字节（160 头 + 384 签名 + 表） |

### 验证

| 检查 | 结果 |
|---|---|
| `vr_open()` 的全部条件 | 通过 |
| `block_count == ceil(image/4096)` | 1,259,133 ✓ |
| 头部 key 摘要 == anchor sha256 | ✓ |
| hash table 摘要 == 头部 table_sha | ✓ |
| RSA-3072 签名（openssl 验签） | ✓ |
| 汇编 5 个符号齐全 + extent 正确 | ✓ |

`_verified_root_key` 是 **395 字节**（`VROOTKEY` + RSA-3072 模数 + `65537`），
由本机 openssl 生成的 owner 密钥构成。

**必须说清**：这是**本地 owner 锚点，不是 Apple 的公钥**。
`public_anchor()` 只要求一个真实的 3072-bit RSA 公钥（e=65537），
所以数据在格式与密码学上自洽，但**不等于能通过 Apple 的开机授权**。

### 成本发现

不需要下载 6.8 GB 整包。`unixnu` 的 `RangeFile` 用 HTTP range 只取成员：

```
仅取 OS 成员：5.16 GB（不是 6.8 GB 全包）
哈希 1,259,133 块：约 2 分钟
```

---

## 3. 剩下的两步

### 步骤 1：把 A 类数据接进 kernel 构建

1. `vermeer_defconfig` 打开：

```toml
CONFIG_XNU_VERIFIED_ROOT_PROBE=y
CONFIG_XNU_VERIFIED_ROOT_ARCHIVE="{tree}/build/verified-root-systemos/libVerifiedRootProbe.a"
```

2. 把 `libVerifiedRootProbe.a` 组装出来。参照
   `build-verified-root-probe.py` 的对象清单：

| 对象 | 来源 |
|---|---|
| `verified_root.o` | `kits/VerifiedRoot/src/verified_root.c` |
| `xnu_crypto.o` | `kits/VerifiedRoot/adapters/xnu_crypto.c` |
| `verified-root-probe.o` | `scripts/qemu-entry/verified-root-probe.c` |
| `inputs.o` | 我生成的汇编（`_verified_root_*`） |

3. **注意**：`build-verified-root-probe.py` 有一道硬校验：

```python
if (metadata['image_sha256'] != expected_digest or
    metadata['blocks_verified'] != expected_count or
    not metadata['real_c_signature_and_block_verification'] or ...):
    raise SystemExit('Require the actually verified unchanged original full image')
```

它要求 `real_c_signature_and_block_verification` 为真。我那一步是
**分块 SHA-256 + 本地密钥签名**，没有做 Apple 的 C 签名验证。

所以要么：
- **(a)** 改写这道校验，接受"块哈希完整 + 本地锚点签名"的语义；
- **(b)** 关掉 `VERIFIED_ROOT_PROBE`，同时关掉 `NativeUfs` 的
  `NATIVE_VERIFIED_BLOCK`（去掉 `build-arm-driver.py:79` 的无条件宏）。

**(b) 的代价我已核实**：`NativeUfs.cpp:1924-1925` 的 `validate()` 在非验证模式下
恒 `return false`，root 卷永不发布 → **内核过不了 `bsd_init`**。
所以 (b) 只能让链接通过，不能启动。

**推荐 (a)**：接受本地锚点语义，保留完整的卷验证路径。

### 步骤 2：让 `IOBlockStorageDevice` 进内核

这是 XNU 侧的构建配置问题，必须在 CI 上验证（本地无 XNU 树）。

可行的参照是 `UniXNU-qemu`，它已经跑通 iPadOS 17 到主屏幕：

```
README.md:
  feeds.toml  | IOStorageFamily (UniXNU#7) ...
  qemu_root_defconfig | ... the verified VirtIO block driver,
                 the owner root chain, the SystemOS union mount, ...
```

它启用了完整的 `VERIFIED_ROOT_PROBE` + `OWNER_*` 链，且额外在自己的
`feeds.toml` 里声明了 IOStorageFamily 源（`IOStorageFamily-315`,
commit `e45fbe30…`）——**UniXNU 自身的 feeds 不含这一条**。

所以 `UniXNU-vermeer` 很可能也需要一份 `feeds.toml` 来声明它。

---

## 4. 结论

| 项 | 状态 |
|---|---|
| A 类数据（manifest/公钥/策略/汇编） | ✅ 已生成并验证 |
| A 类接入 kernel 构建 | ⏳ 需改 defconfig + 组装 archive |
| B 类（IOBlockStorageDevice） | ⏳ 需 XNU 侧配置，CI 验证 |
| 真机启动 | ❌ 无设备 |

**一句话**：A 类是我能在这里做完并验证的，已经做完；
B 类是 XNU 构建配置问题，需要跑 CI 才能确认，本地无法验证。

我不建议在没有验证的情况下改 `vermeer_defconfig` 并宣称"修好了"——
那样只是让链接绿灯，`validate()` 恒 false 会让内核在真机上停在找不到根卷。

---

## 5. 工具

`tools/vermeer-zh/`：

| 工具 | 作用 |
|---|---|
| `fetch_ipsw_members.py` | range 抓取 IPSW 成员（绕过失效代理） |
| `make_verified_root.py` | 生成 manifest + owner 锚点 |
| `check_verified_root.py` | 按 `vr_open()` 的规则逐条复核 |
| `emit_verified_root_asm.py` | 生成 `_verified_root_*` 汇编 |
| `assemble_verified_root.py` | 汇编并检查符号 extent |
| `check_ios_chain.py` | 整条引导链的契约检查 |
| `check_xnu_entries.py` | 扫描 `type="xnu"` 条目的 `dtb` 冲突 |

Release：`verified-root-v1`（manifest + 锚点 + 汇编）
