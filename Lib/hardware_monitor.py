import time
import random
import threading
import psutil

# GPUtil 延迟导入，避免无 GPU 时启动报错
GPUtil = None


class HardwareMonitor:
    def __init__(self, alert_cooldown=30, poll_interval=10):
        self.status = {
            "cpu": 0,
            "mem": 0,
            "gpu_load": 0,
            "gpu_temp": 0,
        }
        self.alert_message = None
        self.alert_time = 0
        self.alert_cooldown = alert_cooldown

        self.poll_interval = poll_interval

        self._stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

        self._gpu_fail_count = 0
        self._gpu_skip_until = 0

    def start(self):
        # 预热 cpu_percent，第一次调用总是返回 0
        psutil.cpu_percent(interval=None)
        self._stop_event.clear()
        self.thread.start()

    def stop(self):
        self._stop_event.set()
        self.thread.join(timeout=2)

    def _loop(self):
        global GPUtil

        # 延迟导入 GPUtil
        if GPUtil is None:
            try:
                import GPUtil as _g
                GPUtil = _g
            except ImportError:
                GPUtil = False  # 标记为不可用

        while not self._stop_event.is_set():
            # ---------- CPU / 内存 ----------
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory().percent
            except Exception:
                cpu, mem = 0, 0

            # ---------- GPU（带保护）----------
            gpu_load = 0
            gpu_temp = 0
            now = time.time()

            if GPUtil and now >= self._gpu_skip_until:
                try:
                    gpus = GPUtil.getGPUs()
                    if gpus:
                        gpu = gpus[0]
                        gpu_load = gpu.load * 100
                        gpu_temp = gpu.temperature
                    self._gpu_fail_count = 0
                except Exception:
                    self._gpu_fail_count += 1
                    if self._gpu_fail_count >= 3:
                        self._gpu_skip_until = now + 60

            self.status["cpu"] = cpu
            self.status["mem"] = mem
            self.status["gpu_load"] = gpu_load
            self.status["gpu_temp"] = gpu_temp

            # ---------- 报警判断 ----------
            alerts = []
            if cpu >= 75:
                alerts.append(f"CPU 占用 {cpu:.0f}% 啦！")
            if mem >= 95:
                alerts.append("内存满了！！！")
            if gpu_temp > 60:
                alerts.append(f"热...热死啦！GPU {gpu_temp}°C")
            if gpu_load > 80:
                alerts.append("...好累")

            if alerts and now - self.alert_time >= self.alert_cooldown:
                self.alert_message = random.choice(alerts)
                self.alert_time = now
            else:
                self.alert_message = None

            self._stop_event.wait(timeout=self.poll_interval)

    def take_alert_message(self):
        msg = self.alert_message
        self.alert_message = None
        return msg
