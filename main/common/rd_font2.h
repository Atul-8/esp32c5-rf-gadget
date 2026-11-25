/* 自动生成，勿手改（generator: gen_font2.py）。
 * 真实字形点阵：8x16 正文、16x30 标题。
 * 旧方案是把 5x7 逐像素用 rd_rect 放大，实物上是马赛克，已废弃。 */
#ifndef RD_FONT2_H
#define RD_FONT2_H

#include <stdint.h>

#define RD_FONT2_FIRST 32
#define RD_FONT2_CHARS 91
#define RD_FONT2_SW    10   /* 正文字宽 */
#define RD_FONT2_SH    18   /* 正文行高 */
#define RD_FONT2_BW    16   /* 标题字宽 */
#define RD_FONT2_BH    30   /* 标题行高 */

extern const uint16_t rd_font2_small[RD_FONT2_CHARS][RD_FONT2_SH];
extern const uint16_t rd_font2_big[RD_FONT2_CHARS][RD_FONT2_BH];

#endif
