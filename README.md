# arXiv 每日摘要 GitHub Pages 站点

这是把本地 arXiv 每日摘要做成 GitHub Pages 的静态站点骨架。

## 用法

1. 运行：`python3 build_site.py`
2. 确认 `docs/index.html` 已生成
3. 把这个目录推到 GitHub 仓库的 `main` 分支
4. 在仓库设置里开启 GitHub Pages，并选择 `GitHub Actions`

## 自动更新

如果你希望每天自动更新网页内容，需要再加一个“生成并推送”的步骤：

- 由本机 cron 生成最新 `docs/index.html`
- 再用 git 推送到 GitHub

这样网页就会跟随最新 arXiv 输出一起更新。
