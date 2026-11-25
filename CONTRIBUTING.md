# 参与方式

个人项目，仍在演进。当前阶段的协作边界说在前面：

- **欢迎**：issue（bug、体验问题、用法疑问）、文档纠错、小的可独立验证的修复。
- **暂不接受**：大功能 PR、改变数据模型 / 分级聚合规则的 PR。这类改动需要先对齐
  设计意图——先开 issue 讨论，别直接写代码。

## 提 PR 前

1. `cd reader && python manage.py test core` 全绿（单元测试不调 LLM、不碰 Miniflux）；
2. 改动界面/流程的，跑一下 `reader/scripts/verify_*` 离线自检（见 [docs/deploy.md](docs/deploy.md)）；
3. commit message 一句话说清「为什么改」，风格参考 git log。

## 提 issue 前

先翻 [docs/](docs/) 与 README 的常见问题；涉及「分级判得不准」的，请带上该条目的
判定依据截图（悬停徽标可见）——没有判据的「不准」反馈无法定位。
