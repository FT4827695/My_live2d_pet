"""
ui.py
聊天窗口的 UI 渲染与命中测试。
"""
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Any, Dict
from PIL import Image, ImageDraw, ImageFont
import re
from services import TextUtils
from .input_box import InputBox
from .scroll_manager import ScrollManager
from .viewmodels import ChatViewModel
from .docs_uploader import DocsUploader
from prompt import get_character_name
import bisect

CURSOR_Y_OFFSET = 8
MSG_TEXT_X = 24
MSG_AREA_Y_PAD = 5

def compute_header_rects(width: int, scale: float) -> Dict[str, Any]:
    margin    = int(12 * scale)                      # 木框宽
    frame_off = 3                                    # 内框线相对 margin 的偏移
    frame_w   = 2                                    # 内框线线宽
    gap       = max(1, round(1 * scale))             # 按钮与内框线的安全间隙
    inset     = margin + frame_off + frame_w + gap   # 按钮顶/右锚点
    btn       = max(14, int(30 * scale))             # 按钮边长（下限保护）
    spacing   = max(2, int(5 * scale))               # 两按钮间距
    close_x   = width - inset - btn
    close_y   = inset
    gear_x    = close_x - btn - spacing
    return {
        "margin":    margin,
        "frame_off": frame_off,
        "frame_w":   frame_w,
        "btn":       btn,
        "spacing":   spacing,
        "close":     (close_x, close_y, btn, btn),
        "gear":      (gear_x,  close_y, btn, btn),
    }

_UI_CHARACTER_NAME = get_character_name()

def refresh_character_name():
    global _UI_CHARACTER_NAME
    _UI_CHARACTER_NAME = get_character_name()

@dataclass(frozen=True)
class RenderContext:
    # 窗口尺寸
    width: int
    height: int
    scale: float
    # 字体（全局共享）
    font: ImageFont.FreeTypeFont
    font_sizes: dict
    # 子模块
    input_box: InputBox
    scroll_manager: ScrollManager
    vm: ChatViewModel
    doc_uploader: DocsUploader
    # 状态
    selected_messages: set
    recording_status_text: str
    context_menu: dict          # 由 MenuManager 提供
    settings_menu: dict         # 由 MenuManager 提供（菜单A）
    settings_menu_b: Optional[dict] = None   # 由 MenuManager 提供（菜单B，语音设置）
    hovered_menu_item: Optional[Tuple[str, Any]] = None  # (menu_type, item_id)
    # 布局常量（由窗口计算好传入）
    msg_top: int = 0
    input_h: int = 0
    msg_bottom_margin: int = 0
    input_y_offset: int = 0
    input_line_spacing: int = 0
    text_margin: int = 0
    send_btn_width: int = 0
    msg_line_h: int = 0
    input_visible_lines: int = 0
    fixed_ascent: int = 0           # 文本基线偏移量（用于输入框光标位置）
    fixed_descent: int = 0
    selecting: bool = False
    msg_text_selection: Optional[Tuple] = None
    layout_snapshot: Optional[Any] = None
    character_editor: Optional[Any] = None

    def get_text_width(self, text: str) -> float:
        return self.font.getlength(text) if hasattr(self.font, 'getlength') else self.font.getsize(text)[0]

@dataclass
class RenderResult:
    cursor_rect: Optional[Tuple[int, int, int, int]]  # (x, y, width, height) 光标矩形，None 表示不绘制


class ChatUIRenderer:
    def __init__(self, get_font_func):
        self.get_font = get_font_func
        self._static_cache = None
        self._static_key = None

    # ---------- 静态背景层 ----------
    def _render_static(self, ctx: RenderContext) -> Image.Image:
        img = Image.new('RGBA', (ctx.width, ctx.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        board_black = (20, 30, 20, 255)
        wood_color = (210, 180, 140, 255)
        hdr = compute_header_rects(ctx.width, ctx.scale)
        margin = hdr["margin"]
        fo, fw = hdr["frame_off"], hdr["frame_w"]
        
        # 外边框
        draw.rounded_rectangle([0, 0, ctx.width-1, ctx.height-1], radius=6,
                               fill=wood_color, outline=(60,40,20,255), width=2)
        # 内黑底
        draw.rounded_rectangle([margin, margin, ctx.width-margin, ctx.height-margin],
                               radius=10, fill=board_black)
        draw.rounded_rectangle([margin+fo, margin+fo, ctx.width-margin-fo, ctx.height-margin-fo],
                               radius=8, outline=(255,255,255,255), width=fw)
        
        # 标题
        title = f"{_UI_CHARACTER_NAME or '雫'}的小黑板"
        title_font = self.get_font(ctx.font_sizes['title'])
        bbox = title_font.getbbox(title)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        title_x = (ctx.width - tw) // 2
        title_y = int(18 * ctx.scale)
        draw.text((title_x, title_y), title, font=title_font, fill=(255,255,255,255))

        line_y = title_y + th + int(12 * ctx.scale)
        draw.line([(int(40 * ctx.scale), line_y), (ctx.width - int(40 * ctx.scale), line_y)],
                  fill=(255,255,255,255), width=int(2 * ctx.scale))

        # 关闭按钮
        close_x, close_y, close_size, _ = hdr["close"]
        gear_x, gear_y, gear_size, _ = hdr["gear"]
        draw.rounded_rectangle([close_x, close_y, close_x+close_size, close_y+close_size],
                               radius=int(5 * ctx.scale), fill=(70,70,100,230),
                               outline=(200,200,255,100), width=max(1, int(1 * ctx.scale)),)
        close_text = "X"
        cf_font = self.get_font(ctx.font_sizes['close'])
        bbox_c = cf_font.getbbox(close_text)
        cw = bbox_c[2] - bbox_c[0]
        ch = bbox_c[3] - bbox_c[1]
        text_x = close_x + (close_size - cw)//2
        text_y = close_y + (close_size - ch)//2 - int(6 * ctx.scale)
        draw.text((text_x, text_y), close_text, font=cf_font, fill=(255,100,100,255))

        # 齿轮按钮（设置菜单入口）
        close_x, close_y, close_size, _ = hdr["close"]
        gear_x, gear_y, gear_size, _ = hdr["gear"]
        draw.rounded_rectangle([gear_x, gear_y, gear_x+gear_size, gear_y+gear_size],
                               radius=int(5 * ctx.scale), fill=(70,70,100,230),
                               outline=(200,200,255,100), width=max(1, int(1 * ctx.scale)),)
        cx = gear_x + gear_size // 2
        cy = gear_y + gear_size // 2
        r_outer = int(10 * ctx.scale)
        r_inner = int(5 * ctx.scale)
        tooth_color = (255, 255, 255, 255)
        bg_color = (70, 70, 100, 230)
        draw.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], fill=tooth_color)
        t_w, t_h = int(4 * ctx.scale), int(6 * ctx.scale)
        draw.rectangle([cx - t_w//2, cy - r_outer - int(3 * ctx.scale),
                        cx + t_w//2, cy - r_outer + int(2 * ctx.scale)], fill=tooth_color)
        draw.rectangle([cx - t_w//2, cy + r_outer - int(2 * ctx.scale),
                        cx + t_w//2, cy + r_outer + int(3 * ctx.scale)], fill=tooth_color)
        draw.rectangle([cx - r_outer - int(3 * ctx.scale), cy - t_w//2,
                        cx - r_outer + int(2 * ctx.scale), cy + t_w//2], fill=tooth_color)
        draw.rectangle([cx + r_outer - int(2 * ctx.scale), cy - t_w//2,
                        cx + r_outer + int(3 * ctx.scale), cy + t_w//2], fill=tooth_color)
        offset = int(r_outer * 0.7)
        t_size = int(5 * ctx.scale)
        draw.rectangle([cx - offset - t_size//2, cy - offset - t_size//2,
                        cx - offset + t_size//2, cy - offset + t_size//2], fill=tooth_color)
        draw.rectangle([cx + offset - t_size//2, cy - offset - t_size//2,
                        cx + offset + t_size//2, cy - offset + t_size//2], fill=tooth_color)
        draw.rectangle([cx - offset - t_size//2, cy + offset - t_size//2,
                        cx - offset + t_size//2, cy + offset + t_size//2], fill=tooth_color)
        draw.rectangle([cx + offset - t_size//2, cy + offset - t_size//2,
                        cx + offset + t_size//2, cy + offset + t_size//2], fill=tooth_color)
        draw.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], fill=bg_color)

        # 输入框背景（白色纸）
        input_y = ctx.height - ctx.input_h - ctx.input_y_offset
        draw.rectangle((int(12 * ctx.scale), input_y,
                        ctx.width - int(12 * ctx.scale), input_y + ctx.input_h),
                       fill=(245, 240, 225, 245), outline=(180,170,140,255), width=int(2 * ctx.scale))

        # 发送按钮
        btn_w = int(56 * ctx.scale)
        btn_h = int(38 * ctx.scale)
        btn_x = ctx.width - btn_w - int(16 * ctx.scale)
        btn_y = input_y + (ctx.input_h - btn_h) // 2 - int(24 * ctx.scale)
        draw.rectangle((btn_x, btn_y, btn_x+btn_w, btn_y+btn_h),
                       fill=(80,120,200), outline=(200,200,255), width=int(1 * ctx.scale))
        btn_text = "发送"
        bbox_btn = ctx.font.getbbox(btn_text)
        tw_btn = bbox_btn[2] - bbox_btn[0]
        th_btn = bbox_btn[3] - bbox_btn[1]
        text_x = btn_x + (btn_w - tw_btn)//2
        text_y = btn_y + (btn_h - th_btn)//2 - int(6 * ctx.scale)
        draw.text((text_x, text_y), btn_text, font=ctx.font, fill=(255,255,255))

        return img

    # ---------- 通用下拉菜单渲染（菜单A / 菜单B 复用） ----------
    def _render_dropdown_menu(self, draw, ctx: RenderContext, menu: dict,
                              menu_type: str, menu_font: ImageFont.ImageFont,
                              item_h: int, menu_w: int, sep_h: int) -> None:
        if not menu.get('visible'):
            return
        mx, my = menu['x'], menu['y']
        total_h = int(10 * ctx.scale)
        for item in menu['items']:
            total_h += sep_h if str(item['id']).startswith('sep') else item_h
        # 边界修正
        if mx + menu_w > ctx.width:
            mx = ctx.width - menu_w - int(5 * ctx.scale)
        if my + total_h > ctx.height:
            my = ctx.height - total_h - int(5 * ctx.scale)
        # 阴影 + 主体
        draw.rectangle((mx+3, my+3, mx+menu_w+3, my+total_h+3), fill=(0,0,0,80))
        draw.rectangle((mx, my, mx + menu_w, my + total_h),
                       fill=(250, 245, 230, 255), outline=(160, 150, 130, 255),
                       width=int(1 * ctx.scale))
        curr_y = my + int(5 * ctx.scale)
        menu['rects'] = []
        for item in menu['items']:
            if str(item['id']).startswith('sep'):
                draw.line([(mx + int(5 * ctx.scale), curr_y + int(5 * ctx.scale)),
                           (mx + menu_w - int(5 * ctx.scale), curr_y + int(5 * ctx.scale))],
                          fill=(180,170,150,255), width=int(1 * ctx.scale))
                curr_y += sep_h
                menu['rects'].append(None)
            else:
                rect = (mx, curr_y, mx + menu_w, curr_y + item_h)
                menu['rects'].append(rect)
                is_hovered = (ctx.hovered_menu_item is not None
                              and ctx.hovered_menu_item[0] == menu_type
                              and ctx.hovered_menu_item[1] == item['id'])
                if is_hovered and item.get('enabled', True):
                    draw.rectangle(rect, fill=(200, 210, 240, 200))
                text_color = (40, 40, 30, 255) if item.get('enabled', True) else (160, 160, 150, 255)
                draw.text((mx + int(20 * ctx.scale), curr_y + int(5 * ctx.scale)),
                          item['text'], font=menu_font, fill=text_color)
                curr_y += item_h

    # ---------- 动态层渲染 ----------
    def _render_dynamic(self, ctx: RenderContext, img: Image.Image) -> RenderResult:
        draw = ImageDraw.Draw(img)

        # 消息区域
        msg_bottom = ctx.height - ctx.input_h - ctx.msg_bottom_margin
        y_off = ctx.msg_top + int(5 * ctx.scale)
        msg_text_x = int(MSG_TEXT_X * ctx.scale)
        available_width = ctx.width - ctx.text_margin

        sel = ctx.msg_text_selection
        stats_size = max(10, int(ctx.font_sizes['message'] * 0.78))
        stats_font = self.get_font(stats_size)

        layout = getattr(ctx, "layout_snapshot", None)
        stop_render = False

        if layout is not None and not layout.is_empty():
            # ==================== 使用布局缓存的虚拟化渲染 ====================
            total_lines = layout.total_lines

            max_visible = max(0, int((msg_bottom - y_off) // ctx.msg_line_h))
            start_line = max(0, min(ctx.scroll_manager.scroll_offset, total_lines))
            end_line = min(total_lines, start_line + max_visible)

            if end_line > start_line:
                start_msg = max(0, bisect.bisect_right(layout.prefix, start_line) - 1)

                for idx in range(start_msg, len(layout.entries)):
                    msg_start_line = layout.prefix[idx]

                    if msg_start_line >= end_line:
                        break

                    entry = layout.entries[idx]
                    sender = entry.sender

                    is_stats = (sender == "stats")
                    is_selected = idx in ctx.selected_messages

                    skip = max(0, start_line - msg_start_line)

                    for line_in_msg in range(skip, entry.line_count):
                        if y_off + ctx.msg_line_h > msg_bottom:
                            stop_render = True
                            break

                        line = entry.lines[line_in_msg]

                        clean_line = (
                            line.replace('\n', '')
                            .replace('\r', '')
                            .replace('\x0b', '')
                            .replace('\x0c', '')
                        )
                        clean_line = re.sub(r'[\u2028\u2029\u0085]', '', clean_line)

                        # ---- 整条消息选中高亮 ----
                        if is_selected:
                            if clean_line.strip():
                                line_w = ctx.get_text_width(clean_line)
                                draw.rectangle(
                                    (
                                        msg_text_x,
                                        y_off,
                                        msg_text_x + line_w + int(10 * ctx.scale),
                                        y_off + ctx.msg_line_h,
                                    ),
                                    fill=(255, 240, 180, 100),
                                )
                            else:
                                draw.rectangle(
                                    (
                                        msg_text_x,
                                        y_off,
                                        ctx.width - msg_text_x,
                                        y_off + ctx.msg_line_h,
                                    ),
                                    fill=(255, 240, 180, 100),
                                )

                        # ---- 字符选区高亮 ----
                        if sel is not None and clean_line:
                            (s_msg, s_line, s_char), (e_msg, e_line, e_char) = sel
                            cur = (idx, line_in_msg)

                            if (s_msg, s_line) <= cur <= (e_msg, e_line):
                                if cur == (s_msg, s_line) and cur == (e_msg, e_line):
                                    hl_start, hl_end = s_char, e_char
                                elif cur == (s_msg, s_line):
                                    hl_start, hl_end = s_char, len(clean_line)
                                elif cur == (e_msg, e_line):
                                    hl_start, hl_end = 0, e_char
                                else:
                                     hl_start, hl_end = 0, len(clean_line)

                                if hl_start < hl_end:
                                    prefix_w = ctx.get_text_width(clean_line[:hl_start])
                                    hl_w = ctx.get_text_width(clean_line[hl_start:hl_end])
                                    draw.rectangle(
                                        (
                                            msg_text_x + prefix_w,
                                            y_off,
                                            msg_text_x + prefix_w + hl_w,
                                            y_off + ctx.msg_line_h,
                                        ),
                                        fill=(100, 200, 255, 180),
                                    )

                        # ---- 绘制文本 ----
                        if clean_line.strip():
                            if is_stats:
                                draw.text(
                                    (
                                        msg_text_x + int(2 * ctx.scale),
                                        y_off + int(3 * ctx.scale),
                                    ),
                                    clean_line,
                                    font=stats_font,
                                    fill=(150, 162, 150, 215),
                                )
                            else:
                                text_color = (
                                    (255, 255, 200)
                                    if sender == "user"
                                    else (200, 240, 255)
                                )
                                draw.text(
                                    (msg_text_x, y_off),
                                    clean_line,
                                    font=ctx.font,
                                    fill=text_color,
                                )

                        y_off += ctx.msg_line_h

                    if stop_render:
                        break

        # 滚动条绘制
        need_scrollbar = False
        total_lines = 0
        max_visible = 0

        if layout is not None and not layout.is_empty():
            scrollbar_bottom = ctx.height - ctx.input_h - ctx.msg_bottom_margin
            scrollbar_top = ctx.msg_top + int(2 * ctx.scale)
            scrollbar_visible_h = scrollbar_bottom - scrollbar_top

            max_visible = max(1, int(scrollbar_visible_h // ctx.msg_line_h))
            total_lines = layout.total_lines
            need_scrollbar = total_lines > max_visible
        else:
            need_scrollbar = ctx.scroll_manager._need_scrollbar()
            if need_scrollbar:
                total_lines = ctx.scroll_manager._get_total_lines()
                max_visible = ctx.scroll_manager._get_max_visible_lines()

        if (
            ctx.scroll_manager.scrollbar_visible or ctx.scroll_manager.dragging_scroll
        ) and need_scrollbar:
            scrollbar_width = int(10 * ctx.scale)
            scrollbar_margin = int(8 * ctx.scale)
            scrollbar_x = ctx.width - scrollbar_margin - scrollbar_width
            scrollbar_y = ctx.msg_top + int(2 * ctx.scale)
            scrollbar_h = (
                ctx.height - ctx.input_h - ctx.msg_bottom_margin
            ) - ctx.msg_top - int(4 * ctx.scale)

            max_offset = max(0, total_lines - max_visible)

            thumb_h = max(
                int(20 * ctx.scale),
                (max_visible / max(1, total_lines)) * scrollbar_h,
            )

            if max_offset > 0:
                thumb_y = scrollbar_y + (
                    ctx.scroll_manager.scroll_offset / max_offset
                ) * (scrollbar_h - thumb_h)
            else:
                thumb_y = scrollbar_y

            ctx.scroll_manager.scrollbar_rect = (
                scrollbar_x,
                scrollbar_y,
                scrollbar_width,
                scrollbar_h,
                )
            ctx.scroll_manager.thumb_rect = (
                scrollbar_x,
                thumb_y,
                scrollbar_width,
                thumb_h,
            )

            draw.rectangle(
                (
                    scrollbar_x,
                    scrollbar_y,
                    scrollbar_x + scrollbar_width,
                    scrollbar_y + scrollbar_h,
                ),
                fill=(200, 200, 200, 80),
            )
            draw.rectangle(
                (
                    scrollbar_x,
                    thumb_y,
                    scrollbar_x + scrollbar_width,
                    thumb_y + thumb_h,
                ),      
                fill=(255, 255, 255, 200),
            )   
        # ---------- 输入框文本和光标 ----------
        input_y = ctx.height - ctx.input_h - ctx.input_y_offset
        input_h = ctx.input_h
        if (ctx.fixed_ascent + ctx.fixed_descent) > 0:
            ascent = ctx.fixed_ascent
            descent = ctx.fixed_descent
        else:
            try:
                bbox = ctx.font.getbbox("中Ag")
                ascent = max(0, -bbox[1])
                descent = max(0, bbox[3] - bbox[1] - ascent)
            except Exception:
                ascent = int(18 * ctx.scale)
                descent = int(5 * ctx.scale)
        # 文字本身高度
        text_h = ascent + descent
        # 行高 = 文字高度 + 行间距
        line_h = text_h + ctx.input_line_spacing
        caret_inset = max(0, int(1 * ctx.scale))
        selection_inset = max(0, int(1 * ctx.scale))
        start = ctx.input_box.scroll_offset
        end = min(len(ctx.input_box.lines), start + ctx.input_visible_lines)
        visible_lines = end - start
        total_text_h = visible_lines * line_h
        y_center_offset = (input_h - total_text_h) // 2
        # 选择区域
        sel_start = ctx.input_box.selection_start
        sel_end = ctx.input_box.selection_end
        if sel_start is not None and sel_end is not None:
            if sel_start > sel_end:
                sel_start, sel_end = sel_end, sel_start
        else:
            sel_start = sel_end = None
        cursor_rect = None  # 最终返回
        text_left = int(15 * ctx.scale)
        for i in range(start, end):
            line = ctx.input_box.lines[i]
            # 当前行的整行顶部
            row_top = input_y + y_center_offset + (i - start) * line_h
            # 文字在行内垂直居中
            text_top = row_top + max(0, (line_h - text_h) // 2)
            # 绘制文本使用的基线/顶部坐标
            y_baseline = text_top + ascent
            line_abs_start = ctx.input_box.get_line_abs_start(i)
            line_abs_end = line_abs_start + len(line)
            segments = []
            if sel_start is not None and sel_end is not None and sel_start < sel_end:
                seg1_end = max(line_abs_start, min(line_abs_end, sel_start))
                if seg1_end > line_abs_start:
                    segments.append((line_abs_start, seg1_end, False))
                seg2_start = max(line_abs_start, sel_start)
                seg2_end = min(line_abs_end, sel_end)
                if seg2_end > seg2_start:
                    segments.append((seg2_start, seg2_end, True))
                seg3_start = max(line_abs_start, sel_end)
                if seg3_start < line_abs_end:
                    segments.append((seg3_start, line_abs_end, False))
            else:
                segments.append((line_abs_start, line_abs_end, False))
            x_offset = text_left
            for seg_start, seg_end, is_selected in segments:
                sub_text = line[seg_start - line_abs_start : seg_end - line_abs_start]
                if not sub_text:
                    continue
                w = ctx.get_text_width(sub_text)
                if is_selected:
                    draw.rectangle(
                        (x_offset, row_top, x_offset + w, row_top + line_h),
                        fill=(30, 100, 255, 160)
                    )
                    text_color = (255, 255, 255)
                else:
                    text_color = (40, 40, 30)
                draw.text((x_offset, y_baseline), sub_text, font=ctx.font, fill=text_color)
                x_offset += w
            # 光标位置（在光标行）
            if i == ctx.input_box.cursor_line:
                prefix = line[:ctx.input_box.cursor_col]
                cursor_x = text_left + ctx.get_text_width(prefix)
                # 光标覆盖整行，y 为行顶部，高度为行高
                cursor_y = row_top + CURSOR_Y_OFFSET * ctx.scale
                cursor_h = ascent + descent
                cursor_rect = (int(cursor_x), int(cursor_y), 0, int(cursor_h))

        # 文档上传按钮
        upload_btn_w = int(56 * ctx.scale)
        upload_btn_h = int(38 * ctx.scale)
        # 发送按钮位置
        btn_w = int(56 * ctx.scale)
        btn_h = int(38 * ctx.scale)
        btn_x = ctx.width - btn_w - int(16 * ctx.scale)
        btn_y = input_y + (ctx.input_h - btn_h) // 2 - int(24 * ctx.scale)
        upload_btn_x = ctx.width - upload_btn_w - int(16 * ctx.scale)
        upload_btn_y = btn_y + btn_h + int(11 * ctx.scale)
        # 设置 doc_uploader 的矩形
        ctx.doc_uploader.upload_rect = (upload_btn_x, upload_btn_y, upload_btn_w, upload_btn_h)
        ctx.doc_uploader.draw(draw, ctx.width, ctx.height, self.get_font)

        # 录音状态文字
        if ctx.recording_status_text:
            status_font = self.get_font(ctx.font_sizes['status'])
            bbox = status_font.getbbox(ctx.recording_status_text)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x_status = (ctx.width - tw) // 2
            y_status = ctx.height - int(45 * ctx.scale)
            draw.text((x_status, y_status), ctx.recording_status_text,
                      font=status_font, fill=(255, 100, 100, 255))

        # ---------- 菜单渲染（通用方法复用） ----------
        menu_font = self.get_font(ctx.font_sizes['menu'])
        settings_menu_w = int(
            190 * ctx.scale + 2 * ctx.font_sizes.get('menu', 24)
        )

        # 上下文菜单（右键菜单，尺寸不同）
        if ctx.context_menu.get('visible'):
            menu = ctx.context_menu
            mx, my = menu['x'], menu['y']
            item_h = int(34 * ctx.scale)
            sep_h = int(14 * ctx.scale)
            menu_w = int(130 * ctx.scale)
            total_h = int(10 * ctx.scale)
            for item in menu['items']:
                total_h += sep_h if item['id'] == 'sep' else item_h
            if mx + menu_w > ctx.width:
                mx = ctx.width - menu_w - int(5 * ctx.scale)
            if my + total_h > ctx.height:
                my = ctx.height - total_h - int(5 * ctx.scale)
            draw.rectangle((mx+3, my+3, mx+menu_w+3, my+total_h+3), fill=(0,0,0,80))
            draw.rectangle((mx, my, mx+menu_w, my+total_h),
                           fill=(250, 245, 230, 255), outline=(160,150,130,255), width=int(1 * ctx.scale))
            curr_y = my + int(5 * ctx.scale)
            menu['rects'] = []
            for item in menu['items']:
                if item['id'] == 'sep':
                    draw.line([(mx + int(5 * ctx.scale), curr_y + int(5 * ctx.scale)),
                               (mx + menu_w - int(5 * ctx.scale), curr_y + int(5 * ctx.scale))],
                              fill=(180,170,150,255), width=int(1 * ctx.scale))
                    curr_y += sep_h
                    menu['rects'].append(None)
                else:
                    rect = (mx, curr_y, mx+menu_w, curr_y+item_h)
                    menu['rects'].append(rect)
                    is_hovered = (ctx.hovered_menu_item is not None and
                                  ctx.hovered_menu_item[0] == 'context' and
                                  ctx.hovered_menu_item[1] == item['id'])
                    if is_hovered and item['enabled']:
                        draw.rectangle(rect, fill=(200, 210, 240, 200))
                    text_color = (40,40,30,255) if item['enabled'] else (160,160,150,255)
                    draw.text((mx + int(20 * ctx.scale), curr_y + int(5 * ctx.scale)),
                              item['text'], font=menu_font, fill=text_color)
                    curr_y += item_h

        # 设置菜单A（左键齿轮）
        if ctx.settings_menu.get('visible'):
            self._render_dropdown_menu(
                draw, ctx, ctx.settings_menu,
                menu_type='settings',
                menu_font=menu_font,
                item_h=int(38 * ctx.scale),
                menu_w=settings_menu_w,
                sep_h=int(14 * ctx.scale),
            )
        # 设置菜单B（右键齿轮 - 语音设置）
        settings_menu_b = getattr(ctx, 'settings_menu_b', None)
        if settings_menu_b is not None and settings_menu_b.get('visible'):
            self._render_dropdown_menu(
                draw, ctx, settings_menu_b,
                menu_type='settings_b',
                menu_font=menu_font,
                item_h=int(38 * ctx.scale),
                menu_w=settings_menu_w,
                sep_h=int(14 * ctx.scale),
            )
        # 人设编辑弹窗（最顶层）
        character_editor = getattr(ctx, "character_editor", None)
        if character_editor is not None and getattr(character_editor, "visible", False):
            editor_cursor = character_editor.render(img, draw, ctx)
            return RenderResult(cursor_rect=editor_cursor)

        # 返回光标矩形（宽度留空，由窗口补全）
        return RenderResult(cursor_rect=cursor_rect)
    
    # ---------- 主渲染入口 ----------
    def render(self, ctx: RenderContext):
        # 静态层缓存
        static_key = (ctx.width, ctx.height, ctx.scale)
        if self._static_cache is None or self._static_key != static_key:
            self._static_cache = self._render_static(ctx)
            self._static_key = static_key
        img = self._static_cache.copy()
        result = self._render_dynamic(ctx, img)
        # 最终图像上下翻转（OpenGL 纹理坐标）
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        return img, result


class ChatUI:
    def __init__(self):
        self._msg_line_cache = {}
        self._msg_line_cache_max = 512

    def _get_message_lines_cached(self, sender, text, available_width, get_text_width):
        key = (sender, text, available_width)
        lines = self._msg_line_cache.get(key)
        if lines is None:
            lines = TextUtils.get_message_lines(sender, text, available_width, get_text_width)
            if len(self._msg_line_cache) >= self._msg_line_cache_max:
                self._msg_line_cache.clear()      # 简单全清，防止无限增长
            self._msg_line_cache[key] = lines
        return lines

    def hit_test(self, x: int, y: int, ctx: RenderContext,
                 is_click: bool = False, need_msg_char: bool = False):
        result = {'area': 'empty'}

        character_editor = getattr(ctx, "character_editor", None)
        if character_editor is not None and getattr(character_editor, "visible", False):
            return character_editor.hit_test(x, y, ctx, is_click=is_click)

        # 计算所有按钮位置（与渲染保持一致）
        hdr = compute_header_rects(ctx.width, ctx.scale)
        close_x, close_y, close_size, _ = hdr["close"]
        gear_x, gear_y, gear_size, _ = hdr["gear"]
        # 输入框区域
        input_y = ctx.height - ctx.input_h - ctx.input_y_offset
        input_x = int(12 * ctx.scale)
        input_w = ctx.width - int(12 * ctx.scale) * 2 - ctx.send_btn_width - int(16 * ctx.scale)
        # 发送按钮
        btn_w = int(56 * ctx.scale)
        btn_h = int(38 * ctx.scale)
        btn_x = ctx.width - btn_w - int(16 * ctx.scale)
        btn_y = input_y + (ctx.input_h - btn_h) // 2 - int(24 * ctx.scale)
        # 文档上传按钮
        upload_btn_w = int(56 * ctx.scale)
        upload_btn_h = int(38 * ctx.scale)
        upload_btn_x = ctx.width - upload_btn_w - int(16 * ctx.scale)
        upload_btn_y = btn_y + btn_h + int(11 * ctx.scale)
        # 消息区域
        msg_bottom = ctx.height - ctx.input_h - ctx.msg_bottom_margin

        # --- 优先检测菜单项（菜单A / 菜单B 互斥） ---
        if ctx.context_menu.get('visible'):
            menu = ctx.context_menu
            for idx, rect in enumerate(menu.get('rects', [])):
                if rect and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                    item = menu['items'][idx]
                    if item.get('enabled', True):
                        return {'area': 'context_menu', 'item_id': item['id']}
            return {'area': 'context_menu_bg'}

        if ctx.settings_menu.get('visible'):
            menu = ctx.settings_menu
            for idx, rect in enumerate(menu.get('rects', [])):
                if rect and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                    item = menu['items'][idx]
                    if item.get('enabled', True):
                        return {'area': 'settings_menu', 'item_id': item['id']}
            return {'area': 'settings_menu_bg'}

        settings_menu_b = getattr(ctx, 'settings_menu_b', None)
        if settings_menu_b is not None and settings_menu_b.get('visible'):
            for idx, rect in enumerate(settings_menu_b.get('rects', [])):
                if rect and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                    item = settings_menu_b['items'][idx]
                    if item.get('enabled', True):
                        return {'area': 'settings_menu_b', 'item_id': item['id']}
            return {'area': 'settings_menu_b_bg'}

        # --- 齿轮和关闭按钮 ---
        if gear_x <= x <= gear_x + gear_size and gear_y <= y <= gear_y + gear_size:
            return {'area': 'settings_btn'}
        if close_x <= x <= close_x + close_size and close_y <= y <= close_y + close_size:
            return {'area': 'close_btn'}

        # --- 滚动条 ---
        if ctx.scroll_manager.scrollbar_rect:
            sx, sy, sw, sh = ctx.scroll_manager.scrollbar_rect
            if sx <= x <= sx + sw and sy <= y <= sy + sh:
                if ctx.scroll_manager.thumb_rect:
                    tx, ty, tw, th = ctx.scroll_manager.thumb_rect
                    if tx <= x <= tx + tw and ty <= y <= ty + th:
                        return {'area': 'scrollbar_thumb'}
                return {'area': 'scrollbar_track', 'y': y}

        # --- 文档上传按钮 ---
        ctx.doc_uploader.upload_rect = (upload_btn_x, upload_btn_y, upload_btn_w, upload_btn_h)
        uploader_hit = ctx.doc_uploader.hit_test(x, y)
        if uploader_hit:
            return {'area': f'doc_{uploader_hit}'}

        # --- 发送按钮 ---
        if btn_x <= x <= btn_x + btn_w and btn_y <= y <= btn_y + btn_h:
            return {'area': 'send_btn'}

        # --- 输入框 ---
        if input_x <= x <= input_x + input_w and input_y <= y <= input_y + ctx.input_h:
            if (
                is_click
                or getattr(ctx, "selecting", False)
                or getattr(ctx.input_box, "selecting", False)
            ):
                # 计算点击处的字符索引
                char_idx = self._get_abs_index_by_pos(x, y, ctx)
            else:
                char_idx = 0
            return {'area': 'input_box', 'char_index': char_idx}

        # --- 消息列表 ---
        if ctx.msg_top <= y <= msg_bottom:
            if is_click or need_msg_char:
                char_info = self._get_message_char_at_pos(x, y, ctx)
                if char_info:
                    msg_idx, line_in_msg, char_in_line = char_info
                    return {'area': 'msg_list', 'msg_index': msg_idx,
                            'line_in_msg': line_in_msg, 'char_in_line': char_in_line}
            return {'area': 'msg_list', 'msg_index': None}

        return {'area': 'empty'}

    # ---------- 辅助方法（仅依赖 ctx） ----------
    def _get_abs_index_by_pos(self, x: int, y: int, ctx: RenderContext) -> int:
        if not ctx.input_box.lines:
            return 0
        input_y = ctx.height - ctx.input_h - ctx.input_y_offset
        input_h = ctx.input_h
        # 与渲染保持一致的字体度量
        if (ctx.fixed_ascent + ctx.fixed_descent) > 0:
            ascent = ctx.fixed_ascent
            descent = ctx.fixed_descent
        else:
            try:
                bbox = ctx.font.getbbox("中Ag")
                ascent = max(0, -bbox[1])
                descent = max(0, bbox[3] - bbox[1] - ascent)
            except Exception:
                ascent = int(18 * ctx.scale)
                descent = int(5 * ctx.scale)
        text_h = ascent + descent
        line_h = text_h + ctx.input_line_spacing
        visible_lines = min(
            ctx.input_visible_lines,
            len(ctx.input_box.lines) - ctx.input_box.scroll_offset
        )
        visible_lines = max(0, visible_lines)
        total_text_h = visible_lines * line_h
        y_center_offset = (input_h - total_text_h) // 2
        # 相对于输入框文本区域顶部的 y
        rel_y = y - (input_y + y_center_offset)
        if rel_y < 0:
            clicked_line = ctx.input_box.scroll_offset
        else:
            clicked_line = ctx.input_box.scroll_offset + int(rel_y // line_h)
        clicked_line = max(0, min(clicked_line, len(ctx.input_box.lines) - 1))
        target_x = x - int(15 * ctx.scale)
        line_text = ctx.input_box.lines[clicked_line]
        line_start_abs = ctx.input_box.get_line_abs_start(clicked_line)
        char_idx = 0
        if target_x > 0:
            for i, ch in enumerate(line_text):
                prefix = line_text[:i + 1]
                w = ctx.get_text_width(prefix)
                if w > target_x:
                    break
                char_idx = i + 1
        abs_idx = line_start_abs + char_idx
        return min(abs_idx, len(ctx.input_box.get_text()))

    def _get_message_char_at_pos(self, x: int, y: int, ctx: RenderContext):
        """返回 (msg_index, line_in_msg, char_in_line) 或 None"""
        msg_top = ctx.msg_top
        msg_bottom = ctx.height - ctx.input_h - ctx.msg_bottom_margin

        if y < msg_top or y > msg_bottom:
            return None

        msg_text_x = int(MSG_TEXT_X * ctx.scale)
        relative_y = y - msg_top - int(MSG_AREA_Y_PAD * ctx.scale)

        if relative_y < 0:
            return None

        visible_line_index = relative_y // ctx.msg_line_h
        target_global_line = ctx.scroll_manager.scroll_offset + visible_line_index

        layout = getattr(ctx, "layout_snapshot", None)

        if layout is not None and not layout.is_empty():
            if target_global_line < 0 or target_global_line >= layout.total_lines:
                return None

            idx = max(0, bisect.bisect_right(layout.prefix, target_global_line) - 1)
            entry = layout.entries[idx]

            line_in_msg = target_global_line - layout.prefix[idx]
    
            if line_in_msg < 0 or line_in_msg >= entry.line_count:
                return None

            raw_line = entry.lines[line_in_msg]

            clean_line = (
                raw_line.replace('\n', '')
                .replace('\r', '')
                .replace('\x0b', '')
                .replace('\x0c', '')
            )
            clean_line = re.sub(r'[\u2028\u2029\u0085]', '', clean_line)

            target_x = x - msg_text_x
            char_in_line = 0

            if target_x > 0 and clean_line:
                for i in range(len(clean_line)):
                    if ctx.get_text_width(clean_line[:i + 1]) > target_x:
                        break
                    char_in_line = i + 1

            return idx, line_in_msg, char_in_line
