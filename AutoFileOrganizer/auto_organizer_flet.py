import os
import shutil
import time
import logging
import threading
import json
import sys
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import flet as ft

# ===================== 尝试导入系统托盘模块 =====================
try:
    import pystray
    from PIL import Image
    TRAY_ENABLED = True
except ImportError:
    TRAY_ENABLED = False
    print("⚠️  未安装pystray或pillow库，系统托盘功能已禁用")


# ===================== 配置管理类（保持不变）=====================
class Config:
    def __init__(self):
        self.config_file = "file_organizer_config.json"
        self.default_config = {
            "folder_to_watch": os.path.expanduser("~/Downloads"),
            "auto_organize_on_startup": False,
            "tray_icon_path": "icon.ico",
            "file_types": {
                "图片": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"],
                "文档": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt"],
                "视频": [".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".rmvb"],
                "音频": [".mp3", ".wav", ".flac", ".aac", ".ogg"],
                "压缩包": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"],
                "安装包": [".exe", ".msi", ".dmg", ".deb", ".rpm"],
                "代码": [".py", ".java", ".cpp", ".c", ".html", ".css", ".js", ".json"],
                "镜像": [".iso", ".img", ".vmdk"],
                "电子书": [".epub", ".mobi", ".azw3"]
            },
            "initial_wait": 3,
            "check_interval": 2,
            "stable_checks": 3,
            "max_wait_time": 86400,
            "temp_extensions": [".crdownload", ".part", ".xltd", ".td", ".cfg", ".tmp", ".download"],
            "notification_timeout": 5,
            "log_file": "file_organizer.log",
            "max_log_size": 10 * 1024 * 1024,
            "debounce_time": 2,
            "start_on_boot": False,
            "remember_close_action": False,
            "close_action": 0,
            "theme_mode": "light"
        }
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded_config = json.load(f)
                self.config = self.default_config.copy()
                self.config.update(loaded_config)
                logging.info("✅ 配置文件加载成功")
            except Exception as e:
                logging.error(f"❌ 加载配置文件失败: {e}，使用默认配置")
                self.config = self.default_config.copy()
        else:
            logging.info("配置文件不存在，使用默认配置")
            self.config = self.default_config.copy()
            self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            logging.info("✅ 配置文件保存成功")
            return True
        except Exception as e:
            logging.error(f"❌ 保存配置文件失败: {e}")
            return False

    def get(self, key):
        return self.config.get(key, self.default_config.get(key))

    def set(self, key, value):
        self.config[key] = value


# ===================== 核心功能类（保持不变）=====================
class FileOrganizer:
    def __init__(self, config, send_notification_func):
        self.config = config
        self.send_notification = send_notification_func
        self.is_running = True
        self.observer = None
        self.file_processing_times = {}
        self.category_folders = set()
        self.update_category_folders()

    def setup_logging(self):
        log_file = self.config.get("log_file")
        max_log_size = self.config.get("max_log_size")
        if os.path.exists(log_file) and os.path.getsize(log_file) > max_log_size:
            os.rename(log_file, f"{log_file}.old")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler()
            ]
        )

    def get_unique_file_path(self, target_path):
        if not os.path.exists(target_path):
            return target_path
        filename, ext = os.path.splitext(os.path.basename(target_path))
        directory = os.path.dirname(target_path)
        counter = 1
        while True:
            new_filename = f"{filename}({counter}){ext}"
            new_path = os.path.join(directory, new_filename)
            if not os.path.exists(new_path):
                return new_path
            counter += 1

    def is_file_download_complete(self, file_path):
        if not os.path.exists(file_path):
            logging.warning(f"文件已被删除: {os.path.basename(file_path)}")
            return False
        file_extension = os.path.splitext(file_path)[1].lower()
        if file_extension in self.config.get("temp_extensions"):
            logging.info(f"检测到临时文件，跳过: {os.path.basename(file_path)}")
            return False
        time.sleep(self.config.get("initial_wait"))
        last_size = -1
        stable_count = 0
        start_time = time.time()
        while True:
            if time.time() - start_time > self.config.get("max_wait_time"):
                error_msg = f"等待超时，放弃处理: {os.path.basename(file_path)}"
                logging.error(error_msg)
                self.send_notification("整理失败", error_msg)
                return False
            if not os.path.exists(file_path):
                logging.warning(f"文件已被删除: {os.path.basename(file_path)}")
                return False
            try:
                current_size = os.path.getsize(file_path)
            except OSError as e:
                logging.info(f"文件被占用，继续等待: {os.path.basename(file_path)} - {e}")
                stable_count = 0
                time.sleep(self.config.get("check_interval"))
                continue
            if current_size == last_size:
                stable_count += 1
                logging.debug(f"检查中: {os.path.basename(file_path)} | 大小: {current_size/1024/1024:.1f}MB | 稳定次数: {stable_count}/{self.config.get('stable_checks')}")
                if stable_count >= self.config.get("stable_checks"):
                    try:
                        with open(file_path, "r+b") as f:
                            pass
                        logging.info(f"文件下载完成: {os.path.basename(file_path)}")
                        return True
                    except PermissionError as e:
                        logging.info(f"文件仍被占用，继续等待: {os.path.basename(file_path)} - {e}")
                        stable_count = 0
            else:
                stable_count = 0
                last_size = current_size
                logging.info(f"文件正在下载: {os.path.basename(file_path)} | 当前大小: {current_size/1024/1024:.1f}MB")
            time.sleep(self.config.get("check_interval"))

    def update_category_folders(self):
        self.category_folders.clear()
        folder_to_watch = self.config.get("folder_to_watch")
        for folder in self.config.get("file_types").keys():
            self.category_folders.add(os.path.join(folder_to_watch, folder))
        self.category_folders.add(os.path.join(folder_to_watch, "其他"))
        logging.debug(f"更新分类文件夹集合: {self.category_folders}")

    def organize_file(self, file_path):
        try:
            filename = os.path.basename(file_path)
            file_dir = os.path.dirname(file_path)
            if os.path.isdir(file_path):
                return False
            file_extension = os.path.splitext(filename)[1].lower()
            if file_extension in self.config.get("temp_extensions"):
                return False
            self.update_category_folders()
            if file_dir in self.category_folders:
                current_folder_name = os.path.basename(file_dir)
                target_folder_name = "其他"
                for folder, extensions in self.config.get("file_types").items():
                    if file_extension in extensions:
                        target_folder_name = folder
                        break
                if current_folder_name == target_folder_name:
                    logging.debug(f"文件已在正确位置，跳过: {filename}")
                    return False
            target_folder = "其他"
            for folder, extensions in self.config.get("file_types").items():
                if file_extension in extensions:
                    target_folder = folder
                    break
            target_folder_path = os.path.join(self.config.get("folder_to_watch"), target_folder)
            if not os.path.exists(target_folder_path):
                os.makedirs(target_folder_path)
                logging.info(f"创建目标文件夹: {target_folder_path}")
                self.update_category_folders()
            target_path = os.path.join(target_folder_path, filename)
            target_path = self.get_unique_file_path(target_path)
            shutil.move(file_path, target_path)
            success_msg = f"{filename}\n→ {target_folder}"
            logging.info(f"已整理: {success_msg}")
            self.send_notification("文件整理成功", success_msg)
            return True
        except PermissionError as e:
            error_msg = f"{filename}\n原因: 没有权限访问文件"
            logging.error(f"整理失败: {error_msg} - {e}")
            self.send_notification("整理失败", error_msg)
            return False
        except OSError as e:
            if e.errno == 28:
                error_msg = f"{filename}\n原因: 磁盘空间不足"
            else:
                error_msg = f"{filename}\n原因: {str(e)}"
            logging.error(f"整理失败: {error_msg}")
            self.send_notification("整理失败", error_msg)
            return False
        except Exception as e:
            error_msg = f"{filename}\n原因: 未知错误"
            logging.error(f"整理失败: {error_msg} - {e}", exc_info=True)
            self.send_notification("整理失败", error_msg)
            return False

    def organize_existing_files(self):
        logging.info("开始扫描并整理已有文件...")
        self.send_notification("开始整理", "正在扫描并整理已有文件...")
        success_count = 0
        fail_count = 0
        skip_count = 0
        try:
            for root, dirs, files in os.walk(self.config.get("folder_to_watch")):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    if self.organize_file(file_path):
                        success_count += 1
                    else:
                        file_extension = os.path.splitext(filename)[1].lower()
                        if file_extension in self.config.get("temp_extensions"):
                            skip_count += 1
                        else:
                            fail_count += 1
        except Exception as e:
            logging.error(f"扫描文件夹失败: {e}", exc_info=True)
            self.send_notification("错误", f"扫描文件夹失败: {str(e)}")
            return
        summary_msg = (
            f"整理完成\n"
            f"成功: {success_count} 个\n"
            f"失败: {fail_count} 个\n"
            f"跳过: {skip_count} 个"
        )
        logging.info(summary_msg)
        self.send_notification("整理完成", summary_msg)

    def debounced_process_file(self, file_path):
        current_time = time.time()
        debounce_time = self.config.get("debounce_time")
        if file_path in self.file_processing_times:
            last_time = self.file_processing_times[file_path]
            if current_time - last_time < debounce_time:
                logging.debug(f"防抖跳过: {os.path.basename(file_path)}")
                return
        self.file_processing_times[file_path] = current_time
        time.sleep(debounce_time)
        if os.path.exists(file_path) and self.file_processing_times.get(file_path, 0) == current_time:
            if self.is_file_download_complete(file_path):
                self.organize_file(file_path)
            if file_path in self.file_processing_times:
                del self.file_processing_times[file_path]

    def start_monitoring(self):
        if self.observer and self.observer.is_alive():
            logging.warning("监控已经在运行中")
            return
        event_handler = FileOrganizerHandler(self)
        self.observer = Observer()
        self.observer.schedule(event_handler, self.config.get("folder_to_watch"), recursive=True)
        self.observer.start()
        self.is_running = True
        logging.info(f"✅ 开始实时监控所有文件: {self.config.get('folder_to_watch')}（包括子文件夹）")
        self.send_notification("监控已启动", f"正在监控: {self.config.get('folder_to_watch')}")

    def stop_monitoring(self):
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            self.is_running = False
            logging.info("❌ 文件监控已停止")
            self.send_notification("监控已停止", "文件监控已暂停")


class FileOrganizerHandler(FileSystemEventHandler):
    def __init__(self, organizer):
        self.organizer = organizer

    def process_event(self, event):
        if not self.organizer.is_running:
            return
        if event.is_directory:
            return
        file_path = event.src_path
        logging.debug(f"检测到文件事件: {event.event_type} - {file_path}")
        threading.Thread(target=self.organizer.debounced_process_file, args=(file_path,), daemon=True).start()

    def on_created(self, event):
        self.process_event(event)

    def on_modified(self, event):
        self.process_event(event)

    def on_moved(self, event):
        if not self.organizer.is_running:
            return
        if event.is_directory:
            return
        dest_path = event.dest_path
        logging.debug(f"检测到文件移动/重命名: {event.src_path} → {dest_path}")
        threading.Thread(target=self.organizer.debounced_process_file, args=(dest_path,), daemon=True).start()


# ===================== Flet 图形化界面类（全新重写）=====================
class FletFileOrganizerApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.config = Config()
        # 先持有自己的引用，以便传给 FileOrganizer
        self._notify_ref = None
        self.organizer = FileOrganizer(self.config, self._do_notification)
        self.organizer.setup_logging()

        # 状态变量
        self._building = True  # 初始化期间跳过 update() 调用
        self._quitting = False  # 防止重复退出
        self._window_visible = True  # 跟踪窗口可见性
        self.tray_icon = None
        self.tray_thread = None
        self.selected_category = None  # 文件类型配置中当前选中的分类
        self.monitoring_status = "监控中"

        # 存储需要动态更新的控件引用
        self.status_text = None
        self.toggle_button = None
        self.category_list = None
        self.extension_field = None
        self.log_field = None
        self.organize_log = None
        self.temp_field = None

        # 设置窗口属性
        self.page.title = "文件夹自动整理机器人"
        self.page.window.width = 820
        self.page.window.height = 620
        self.page.window.min_width = 600
        self.page.window.min_height = 450
        self.page.window.prevent_close = True
        self.page.window.on_event = self._on_window_event
        # 自定义窗口图标
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            script_dir = os.getcwd()
        icon_path = os.path.join(script_dir, "icon.ico")
        if os.path.exists(icon_path):
            self.page.window.icon = icon_path
            logging.info(f"窗口图标已设置: {icon_path}")
        else:
            logging.warning(f"图标文件不存在: {icon_path}")
        # 主题设置
        saved_theme = self.config.get("theme_mode")
        self.page.theme_mode = ft.ThemeMode(saved_theme) if saved_theme else ft.ThemeMode.LIGHT
        self.page.theme = ft.Theme(color_scheme_seed="blue")

        # 文件选择器（用于选择目录）
        self.file_picker = ft.FilePicker()

        # 构建界面
        self._build_ui()
        self._building = False

        # 首次加载日志内容
        self._refresh_log(None)

        # 启动时可选整理已有文件
        if self.config.get("auto_organize_on_startup"):
            threading.Thread(target=self.organizer.organize_existing_files, daemon=True).start()

        # 启动文件监控
        self.organizer.start_monitoring()

        # 系统托盘
        if TRAY_ENABLED:
            self._create_tray_icon()

        logging.info("=" * 60)
        logging.info("文件夹自动整理机器人启动 (Flet版)")
        logging.info("=" * 60)

    # ---------- 通知回调 ----------
    def _do_notification(self, title, message):
        """线程安全的通知回调——供 FileOrganizer 在后台线程中调用"""
        logging.info(f"通知: {title} - {message}")
        # 更新首页整理日志
        if self.organize_log:
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            self.organize_log.value += f"[{ts}] {title}: {message}\n"
            if not self._building:
                self.organize_log.update()
        # Flet 控件支持跨线程更新
        try:
            full_msg = f"{title}: {message}" if title else message
            self.page.show_dialog(ft.SnackBar(
                content=ft.Text(full_msg[:120], size=14),
                duration=3000,
                behavior=ft.SnackBarBehavior.FLOATING,
            ))
        except Exception:
            pass
        # 系统托盘通知
        if TRAY_ENABLED and self.tray_icon:
            try:
                self.tray_icon.notify(message[:200], title[:50])
            except Exception:
                pass

    # ---------- 构建整体 UI ----------
    def _build_ui(self):
        self.page.add(
            ft.Tabs(
                length=4,
                selected_index=0,
                expand=True,
                content=ft.Column(
                    expand=True,
                    spacing=0,
                    controls=[
                        ft.TabBar(
                            tabs=[
                                ft.Tab(label="🏠 主页"),
                                ft.Tab(label="📁 文件类型配置"),
                                ft.Tab(label="⚙️ 高级设置"),
                                ft.Tab(label="📋 运行日志"),
                            ]
                        ),
                        ft.TabBarView(
                            expand=True,
                            controls=[
                                self._build_home_tab(),
                                self._build_file_types_tab(),
                                self._build_settings_tab(),
                                self._build_log_tab(),
                            ],
                        ),
                    ],
                ),
            )
        )

    # ===================== 主页标签 =====================
    def _build_home_tab(self):
        # 文件夹选择行
        self.folder_path_text = ft.Text(
            value=self.config.get("folder_to_watch"),
            size=13,
            italic=True,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )

        folder_row = ft.Container(
            content=ft.Row([
                ft.Text("目标文件夹:", size=15, weight=ft.FontWeight.BOLD),
                self.folder_path_text,
                ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="更换目标文件夹", on_click=self._select_folder),
            ], alignment=ft.MainAxisAlignment.START),
            margin=ft.Margin(top=10, left=10, right=10, bottom=0),
        )

        # 状态显示
        self.status_text = ft.Text(value="● 监控中", size=18, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN)
        status_row = ft.Container(
            content=ft.Row([
                ft.Text("运行状态:", size=15, weight=ft.FontWeight.BOLD),
                self.status_text,
            ]),
            margin=ft.Margin(top=10, left=10, right=10, bottom=0),
        )

        # 按钮行
        self.toggle_button = ft.FilledButton(
            "⏸ 暂停监控",
            icon=ft.Icons.PAUSE,
            on_click=self._toggle_monitoring,
            tooltip="暂停/恢复对文件的实时监控",
        )
        button_row = ft.Row([
            self.toggle_button,
            ft.FilledButton("立即整理全部", icon=ft.Icons.PLAY_ARROW, on_click=self._organize_all,
                              tooltip="立即扫描目标文件夹下所有文件并按规则整理"),
            ft.FilledButton("打开目标文件夹", icon=ft.Icons.OPEN_IN_BROWSER, on_click=self._open_folder,
                              tooltip="在资源管理器中打开当前目标文件夹"),
        ], alignment=ft.MainAxisAlignment.CENTER)

        # 整理实时日志
        self.organize_log = ft.TextField(
            multiline=True,
            read_only=True,
            expand=True,
            text_size=13,
            value="",
            hint_text="文件整理记录将在此实时显示...",
            min_lines=6,
            max_lines=20,
        )
        log_row = ft.Row([
            ft.TextButton("清空记录", icon=ft.Icons.CLEAR_ALL, on_click=self._clear_organize_log,
                          tooltip="清空当前显示的整理记录"),
        ], alignment=ft.MainAxisAlignment.END)

        return ft.Container(
            content=ft.Column([
                folder_row,
                ft.Divider(),
                status_row,
                ft.Divider(),
                button_row,
                ft.Divider(),
                ft.Text("整理记录", size=15, weight=ft.FontWeight.BOLD),
                self.organize_log,
                log_row,
            ], scroll=ft.ScrollMode.AUTO, expand=True),
            padding=10,
            expand=True,
        )

    # ===================== 文件类型配置标签 =====================
    def _build_file_types_tab(self):
        # 左侧：分类列表
        self.category_list = ft.ListView(expand=True, spacing=2)

        cat_buttons = ft.Row([
            ft.TextButton("添加", icon=ft.Icons.ADD, on_click=self._add_category,
                          tooltip="新建一个文件分类"),
            ft.TextButton("重命名", icon=ft.Icons.EDIT, on_click=self._rename_category,
                          tooltip="修改选中分类的名称"),
            ft.TextButton("删除", icon=ft.Icons.DELETE, on_click=self._delete_category,
                          tooltip="删除选中的分类"),
        ], spacing=0, wrap=True)

        left_panel = ft.Container(
            content=ft.Column([
                ft.Text("文件分类", size=16, weight=ft.FontWeight.BOLD),
                self.category_list,
                cat_buttons,
            ], spacing=8, expand=True),
            width=200,
            padding=10,
            border=ft.Border.all(1, ft.Colors.GREY_300),
            border_radius=8,
            margin=ft.Margin.all(10),
        )

        # 右侧：后缀编辑器
        self.extension_field = ft.TextField(
            label="文件后缀（每行一个，以 . 开头）",
            multiline=True,
            min_lines=10,
            max_lines=20,
            expand=True,
            hint_text="例如:\n.jpg\n.png\n.pdf",
            tooltip="每行一个文件后缀（例如 .jpg .png），注意必须包含点号，不支持通配符",
        )
        save_ext_btn = ft.FilledButton("保存修改", icon=ft.Icons.SAVE, on_click=self._save_file_types,
                                         tooltip="保存当前分类的后缀配置到文件中")

        right_panel = ft.Container(
            content=ft.Column([
                ft.Text("文件后缀", size=16, weight=ft.FontWeight.BOLD),
                self.extension_field,
                ft.Row([save_ext_btn], alignment=ft.MainAxisAlignment.END),
            ], spacing=8, expand=True),
            expand=True,
            padding=10,
            border=ft.Border.all(1, ft.Colors.GREY_300),
            border_radius=8,
            margin=ft.Margin.all(10),
        )

        # 初始加载分类
        self._refresh_category_list()

        return ft.Row([
            left_panel,
            right_panel,
        ], expand=True)

    # ===================== 高级设置标签 =====================
    def _build_settings_tab(self):
        # 基本设置
        self.auto_start_switch = ft.Switch(
            value=self.config.get("auto_organize_on_startup"),
            on_change=self._save_basic_settings,
            label="启动时自动整理已有文件",
            label_text_style=ft.TextStyle(size=14),
            label_position=ft.LabelPosition.RIGHT,
        )
        self.boot_switch = ft.Switch(
            value=self.config.get("start_on_boot"),
            on_change=self._toggle_boot,
            label="开机自动启动",
            label_text_style=ft.TextStyle(size=14),
            label_position=ft.LabelPosition.RIGHT,
        )
        self.dark_mode_switch = ft.Switch(
            value=(self.config.get("theme_mode") == "dark"),
            on_change=self._toggle_dark_mode,
            label="深色模式",
            label_text_style=ft.TextStyle(size=14),
            label_position=ft.LabelPosition.RIGHT,
        )

        basic_card = ft.Container(
            content=ft.Column([
                ft.Text("基本设置", size=16, weight=ft.FontWeight.BOLD),
                self.auto_start_switch,
                self.boot_switch,
                self.dark_mode_switch,
                ft.TextButton("重置关闭选项", icon=ft.Icons.RESTART_ALT, on_click=self._reset_close_action,
                              tooltip="清除「记住关闭选项」的设置，下次关闭窗口时会再次询问"),
            ], spacing=8, alignment=ft.MainAxisAlignment.CENTER),
            padding=15,
            border=ft.Border.all(1, ft.Colors.GREY_300),
            border_radius=8,
            margin=ft.Margin.all(10),
            expand=True,
            height=230,
        )

        # 下载完成检测参数
        self.initial_wait_field = ft.TextField(
            label="初始等待时间(秒)", value=str(self.config.get("initial_wait")), width=110,
            text_size=13,
            tooltip="文件出现后等待多少秒才开始检测大小变化",
        )
        self.check_interval_field = ft.TextField(
            label="检查间隔(秒)", value=str(self.config.get("check_interval")), width=110,
            text_size=13,
            tooltip="每隔多少秒检测一次文件大小是否变化",
        )
        self.stable_checks_field = ft.TextField(
            label="稳定次数", value=str(self.config.get("stable_checks")), width=110,
            text_size=13,
            tooltip="文件大小连续多少次不变才认为是下载完成",
        )
        self.debounce_field = ft.TextField(
            label="防抖时间(秒)", value=str(self.config.get("debounce_time")), width=110,
            text_size=13,
            tooltip="文件最后一次修改后等待多久才认为它已稳定，避免频繁触发",
        )

        params_row1 = ft.Row([self.initial_wait_field, self.check_interval_field], spacing=8)
        params_row2 = ft.Row([self.stable_checks_field, self.debounce_field], spacing=8)

        download_card = ft.Container(
            content=ft.Column([
                ft.Text("下载完成检测参数", size=16, weight=ft.FontWeight.BOLD),
                params_row1,
                params_row2,
                ft.FilledButton("保存检测参数", icon=ft.Icons.SAVE, on_click=self._save_detection_settings),
            ], spacing=10, alignment=ft.MainAxisAlignment.CENTER),
            padding=15,
            border=ft.Border.all(1, ft.Colors.GREY_300),
            border_radius=8,
            margin=ft.Margin.all(10),
            expand=True,
            height=230,
        )
        top_row = ft.Row([
            basic_card,
            download_card,
        ], expand=True)

        # 临时文件后缀
        self.temp_field = ft.TextField(
            label="临时文件后缀（一行一个，以 . 开头）",
            multiline=True,
            min_lines=4,
            max_lines=8,
            value="\n".join(self.config.get("temp_extensions")),
            tooltip="这些后缀的文件不会被整理（如下载未完成的临时文件）",
            expand=True,
        )

        temp_card = ft.Container(
            content=ft.Column([
                ft.Text("临时文件后缀", size=16, weight=ft.FontWeight.BOLD),
                self.temp_field,
                ft.Row([
                    ft.FilledButton("保存", icon=ft.Icons.SAVE, on_click=self._save_temp_extensions),
                ], alignment=ft.MainAxisAlignment.END),
            ], spacing=8),
            padding=15,
            border=ft.Border.all(1, ft.Colors.GREY_300),
            border_radius=8,
            margin=ft.Margin.all(10),
            expand=True,
        )

        return ft.Column([
            top_row,
            temp_card,
        ], scroll=ft.ScrollMode.AUTO, expand=True)

    # ===================== 运行日志标签 =====================
    def _build_log_tab(self):
        self.log_field = ft.TextField(
            multiline=True,
            read_only=True,
            expand=True,
            text_size=13,
            value="",
            hint_text="日志内容将在此显示...",
        )

        button_row = ft.Row([
            ft.FilledButton("刷新日志", icon=ft.Icons.REFRESH, on_click=self._refresh_log,
                              tooltip="重新从日志文件中加载最新内容"),
            ft.FilledButton("清空日志", icon=ft.Icons.DELETE_FOREVER, on_click=self._clear_log,
                              tooltip="清空日志文件内容（不可恢复）"),
        ], alignment=ft.MainAxisAlignment.CENTER)

        return ft.Container(
            content=ft.Column([
                self.log_field,
                button_row,
            ], spacing=10, expand=True),
            padding=10,
            expand=True,
        )

    # ===================== 分类列表刷新 =====================
    def _refresh_category_list(self):
        """重新生成分类列表 UI"""
        if self.category_list is None:
            return
        self.category_list.controls.clear()
        file_types = self.config.get("file_types")
        for category in file_types.keys():
            is_selected = (category == self.selected_category)
            tile = ft.ListTile(
                title=ft.Text(category, size=14),
                selected=is_selected,
                selected_color=ft.Colors.WHITE,
                selected_tile_color=ft.Colors.BLUE_400,
                dense=True,
                on_click=lambda e, c=category: self._on_category_click(c),
            )
            self.category_list.controls.append(tile)
        if not self._building:
            self.page.update()

    def _on_category_click(self, category):
        """点击分类列表中的某一项"""
        self.selected_category = category
        self._refresh_category_list()
        # 在右侧后缀编辑器中显示对应后缀
        extensions = self.config.get("file_types").get(category, [])
        self.extension_field.value = "\n".join(extensions)
        self.extension_field.update()

    # ===================== 主页事件处理 =====================
    def _select_folder(self, e):
        self.page.run_task(self._pick_folder)

    async def _pick_folder(self):
        folder = await self.file_picker.get_directory_path(dialog_title="选择要监控的文件夹")
        if folder:
            self.config.set("folder_to_watch", folder)
            self.config.save_config()
            self.organizer.stop_monitoring()
            self.organizer.start_monitoring()
            self.folder_path_text.value = folder
            self.folder_path_text.update()
            self._show_snack(f"目标文件夹已更改为:\n{folder}", success=True)

    def _toggle_monitoring(self, e):
        if self.organizer.is_running:
            self.organizer.stop_monitoring()
            self.status_text.value = "⏸ 已暂停"
            self.status_text.color = ft.Colors.ORANGE
            self.toggle_button.text = "▶ 恢复监控"
            self.toggle_button.icon = ft.Icons.PLAY_ARROW
        else:
            self.organizer.start_monitoring()
            self.status_text.value = "● 监控中"
            self.status_text.color = ft.Colors.GREEN
            self.toggle_button.text = "⏸ 暂停监控"
            self.toggle_button.icon = ft.Icons.PAUSE
        self.status_text.update()
        self.toggle_button.update()

    def _organize_all(self, e):
        threading.Thread(target=self.organizer.organize_existing_files, daemon=True).start()

    def _clear_organize_log(self, e):
        if self.organize_log:
            self.organize_log.value = ""
            self.organize_log.update()

    def _open_folder(self, e):
        try:
            os.startfile(self.config.get("folder_to_watch"))
        except Exception as ex:
            self._show_snack(f"打开文件夹失败: {str(ex)}", success=False)

    # ===================== 文件类型配置事件处理 =====================
    def _add_category(self, e):
        self._show_text_input_dialog("添加分类", "请输入分类名称:", lambda name: self._do_add_category(name))

    def _do_add_category(self, name):
        if not name:
            return
        if name in self.config.get("file_types"):
            self._show_snack("分类名称已存在", success=False)
            return
        file_types = self.config.get("file_types")
        file_types[name] = []
        self.config.set("file_types", file_types)
        self.config.save_config()
        self.selected_category = name
        self._refresh_category_list()
        self.extension_field.value = ""
        self.extension_field.update()
        self._show_snack(f"分类 '{name}' 添加成功", success=True)

    def _delete_category(self, e):
        if not self.selected_category:
            self._show_snack("请先选择要删除的分类", success=False)
            return
        self._show_confirm_dialog(
            f"确定要删除分类 '{self.selected_category}' 吗？\n注意：已分类的文件不会被移动",
            lambda: self._do_delete_category()
        )

    def _do_delete_category(self):
        category = self.selected_category
        file_types = self.config.get("file_types")
        del file_types[category]
        self.config.set("file_types", file_types)
        self.config.save_config()
        self.selected_category = None
        self._refresh_category_list()
        self.extension_field.value = ""
        self.extension_field.update()
        self._show_snack(f"分类 '{category}' 删除成功", success=True)

    def _rename_category(self, e):
        if not self.selected_category:
            self._show_snack("请先选择要重命名的分类", success=False)
            return
        self._show_text_input_dialog(
            "重命名分类",
            "请输入新的分类名称:",
            lambda new_name: self._do_rename_category(new_name),
            default_value=self.selected_category
        )

    def _do_rename_category(self, new_name):
        if not new_name or new_name == self.selected_category:
            return
        if new_name in self.config.get("file_types"):
            self._show_snack("分类名称已存在", success=False)
            return
        old_name = self.selected_category
        file_types = self.config.get("file_types")
        file_types[new_name] = file_types.pop(old_name)
        self.config.set("file_types", file_types)
        self.config.save_config()
        self.selected_category = new_name
        self._refresh_category_list()
        self._show_snack(f"分类已重命名为 '{new_name}'", success=True)

    def _save_file_types(self, e):
        if not self.selected_category:
            self._show_snack("请先选择一个分类", success=False)
            return
        extensions_text = (self.extension_field.value or "").strip()
        extensions = [ext.strip() for ext in extensions_text.split("\n") if ext.strip()]
        for ext in extensions:
            if not ext.startswith("."):
                self._show_snack(f"后缀 '{ext}' 格式错误，必须以 '.' 开头", success=False)
                return
        file_types = self.config.get("file_types")
        file_types[self.selected_category] = extensions
        self.config.set("file_types", file_types)
        if self.config.save_config():
            self.organizer.update_category_folders()
            self._show_snack(f"分类 '{self.selected_category}' 的文件类型已保存", success=True)

    # ===================== 高级设置事件处理 =====================
    def _save_basic_settings(self, e):
        self.config.set("auto_organize_on_startup", self.auto_start_switch.value)
        self.config.save_config()

    def _toggle_dark_mode(self, e):
        mode = "dark" if self.dark_mode_switch.value else "light"
        self.page.theme_mode = ft.ThemeMode.DARK if self.dark_mode_switch.value else ft.ThemeMode.LIGHT
        self.config.set("theme_mode", mode)
        self.config.save_config()
        self.page.update()

    def _save_detection_settings(self, e):
        try:
            self.config.set("initial_wait", int(self.initial_wait_field.value))
            self.config.set("check_interval", int(self.check_interval_field.value))
            self.config.set("stable_checks", int(self.stable_checks_field.value))
            self.config.set("debounce_time", int(self.debounce_field.value))
            if self.config.save_config():
                self._show_snack("检测参数已保存", success=True)
        except ValueError:
            self._show_snack("请输入有效的数字", success=False)

    def _save_temp_extensions(self, e):
        temp_text = (self.temp_field.value or "").strip()
        temp_extensions = [ext.strip() for ext in temp_text.split("\n") if ext.strip()]
        for ext in temp_extensions:
            if not ext.startswith("."):
                self._show_snack(f"后缀 '{ext}' 格式错误，必须以 '.' 开头", success=False)
                return
        self.config.set("temp_extensions", temp_extensions)
        if self.config.save_config():
            self._show_snack("临时文件后缀已保存", success=True)

    def _toggle_boot(self, e):
        enabled = self.boot_switch.value
        if sys.platform != "win32":
            self._show_snack("开机自动启动功能仅支持Windows系统", success=False)
            self.boot_switch.value = False
            self.boot_switch.update()
            return
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_ALL_ACCESS)
            if enabled:
                script_path = os.path.abspath(sys.argv[0])
                if script_path.endswith(".py"):
                    command = f'pythonw.exe "{script_path}"'
                else:
                    command = f'"{script_path}"'
                winreg.SetValueEx(key, "FileOrganizerRobot", 0, winreg.REG_SZ, command)
                self.config.set("start_on_boot", True)
                self._show_snack("已设置开机自动启动", success=True)
            else:
                try:
                    winreg.DeleteValue(key, "FileOrganizerRobot")
                except WindowsError:
                    pass
                self.config.set("start_on_boot", False)
                self._show_snack("已取消开机自动启动", success=True)
            winreg.CloseKey(key)
            self.config.save_config()
        except Exception as ex:
            self._show_snack(f"设置开机启动失败: {str(ex)}", success=False)
            self.boot_switch.value = not enabled
            self.boot_switch.update()

    def _reset_close_action(self, e):
        self.config.set("remember_close_action", False)
        self.config.set("close_action", 0)
        self.config.save_config()
        self._show_snack("关闭选项已重置\n下次关闭窗口时会再次询问", success=True)

    # ===================== 日志事件处理 =====================
    def _refresh_log(self, e):
        log_file = self.config.get("log_file")
        if not os.path.exists(log_file):
            self.log_field.value = "日志文件不存在"
        else:
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    log_content = f.read()
                # 只显示最后 50000 个字符，避免 GUI 卡顿
                self.log_field.value = log_content[-50000:] if len(log_content) > 50000 else log_content
            except Exception as ex:
                self.log_field.value = f"读取日志失败: {str(ex)}"
        if not self._building:
            self.log_field.update()

    def _clear_log(self, e):
        def do_clear():
            log_file = self.config.get("log_file")
            try:
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write("")
                self._refresh_log(None)
                self._show_snack("日志已清空", success=True)
            except Exception as ex:
                self._show_snack(f"清空日志失败: {str(ex)}", success=False)

        self._show_confirm_dialog("确定要清空所有日志吗？", do_clear)

    # ===================== 通用对话框 =====================
    def _show_snack(self, message: str, success: bool = True):
        """显示 SnackBar 提示"""
        self.page.show_dialog(ft.SnackBar(
            content=ft.Row([
                ft.Icon(ft.Icons.CHECK_CIRCLE if success else ft.Icons.ERROR,
                        color=ft.Colors.WHITE, size=18),
                ft.Text(message[:200], size=14),
            ]),
            bgcolor=ft.Colors.GREEN_700 if success else ft.Colors.RED_700,
            duration=3000,
            behavior=ft.SnackBarBehavior.FLOATING,
        ))

    def _show_confirm_dialog(self, message: str, on_confirm):
        """显示确认对话框"""
        def close_yes(e):
            self.page.pop_dialog()
            on_confirm()

        def close_no(e):
            self.page.pop_dialog()

        dialog = ft.AlertDialog(
            title=ft.Text("确认操作"),
            content=ft.Text(message),
            actions=[
                ft.TextButton("取消", on_click=close_no),
                ft.FilledButton("确定", on_click=close_yes),
            ],
        )
        self.page.show_dialog(dialog)

    def _show_text_input_dialog(self, title: str, label: str, on_submit, default_value: str = ""):
        """显示文本输入对话框"""
        input_field = ft.TextField(label=label, value=default_value, autofocus=True)

        def close_ok(e):
            self.page.pop_dialog()
            on_submit(input_field.value)

        def close_cancel(e):
            self.page.pop_dialog()

        dialog = ft.AlertDialog(
            title=ft.Text(title),
            content=input_field,
            actions=[
                ft.TextButton("取消", on_click=close_cancel),
                ft.FilledButton("确定", on_click=close_ok),
            ],
        )
        self.page.show_dialog(dialog)

    # ===================== 系统托盘 =====================
    def _load_tray_icon(self):
        if not TRAY_ENABLED:
            return None
        icon_paths = [
            "icon.ico",
            os.path.join(os.path.dirname(__file__), "icon.ico"),
            os.path.join(sys._MEIPASS, "icon.ico") if getattr(sys, 'frozen', False) else None,
        ]
        icon_paths = [path for path in icon_paths if path is not None]
        icon = None
        for path in icon_paths:
            try:
                icon = Image.open(path)
                icon = icon.resize((32, 32), Image.Resampling.LANCZOS)
                if icon.mode != 'RGBA':
                    icon = icon.convert('RGBA')
                logging.info(f"成功加载图标: {path}")
                break
            except Exception:
                continue
        if icon is None:
            return Image.new('RGB', (32, 32), color=(33, 150, 243))
        return icon

    def _create_tray_icon(self):
        if not TRAY_ENABLED:
            return
        icon = self._load_tray_icon()
        menu = pystray.Menu(
            pystray.MenuItem("显示/隐藏主窗口", self._tray_toggle_window, default=True),
            pystray.MenuItem("立即整理全部",
                           lambda: threading.Thread(target=self.organizer.organize_existing_files, daemon=True).start()),
            pystray.MenuItem("暂停/恢复监控", self._tray_toggle_monitoring,
                           checked=lambda item: not self.organizer.is_running),
            pystray.MenuItem("退出", self._tray_quit),
        )
        self.tray_icon = pystray.Icon(
            "file_organizer_robot_flet",
            icon,
            "文件夹自动整理机器人",
            menu,
        )
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()
        logging.info("✅ 系统托盘图标创建成功")

    def _tray_toggle_window(self, icon=None, item=None):
        """托盘：切换窗口显示/隐藏"""
        self._window_visible = not self._window_visible
        self.page.run_task(self._tray_toggle_window_async, self._window_visible)

    async def _tray_toggle_window_async(self, show: bool):
        if show:
            self.page.window.visible = True
            self.page.window.focused = True
        else:
            self.page.window.visible = False
        self.page.update()

    def _tray_toggle_monitoring(self, icon, item):
        """托盘：切换监控状态"""
        if self.organizer.is_running:
            self.organizer.stop_monitoring()
            self.status_text.value = "⏸ 已暂停"
            self.status_text.color = ft.Colors.ORANGE
            self.toggle_button.text = "▶ 恢复监控"
            self.toggle_button.icon = ft.Icons.PLAY_ARROW
        else:
            self.organizer.start_monitoring()
            self.status_text.value = "● 监控中"
            self.status_text.color = ft.Colors.GREEN
            self.toggle_button.text = "⏸ 暂停监控"
            self.toggle_button.icon = ft.Icons.PAUSE
        if self.status_text:
            self.status_text.update()
        if self.toggle_button:
            self.toggle_button.update()

    def _tray_quit(self, icon=None, item=None):
        """托盘：退出程序"""
        self._quitting = True
        if self.organizer.observer:
            self.organizer.observer.stop()
            self.organizer.observer.join()
            logging.info("文件监控已停止")
        if self.tray_icon:
            self.tray_icon.stop()
            logging.info("系统托盘已关闭")
        logging.info("程序正常退出")
        self.page.window.prevent_close = False
        self.page.run_task(self._safe_close_window)

    # ===================== 窗口关闭事件 =====================
    def _on_window_event(self, e):
        """拦截窗口关闭事件"""
        if e.type == ft.WindowEventType.CLOSE and not self._quitting:
            self._handle_close()

    def _handle_close(self):
        """处理窗口关闭"""
        # 如果用户选择了"记住关闭选项"
        if self.config.get("remember_close_action"):
            action = self.config.get("close_action")
            if action == 1:
                self.page.window.visible = False
                self.page.update()
                return
            else:
                self._do_quit()
                return

        # 如果托盘不可用，直接确认退出
        if not TRAY_ENABLED:
            self._show_confirm_dialog("确定要退出程序吗？", self._do_quit)
            return

        # 显示关闭选项对话框
        remember_var = [False]  # 用列表包装以实现可变引用（闭包内修改需要可变容器）

        def on_yes(e):
            self.page.pop_dialog()
            if remember_var[0]:
                self.config.set("remember_close_action", True)
                self.config.set("close_action", 1)
                self.config.save_config()
            self.page.window.visible = False
            self.page.update()

        def on_no(e):
            self.page.pop_dialog()
            if remember_var[0]:
                self.config.set("remember_close_action", True)
                self.config.set("close_action", 2)
                self.config.save_config()
            self._do_quit()

        remember_check = ft.Checkbox(
            label="记住我的选择，下次不再询问",
            value=False,
            on_change=lambda e: remember_var.__setitem__(0, e.control.value),
        )

        dialog = ft.AlertDialog(
            title=ft.Text("提示", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Column([
                ft.Text("是否最小化到系统托盘？"),
                ft.Text("✅ 是：程序继续在后台运行\n❌ 否：完全退出程序", size=13, color=ft.Colors.GREY_600),
                remember_check,
            ], spacing=10, tight=True, height=130),
            actions=[
                ft.FilledButton("是（最小化）", on_click=on_yes),
                ft.TextButton("否（退出）", on_click=on_no),
            ],
        )
        self.page.show_dialog(dialog)
    def _do_quit(self):
        """执行退出"""
        self._quitting = True
        logging.info("正在退出程序...")
        if self.organizer.observer:
            self.organizer.observer.stop()
            self.organizer.observer.join()
            logging.info("文件监控已停止")
        if self.tray_icon:
            self.tray_icon.stop()
            logging.info("系统托盘已关闭")
        logging.info("程序正常退出")
        self.page.window.prevent_close = False
        self.page.run_task(self._safe_close_window)

    async def _safe_close_window(self):
        """安全关闭窗口，忽略 Session closed 错误"""
        try:
            await self.page.window.destroy()
        except Exception:
            pass  # 会话已关闭，这是正常行为


# ===================== 程序入口 =====================
def main(page: ft.Page):
    """Flet 入口函数"""

    # ---- 尽早设置窗口图标（必须在构建界面前设置，否则 Windows 下不会生效）----
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        script_dir = os.getcwd()
    icon_path = os.path.join(script_dir, "icon.ico").replace("\\", "/")
    if os.path.exists(icon_path):
        page.window.icon = icon_path
        logging.info(f"窗口图标已设置: {icon_path}")
    else:
        logging.warning(f"图标文件不存在: {icon_path}")

    app = FletFileOrganizerApp(page)
    page.update()


if __name__ == "__main__":
    try:
        ft.run(main)
    except Exception as e:
        logging.critical(f"程序崩溃: {e}", exc_info=True)
        # Flet 环境下的错误提示
        print(f"发生严重错误: {str(e)}\n请查看日志文件获取详细信息")
