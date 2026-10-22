/** @file
  Redmi K70（vermeer / SM8550）中文启动器。

  为什么是“应用 + 注册器”而不是一个 DXE 驱动：

    BdsDxe 的 DEPEX 是 TRUE，DXE 调度器无法保证任何一个“也依赖 GOP”的驱动
    一定在 BDS 之前被调度——两者会竞争，界面可能根本不显示，或者和 BDS
    抢屏幕。
    UniXNUDxe 已经解决了同一个问题：它在 DXE 阶段写入一个 Boot#### 选项、
    放到 BootOrder 首位，让 BDS 把应用当成正常启动项来跑。这个平台复用
    那套机制：中文界面是一个 UEFI 应用，由 VermeerChineseUiDxe 注册成
    BDS 的启动项。

  设计约束：

  * 全部界面文字是简体中文，字形来自 VermeerCjkFontData.c 的 16x16 点阵。
  * 只依赖 GOP + ConIn：不碰任何厂商驱动，所以 VERMEER_VENDOR_HW = 0 的
    首启路径也能显示。
  * 只读内存、只画屏、只用 ResetSystem：不改任何分区、不写 flash。
  * 菜单结束后返回 EFI_SUCCESS，BDS 继续它原来的引导顺序。

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <Uefi.h>

#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/UefiApplicationEntryPoint.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>
#include <Library/UefiRuntimeServicesTableLib.h>

#include <Protocol/GraphicsOutput.h>
#include <Protocol/SimpleTextIn.h>

#include "VermeerCjkFont.h"

//
// 布局常量：针对 1440x3200 的竖屏面板（K70 WQHD+）。
//
#define VERMEER_BG_R        0x0B
#define VERMEER_BG_G        0x0C
#define VERMEER_BG_B        0x0F
#define VERMEER_TEXT_R      0xF2
#define VERMEER_TEXT_G      0xF3
#define VERMEER_TEXT_B      0xF5
#define VERMEER_DIM_R       0x7C
#define VERMEER_DIM_G       0x83
#define VERMEER_DIM_B       0x8D
#define VERMEER_ACCENT_R    0x2A
#define VERMEER_ACCENT_G    0x5B
#define VERMEER_ACCENT_B    0xD7
#define VERMEER_ROW_R       0x17
#define VERMEER_ROW_G       0x1A
#define VERMEER_ROW_B       0x20

#define VERMEER_TIMEOUT_SEC  10
#define VERMEER_TICK_MS      200

typedef enum {
  VermeerActionContinue = 0,
  VermeerActionRestart,
  VermeerActionShutdown,
  VermeerActionCount
} VERMEER_ACTION;

typedef struct {
  CONST CHAR16     *Label;
  VERMEER_ACTION   Action;
} VERMEER_ITEM;

//
// 菜单项：第 0 项是默认项，倒计时结束就选它。
//
STATIC VERMEER_ITEM  mItems[] = {
  { L"继续启动", VermeerActionContinue },
  { L"重新启动", VermeerActionRestart  },
  { L"关闭电源", VermeerActionShutdown }
};

#define VERMEER_ITEM_COUNT  (sizeof (mItems) / sizeof (mItems[0]))

STATIC EFI_GRAPHICS_OUTPUT_PROTOCOL  *mGop;
STATIC UINT32                        mWidth;
STATIC UINT32                        mHeight;
STATIC UINTN                         mScale;

// ---------------------------------------------------------------------------
// 颜色与基本绘制
// ---------------------------------------------------------------------------

STATIC
EFI_GRAPHICS_OUTPUT_BLT_PIXEL
Rgb (
  IN UINT8  Red,
  IN UINT8  Green,
  IN UINT8  Blue
  )
{
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Pixel;

  Pixel.Red      = Red;
  Pixel.Green    = Green;
  Pixel.Blue     = Blue;
  Pixel.Reserved = 0;
  return Pixel;
}

STATIC
VOID
FillRect (
  IN UINTN                          X,
  IN UINTN                          Y,
  IN UINTN                          Width,
  IN UINTN                          Height,
  IN EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Color
  )
{
  if ((Width == 0) || (Height == 0) || (X >= mWidth) || (Y >= mHeight)) {
    return;
  }

  Width  = MIN (Width, (UINTN)mWidth - X);
  Height = MIN (Height, (UINTN)mHeight - Y);
  mGop->Blt (mGop, &Color, EfiBltVideoFill, 0, 0, X, Y, Width, Height, 0);
}

// ---------------------------------------------------------------------------
// 文本
// ---------------------------------------------------------------------------

STATIC
UINTN
GlyphAdvance (
  IN UINT32  Codepoint
  )
{
  UINTN  Width;
  UINTN  Stride;

  if (VermeerFontLookup (Codepoint, &Width, &Stride) != NULL) {
    return Width * mScale;
  }

  //
  // 缺字：按汉字宽度留位，渲染成空心方框，避免文字挤在一起。
  //
  return VERMEER_CJK_WIDTH * mScale;
}

STATIC
UINTN
MeasureText (
  IN CONST CHAR16  *Text
  )
{
  UINTN  Total;

  Total = 0;
  while (*Text != L'\0') {
    Total += GlyphAdvance ((UINT32)*Text);
    Text++;
  }

  return Total;
}

/**
  在 (X, Y) 处绘制一行文字，左上角对齐。

  @param[in] MaxWidth  可用宽度；文字超出时按字符截断。
  @return 实际绘制宽度。
**/
STATIC
UINTN
DrawText (
  IN UINTN                          X,
  IN UINTN                          Y,
  IN CONST CHAR16                   *Text,
  IN EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Color,
  IN UINTN                          MaxWidth
  )
{
  UINTN                          PenX;
  UINTN                          Width;
  UINTN                          Stride;
  UINTN                          Row;
  UINTN                          Col;
  UINTN                          PixelX;
  UINTN                          PixelY;
  CONST UINT8                    *Glyph;
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Box;

  PenX = X;
  while (*Text != L'\0') {
    Glyph = VermeerFontLookup ((UINT32)*Text, &Width, &Stride);
    if (Glyph == NULL) {
      //
      // 缺字占位：画一个描边方框。
      //
      Box    = Color;
      PixelX = PenX + mScale;
      PixelY = Y + mScale;
      FillRect (PixelX, PixelY, VERMEER_CJK_WIDTH * mScale - 2 * mScale, mScale, Box);
      FillRect (
        PixelX,
        PixelY + VERMEER_GLYPH_HEIGHT * mScale - 2 * mScale,
        VERMEER_CJK_WIDTH * mScale - 2 * mScale,
        mScale,
        Box
        );
      PenX += VERMEER_CJK_WIDTH * mScale;
      Text++;
      continue;
    }

    if ((MaxWidth > 0) && (PenX + Width * mScale > X + MaxWidth)) {
      break;
    }

    for (Row = 0; Row < VERMEER_GLYPH_HEIGHT; Row++) {
      for (Col = 0; Col < Width; Col++) {
        UINT8  Byte;

        Byte = Glyph[Row * Stride + (Col / 8)];
        if ((Byte & (0x80 >> (Col % 8))) == 0) {
          continue;
        }

        FillRect (PenX + Col * mScale, Y + Row * mScale, mScale, mScale, Color);
      }
    }

    PenX += Width * mScale;
    Text++;
  }

  return PenX - X;
}

STATIC
VOID
DrawTextCentered (
  IN UINTN                          Left,
  IN UINTN                          Width,
  IN UINTN                          Y,
  IN CONST CHAR16                   *Text,
  IN EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Color
  )
{
  UINTN  TextWidth;

  TextWidth = MeasureText (Text);
  if (TextWidth > Width) {
    DrawText (Left, Y, Text, Color, Width);
    return;
  }

  DrawText (Left + (Width - TextWidth) / 2, Y, Text, Color, Width);
}

// ---------------------------------------------------------------------------
// 界面
// ---------------------------------------------------------------------------

/**
  画出整个界面。Selected 是当前高亮项，RemainingSec < 0 表示不显示倒计时。
**/
STATIC
VOID
DrawScreen (
  IN UINTN  Selected,
  IN INTN   RemainingSec
  )
{
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Background;
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Text;
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Dim;
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Accent;
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Row;
  UINTN                          Margin;
  UINTN                          Inner;
  UINTN                          PenY;
  UINTN                          LineHeight;
  UINTN                          Index;
  CHAR16                         Buffer[64];

  Background = Rgb (VERMEER_BG_R, VERMEER_BG_G, VERMEER_BG_B);
  Text       = Rgb (VERMEER_TEXT_R, VERMEER_TEXT_G, VERMEER_TEXT_B);
  Dim        = Rgb (VERMEER_DIM_R, VERMEER_DIM_G, VERMEER_DIM_B);
  Accent     = Rgb (VERMEER_ACCENT_R, VERMEER_ACCENT_G, VERMEER_ACCENT_B);
  Row        = Rgb (VERMEER_ROW_R, VERMEER_ROW_G, VERMEER_ROW_B);

  Margin     = mWidth / 12;
  Inner      = mWidth - 2 * Margin;
  LineHeight = VERMEER_GLYPH_HEIGHT * mScale;

  FillRect (0, 0, mWidth, mHeight, Background);

  //
  // 标题
  //
  PenY = mHeight / 8;
  DrawTextCentered (Margin, Inner, PenY, L"红米 K70 中文启动器", Text);
  PenY += LineHeight + mScale * 4;

  DrawTextCentered (Margin, Inner, PenY, L"维米尔 UEFI 研究固件", Dim);
  PenY += LineHeight + mScale * 10;

  //
  // 标题下的强调线
  //
  FillRect (mWidth / 2 - Inner / 8, PenY, Inner / 4, MAX ((UINTN)2, mScale), Accent);
  PenY += mScale * 16;

  //
  // 菜单项：每项一个卡片，高亮项带底色和左侧色条。
  //
  for (Index = 0; Index < VERMEER_ITEM_COUNT; Index++) {
    UINTN  CardHeight;
    UINTN  CardY;

    CardHeight = LineHeight + mScale * 14;
    CardY      = PenY;

    if (Index == Selected) {
      FillRect (Margin / 2, CardY, mWidth - Margin, CardHeight, Row);
      FillRect (Margin / 2, CardY, MAX ((UINTN)3, mScale * 2), CardHeight, Accent);
    }

    DrawTextCentered (
      Margin,
      Inner,
      CardY + mScale * 7,
      mItems[Index].Label,
      (Index == Selected) ? Text : Dim
      );
    PenY = CardY + CardHeight + mScale * 6;
  }

  //
  // 页脚：倒计时与按键说明
  //
  PenY = mHeight - mHeight / 10;

  if (RemainingSec >= 0) {
    UnicodeSPrint (Buffer, sizeof (Buffer), L"%d 秒后自动启动", (UINT32)RemainingSec);
    DrawTextCentered (Margin, Inner, PenY, Buffer, Dim);
  } else {
    DrawTextCentered (Margin, Inner, PenY, L"按音量键选择 按电源键确认", Dim);
  }

  PenY += LineHeight + mScale * 4;
  DrawTextCentered (Margin, Inner, PenY, L"此固件不会修改任何分区", Dim);
}

// ---------------------------------------------------------------------------
// 输入与计时
// ---------------------------------------------------------------------------

STATIC
VOID
DoAction (
  IN VERMEER_ACTION  Action
  )
{
  switch (Action) {
    case VermeerActionRestart:
      gRT->ResetSystem (EfiResetCold, EFI_SUCCESS, 0, NULL);
      break;
    case VermeerActionShutdown:
      gRT->ResetSystem (EfiResetShutdown, EFI_SUCCESS, 0, NULL);
      break;
    default:
      break;
  }
}

/**
  菜单主循环。

  @retval EFI_SUCCESS  用户选了“继续启动”，调用方把控制权交回固件。
**/
STATIC
EFI_STATUS
RunMenu (
  VOID
  )
{
  EFI_STATUS     Status;
  EFI_EVENT      Timer;
  EFI_EVENT      Events[2];
  UINTN          EventCount;
  UINTN          Signaled;
  EFI_INPUT_KEY  Key;
  UINTN          Selected;
  INTN           RemainingSec;
  UINTN          TicksPerSecond;
  UINTN          TicksLeftInSecond;
  UINTN          Index;
  BOOLEAN        UserActive;

  Selected      = 0;
  RemainingSec  = VERMEER_TIMEOUT_SEC;
  UserActive    = FALSE;

  //
  // 定时器每 VERMEER_TICK_MS 触发一次；累加成整秒，倒计时才准。
  //
  TicksPerSecond    = (1000 + VERMEER_TICK_MS - 1) / VERMEER_TICK_MS;
  TicksLeftInSecond = TicksPerSecond;

  DrawScreen (Selected, RemainingSec);

  Status = gBS->CreateEvent (EVT_TIMER, TPL_CALLBACK, NULL, NULL, &Timer);
  if (EFI_ERROR (Status)) {
    //
    // 没有定时器就直接返回，让固件按默认顺序继续。
    //
    return EFI_SUCCESS;
  }

  gBS->SetTimer (Timer, TimerPeriodic, EFI_TIMER_PERIOD_MILLISECONDS (VERMEER_TICK_MS));

  EventCount = 0;
  Events[EventCount++] = Timer;
  if ((gST->ConIn != NULL) && (gST->ConIn->WaitForKey != NULL)) {
    Events[EventCount++] = gST->ConIn->WaitForKey;
  }

  for ( ; ;) {
    Status = gBS->WaitForEvent (EventCount, Events, &Signaled);
    if (EFI_ERROR (Status)) {
      break;
    }

    if (Signaled == 0) {
      //
      // 定时器：满一秒减一；减到 0 就按默认项继续。
      //
      if (TicksLeftInSecond > 0) {
        TicksLeftInSecond--;
      }

      if (TicksLeftInSecond == 0) {
        TicksLeftInSecond = TicksPerSecond;
        RemainingSec--;
        if (RemainingSec <= 0) {
          break;
        }

        DrawScreen (Selected, RemainingSec);
      }

      continue;
    }

    if (EFI_ERROR (gST->ConIn->ReadKeyStroke (gST->ConIn, &Key))) {
      continue;
    }

    //
    // 第一次按键：接管控制，关掉倒计时；高亮回到默认项。
    //
    if (!UserActive) {
      UserActive   = TRUE;
      RemainingSec = -1;
      Selected     = 0;
    }

    if ((Key.ScanCode == SCAN_UP) || (Key.UnicodeChar == L'k')) {
      Selected = (Selected + VERMEER_ITEM_COUNT - 1) % VERMEER_ITEM_COUNT;
      DrawScreen (Selected, RemainingSec);
    } else if ((Key.ScanCode == SCAN_DOWN) || (Key.UnicodeChar == L'j')) {
      Selected = (Selected + 1) % VERMEER_ITEM_COUNT;
      DrawScreen (Selected, RemainingSec);
    } else if ((Key.UnicodeChar == CHAR_CARRIAGE_RETURN) || (Key.UnicodeChar == CHAR_LINEFEED) ||
               (Key.UnicodeChar == L' ') || (Key.ScanCode == SCAN_SUSPEND))
    {
      DoAction (mItems[Selected].Action);
      //
      // 只有“继续启动”会走到这里。
      //
      if (mItems[Selected].Action == VermeerActionContinue) {
        break;
      }
    } else if ((Key.UnicodeChar >= L'1') && (Key.UnicodeChar <= L'9')) {
      Index = Key.UnicodeChar - L'1';
      if (Index < VERMEER_ITEM_COUNT) {
        DoAction (mItems[Index].Action);
        if (mItems[Index].Action == VermeerActionContinue) {
          break;
        }
      }
    }
  }

  gBS->SetTimer (Timer, TimerCancel, 0);
  gBS->CloseEvent (Timer);
  return EFI_SUCCESS;
}

EFI_STATUS
EFIAPI
VermeerChineseUiEntry (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_STATUS  Status;

  //
  // 引导界面要等人，不能让 BDS 的 5 分钟看门狗复位机器。
  //
  gBS->SetWatchdogTimer (0, 0, 0, NULL);

  Status = gBS->LocateProtocol (&gEfiGraphicsOutputProtocolGuid, NULL, (VOID **)&mGop);
  if (EFI_ERROR (Status) || (mGop == NULL) || (mGop->Mode == NULL)) {
    //
    // 没有 GOP 就什么都不画，直接回到 BDS——不要因为没屏幕就卡住引导。
    //
    DEBUG ((DEBUG_ERROR, "VermeerChineseUi: 没有 GOP (%r)，跳过界面\n", Status));
    return EFI_SUCCESS;
  }

  mWidth  = mGop->Mode->Info->HorizontalResolution;
  mHeight = mGop->Mode->Info->VerticalResolution;

  //
  // 1440 宽的屏用 scale 3 刚好；分辨率变了按宽度自适应，范围 1..4。
  //
  mScale = mWidth / 480;
  mScale = MIN (MAX (mScale, (UINTN)1), (UINTN)4);

  DEBUG ((
    DEBUG_INFO,
    "VermeerChineseUi: GOP %ux%u format %u, scale %u\n",
    mWidth,
    mHeight,
    mGop->Mode->Info->PixelFormat,
    mScale
    ));

  RunMenu ();

  //
  // 回到 BDS，让它继续原来的引导顺序。
  //
  return EFI_SUCCESS;
}
