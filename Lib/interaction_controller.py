# -*- coding: utf-8 -*-
"""
interaction_controller.py
"""
import time
import glfw
import pyautogui
from pynput.mouse import Listener as MouseListener


class InteractionController:
    def __init__(self, pet):
        self.pet = pet
        self.is_dragging = False
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.last_click_time = 0.0
        self.click_cooldown = 0.5
        self.last_shake_time = 0.0
        self.SHAKE_COOLDOWN = 1.0
        self.scroll_events = []
        self.last_global_trigger_time = 0.0
        self.GLOBAL_SCROLL_COOLDOWN = 60.0
        self.SCROLL_BURST_WINDOW = 0.8
        self.SCROLL_BURST_THRESHOLD = 3
        self._pending_scroll_action = None

    def consume_pending_scroll_action(self):
        direction = self._pending_scroll_action
        self._pending_scroll_action = None
        return direction

    def on_mouse_move(self, window, xpos, ypos):
        pet = self.pet
        pet.last_interaction_time = time.time()
        if pet.chat_window and pet.chat_window.active:
            return
        if not self.is_dragging:
            return
        sx, sy = pyautogui.position()
        glfw.set_window_pos(window, int(sx - self.drag_start_x), int(sy - self.drag_start_y))
        now = time.time()
        if not pet.motion_lock and now - self.last_shake_time >= self.SHAKE_COOLDOWN:
            if pet.start_motion("shake", dialog_msg=None, sound_group="shake"):
                self.last_shake_time = now

    def on_mouse_button(self, window, button, action, mods):
        pet = self.pet
        if action == glfw.PRESS:
            pet.last_interaction_time = time.time()

        if button == glfw.MOUSE_BUTTON_RIGHT and action == glfw.PRESS:
            pet.toggle_chat_window(window)
            return

        if button == glfw.MOUSE_BUTTON_LEFT and action == glfw.PRESS:
            sx, sy = pyautogui.position()
            wx, wy = glfw.get_window_pos(window)
            rel_x, rel_y = sx - wx, sy - wy

            if pet.setting_button.on_click(rel_x, rel_y):
                return

            # === 细粒度部位分类（单次 HitPart，规则优先级 + 分区兜底） ===
            part = pet.classify_hit(rel_x, rel_y)
            if part:
                now = time.time()
                if now - self.last_click_time < self.click_cooldown:
                    return
                self.last_click_time = now
                pet.play_part_motion(part)
                return

            if pet.chat_window and pet.chat_window.active:
                return

            self.is_dragging = True
            self.drag_start_x, self.drag_start_y = rel_x, rel_y

        elif button == glfw.MOUSE_BUTTON_LEFT and action == glfw.RELEASE:
            self.is_dragging = False

    def on_global_scroll(self, x, y, dx, dy, injected=False):
        pet = self.pet
        if injected:
            return
        if pet.chat_window and pet.chat_window.active:
            return
        now = time.time()
        if now - self.last_global_trigger_time < self.GLOBAL_SCROLL_COOLDOWN:
            return
        direction = 1 if dy > 0 else -1 if dy < 0 else 0
        if direction == 0:
            return
        pet.last_interaction_time = time.time()
        self.scroll_events = [(t, d) for t, d in self.scroll_events if now - t <= self.SCROLL_BURST_WINDOW]
        self.scroll_events.append((now, direction))
        if len(self.scroll_events) >= self.SCROLL_BURST_THRESHOLD:
            up_cnt = sum(1 for _, d in self.scroll_events if d == 1)
            down_cnt = sum(1 for _, d in self.scroll_events if d == -1)
            final_dir = 1 if up_cnt > down_cnt else -1
            self._pending_scroll_action = final_dir
            self.last_global_trigger_time = now
            self.scroll_events.clear()
            if pet.window:
                glfw.post_empty_event()

    def start_global_listener(self):
        with MouseListener(on_scroll=self.on_global_scroll) as listener:
            listener.join()
