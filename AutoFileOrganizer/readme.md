# 文件夹自动整理机器人 — Flet 版改进说明

## 概览

原版基于 **tkinter**（`auto_organizerGP.pyw`），新版使用 **Flet**（`auto_organizer_flet.py`）完全重写了图形界面。核心功能（文件监控、下载检测、配置管理）保持不变，UI 层面全面升级。

---

## 主要改进

### 1. 界面框架：tkinter → Flet（Flutter）

| 方面 | 原版 (tkinter) | 新版 (Flet) |
|------|---------------|-------------|
| UI 引擎 | tkinter/ttk，Windows 经典风格 | Flet（Flutter 渲染），Material Design 3 |
| 视觉效果 | 系统原生控件，样式陈旧 | 现代圆角卡片、阴影、动画过渡 |
| 图标系统 | 无图标 | Material Icons 全套图标 |
| 响应式布局 | `pack()`/`grid()` 手动布局 | `expand=True` 自适应填充 |
| 控件悬停提示 | 约 30 行手动实现 `Toplevel` 弹窗 | 所有控件原生支持 `tooltip` 参数 |

### 2. 选项卡（Tabs）重构

- **原版**：`ttk.Notebook` 标签页，纯文字标题，无法定制外观
- **新版**：`ft.Tabs` + `TabBar` + `TabBarView`，标签带 emoji 图标（🏠 📁 ⚙️ 📋），可独立控制标签栏和内容区

### 3. 主页：使用提示 → 实时整理日志

- **原版**：主页底部是静态"使用提示"（5 条固定文字）
- **新版**：替换为**实时整理记录**文本框，每次文件整理都会追加带时间戳的记录：
  ```
  [14:32:05] 文件整理成功: report.pdf → 文档
  [14:32:18] 文件整理成功: photo.jpg → 图片
  ```
  支持一键清空记录

### 4. 深色模式

- **原版**：不支持
- **新版**："高级设置"→"基本设置"中新增深色模式开关，切换即时生效，偏好自动保存

### 5. 设置面板布局

- **原版**："基本设置"、"下载完成检测"、"临时文件后缀"三个区域**垂直堆叠**，需要滚动查看
- **新版**："基本设置"和"下载完成检测参数"**水平并排**为等大卡片，充分利用宽屏空间；仅"临时文件后缀"在下行独立显示

### 6. 开关控件 (Switch)

- **原版**：`ttk.Checkbutton` 勾选框，文字在右侧
- **新版**：`ft.Switch` 滑动开关，Material Design 风格，文字在左侧，视觉更现代

### 7. 对话框系统

- **原版**：
  - 文件夹选择：`filedialog.askdirectory()` 阻塞式原生对话框
  - 文本输入：`simpledialog.askstring()` 阻塞式
  - 确认提示：`messagebox.askyesno()` 阻塞式
  - 通知提示：`messagebox.showinfo()` 阻塞式
- **新版**：
  - 文件夹选择：`FilePicker.get_directory_path()` 异步非阻塞
  - 文本输入/确认：自定义 `AlertDialog`，内容灵活可定制
  - 操作反馈：`SnackBar` 底部浮动提示（不打断操作），带成功/失败图标和颜色

### 8. 按钮样式

- **原版**：`ttk.Button` 平面按钮
- **新版**：`ft.FilledButton` 填充按钮 + `ft.IconButton` 图标按钮 + `ft.TextButton` 文字按钮，视觉层次分明

### 9. 窗口管理

- **原版**：`root.protocol("WM_DELETE_WINDOW")` 处理关闭事件
- **新版**：`page.window.on_event` 事件系统，支持 `prevent_close` 拦截 + 自定义关闭对话框，`page.window.visible/focused` 精细控制窗口显隐

### 10. 文件选择器

- **原版**：`filedialog.askdirectory()`，同步阻塞主线程
- **新版**：`ft.FilePicker` 服务，`async/await` 异步调用，`page.run_task()` 从同步事件调度，不阻塞界面

---

## 保持不变

- 底层核心逻辑完全继承原版：
  - `Config` 类 —— JSON 配置文件读写
  - `FileOrganizer` 类 —— 文件分类、移动、去重
  - `FileOrganizerHandler` 类 —— watchdog 事件处理
  - 下载完成检测算法（大小稳定检测 + 防抖）
- `pystray` 系统托盘功能
- 开机自启动（Windows 注册表）

---

## 运行环境要求

| 依赖 | 原版 | 新版 |
|------|------|------|
| Python | 3.x | 3.14+ |
| watchdog | ✅ | ✅ |
| pystray | 可选 | 可选 |
| Pillow | pystray 的依赖 | pystray 的依赖 |
| tkinter | ✅ | — |
| **flet** | — | ✅ (`pip install flet`) |

---

## 启动命令

```bash
# 原版（无窗口后台运行）
pythonw auto_organizerGP.pyw

# 新版（Flet 桌面应用）
python auto_organizer_flet.py
```
