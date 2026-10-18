# QuantForge 股票截图报告前端

当前默认页面只提供截图报告，旧研究、资金、账户和实盘页面已删除。
保留上传/预览、代码日期确认、模型配置与预算、任务记录、Core四策略/价格回放、模型报告和导出。

在项目根先执行 `. ./governance-tools/Enter-ProjectEnvironment.ps1`。
前端目录中 `npm ci` 安装锁定依赖，`npm run dev` 启动，`npm run typecheck`、`npm test`、`npm run build` 校验。
主机开发API由Vite的/gateway代理到127.0.0.1:4188；Docker使用Nginx同源代理。

后端启动、Docker与维护记录见根README.md。当前浏览器核验在governance-tools/verify_chart_saved_report.py；
脚本读取已有成功报告，不重复请求模型。旧UI专用测试随已退役功能删除，当前截图/API契约测试保留。
