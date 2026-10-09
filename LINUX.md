# Linux 试用版

网页和分析逻辑不变。MI 在 Linux 使用 Kerberos ticket；RTD 使用管理员浏览器 SSO 的 Cookie。
这是共享后端身份模式：网页登录不会把访问者的身份传给 MI/RTD。尚需在公司网络验证。
先在 Ubuntu 22.04/24.04 的桌面环境（或配置好 SSH X11 转发的服务器）试用原生部署。
没有图形界面时，API 可以启动，但首次 RTD 登录仍需通过受限远程桌面或 X11 完成。

## 1. 安装

```bash
sudo apt-get update
sudo apt-get install -y build-essential python3-dev libkrb5-dev krb5-user
cd /opt/pim
uv sync --extra linux --extra rtd
uv run playwright install --with-deps chromium
cp linux.env.example linux.env
chmod 600 linux.env
mkdir -p data/rtd
chmod 700 data data/rtd
```

编辑 linux.env：修改网页登录账号密码，路径改为真实安装路径。
系统 /etc/krb5.conf 的 Realm/KDC 需向公司 IT 确认；本项目不猜测公司域配置。
公司 CA 如未被系统信任，安装可信 CA，并按需配置 REQUESTS_CA_BUNDLE、NODE_EXTRA_CA_CERTS。
不要通过关闭 TLS 验证解决证书问题。

## 2. 验证 MI

以下命令在同一个终端、以运行服务的同一个 Linux 用户执行：

```bash
set -a
source linux.env
set +a
kinit '你的域账号@实际KERBEROS_REALM'
klist
uv run python -c 'from mi_client import MiClient; c=MiClient(); print(c.test_history("填入一个已知有效的整机序列号"))'
```

`kinit` 成功只证明拿到 ticket；最后的真实查询成功才证明 MI 接受这个认证。
如果 401/403，确认 MI 的 SPN/Realm、账号权限、DNS 和时间同步，以及 Linux 到内网的网络。
不要把上面的真实生产数据输出贴到公共 issue。

## 3. 验证 RTD

```bash
uv run python refresh_cookies.py
```

默认使用 Playwright Chromium；在弹出的浏览器完成 SSO。Cookie 和模板保存到配置路径，
浏览器 profile 保留登录态，凭据文件以私有权限原子写入。必须以同一个 OS 用户刷新和运行 API。
支持 `PIM_RTD_BROWSER=chrome`（系统 Chrome）或 `chromium`。

之后可尝试无需窗口的刷新：

```bash
uv run python refresh_cookies.py --headless
```

如果返回退出码 2，表示需要人工 SSO（或没有取得目标域 Cookie）；重新运行不带 --headless 的命令。
退出码 1 表示缺少必需模板，3 表示浏览器无法启动。不要同时运行多个刷新脚本或打开同一个 profile。
本版本不自动定时刷新；先验证公司 SSO 能否稳定复用，再安排受控定时刷新。
Cookie 实测约 11 小时有效，不保证每次都能无交互续期。

## 4. 启动 Web

```bash
bash start-linux.sh
```

默认 http://127.0.0.1:8000。远程试用推荐 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 你的Linux账号@服务器地址
```

然后本地浏览器打开 http://localhost:8000，使用 linux.env 中的网页登录账号。
远程凭据刷新按钮关闭；管理员通过独立脚本刷新。新 Cookie/模板无需重启 API。
`/api/health` 只检查应用与 RTD 文件状态，不验证 MI ticket 是否有效；必须完成真实查询。
网页登录保持原有的单账号实现，试用时使用单进程。对组织正式开放前接入 HTTPS 和组织身份认证。

## 5. 常驻服务（验证成功后）

将目录放在 /opt/pim，创建服务用户 pim 并让其可读代码、可写 data，虚拟环境也应由它管理。
必须以 pim 用户完成 kinit/RTD 登录；个人用户的 ticket cache 和浏览器 profile 不会自动共享。

```bash
sudo cp deploy/pim.service /etc/systemd/system/pim.service
sudo systemctl daemon-reload
sudo systemctl enable --now pim
sudo journalctl -u pim -n 100
```

交互式 kinit 获取的 ticket 会过期。长期运行需 IT 提供允许访问 MI 的服务主体/keytab，
用受控任务执行 `kinit -kt /安全路径/mi.keytab 服务主体`，更新同一 KRB5CCNAME。
keytab、Cookie、profile、linux.env 都是敏感运行数据，不提交仓库。

## 6. 可选 Docker（API 容器，浏览器在宿主机维护）

先在宿主机完成上述 MI/RTD 验证。镜像不安装图形桌面或浏览器；宿主机负责维护票据和 Cookie。
data 的凭据文件需由 UID 10001 可读；建议使用相同 UID 的专用宿主机用户完成维护。
容器路径覆盖为 /app/data，挂载整个目录而非单个 Cookie 文件，以支持原子替换。

```bash
docker build -t pim-linux:trial .
docker run -d --name pim --restart unless-stopped \
  --env-file linux.env \
  -e KRB5CCNAME=FILE:/app/data/krb5cc \
  -e PIM_RTD_COOKIES=/app/data/rtd/cookies.json \
  -e PIM_RTD_TEMPLATES=/app/data/rtd/templates.json \
  -v "$PWD/data:/app/data:ro" \
  -v /etc/krb5.conf:/etc/krb5.conf:ro \
  -p 127.0.0.1:8000:8000 pim-linux:trial
```

确认容器内 DNS、内网路由、公司 CA 和代理可用；宿主机的可用网络不代表容器必然可用。
票据过期后在宿主机更新，容器使用相同挂载目录读取。
