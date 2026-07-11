# Security Policy

## 报告安全问题

请通过 GitHub Security Advisories 私下报告漏洞，不要在公开 Issue 中粘贴
API key、资金账号、交易日志、Cookie 或券商客户端路径。

## 本地秘密管理

- 复制 `.env.example` 仅用于查看变量清单；程序直接读取进程环境。
- `.env`、QMT `userdata_mini`、浏览器用户目录、日志和报告均不入库。
- 泄露过的凭据必须在提供方轮换；删除文件或重写 Git 历史不能使旧凭据失效。
- 推送前运行 `python scripts/scan_secrets.py`；安装 gitleaks 后再运行
  `gitleaks git --config .gitleaks.toml --redact` 扫描完整历史。
