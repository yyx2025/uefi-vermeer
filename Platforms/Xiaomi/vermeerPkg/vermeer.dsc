##
#  Copyright (c) 2011 - 2022, ARM Limited. All rights reserved.
#  Copyright (c) 2014, Linaro Limited. All rights reserved.
#  Copyright (c) 2015 - 2020, Intel Corporation. All rights reserved.
#  Copyright (c) 2018, Bingxing Wang. All rights reserved.
#  Copyright (c) Microsoft Corporation.
#
#  SPDX-License-Identifier: BSD-2-Clause-Patent
##

################################################################################
#
# Defines Section - statements that will be processed to create a Makefile.
#
################################################################################
[Defines]
  PLATFORM_NAME                  = Vermeer
  PLATFORM_GUID                  = E2DF0557-79F9-4EB2-90B0-FA34DC941EEC
  PLATFORM_VERSION               = 0.1
  DSC_SPECIFICATION              = 0x00010005
  OUTPUT_DIRECTORY               = Build/vermeerPkg
  SUPPORTED_ARCHITECTURES        = AARCH64
  BUILD_TARGETS                  = RELEASE|DEBUG
  SKUID_IDENTIFIER               = DEFAULT
  FLASH_DEFINITION               = vermeerPkg/vermeer.fdf
  USE_CUSTOM_DISPLAY_DRIVER      = 0

  #
  # 0 = Mu drivers only. The previous stage's display keeps scanning out, so
  #     the boot manager is visible without any vendor hardware driver.
  # 1 = also load the stock vendor hardware drivers (SMEM, clocks, RPMh, UFS,
  #     USB, charger, ...). Their initialization currently blanks the panel.
  #
  VERMEER_VENDOR_HW                = 0

  #
  # 0 = SM8550-AB
  # 1 = SM8550-AC
  #
  SOC_TYPE                       = 0

  #
  # 1 = 编入中文启动界面。
  # 两个模块：VermeerChineseUiDxe 把 VermeerChineseUi（UEFI 应用）注册成
  # BDS 的启动项。它们只依赖 GOP 与 ConIn，不需要任何厂商驱动，
  # 所以 VERMEER_VENDOR_HW = 0 的首启路径也能显示。
  #
  VERMEER_CHINESE_UI             = 1

!include KailuaPkg/KailuaPkg.dsc.inc

!if ($(USE_CUSTOM_DISPLAY_DRIVER) == 1) && ($(VERMEER_VENDOR_HW) == 0)
  !error The stock DisplayDxe chain needs VERMEER_VENDOR_HW = 1
!endif

[PcdsFixedAtBuild]
  #
  # DDR Memory
  #
  gArmTokenSpaceGuid.PcdSystemMemoryBase|0x80000000

  #
  # UEFI Stack
  #
  gArmPlatformTokenSpaceGuid.PcdCPUCoresStackBase|0xA760D000
  gArmPlatformTokenSpaceGuid.PcdCPUCorePrimaryStackSize|0x40000

  #
  # SMBIOS
  #
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemManufacturer|"Xiaomi"
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemModel|"Vermeer"
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemRetailModel|"Vermeer"
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemRetailSku|"Vermeer"
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemBoardModel|"Vermeer"

  #
  # Simple Frame Buffer
  #
  # Active size of the K70 WQHD+ panel (1440x3200, 32bpp). Without these the
  # DEBUG frame-buffer log draws nothing after SEC clears "Display Reserved",
  # leaving a black screen. ABL's scan-out stride is assumed equal to the
  # width; unverified on this device.
  #
  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferWidth|1440
  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferHeight|3200
  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferColorDepth|32
  # Scale 3 keeps the early log readable on the 1440-wide panel.
  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferFontScale|3
  # Pause per log line so a video of the screen shows what loads last.
  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferDelay|30000


  #
  # Platform PEI
  #
  gQcomPkgTokenSpaceGuid.PcdPlatformType|"LA"
!if $(VERMEER_VENDOR_HW) == 0
  # Intermittent stall right after "MMU: enabled" (int-103 photos): PlatformPeim scanned the stock UEFI's leftover FD window and
  # called into its scheduler protocol. Without the stock vendor drivers nothing reads those HOBs.
  gQcomPkgTokenSpaceGuid.PcdBuildXblHobs|FALSE
!endif

[LibraryClasses]
  #
  # Memory Libraries
  #
  MemoryMapLib|vermeerPkg/Library/MemoryMapLib/MemoryMapLib.inf

  #
  # QCOM Libraries
  #
  ConfigurationMapLib|vermeerPkg/Library/ConfigurationMapLib/ConfigurationMapLib.inf

[Components]
  #
  # ACPI Tables
  #
  UniXNUPkg/Application/UniXNU/UniXNU.inf
  UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf
  vermeerPkg/Drivers/VermeerDisplayDepexDxe/VermeerDisplayDepexDxe.inf
  vermeerPkg/Drivers/VermeerGopLogDxe/VermeerGopLogDxe.inf
  vermeerPkg/Drivers/VermeerBootMemoryDxe/VermeerBootMemoryDxe.inf
  vermeerPkg/Drivers/VermeerKeyInputDxe/VermeerKeyInputDxe.inf
  #
  # 中文启动界面：DXE 驱动，DEPEX 等 GOP，排在 BdsDxe 之前跑。
  # VERMEER_CHINESE_UI = 0 时不编进 FV（用于排查 GOP 相关问题时排除它）。
  #
!if $(VERMEER_CHINESE_UI) == 1
  vermeerPkg/Drivers/VermeerChineseUi/VermeerChineseUiDxe.inf
  vermeerPkg/Drivers/VermeerChineseUi/VermeerChineseUi.inf
!endif
  DarwinBootPkg/Driver/DarwinBoot.inf

[LibraryClasses]
  UniXNULib|UniXNUPkg/Library/UniXNULib/UniXNULib.inf
  FdtLib|MdePkg/Library/BaseFdtLib/BaseFdtLib.inf
