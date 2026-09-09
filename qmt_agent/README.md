# qmt_agent — AI 量化交易代理

## 简介

qmt_agent 是 AI 量化的客户端交易代理，负责连接 QMT 交易端并将行情数据实时推送到系统服务器。
支持同时连接多个云端服务器。

## 快速开始

### 方式一：一键启动菜单（推荐）

1. 安装 Python 3.9+
2. 双击 `install.bat`，首次运行自动安装依赖，之后显示功能菜单
3. 选择 `[1] 配置` 打开图形界面填写配置
4. 选择 `[2] 启动` 启动 Agent

### 方式二：命令行手动启动

1. 安装 Python 3.9+
2. 安装依赖：`pip install -r requirements.txt`
3. 配置：`python config_gui.py`（图形界面）或手动编辑 `config.yaml`
4. 启动：`python agent.py`

### 方式三：打包为 EXE（可选）

1. 运行 `scripts\build_agent_exe.bat`
2. 解压 `qmt_agent_v1.1.zip`
3. 编辑 `config.yaml` 或运行 `python config_gui.py`
4. 双击 `qmt_agent.exe` 启动

## 配置说明

### 图形界面配置

```bash
python config_gui.py
```

打开配置窗口，可编辑全局参数和服务器列表。

### 手动编辑 config.yaml

```yaml
# qmt_agent 全局配置
qmt_path: "D:\\光大证券金阳光QMT实盘\\userdata_mini"
log_level: "INFO"
host: "唐为Windows电脑"
platform: "win"

# 多服务器连接配置（至少一个）
servers:
  - name: "生产环境"
    server_url: "ws://111.229.104.188"
    api_key: "your-api-key"
    account_id: 40249427
    enabled: true
  - name: "测试环境"
    server_url: "ws://192.168.1.100"
    api_key: "your-api-key"
    account_id: ""
    enabled: false
```

### 配置项说明

| 字段 | 说明 |
|------|------|
| `qmt_path` | QMT 交易端安装路径（userdata_mini） |
| `log_level` | 日志级别：DEBUG / INFO / WARNING / ERROR |
| `host` | 主机名标识（用于服务器识别） |
| `platform` | 平台标识（固定为 win） |
| `servers[].name` | 服务器名称（自定义） |
| `servers[].server_url` | 云端 WebSocket 地址 |
| `servers[].api_key` | **唯一认证凭据**，从管理后台获取，连接时服务端通过 api_key 自动识别用户 |
| `servers[].account_id` | 资金账户 ID（可选，留空为只读模式） |
| `servers[].enabled` | 是否启用该连接 |

## 多服务器连接

qmt_agent 支持同时连接多个云端服务器：
- 在 `servers` 列表中配置多个服务器
- 每个服务器独立连接，互不影响
- 交易指令从任意服务器下发，执行后回报广播到所有服务器
- 行情数据同时推送到所有连接的服务器

## 常见问题

**Q: 连接失败怎么办？**
A: 检查 `server_url` 是否正确，确认服务器已启动且网络可达。

**Q: 行情数据不更新？**
A: 确认 QMT 交易端已启动并登录，行情数据源正常。

**Q: 如何获取 API Key？**
A: 登录 AI 量化 Web 管理后台 → 用户设置 → API Key 管理。如有疑问，请联系系统管理员。

**Q: 配置中为什么不需要 user_id？**
A: qmt_agent 连接时只需要 `api_key`，服务端会自动通过 api_key 校验身份并关联到对应的用户，无需额外配置 user_id。

**Q: 支持哪些操作系统？**
A: 仅支持 Windows（QMT 交易端限制）。推荐 Windows 10/11 64位。

**Q: 如何配置多个服务器？**
A: 运行 `python config_gui.py` 打开配置窗口，在服务器列表中点击"添加服务器"即可。