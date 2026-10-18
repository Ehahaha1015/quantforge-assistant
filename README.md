# QuantForge 多模态截图分析助手

面向AI应用开发/Python后端的个人工程项目。用户上传走势图并确认代码、周期和日期，
系统通过异步任务处理图像理解、确定性行情校验和条件性报告。
真实模型输出、规则计算和资料不足分别展示；尚未证明股票预测优势。

## 三分钟运行

需要Docker Desktop/Compose。Windows在项目根目录执行：

```powershell
./run_showcase.ps1 -Build
```

Linux/macOS：`sh ./run_showcase.sh`。
打开 http://127.0.0.1:4182/#chart-reports ，无需模型密钥或QQ账号。
首次需要下载构建依赖；运行时API只连接Docker内部网络，没有外部模型/行情网络。
前端另有用于发布本机端口的网络，未挂载账号目录。

演示使用独立`quantforge-showcase` Compose项目和`showcase-data`卷，不挂载生产卷。
只允许全新空目录或带正确Demo标记的既有演示目录；已有非演示目录会拒绝启动。
离线输出和图像由代码合成，复用真实HTTP、任务、事务、校验、归档和唯一worker流程。
其中“成功”表示软件链路完成，不是模型正确识图或真实市场预测成功。

## 三分钟演示顺序

1. 指出页面“离线工程演示”提示，解释样例全部合成。
2. 选择“报告已生成”的样例，展示结构化观察、规则结论和原图来源。
3. 展开“处理过程与失败原因”，说明接收、规则计算、调用预留、报告保存。
4. 选择资料不足样例，展示暂停有效结论；选择失败和未知样例，解释两者差别。
5. 打开API契约，说明幂等键、显式模式、输入校验、响应模型。

已有11份真实报告只在原生产目录中保留，不进入分享包。新增真实调用应使用原预算和授权。

## 验证命令

先安装Python3.12、Node24、uv0.12.9，按锁文件安装：

```sh
python governance-tools/ci_showcase.py --install
```

脚本在安装依赖之前将缓存/临时文件放在工作区`local-tools/cache`，其验证目录需未存在，重复运行使用新的工作区
或分别指定新评测输出目录。依赖已安装时可不传`--install`。
CI在Windows运行Platform完整测试（包括DPAPI），Linux运行当前截图/个人助手/QQ/worker测试。
两边均运行Ruff/mypy、UI类型/测试/构建、25条输出契约评测、
7个真实进程中断点及100次同幂等键HTTP提交。
运行验证前需有Git代码上下文；ZIP解压后先执行`git init`、`git add .`和`git commit`提交源码，或使用
将导出内容提交到新仓库后的CI。不要在没有Git上下文时伪造一个历史提交号。

## 技术材料

- [架构、状态与事务取舍](docs/INTERNSHIP-ARCHITECTURE.md)
- [代码阅读、动手任务与面试讲解](docs/INTERNSHIP-INTERVIEW.md)

分享ZIP根目录中的README是本文件副本；以上链接在ZIP根目录请使用`docs/`前缀。

## 诚实边界

本地单worker、SQLite，不宣称分布式执行或大规模并发。
合成契约样本验证软件的接受/拒绝行为，不代表真实模型准确率。
已有真实输出只做结构和元数据一致性审计，没有独立人工图像标签时不计算识图准确率。
同幂等键性能测试不等于不同图片吞吐，离线处理时间不包含真实模型延迟。
项目使用AI辅助实现；简历应写本人实际设计、理解、修改和验证的部分。

## 停止演示

`docker compose -f compose.showcase.yaml stop`。不要附加`down -v`删除留存演示记录。
生产入口仍为 http://127.0.0.1:4180 ，个人问答与QQ不在离线演示中启动。

## 分享与CI

开发工作区是多个独立Git仓库。运行`governance-tools/export_showcase.py --output
local-smoke/showcase-source.zip`，会生成一个显式白名单的源码ZIP及逐文件摘要，
不复制环境、生产图片、运行日志、账号或账本。分享前仍需自行检查源码及文档中的公开信息。
将导出内容导入单仓库后，`.github/workflows/showcase.yml`才能按预期checkout所有源码运行。
本仓库为完整的源码发布快照；自动检查状态请查看本仓库 Actions 页面。
默认启动独立离线演示，不需要个人账号或模型密钥。
