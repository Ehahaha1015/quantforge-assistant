# QuantForge 离线演示

当前的安装、启动、验收、故障排查和源码导出说明统一维护在 [根 README](../README.md)。

无需 Docker：

```sh
python governance-tools/local_showcase.py --install
```

打开 [本地演示](http://127.0.0.1:4173/#chart-reports)，依次查看完整报告、资料不足、
输出校验失败和调用结果未知。所有样例均为合成数据，不需要模型密钥。

Docker 入口：Windows 执行 `./run_showcase.ps1 -Build`，Linux/macOS 执行 `sh ./run_showcase.sh`，
打开 [容器演示](http://127.0.0.1:4182/#chart-reports)。

设计材料：[架构与事务](INTERNSHIP-ARCHITECTURE.md)、[代码阅读与讲解](INTERNSHIP-INTERVIEW.md)。
