/** @file
  把中文启动界面注册成 BDS 的一个启动项。

  背景（为什么不能只做一个依赖 GOP 的 DXE 驱动）：

    BdsDxe 的 DEPEX 是 TRUE，而 DXE 调度器只按 DEPEX 满足情况派发，不按 FDF
    里的书写顺序。一个“也依赖 GOP”的驱动因此没有确定的执行时机，可能落在
    BDS 之后（界面永不显示），也可能和 BDS 抢屏幕。

  这个驱动用的是 UniXNUDxe 已经在同一平台上验证过的做法：写一个 Boot####
  变量（设备路径指向本 FV 里的应用文件），再把它放到 BootOrder 首位。BDS
  于是像启动任何普通应用一样启动中文界面；界面返回后，BDS 接着走它原来的
  启动顺序。

  与 UniXNUDxe 的次序问题：

    两个注册器都把“自己”放到 BootOrder 首位，谁后派发谁就赢。为了不依赖
    派发顺序，中文界面应用在进入时会把自己重新排到首位（幂等操作），
    所以第一次启动之后 BootOrder 就稳定为“中文界面优先”。
    这不是必需的：即使 UniXNU 抢先，界面也只是晚一轮出现。

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <PiDxe.h>

#include <Guid/GlobalVariable.h>
#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>
#include <Library/DevicePathLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/PcdLib.h>
#include <Library/PrintLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>
#include <Library/UefiRuntimeServicesTableLib.h>
#include <Protocol/DevicePath.h>
#include <Protocol/FirmwareVolume2.h>
#include <Protocol/LoadedImage.h>

#define OPTION_ATTRIBUTES  (EFI_VARIABLE_NON_VOLATILE | EFI_VARIABLE_BOOTSERVICE_ACCESS | EFI_VARIABLE_RUNTIME_ACCESS)

//
// 启动项的描述文字与可选数据。可选数据是应用识别“自己是被固件启动的”
// 的标记，同时也让 BDS 的启动菜单里能区分这两个中文项。
//
STATIC CONST CHAR16  mDescription[] = L"红米 K70 中文启动器";
STATIC CONST CHAR16  mMarker[]      = L"vermeer:chinese-ui";

//
// 中文界面的应用文件 GUID，必须与 VermeerChineseUi.inf 的 FILE_GUID 一致。
//
STATIC CONST EFI_GUID  mApplicationFile = {
  0x7E2B4C61, 0x9A35, 0x4F8D, { 0xB1, 0xE7, 0x3C, 0x5A, 0x90, 0xD2, 0x4F, 0x18 }
};

/**
  这个 FV 里有没有中文界面的应用文件。
**/
STATIC
BOOLEAN
FvHasFile (
  IN EFI_HANDLE      Handle,
  IN CONST EFI_GUID  *File
  )
{
  EFI_FIRMWARE_VOLUME2_PROTOCOL  *Fv;
  EFI_FV_FILETYPE                Type;
  EFI_FV_FILE_ATTRIBUTES         Attributes;
  UINT32                         AuthStatus;
  UINTN                          Size;

  if (EFI_ERROR (gBS->HandleProtocol (Handle, &gEfiFirmwareVolume2ProtocolGuid, (VOID **)&Fv))) {
    return FALSE;
  }

  return !EFI_ERROR (Fv->ReadFile (Fv, File, NULL, &Size, &Type, &Attributes, &AuthStatus));
}

/**
  构造应用文件的设备路径：先找本驱动所在的 FV，再退回到任何含有该文件的 FV。
**/
STATIC
EFI_DEVICE_PATH_PROTOCOL *
FindApplicationPath (
  IN EFI_HANDLE  ImageHandle
  )
{
  EFI_LOADED_IMAGE_PROTOCOL          *Loaded;
  EFI_HANDLE                         Fv;
  EFI_HANDLE                         *Handles;
  UINTN                              Count;
  UINTN                              Index;
  MEDIA_FW_VOL_FILEPATH_DEVICE_PATH  Node;

  Fv = NULL;
  if (!EFI_ERROR (gBS->HandleProtocol (ImageHandle, &gEfiLoadedImageProtocolGuid, (VOID **)&Loaded)) &&
      FvHasFile (Loaded->DeviceHandle, &mApplicationFile))
  {
    Fv = Loaded->DeviceHandle;
  }

  if ((Fv == NULL) &&
      !EFI_ERROR (gBS->LocateHandleBuffer (ByProtocol, &gEfiFirmwareVolume2ProtocolGuid, NULL, &Count, &Handles)))
  {
    for (Index = 0; (Index < Count) && (Fv == NULL); Index++) {
      if (FvHasFile (Handles[Index], &mApplicationFile)) {
        Fv = Handles[Index];
      }
    }

    FreePool (Handles);
  }

  if ((Fv == NULL) || (DevicePathFromHandle (Fv) == NULL)) {
    return NULL;
  }

  EfiInitializeFwVolDevicepathNode (&Node, &mApplicationFile);
  return AppendDevicePathNode (DevicePathFromHandle (Fv), (EFI_DEVICE_PATH_PROTOCOL *)&Node);
}

/**
  按 UEFI 规范 3.1.3 序列化一个 EFI_LOAD_OPTION。
**/
STATIC
VOID *
BuildLoadOption (
  IN     EFI_DEVICE_PATH_PROTOCOL  *Path,
  IN OUT UINTN                     *Size
  )
{
  UINTN  PathSize;
  UINT8  *Option;
  UINT8  *Cursor;

  PathSize = GetDevicePathSize (Path);
  *Size    = sizeof (UINT32) + sizeof (UINT16) + sizeof (mDescription) + PathSize + sizeof (mMarker);
  Option   = AllocateZeroPool (*Size);
  if (Option == NULL) {
    return NULL;
  }

  Cursor = Option;
  WriteUnaligned32 ((UINT32 *)Cursor, LOAD_OPTION_ACTIVE);
  Cursor += sizeof (UINT32);
  WriteUnaligned16 ((UINT16 *)Cursor, (UINT16)PathSize);
  Cursor += sizeof (UINT16);
  CopyMem (Cursor, mDescription, sizeof (mDescription));
  Cursor += sizeof (mDescription);
  CopyMem (Cursor, Path, PathSize);
  Cursor += PathSize;
  CopyMem (Cursor, mMarker, sizeof (mMarker));
  return Option;
}

EFI_STATUS
EFIAPI
VermeerChineseUiDxeEntry (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_STATUS                Status;
  EFI_DEVICE_PATH_PROTOCOL  *Path;
  VOID                      *Option;
  UINTN                     OptionSize;
  UINT16                    *Order;
  UINTN                     OrderSize;
  UINT16                    *NewOrder;
  UINTN                     Index;
  UINTN                     Out;
  UINT16                    Number;
  CHAR16                    Name[sizeof ("Boot####")];
  VOID                      *Existing;
  UINTN                     ExistingSize;
  BOOLEAN                   Found;

  Path = FindApplicationPath (ImageHandle);
  if (Path == NULL) {
    DEBUG ((DEBUG_ERROR, "VermeerChineseUiDxe: 找不到中文界面应用文件\n"));
    return EFI_NOT_FOUND;
  }

  Option = BuildLoadOption (Path, &OptionSize);
  FreePool (Path);
  if (Option == NULL) {
    return EFI_OUT_OF_RESOURCES;
  }

  Order     = NULL;
  OrderSize = 0;
  GetEfiGlobalVariable2 (EFI_BOOT_ORDER_VARIABLE_NAME, (VOID **)&Order, &OrderSize);

  //
  // 已有同样的启动项就复用，否则用最小的空闲编号。
  //
  Found = FALSE;
  for (Index = 0; (Index < OrderSize / sizeof (UINT16)) && !Found; Index++) {
    UnicodeSPrint (Name, sizeof (Name), L"Boot%04x", Order[Index]);
    if (!EFI_ERROR (GetEfiGlobalVariable2 (Name, &Existing, &ExistingSize))) {
      Found = (ExistingSize == OptionSize) && (CompareMem (Existing, Option, OptionSize) == 0);
      if (Found) {
        Number = Order[Index];
      }

      FreePool (Existing);
    }
  }

  if (!Found) {
    for (Number = 0; Number < 0xFFFF; Number++) {
      UnicodeSPrint (Name, sizeof (Name), L"Boot%04x", Number);
      if (EFI_ERROR (GetEfiGlobalVariable2 (Name, &Existing, &ExistingSize))) {
        break;
      }

      FreePool (Existing);
    }

    Status = gRT->SetVariable (Name, &gEfiGlobalVariableGuid, OPTION_ATTRIBUTES, OptionSize, Option);
    if (EFI_ERROR (Status)) {
      DEBUG ((DEBUG_ERROR, "VermeerChineseUiDxe: 写 %s 失败：%r\n", Name, Status));
      FreePool (Option);
      if (Order != NULL) {
        FreePool (Order);
      }

      return Status;
    }
  }

  FreePool (Option);

  //
  // BootOrder 变成“本项在前，其余保持原顺序”。
  //
  NewOrder = AllocatePool (OrderSize + sizeof (UINT16));
  if (NewOrder == NULL) {
    if (Order != NULL) {
      FreePool (Order);
    }

    return EFI_OUT_OF_RESOURCES;
  }

  NewOrder[0] = Number;
  Out         = 1;
  for (Index = 0; Index < OrderSize / sizeof (UINT16); Index++) {
    if (Order[Index] != Number) {
      NewOrder[Out++] = Order[Index];
    }
  }

  Status = gRT->SetVariable (
                  EFI_BOOT_ORDER_VARIABLE_NAME,
                  &gEfiGlobalVariableGuid,
                  OPTION_ATTRIBUTES,
                  Out * sizeof (UINT16),
                  NewOrder
                  );
  DEBUG ((DEBUG_INFO, "VermeerChineseUiDxe: Boot%04x 已排在 BootOrder 首位：%r\n", Number, Status));

  FreePool (NewOrder);
  if (Order != NULL) {
    FreePool (Order);
  }

  return Status;
}
