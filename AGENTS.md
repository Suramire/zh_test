# 协作约定

## 发布流程（每次改动都按这个顺序）

1. **先部署本地 8000 端口验证**
   ```bash
   # 首次/换 Python 解释器后先装依赖（PyMySQL 缺了页面会提示「账号没有走数据库」）
   python3 -m pip install -r requirements.txt
   # Homebrew Python 报 externally-managed 时加 --break-system-packages，或用 venv
   python3 web/server.py --port 8000
   # 另开终端：curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health   # 期望 200
   ```
   浏览器打开 <http://127.0.0.1:8000>，确认改动生效、页面无报错；配了 `db.local.json` 时顺手点一下「数据库检测」，确认 `backend=mysql`。已在跑的 8000 服务要先停掉再重启，确保加载的是新代码。
2. **本地验证通过后，再提交并推送**
   ```bash
   git add -A
   git commit -m "feat/fix: ..."
   git push
   ```
3. 推送后 Render 会按 `render.yaml` 的 `autoDeploy` 自动重新部署。**没在本地 8000 验证过的改动不要 push。**

## 注意

- `accounts.json`、`accounts/`、`db.local.json` 含明文密码，已被 `.gitignore` 排除，提交前用 `git status` 确认没有混进去。
- `web/static/` 下的静态文件由服务端按请求实时读取，改完刷新浏览器即可；改 `web/*.py` 必须重启服务。
- `web/static/` 下的静态文件由服务端按请求实时读取，改完刷新浏览器即可；改 `web/*.py` 必须重启服务。

## 自动刷榜的两类账号

`web/auto_rank.py` 里账号分两种，靠账号记录的 `role` 字段区分（`role=anchor` = 固定号）：

| 类型 | 数量 | 昵称 | 做什么 |
|---|---|---|---|
| 轮训号 | 每天 10 个（`BATCH_SIZE`） | `别氪了` + 日期码 + 序号 | 每小时校准榜单，截止前 10 分钟冲榜 |
| 固定号 | 共 25 个（`ANCHOR_COUNT`，一次性建满） | 4~5 个中文 + 3~5 位数字 | 只每天 21:00 加分，不参与轮训 / 魔王 / 竞技场 |

固定号分数规则（`anchor_scores`，天数从各自创建日算）：

- 大选关：3806 起，每天 +3000，封顶 13902
- 爬塔：8101000 起，每天 +1000（不封顶）

两个约束：

1. 冲榜目标永远是「**其他玩家**里的第一名」，固定号不算目标；
2. `ANCHOR_GUARD=True` 时冲刺目标会压在「最小固定号分 − 100」以下，不越过固定号。

建号流程：`_ensure_today_batch()`（轮训号，按天）+ `_ensure_anchors()`（固定号，按总数补齐）。
