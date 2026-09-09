"""
qmt_agent 配置 GUI

使用 Python 内置 tkinter 提供简易配置窗口。
零额外依赖，双击即可运行。

用法:
    python config_gui.py
    python config_gui.py config.yaml
    python agent.py --config-gui
"""

import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox

import yaml


def load_config_yaml(config_path: str) -> dict:
    """加载 config.yaml，兼容新旧格式"""
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    # 确保是新格式
    if "servers" not in config:
        config = {
            "qmt_path": config.get("qmt_path", ""),
            "log_level": config.get("log_level", "INFO"),
            "host": config.get("host", ""),
            "platform": config.get("platform", "win"),
            "servers": [{
                "name": "默认服务器",
                "server_url": config.get("server_url", ""),
                "api_key": config.get("api_key", ""),
                "account_id": config.get("account_id", ""),
                "enabled": True,
            }],
        }
    return config


def save_config_yaml(config_path: str, config: dict):
    """保存配置到 config.yaml"""
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


class ConfigWindow:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = load_config_yaml(config_path)

        self.root = tk.Tk()
        self.root.title("qmt_agent 配置")
        self.root.geometry("700x550")
        self.root.resizable(True, True)

        # 服务器列表的 frame 引用
        self.server_frames: list = []
        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # ---- 全局配置 ----
        global_frame = ttk.LabelFrame(main, text="全局配置", padding=10)
        global_frame.pack(fill=tk.X, pady=(0, 10))

        row = 0
        # QMT 路径
        ttk.Label(global_frame, text="QMT 路径:").grid(row=row, column=0, sticky=tk.W, pady=2)
        self.qmt_path_var = tk.StringVar(value=self.config.get("qmt_path", ""))
        ttk.Entry(global_frame, textvariable=self.qmt_path_var, width=60).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        # 日志级别
        ttk.Label(global_frame, text="日志级别:").grid(row=row, column=0, sticky=tk.W, pady=2)
        self.log_level_var = tk.StringVar(value=self.config.get("log_level", "INFO"))
        log_combo = ttk.Combobox(global_frame, textvariable=self.log_level_var, values=["DEBUG", "INFO", "WARNING", "ERROR"], width=10)
        log_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        # 主机名
        ttk.Label(global_frame, text="主机名:").grid(row=row, column=0, sticky=tk.W, pady=2)
        self.host_var = tk.StringVar(value=self.config.get("host", ""))
        ttk.Entry(global_frame, textvariable=self.host_var, width=60).grid(row=row, column=1, sticky=tk.W, padx=5)

        # ---- 服务器列表 ----
        server_section = ttk.LabelFrame(main, text="服务器连接", padding=10)
        server_section.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # 可滚动的服务器列表区域
        canvas = tk.Canvas(server_section, height=260)
        scrollbar = ttk.Scrollbar(server_section, orient=tk.VERTICAL, command=canvas.yview)
        self.server_container = ttk.Frame(canvas)

        self.server_container.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.server_container, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 鼠标滚轮支持
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # 渲染服务器列表
        self._render_servers()

        # 添加服务器按钮
        btn_frame = ttk.Frame(server_section)
        btn_frame.pack(fill=tk.X, pady=(5, 0))
        ttk.Button(btn_frame, text="+ 添加服务器", command=self._add_server).pack(side=tk.LEFT, padx=5)

        # ---- 底部按钮 ----
        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X, pady=(5, 0))

        ttk.Button(bottom, text="保存配置", command=self._save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bottom, text="取消", command=self.root.destroy).pack(side=tk.RIGHT, padx=5)

    def _render_servers(self):
        """根据 self.config['servers'] 渲染服务器列表"""
        # 清空
        for f in self.server_frames:
            f.destroy()
        self.server_frames.clear()

        servers = self.config.get("servers", [])
        for i, server in enumerate(servers):
            self._add_server_row(i, server)

    def _add_server_row(self, index: int, server: dict):
        """添加一个服务器配置行"""
        frame = ttk.LabelFrame(self.server_container, text=f"服务器 {index + 1}: {server.get('name', '')}", padding=5)
        frame.pack(fill=tk.X, pady=3)
        self.server_frames.append(frame)

        # 存储变量引用
        vars_dict = {}

        r = 0
        ttk.Label(frame, text="名称:").grid(row=r, column=0, sticky=tk.W, pady=1)
        v = tk.StringVar(value=server.get("name", ""))
        vars_dict["name"] = v
        ttk.Entry(frame, textvariable=v, width=20).grid(row=r, column=1, sticky=tk.W, padx=5)

        ttk.Label(frame, text="服务器地址:").grid(row=r, column=2, sticky=tk.W, pady=1, padx=(15, 0))
        v = tk.StringVar(value=server.get("server_url", ""))
        vars_dict["server_url"] = v
        ttk.Entry(frame, textvariable=v, width=30).grid(row=r, column=3, sticky=tk.W, padx=5)
        r += 1

        ttk.Label(frame, text="API Key:").grid(row=r, column=0, sticky=tk.W, pady=1)
        v = tk.StringVar(value=server.get("api_key", ""))
        vars_dict["api_key"] = v
        api_entry = ttk.Entry(frame, textvariable=v, width=50, show="*")
        api_entry.grid(row=r, column=1, columnspan=3, sticky=tk.W, padx=5)

        # 显示/隐藏 API Key 复选框
        self._show_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="显示", variable=self._show_var,
                        command=lambda e=api_entry, v=self._show_var: e.configure(show="" if v.get() else "*")).grid(row=r, column=4, padx=5)
        r += 1

        ttk.Label(frame, text="账户 ID:").grid(row=r, column=2, sticky=tk.W, pady=1, padx=(15, 0))
        v = tk.StringVar(value=str(server.get("account_id", "")))
        vars_dict["account_id"] = v
        ttk.Entry(frame, textvariable=v, width=20).grid(row=r, column=3, sticky=tk.W, padx=5)
        r += 1

        # 启用/禁用
        v = tk.BooleanVar(value=server.get("enabled", True))
        vars_dict["enabled"] = v
        ttk.Checkbutton(frame, text="启用", variable=v).grid(row=r, column=0, sticky=tk.W, pady=2)

        # 删除按钮
        ttk.Button(frame, text="删除",
                   command=lambda idx=index: self._remove_server(idx)).grid(row=r, column=3, sticky=tk.E, pady=2)

        # 保存引用
        frame._vars = vars_dict
        frame._index = index

    def _add_server(self):
        """添加新服务器"""
        new_server = {
            "name": "新服务器",
            "server_url": "",
            "api_key": "",
            "account_id": "",
            "enabled": True,
        }
        self.config.setdefault("servers", []).append(new_server)
        self._render_servers()

    def _remove_server(self, index: int):
        """删除指定服务器"""
        if len(self.config.get("servers", [])) <= 1:
            messagebox.showwarning("提示", "至少保留一个服务器配置")
            return
        if messagebox.askyesno("确认", f"确定删除服务器 {index + 1}？"):
            self.config["servers"].pop(index)
            self._render_servers()

    def _collect_form_data(self):
        """从表单收集数据到 self.config"""
        self.config["qmt_path"] = self.qmt_path_var.get().strip()
        self.config["log_level"] = self.log_level_var.get().strip()
        self.config["host"] = self.host_var.get().strip()

        servers = self.config.get("servers", [])
        for i, frame in enumerate(self.server_frames):
            if i >= len(servers):
                break
            v = frame._vars
            servers[i]["name"] = v["name"].get().strip()
            servers[i]["server_url"] = v["server_url"].get().strip()
            servers[i]["api_key"] = v["api_key"].get().strip()
            servers[i]["account_id"] = v["account_id"].get().strip()
            servers[i]["enabled"] = v["enabled"].get()

    def _save(self):
        """保存配置"""
        self._collect_form_data()

        # 验证
        for s in self.config.get("servers", []):
            if s.get("enabled") and not s.get("server_url"):
                messagebox.showerror("错误", f"服务器 '{s.get('name')}' 已启用但未填写服务器地址")
                return

        save_config_yaml(self.config_path, self.config)
        messagebox.showinfo("成功", "配置已保存！\n请重启 qmt_agent 使配置生效。")
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def open_config_window(config_path: str = "config.yaml"):
    """外部调用入口"""
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config_path)
    window = ConfigWindow(config_path)
    window.run()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    open_config_window(path)