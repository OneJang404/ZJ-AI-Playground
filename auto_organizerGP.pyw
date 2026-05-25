import os
import shutil
import time
import logging
import threading
import json
import sys
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

# 尝试导入系统托盘模块
try:
    import pystray
    from PIL import Image
    TRAY_ENABLED = True
except ImportError:
    TRAY_ENABLED = False
    print("⚠️  未安装pystray或pillow库，系统托盘功能已禁用")

# ===================== 配置管理类 =====================
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
            "close_action": 0  # 0=询问，1=最小化到托盘，2=完全退出
        }
        self.load_config()
    
    def load_config(self):
        """从文件加载配置"""
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
        """保存配置到文件"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            logging.info("✅ 配置文件保存成功")
            return True
        except Exception as e:
            logging.error(f"❌ 保存配置文件失败: {e}")
            return False
    
    def get(self, key):
        """获取配置值"""
        return self.config.get(key, self.default_config.get(key))
    
    def set(self, key, value):
        """设置配置值"""
        self.config[key] = value

# ===================== 核心功能类 =====================
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
        """配置日志系统"""
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
        """生成唯一文件路径"""
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
        """检测文件是否下载完成"""
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
        """更新分类文件夹集合"""
        self.category_folders.clear()
        folder_to_watch = self.config.get("folder_to_watch")
        for folder in self.config.get("file_types").keys():
            self.category_folders.add(os.path.join(folder_to_watch, folder))
        self.category_folders.add(os.path.join(folder_to_watch, "其他"))
        logging.debug(f"更新分类文件夹集合: {self.category_folders}")
    
    def organize_file(self, file_path):
        """整理单个文件"""
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
        """整理所有已有文件"""
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
        """防抖处理文件"""
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
        """启动文件监控"""
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
        """停止文件监控"""
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
        """统一处理文件事件"""
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

# ===================== 图形化界面类 =====================
class FileOrganizerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("文件夹自动整理机器人")
        self.root.geometry("800x600")
        self.root.resizable(True, True)
        
        self.config = Config()
        self.organizer = FileOrganizer(self.config, self.send_notification)
        self.organizer.setup_logging()
        
        self.tray_icon = None
        self.tray_thread = None
        
        self.create_widgets()
        
        if self.config.get("auto_organize_on_startup"):
            threading.Thread(target=self.organizer.organize_existing_files, daemon=True).start()
        
        self.organizer.start_monitoring()
        
        if TRAY_ENABLED:
            self.create_tray_icon()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        logging.info("="*60)
        logging.info("文件夹自动整理机器人启动")
        logging.info("="*60)
    
    def create_widgets(self):
        """创建界面元素"""
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.home_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.home_frame, text="主页")
        
        self.file_types_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.file_types_frame, text="文件类型配置")
        
        self.settings_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.settings_frame, text="高级设置")
        
        self.log_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.log_frame, text="运行日志")
        
        self.create_home_tab()
        self.create_file_types_tab()
        self.create_settings_tab()
        self.create_log_tab()
    
    # ---------- 悬浮提示工具 ----------
    def setup_tooltip(self, widget, text):
        """为任意 tkinter 控件添加鼠标悬浮提示"""
        tooltip = None
        
        def enter(event):
            nonlocal tooltip
            x, y, _, _ = widget.bbox("insert")
            x += widget.winfo_rootx() + 25
            y += widget.winfo_rooty() + 20
            # 创建顶层窗口
            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{x}+{y}")
            label = ttk.Label(tooltip, text=text, background="#ffffe0", relief="solid", borderwidth=1, font=("微软雅黑", 9))
            label.pack()
        
        def leave(event):
            nonlocal tooltip
            if tooltip:
                tooltip.destroy()
                tooltip = None
        
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)
    
    def create_home_tab(self):
        """创建主页标签"""
        folder_frame = ttk.LabelFrame(self.home_frame, text="监控文件夹")
        folder_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.folder_var = tk.StringVar(value=self.config.get("folder_to_watch"))
        folder_entry = ttk.Entry(folder_frame, textvariable=self.folder_var, state="readonly")
        folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, pady=5)
        self.setup_tooltip(folder_entry, "当前被监控的文件夹路径，所有新文件或修改的文件都会被自动整理")
        
        select_btn = ttk.Button(folder_frame, text="选择", command=self.select_folder)
        select_btn.pack(side=tk.RIGHT, padx=5, pady=5)
        self.setup_tooltip(select_btn, "点击更换要监控的文件夹")
        
        status_frame = ttk.LabelFrame(self.home_frame, text="运行状态")
        status_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.status_var = tk.StringVar(value="监控中")
        status_label = ttk.Label(status_frame, textvariable=self.status_var, font=("Arial", 12, "bold"))
        status_label.pack(side=tk.LEFT, padx=5, pady=5)
        self.setup_tooltip(status_label, "显示当前监控状态：监控中 或 已暂停")
        
        button_frame = ttk.Frame(self.home_frame)
        button_frame.pack(fill=tk.X, padx=10, pady=20)
        
        self.toggle_button = ttk.Button(button_frame, text="暂停监控", command=self.toggle_monitoring)
        self.toggle_button.pack(side=tk.LEFT, padx=5, pady=5)
        self.setup_tooltip(self.toggle_button, "暂停/恢复对文件的实时监控，暂停后新文件不会被自动整理")
        
        organize_all_btn = ttk.Button(button_frame, text="立即整理全部", command=self.organize_all)
        organize_all_btn.pack(side=tk.LEFT, padx=5, pady=5)
        self.setup_tooltip(organize_all_btn, "立即扫描监控文件夹下的所有文件并按规则整理（不影响正在下载的文件）")
        
        open_folder_btn = ttk.Button(button_frame, text="打开监控文件夹", command=self.open_folder)
        open_folder_btn.pack(side=tk.LEFT, padx=5, pady=5)
        self.setup_tooltip(open_folder_btn, "在资源管理器中打开当前监控的文件夹")
        
        tip_frame = ttk.LabelFrame(self.home_frame, text="使用提示")
        tip_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        tips = [
            "• 程序会自动监控指定文件夹及其所有子文件夹",
            "• 新下载的文件会在下载完成后自动分类",
            "• 修改文件后缀或重命名会自动重新分类",
            "• 点击右下角托盘图标可以切换窗口显示/隐藏",
            "• 所有设置会自动保存，下次启动自动生效"
        ]
        
        for tip in tips:
            ttk.Label(tip_frame, text=tip, wraplength=750, justify=tk.LEFT).pack(anchor=tk.W, padx=5, pady=2)
    
    def create_file_types_tab(self):
        """创建文件类型配置标签"""
        left_frame = ttk.Frame(self.file_types_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        ttk.Label(left_frame, text="文件分类", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        
        self.category_listbox = tk.Listbox(left_frame, width=20, height=15)
        self.category_listbox.pack(fill=tk.BOTH, expand=True, pady=5)
        self.setup_tooltip(self.category_listbox, "显示所有分类名称，点击某个分类可在右侧编辑其后缀列表")
        self.category_listbox.bind("<<ListboxSelect>>", self.on_category_select)
        
        cat_button_frame = ttk.Frame(left_frame)
        cat_button_frame.pack(fill=tk.X, pady=5)
        
        add_btn = ttk.Button(cat_button_frame, text="添加", command=self.add_category)
        add_btn.pack(side=tk.LEFT, padx=2)
        self.setup_tooltip(add_btn, "新建一个文件分类（例如「文档」、「视频」）")
        
        del_btn = ttk.Button(cat_button_frame, text="删除", command=self.delete_category)
        del_btn.pack(side=tk.LEFT, padx=2)
        self.setup_tooltip(del_btn, "删除选中的分类，之前已分类的文件不会被移动")
        
        rename_btn = ttk.Button(cat_button_frame, text="重命名", command=self.rename_category)
        rename_btn.pack(side=tk.LEFT, padx=2)
        self.setup_tooltip(rename_btn, "修改选中分类的名称")
        
        right_frame = ttk.Frame(self.file_types_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        ttk.Label(right_frame, text="文件后缀", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        
        self.extension_text = scrolledtext.ScrolledText(right_frame, width=40, height=15)
        self.extension_text.pack(fill=tk.BOTH, expand=True, pady=5)
        self.setup_tooltip(self.extension_text, "每行一个文件后缀（例如 .jpg .png），注意必须包含点号，不支持通配符")
        
        save_btn = ttk.Button(right_frame, text="保存修改", command=self.save_file_types)
        save_btn.pack(anchor=tk.E, pady=5)
        self.setup_tooltip(save_btn, "保存当前分类的后缀配置到文件中")
        
        self.load_categories()
    
    def create_settings_tab(self):
        """创建高级设置标签"""
        basic_frame = ttk.LabelFrame(self.settings_frame, text="基本设置")
        basic_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.auto_start_var = tk.BooleanVar(value=self.config.get("auto_organize_on_startup"))
        auto_check = ttk.Checkbutton(basic_frame, text="启动时自动整理已有文件", variable=self.auto_start_var, command=self.save_settings)
        auto_check.pack(anchor=tk.W, padx=5, pady=5)
        self.setup_tooltip(auto_check, "程序启动后立即扫描一次监控文件夹，整理所有现有文件（建议初次使用时开启）")
        
        self.boot_var = tk.BooleanVar(value=self.config.get("start_on_boot"))
        boot_check = ttk.Checkbutton(basic_frame, text="开机自动启动", variable=self.boot_var, command=self.toggle_boot)
        boot_check.pack(anchor=tk.W, padx=5, pady=5)
        self.setup_tooltip(boot_check, "将本程序添加到Windows开机启动项（仅支持Windows）")
        
        reset_btn = ttk.Button(basic_frame, text="重置关闭选项", command=self.reset_close_action)
        reset_btn.pack(anchor=tk.W, padx=5, pady=5)
        self.setup_tooltip(reset_btn, "清除“记住关闭选项”的设置，下次关闭窗口时会再次询问是最小化还是完全退出")
        
        download_frame = ttk.LabelFrame(self.settings_frame, text="下载完成检测")
        download_frame.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(download_frame, text="初始等待时间(秒):").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.initial_wait_var = tk.StringVar(value=str(self.config.get("initial_wait")))
        init_entry = ttk.Entry(download_frame, textvariable=self.initial_wait_var, width=10)
        init_entry.grid(row=0, column=1, padx=5, pady=5)
        self.setup_tooltip(init_entry, "文件出现后等待多少秒才开始检测大小变化（避免刚创建就检测）")
        
        ttk.Label(download_frame, text="检查间隔(秒):").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.check_interval_var = tk.StringVar(value=str(self.config.get("check_interval")))
        interval_entry = ttk.Entry(download_frame, textvariable=self.check_interval_var, width=10)
        interval_entry.grid(row=1, column=1, padx=5, pady=5)
        self.setup_tooltip(interval_entry, "每隔多少秒检测一次文件大小是否变化")
        
        ttk.Label(download_frame, text="稳定次数:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.stable_checks_var = tk.StringVar(value=str(self.config.get("stable_checks")))
        stable_entry = ttk.Entry(download_frame, textvariable=self.stable_checks_var, width=10)
        stable_entry.grid(row=2, column=1, padx=5, pady=5)
        self.setup_tooltip(stable_entry, "文件大小连续多少次不变才认为是下载完成（数值越大越严格）")
        
        ttk.Label(download_frame, text="防抖时间(秒):").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        self.debounce_var = tk.StringVar(value=str(self.config.get("debounce_time")))
        debounce_entry = ttk.Entry(download_frame, textvariable=self.debounce_var, width=10)
        debounce_entry.grid(row=3, column=1, padx=5, pady=5)
        self.setup_tooltip(debounce_entry, "文件最后一次修改后等待多久才认为它已稳定，避免频繁触发")
        
        save_btn = ttk.Button(download_frame, text="保存设置", command=self.save_settings)
        save_btn.grid(row=4, column=0, columnspan=2, pady=10)
        self.setup_tooltip(save_btn, "保存以上所有检测相关的参数")
        
        temp_frame = ttk.LabelFrame(self.settings_frame, text="临时文件后缀（一行一个）")
        temp_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.temp_text = scrolledtext.ScrolledText(temp_frame, height=5)
        self.temp_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.setup_tooltip(self.temp_text, "这些后缀的文件不会被整理（如下载未完成的临时文件），每行一个，必须包含点号")
        
        temp_extensions = self.config.get("temp_extensions")
        self.temp_text.insert(tk.END, "\n".join(temp_extensions))
        
        save_temp_btn = ttk.Button(temp_frame, text="保存", command=self.save_temp_extensions)
        save_temp_btn.pack(anchor=tk.E, padx=5, pady=5)
        self.setup_tooltip(save_temp_btn, "保存临时文件后缀列表")
    
    def create_log_tab(self):
        """创建日志标签"""
        self.log_text = scrolledtext.ScrolledText(self.log_frame, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.setup_tooltip(self.log_text, "显示程序的运行日志，包括文件整理记录、错误信息等")
        
        button_frame = ttk.Frame(self.log_frame)
        button_frame.pack(fill=tk.X, padx=10, pady=5)
        
        refresh_btn = ttk.Button(button_frame, text="刷新日志", command=self.refresh_log)
        refresh_btn.pack(side=tk.LEFT, padx=5)
        self.setup_tooltip(refresh_btn, "重新从日志文件中加载最新内容")
        
        clear_btn = ttk.Button(button_frame, text="清空日志", command=self.clear_log)
        clear_btn.pack(side=tk.LEFT, padx=5)
        self.setup_tooltip(clear_btn, "清空日志文件内容（不可恢复，请谨慎操作）")
        
        self.refresh_log()
    
    # ===================== 界面事件处理 =====================
    def select_folder(self):
        """选择监控文件夹"""
        folder = filedialog.askdirectory(title="选择要监控的文件夹")
        if folder:
            self.folder_var.set(folder)
            self.config.set("folder_to_watch", folder)
            self.config.save_config()
            
            self.organizer.stop_monitoring()
            self.organizer.start_monitoring()
            
            messagebox.showinfo("成功", f"监控文件夹已更改为:\n{folder}")
    
    def toggle_monitoring(self):
        """切换监控状态"""
        if self.organizer.is_running:
            self.organizer.stop_monitoring()
            self.status_var.set("已暂停")
            self.toggle_button.config(text="恢复监控")
        else:
            self.organizer.start_monitoring()
            self.status_var.set("监控中")
            self.toggle_button.config(text="暂停监控")
    
    def organize_all(self):
        """立即整理全部文件"""
        threading.Thread(target=self.organizer.organize_existing_files, daemon=True).start()
    
    def open_folder(self):
        """打开监控文件夹"""
        try:
            os.startfile(self.config.get("folder_to_watch"))
        except Exception as e:
            messagebox.showerror("错误", f"打开文件夹失败: {str(e)}")
    
    def load_categories(self):
        """加载分类列表"""
        self.category_listbox.delete(0, tk.END)
        for category in self.config.get("file_types").keys():
            self.category_listbox.insert(tk.END, category)
    
    def on_category_select(self, event):
        """分类选择事件"""
        selection = self.category_listbox.curselection()
        if not selection:
            return
        
        category = self.category_listbox.get(selection[0])
        extensions = self.config.get("file_types").get(category, [])
        
        self.extension_text.delete(1.0, tk.END)
        self.extension_text.insert(tk.END, "\n".join(extensions))
    
    def add_category(self):
        """添加新分类"""
        name = simpledialog.askstring("添加分类", "请输入分类名称:")
        if not name:
            return
        
        if name in self.config.get("file_types"):
            messagebox.showerror("错误", "分类名称已存在")
            return
        
        file_types = self.config.get("file_types")
        file_types[name] = []
        self.config.set("file_types", file_types)
        self.config.save_config()
        
        self.load_categories()
        messagebox.showinfo("成功", f"分类 '{name}' 添加成功")
    
    def delete_category(self):
        """删除分类"""
        selection = self.category_listbox.curselection()
        if not selection:
            messagebox.showwarning("警告", "请先选择要删除的分类")
            return
        
        category = self.category_listbox.get(selection[0])
        
        if not messagebox.askyesno("确认", f"确定要删除分类 '{category}' 吗？\n注意：已分类的文件不会被移动"):
            return
        
        file_types = self.config.get("file_types")
        del file_types[category]
        self.config.set("file_types", file_types)
        self.config.save_config()
        
        self.load_categories()
        self.extension_text.delete(1.0, tk.END)
        messagebox.showinfo("成功", f"分类 '{category}' 删除成功")
    
    def rename_category(self):
        """重命名分类"""
        selection = self.category_listbox.curselection()
        if not selection:
            messagebox.showwarning("警告", "请先选择要重命名的分类")
            return
        
        old_name = self.category_listbox.get(selection[0])
        new_name = simpledialog.askstring("重命名分类", "请输入新的分类名称:", initialvalue=old_name)
        
        if not new_name or new_name == old_name:
            return
        
        if new_name in self.config.get("file_types"):
            messagebox.showerror("错误", "分类名称已存在")
            return
        
        file_types = self.config.get("file_types")
        file_types[new_name] = file_types.pop(old_name)
        self.config.set("file_types", file_types)
        self.config.save_config()
        
        self.load_categories()
        messagebox.showinfo("成功", f"分类已重命名为 '{new_name}'")
    
    def save_file_types(self):
        """保存文件类型配置"""
        selection = self.category_listbox.curselection()
        if not selection:
            messagebox.showwarning("警告", "请先选择一个分类")
            return
        
        category = self.category_listbox.get(selection[0])
        extensions_text = self.extension_text.get(1.0, tk.END).strip()
        extensions = [ext.strip() for ext in extensions_text.split("\n") if ext.strip()]
        
        for ext in extensions:
            if not ext.startswith("."):
                messagebox.showerror("错误", f"后缀 '{ext}' 格式错误，必须以 '.' 开头")
                return
        
        file_types = self.config.get("file_types")
        file_types[category] = extensions
        self.config.set("file_types", file_types)
        
        if self.config.save_config():
            self.organizer.update_category_folders()
            messagebox.showinfo("成功", f"分类 '{category}' 的文件类型已保存")
    
    def save_settings(self):
        """保存高级设置"""
        try:
            self.config.set("auto_organize_on_startup", self.auto_start_var.get())
            self.config.set("initial_wait", int(self.initial_wait_var.get()))
            self.config.set("check_interval", int(self.check_interval_var.get()))
            self.config.set("stable_checks", int(self.stable_checks_var.get()))
            self.config.set("debounce_time", int(self.debounce_var.get()))
            
            if self.config.save_config():
                messagebox.showinfo("成功", "设置已保存")
        except ValueError:
            messagebox.showerror("错误", "请输入有效的数字")
    
    def save_temp_extensions(self):
        """保存临时文件后缀"""
        temp_text = self.temp_text.get(1.0, tk.END).strip()
        temp_extensions = [ext.strip() for ext in temp_text.split("\n") if ext.strip()]
        
        for ext in temp_extensions:
            if not ext.startswith("."):
                messagebox.showerror("错误", f"后缀 '{ext}' 格式错误，必须以 '.' 开头")
                return
        
        self.config.set("temp_extensions", temp_extensions)
        
        if self.config.save_config():
            messagebox.showinfo("成功", "临时文件后缀已保存")
    
    def toggle_boot(self):
        """切换开机自动启动"""
        enabled = self.boot_var.get()
        
        if sys.platform != "win32":
            messagebox.showwarning("警告", "开机自动启动功能仅支持Windows系统")
            self.boot_var.set(False)
            return
        
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
            
            if enabled:
                script_path = os.path.abspath(sys.argv[0])
                if script_path.endswith(".py"):
                    command = f'pythonw.exe "{script_path}"'
                else:
                    command = f'"{script_path}"'
                
                winreg.SetValueEx(key, "FileOrganizerRobot", 0, winreg.REG_SZ, command)
                self.config.set("start_on_boot", True)
                messagebox.showinfo("成功", "已设置开机自动启动")
            else:
                try:
                    winreg.DeleteValue(key, "FileOrganizerRobot")
                except WindowsError:
                    pass
                self.config.set("start_on_boot", False)
                messagebox.showinfo("成功", "已取消开机自动启动")
            
            winreg.CloseKey(key)
            self.config.save_config()
        
        except Exception as e:
            messagebox.showerror("错误", f"设置开机启动失败: {str(e)}")
            self.boot_var.set(not enabled)
    
    def refresh_log(self):
        """刷新日志显示"""
        log_file = self.config.get("log_file")
        
        if not os.path.exists(log_file):
            self.log_text.config(state=tk.NORMAL)
            self.log_text.delete(1.0, tk.END)
            self.log_text.insert(tk.END, "日志文件不存在")
            self.log_text.config(state=tk.DISABLED)
            return
        
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log_content = f.read()
            
            self.log_text.config(state=tk.NORMAL)
            self.log_text.delete(1.0, tk.END)
            self.log_text.insert(tk.END, log_content)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        except Exception as e:
            messagebox.showerror("错误", f"读取日志失败: {str(e)}")
    
    def clear_log(self):
        """清空日志文件"""
        if not messagebox.askyesno("确认", "确定要清空所有日志吗？"):
            return
        
        log_file = self.config.get("log_file")
        
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                f.write("")
            
            self.refresh_log()
            messagebox.showinfo("成功", "日志已清空")
        except Exception as e:
            messagebox.showerror("错误", f"清空日志失败: {str(e)}")
    
    def reset_close_action(self):
        """重置记住的关闭选项"""
        self.config.set("remember_close_action", False)
        self.config.set("close_action", 0)
        self.config.save_config()
        messagebox.showinfo("成功", "关闭选项已重置\n下次关闭窗口时会再次询问")
    
    # ===================== 系统托盘相关 =====================
    def load_tray_icon(self):
        """加载托盘图标"""
        if not TRAY_ENABLED:
            return None
        
        # 尝试多种方式加载图标
        icon_paths = [
            "icon.ico",  # 当前目录
            os.path.join(os.path.dirname(__file__), "icon.ico"),  # 脚本所在目录
            os.path.join(sys._MEIPASS, "icon.ico") if getattr(sys, 'frozen', False) else None,  # 打包后的临时目录
        ]
        
        icon_paths = [path for path in icon_paths if path is not None]
        
        icon = None
        last_error = None
        
        for path in icon_paths:
            try:
                icon = Image.open(path)
                icon = icon.resize((32, 32), Image.Resampling.LANCZOS)
                if icon.mode != 'RGBA':
                    icon = icon.convert('RGBA')
                logging.info(f"成功加载图标: {path}")
                break
            except Exception as e:
                last_error = e
                continue
        
        if icon is None:
            logging.error(f"所有图标加载尝试失败，最后错误: {last_error}")
            # 备用蓝色图
            return Image.new('RGB', (32, 32), color=(33, 150, 243))
        
        return icon
    
    def create_tray_icon(self):
        """创建系统托盘图标"""
        if not TRAY_ENABLED:
            return
        
        icon = self.load_tray_icon()
        
        menu = pystray.Menu(
            pystray.MenuItem("显示/隐藏主窗口", self.toggle_window, default=True),
            pystray.MenuItem("立即整理全部", lambda: threading.Thread(target=self.organizer.organize_existing_files, daemon=True).start()),
            pystray.MenuItem("暂停监控", self.toggle_monitoring_tray, checked=lambda item: not self.organizer.is_running),
            pystray.MenuItem("退出", self.quit_app)
        )
        
        self.tray_icon = pystray.Icon(
            "file_organizer_robot",
            icon,
            "文件夹自动整理机器人",
            menu,
        )
        
        self.tray_icon.run_detached()
        logging.info("✅ 系统托盘图标创建成功")
    
    def toggle_window(self, icon=None, item=None):
        """切换主窗口显示/隐藏（单击托盘图标一键切换）"""
        def _toggle():
            if not self.root.winfo_exists():
                return
            
            if self.root.state() == 'withdrawn' or self.root.state() == 'iconified':
                self.root.deiconify()
                self.root.state('normal')
                self.root.lift()
                self.root.focus_force()
                self.root.attributes('-topmost', True)
                self.root.attributes('-topmost', False)
            else:
                self.root.withdraw()
        
        self.root.after(0, _toggle)
    
    def toggle_monitoring_tray(self, icon, item):
        """托盘菜单切换监控"""
        self.toggle_monitoring()
    
    def quit_app(self, icon=None, item=None):
        """退出应用"""
        logging.info("正在退出程序...")
        
        
        if self.organizer.observer:
            self.organizer.observer.stop()
            self.organizer.observer.join()
            logging.info("文件监控已停止")
        
        if self.tray_icon:
            self.tray_icon.stop()
            logging.info("系统托盘已关闭")
        
        logging.info("程序正常退出")
        self.root.quit()
        os._exit(0)
    
    def on_close(self):
        """窗口关闭事件（带记住选项功能）"""
        if self.config.get("remember_close_action"):
            action = self.config.get("close_action")
            if action == 1:
                self.root.withdraw()
            else:
                self.quit_app()
            return
        
        dialog = tk.Toplevel(self.root)
        dialog.title("提示")
        dialog.geometry("350x160")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")
        
        ttk.Label(dialog, text="是否最小化到系统托盘？", font=("Arial", 10, "bold")).pack(pady=10)
        ttk.Label(dialog, text="✅ 点击是：程序继续在后台运行\n❌ 点击否：完全退出程序", justify=tk.LEFT).pack(padx=20)
        
        remember_var = tk.BooleanVar(value=False)
        remember_check = ttk.Checkbutton(dialog, text="记住我的选择，下次不再询问", variable=remember_var)
        remember_check.pack(pady=8)
        self.setup_tooltip(remember_check, "勾选后，以后关闭窗口都会自动按照本次选择操作，不再弹出此对话框")
        
        button_frame = ttk.Frame(dialog)
        button_frame.pack(pady=5)
        
        def on_yes():
            dialog.destroy()
            if remember_var.get():
                self.config.set("remember_close_action", True)
                self.config.set("close_action", 1)
                self.config.save_config()
            self.root.withdraw()
        
        def on_no():
            dialog.destroy()
            if remember_var.get():
                self.config.set("remember_close_action", True)
                self.config.set("close_action", 2)
                self.config.save_config()
            self.quit_app()
        
        yes_btn = ttk.Button(button_frame, text="是", command=on_yes, width=10)
        yes_btn.pack(side=tk.LEFT, padx=10)
        self.setup_tooltip(yes_btn, "程序最小化到系统托盘，继续后台运行")
        
        no_btn = ttk.Button(button_frame, text="否", command=on_no, width=10)
        no_btn.pack(side=tk.RIGHT, padx=10)
        self.setup_tooltip(no_btn, "完全退出程序，停止所有功能")
    
    def send_notification(self, title, message):
        """发送桌面通知"""
        logging.info(f"通知: {title} - {message}")
        if TRAY_ENABLED and self.tray_icon:
            try:
                self.tray_icon.notify(message, title)
            except Exception as e:
                logging.warning(f"发送通知失败: {e}")

# ===================== 程序入口 =====================
if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = FileOrganizerGUI(root)
        root.mainloop()
    except Exception as e:
        logging.critical(f"程序崩溃: {e}", exc_info=True)
        messagebox.showerror("程序崩溃", f"发生严重错误: {str(e)}\n请查看日志文件获取详细信息")