# 协作约定

## 发布流程（每次改动都按这个顺序）

1. **先部署本地 8000 端口验证**
   ```bash
   python3 web/server.py --port 8000
   # 另开终端：curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health   # 期望 200
   ```
   浏览器打开 <http://127.0.0.1:8000>，确认改动生效、页面无报错。已在跑的 8000 服务要先停掉再重启，确保加载的是新代码。
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
