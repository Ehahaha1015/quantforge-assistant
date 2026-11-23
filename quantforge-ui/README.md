# QuantForge 股票截图报告前端

React 19 / TypeScript / Vite 页面，提供截图上传、证券与周期确认、任务记录、
综合策略证据、报告下载及处理过程查询。默认通过同源 `/gateway` 访问后端。

从仓库根目录运行完整离线演示：

```sh
python governance-tools/local_showcase.py --install
```

打开 [本地页面](http://127.0.0.1:4173/#chart-reports)。演示自动选择完整报告，支持切换四类样例。
演示模式隐藏上传、密钥配置与真实行情操作；工作区模式未确认时暂停上传和配置。

## 单独开发

在仓库根目录安装依赖：

```sh
uv sync --project qf-platform --frozen
npm ci --prefix quantforge-ui
```

在一个终端启动合成演示 API：

```sh
uv run --project qf-platform python -m qf_platform.chart_demo --root local-smoke/dev-demo --port 4188
```

在另一个终端启动前端：

```sh
npm --prefix quantforge-ui run dev
```

Vite 将 `/gateway` 代理到 `127.0.0.1:4188`；容器部署使用 Nginx 提供同源代理。
需要真实截图工作区时，将 API 命令替换为以下命令，使用独立的数据目录：

```sh
uv run --project qf-platform python -m qf_platform.server --root local-smoke/chart-workspace --port 4188 --chart-assistant --chart-only
```

真实模式下可上传并保存截图；新模型调用需要显式配置密钥与累计预算。
不要同时运行两个使用 4188 的 API，也不要将演示目录用于真实工作区。

## 验证

```sh
npm --prefix quantforge-ui run typecheck
npm --prefix quantforge-ui test
npm --prefix quantforge-ui run build
```

组件与契约测试位于 `tests/`。包含提交幂等性、来源与复权校验、模式重连、
离线只读交互、事件重试/刷新和报告组件唯一性回归。
完整启动、Docker、后端与故障验证见 [根 README](../README.md)。
