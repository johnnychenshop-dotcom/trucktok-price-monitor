# TruckTok 每日价格监控

每天抓取 TruckTok 的全部商品，记录商品名称、链接、最低当前价格和库存状态。脚本会把每日快照保存到 `prices.csv`，并把相对上一次成功运行发生的价格变化保存到 `price_changes.csv`。发现变化时可通过 SMTP 邮件提醒。

网站目前是 Shopify 商店。脚本用 BeautifulSoup 解析 Shopify 的商品站点地图，再用 requests 读取每个商品的公开 JSON 数据，因此页面即使包含前端动态组件也不需要 Playwright。相比浏览器渲染，这种方式速度更快、资源占用更低。

## 输出文件

`prices.csv`：

```text
date,product_name,url,price,stock_status
```

`price_changes.csv`：

```text
date,product_name,url,old_price,new_price,change_amount,change_percent
```

同一天重复运行会替换当天的数据，不会产生重复行。首次运行没有历史价格可比较，因此不会产生价格变化记录或邮件。

## 本地运行

需要 Python 3.10 或更高版本。

```bash
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python price_monitor.py
```

macOS / Linux：

```bash
source .venv/bin/activate
pip install -r requirements.txt
python price_monitor.py
```

邮件提醒使用环境变量配置。可参考 `.env.example`。脚本不会自动读取 `.env` 文件；本地运行时请在终端设置变量，或使用你习惯的环境变量管理工具。

必需的邮件变量：

- `SMTP_HOST`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `EMAIL_TO`

可选变量：

- `SMTP_PORT`：默认 `587`
- `EMAIL_FROM`：默认与 `SMTP_USER` 相同
- `SMTP_USE_SSL`：默认 `false`，即使用 STARTTLS；465 端口通常设为 `true`
- `MONITOR_TIMEZONE`：默认 `Asia/Shanghai`
- `MAX_WORKERS`：默认 `8`
- `REQUEST_TIMEOUT`：默认 `30` 秒

Gmail 需要启用两步验证并使用“应用专用密码”，不能直接填写账号登录密码。

## 部署到 GitHub Actions

1. 新建 GitHub 仓库，把本项目所有文件推送到默认分支。
2. 打开仓库的 **Settings → Secrets and variables → Actions**。
3. 添加以下 Repository secrets：
   - `SMTP_HOST`
   - `SMTP_PORT`
   - `SMTP_USER`
   - `SMTP_PASSWORD`
   - `EMAIL_FROM`
   - `EMAIL_TO`
   - `SMTP_USE_SSL`
4. 打开 **Actions**，选择 **Daily TruckTok price monitor**，点击 **Run workflow** 手动试运行一次。
5. 确认运行成功后，仓库根目录会出现 `prices.csv` 和 `price_changes.csv`。

工作流默认每天 `02:00 UTC` 运行，即北京时间每天 `10:00`。修改 `.github/workflows/daily-price-monitor.yml` 中的 cron 表达式可以更换时间。GitHub Actions 的 cron 始终使用 UTC，并且定时任务可能有几分钟延迟。

工作流拥有 `contents: write` 权限，会在抓取成功后自动提交 CSV。若仓库规则禁止 Actions 直接推送默认分支，需要在分支保护设置中允许 GitHub Actions 推送，或改为将 CSV 上传为 artifact。

## 价格和库存口径

- 商品有多个规格时，`price` 记录当前最低规格价格。
- 任一规格可购买时，`stock_status` 为 `in_stock`；全部不可购买时为 `out_of_stock`。
- “昨天”按上一次成功保存的历史快照理解。这样即使某天 GitHub Actions 没有运行，下一次仍会与最近的有效价格比较。
- 只有价格变化触发邮件；库存变化仍会记录在每日 `prices.csv` 中，但不会发送提醒。

## 故障处理

脚本包含限时、重试和并发控制。如果任意商品最终抓取失败，本次运行会以失败状态退出，并且不会写入不完整的 CSV 快照。GitHub Actions 日志会显示失败的商品链接。

如果未来网站不再提供 Shopify 商品 JSON 接口，可把 `fetch_product()` 改为解析商品页的 JSON-LD；只有在价格必须由 JavaScript 执行后才能出现时，才需要改用 Playwright。
