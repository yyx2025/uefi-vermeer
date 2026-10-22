/** @file
  中文启动器的字体接口与几何常量。

  字模由 tools/gen-cjk-font.py 从 GNU Unifont 生成到 VermeerCjkFontData.c，
  只包含 cjk-charset.txt 里列出的字形：ASCII 8x16、汉字 16x16。

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#ifndef VERMEER_CJK_FONT_H_
#define VERMEER_CJK_FONT_H_

#include <Uefi.h>

//
// 字形几何：ASCII 宽 8、汉字宽 16，高度都是 16。渲染时按 scale 整数倍放大。
// 字形总数由生成的 VermeerCjkFontData.c 定义，这里只固定几何与 ASCII 范围。
//
#define VERMEER_FONT_ASCII_FIRST  0x0020
#define VERMEER_FONT_ASCII_LAST   0x007E
#define VERMEER_FONT_ASCII_COUNT  95

#define VERMEER_GLYPH_HEIGHT      16
#define VERMEER_ASCII_WIDTH       8
#define VERMEER_CJK_WIDTH         16

/**
  在字模表里查找一个码点。

  @param[in]  Codepoint  要查找的 Unicode 码点。
  @param[out] Width      字形宽度（8 或 16 像素）。
  @param[out] Stride     每行字节数（1 或 2）。

  @return 指向字形位图的指针；找不到时返回 NULL。
**/
CONST UINT8 *
VermeerFontLookup (
  IN  UINT32  Codepoint,
  OUT UINTN   *Width,
  OUT UINTN   *Stride
  );

/**
  把 UTF-16 字符串转换成码点序列前，先判断这个码点是否属于基本多语言平面。

  界面上只用到 BMP 内的字（汉字和 ASCII），所以不需要代理对处理；
  遇到代理项时按“缺字”处理，渲染成方框。
**/
BOOLEAN
VermeerFontIsBmp (
  IN UINT16  Unit
  );

#endif
