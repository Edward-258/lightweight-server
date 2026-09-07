# infra

单机自托管栈：Gitea（Git）、Woodpecker（CI/CD）、Nginx 入口，以及 Prometheus / Alertmanager / Loki / Grafana。

仓库路径默认是宿主机 `/root/infra`。对外身份目前是示例域名 `example.com` 和自签证书。密钥与运行时文件不入库，见 [配置与密钥](#配置与密钥)。CD 脚本的命令级说明在 [scripts/README.md](scripts/README.md)。

---

## 目录

| 路径 | 作用 |
|---|---|
| `docker-compose.yml` | 业务栈入口：`include` 五个依赖组，声明共享网和卷 |
| `docker-compose.woodpecker.yml` | Woodpecker 控制面；CD 不部署这份文件 |
| `compose/` | 业务服务拆分：`core` / `alerting` / `metrics` / `logging` / `grafana` |
| `nginx/` | 反代与 TLS 终止 |
| `config/app.ini.example` | Gitea 配置模板；真正挂载的是不入库的 `config/app.ini` |
| `prometheus/` `alertmanager/` `loki/` `promtail/` `grafana/` | 对应组件的配置与看板 |
| `python/` | 共享镜像 `infra-python:3.12`（CI/CD 与 `promtail-sd`） |
| `scripts/` | CI 门禁、健康检查、CD 编排 |
| `.woodpecker/woodpecker.yaml` | CI：所有 push / PR |
| `.woodpecker/cd.yaml` | CD：仅 `main` push，且依赖 CI 通过 |

---

## 两份 Compose、一张网

业务栈和控制面必须用不同的项目名。同一 `-p` 下对其中一份 `up`，另一边会被当成 orphan。

```text
业务栈     docker compose -p infra -f docker-compose.yml --project-directory /root/infra up -d
控制面     docker compose -p woodpecker -f docker-compose.woodpecker.yml --project-directory /root/infra up -d
```

先起业务栈（创建网络 `infra_gitea`），再起 Woodpecker（该网标 `external`）。Woodpecker 的卷名钉成 `infra_woodpecker-data` / `infra_woodpecker-agent-config`，避免换项目名后出现空卷。

现网若 Woodpecker 容器仍属于 project `infra`，不要对 WP 做 `down`/`up`。要用新文件接管时，先 stop 同名容器。

`include` 文件都写了 `project_directory: .`，`.env`、`python/` 等相对路径以仓库根为准。

---

## 对外入口

Nginx 容器名 `nginx-proxy`，映射 `80` / `443`。HTTP 除 ACME 和 `/healthz` 外 301 到 HTTPS。

| 路径 | 上游 |
|---|---|
| `/healthz` | Nginx 本机返回 `200 ok`（HTTP/HTTPS 都有） |
| `/gitea/` | `gitea:3000`（容器内仍听 `/`，前缀由 `ROOT_URL` 对外呈现） |
| `/woodpecker/` | `woodpecker-server:8000` |
| `/grafana/` | `grafana:3000`（`SERVE_FROM_SUB_PATH=true`） |
| `/prometheus/` | `prometheus:9090` |
| `/` | `200 infra` |

Git SSH 走宿主机 `2222:22`。Prometheus 另有 `127.0.0.1:9090`。Grafana、Loki、Alertmanager、Woodpecker HTTP/gRPC/metrics 都不映射公网。

TLS 读 `/etc/ssl/selfsigned/`。`/.well-known/acme-challenge/` 已留，Let's Encrypt 未接通。

---

## 组件

### Gitea

`compose/core.yml`。镜像钉 digest。数据在 `/root/gitea-data`，配置只读挂 `config/app.ini`。SQLite。HTTP 只给 Nginx；发 Webhook 到 Woodpecker 时用自签证书（`SSL_CERT_FILE`）。

`config/app.ini.example` 里的 `__REPLACE_ME__` 需要在宿主机生成：`SECRET_KEY`、`INTERNAL_TOKEN`、`JWT_SECRET`、`LFS_JWT_SECRET`。`SSH_PORT=2222` 与 compose 映射一致。

### Nginx

同文件。配置热更新走 `nginx -t` 再 `nginx -s reload`。Grafana 子路径有一段 `sub_filter`，用来改浏览器里出现的 `/grafana/grafana/`。

### 指标：node_exporter、Prometheus

`compose/metrics.yml`。Prometheus 保留 15 天，开了 `/-/reload`。刮取：

- `node_exporter:9100`
- 自身 `localhost:9090`
- `woodpecker-server:9001`（Docker 内网未鉴权口；主 HTTP 上的 `/metrics` 另有 token，这条 job 不读 token）

`prometheus/rules.yml` 两类规则：

- 告警：节点掉线 1 分钟；CPU 85% / 内存 90% / 根盘 85% 持续 5 分钟；Woodpecker 指标不可用、无 Worker、队列卡住、CD 步骤 15 分钟内失败 ≥ 3 次
- 记录规则：给 Node Overview 看板用的 1 分钟 rate，避免看板上现算

### 告警：am-config、Alertmanager

`compose/alerting.yml`。`am-config` 是一次性容器：从 `.env` 取 SMTP，渲染 `alertmanager.yml.tpl` → `alertmanager.runtime.yml`（忽略入库）。缺 `SMTP_FROM` 或 `SMTP_AUTH_PASSWORD` 会失败。Alertmanager 等它 `service_completed_successfully` 再起，不对外暴露 9093。

### 日志：Loki、promtail-sd、Promtail

`compose/logging.yml`。Loki 单体、本地盘、保留 7 天（`384m`）。

`promtail-sd` 用镜像 `infra-python:3.12`，`network_mode: none`，不挂 docker.sock。每 15 秒读 `/var/lib/docker/containers/*/config.v2.json`，只收录：

`nginx-proxy`、`gitea`、`woodpecker-server`、`woodpecker-agent`

名单写到共享卷，Promtail `file_sd` 来读（`128m`）。

### Grafana

`compose/grafana.yml`。子路径 `/grafana/`，禁止注册，首页 Node Overview，最小刷新 10s，关掉 Live。数据源和看板都是 provisioning，UI 改了下次启动会被覆盖。

两块看板：

- `Node Overview`：CPU / 负载 / 内存 / 磁盘 / 网卡，读上面的记录规则
- `Woodpecker CI/CD Overview`：Worker、队列、CD 失败、server/agent 日志（Loki）

### Woodpecker（控制面，手维）

`docker-compose.woodpecker.yml`。OAuth 走 Gitea；clone 默认镜像改到 DaoCloud 上的 `plugin-git:2.10.0`。流水线容器加入 `infra_gitea`，才能 SSH 到 `gitea:22`。agent 挂宿主机 docker.sock，并把 agent ID 写进独立卷，避免重建变成新 agent。

`WOODPECKER_OPEN=false`。CD 的 yaml 要挂卷，仓库需要 Trusted。

---

## 配置与密钥

复制模板，不要提交副本：

```text
cp .env.example .env
cp config/app.ini.example config/app.ini
```

`.env` 两份 compose 都读。里面是 Grafana 密码、Alertmanager QQ SMTP（授权码）、Woodpecker OAuth/Agent/gRPC、Prometheus token 文件路径、Gitea SSH host key（known_hosts 一行，值里不要有逗号）。

`.gitignore` 忽略：`.env`、`config/app.ini`、证书私钥、`alertmanager.runtime.yml`、`secrets/`、数据库和备份。`*.example` 进库。

共享镜像：

```text
docker compose -p infra -f docker-compose.yml --project-directory /root/infra up -d --build promtail-sd
```

`python/Dockerfile` 基于 Python 3.12 Alpine（digest），apk/pip 走国内镜像，额外装 git、openssh-client、docker-cli、compose 插件。CI 和 CD 都用 `infra-python:3.12`，`pull: false`。

---

## 依赖 DAG

CD 只编排业务栈。Woodpecker server/agent 不在图里，也不在回滚里。

逻辑依赖（compose `depends_on` + 实际数据面）：

```text
                     ┌─ am-config ── alertmanager ──┐
                     │                              │
gitea ── nginx ──────┼─ node_exporter ──────────────┼── prometheus ── grafana
                     │                              │
                     ├─ loki ───────────────────────┼── promtail
                     └─ promtail-sd ────────────────┘
```

| 边 | 原因 |
|---|---|
| gitea → nginx | 反代上游；core 组里 Nginx `depends_on` Gitea |
| nginx 在 alerting / 采集器之前 | 入口先于可观测性刷新，健康检查也走 Nginx |
| am-config → alertmanager | 先渲染 runtime YAML，Alertmanager 才能起 |
| node_exporter、alertmanager → prometheus | Prometheus `depends_on` 这两者；规则和告警接收都依赖它们 |
| loki、promtail-sd → promtail | Promtail 要 Loki 地址和 sd 卷里的采集名单 |
| prometheus → grafana | Grafana `depends_on` Prometheus；看板默认数据源是它 |

`alerting`（am-config / alertmanager）和 `foundation`（node_exporter / loki / promtail-sd）在图上是 nginx 后面的两条叉，**执行时并不并行**。实现是拓扑序压成一条队列，见下一节。

### 波次（执行顺序）

定义在 `scripts/cd/catalog.py` 的 `WAVES`。一次 `deploy` 按这个顺序走，每波次只刷新 `git diff` 命中的单元。

```text
波次          单元                         之后才允许动
────────────────────────────────────────────────────────
core          gitea                        core-proxy
core-proxy    nginx                        alerting / foundation
alerting      am-config, alertmanager      dependent（prometheus 吃 alertmanager）
foundation    node_exporter, loki,         dependent
              promtail-sd
dependent     prometheus, promtail         grafana
grafana       grafana                      （终点）
```

```text
[1] core            gitea
        │
[2] core-proxy      nginx
        │
        ├──────────► [3] alerting        am-config → alertmanager
        │
        └──────────► [4] foundation      node_exporter
                                         loki
                                         promtail-sd
                          │
                     [5] dependent       prometheus
                                         promtail
                          │
                     [6] grafana         grafana
```

同波次内的单元一起 `activate`（oneshot → reload → 一条 `compose up --force-recreate`）。波次与波次之间串行：上一波失败，后面的波次不会开始。

### 路径怎样映射到单元和动作

`impact_paths` 决定「这趟 diff 算不算命中」；命中后再按路径选动作。根 `docker-compose.yml` 变更或初始发布，视为全部业务单元都受影响。

| 单元 | 典型命中路径 | 默认动作 |
|---|---|---|
| gitea | `compose/core.yml` | recreate（`config/` 会 checkout，但不单独当作 impact） |
| nginx | `nginx/` | reload：`nginx -t` + `nginx -s reload`；`compose/core.yml` 则 recreate |
| am-config | `alertmanager/`、`compose/alerting.yml` | oneshot：`compose run --rm` 渲染 runtime YAML |
| alertmanager | 同上 | `alertmanager/` → `amtool check-config` + HUP；compose 文件 → recreate |
| node_exporter | `compose/metrics.yml` | recreate |
| loki | `loki/`、`compose/logging.yml` | recreate |
| promtail-sd | `promtail/sd.py`、`compose/logging.yml` | recreate；`python/` → build+recreate |
| promtail | `promtail/`、`compose/logging.yml` | recreate |
| prometheus | `prometheus/` | reload：`promtool check` + POST `/-/reload`；compose 文件 → recreate |
| grafana | `grafana/`、`compose/grafana.yml` | recreate |

`--no-deps --pull=never`。禁止名单：`woodpecker-server`、`woodpecker-agent`。

无关路径（例如只改 `scripts/README.md`）不会刷新任何业务容器。

---

## CI / CD 总流程

两条 Woodpecker workflow，不是一条 yaml 里的两个 stage。

```text
push / PR
    │
    ▼
┌─────────────────────────────────────────┐
│  workflow「woodpecker」                 │
│  .woodpecker/woodpecker.yaml            │
│                                         │
│  clone（SSH → gitea:22，depth 50）      │
│    ├─ policy        check_scope.py      │
│    ├─ validate      ci_validate.py      │
│    │                + unittest          │
│    └─ healthcheck   仅 PR、以及         │
│                     非 main 的 push     │
│                     （打现网 Nginx）     │
└─────────────────────────────────────────┘
    │
    │  仅当：event=push 且 branch=main
    │  且 CI workflow 成功（cd.yaml depends_on: woodpecker）
    ▼
┌─────────────────────────────────────────┐
│  workflow「cd」                         │
│  .woodpecker/cd.yaml                    │
│  需要仓库 Trusted（要挂卷）              │
│                                         │
│  挂：/root/infra、docker.sock、          │
│      /var/lib/infra-cd                  │
│                                         │
│  1. deploy-host      host_apply.py      │
│  2. deploy-infra     cd_compose.py deploy│
│  3. healthcheck      探测 → 标记 →      │
│                      finalize           │
└─────────────────────────────────────────┘
```

CI 这份 yaml **不能挂 volumes**（未 Trusted 时 lint 会整单失败）。CD 可以挂，所以部署发生在第二条 workflow。

两边 clone 都走 SSH：`ssh://git@gitea:22/admin/infra.git`，私钥来自 secret `ci_clone_ssh_key`，host key 来自 `WOODPECKER_ENVIRONMENT` 注入的 `GITEA_SSH_HOST_KEY`。

---

## CI 逐步说明

`.woodpecker/woodpecker.yaml`：push / PR。镜像 `infra-python:3.12`。

**1. policy**（`check_scope.py`）  
分支 `feat|fix/<area>-*` 或 `chore/ci-*` 只能改对应目录；`main` 跳过。diff 对 `origin/main`（流水线里本地 main 往往就是当前提交）。越界须在说明里写 `ci-scope: cross-scope` 和路径+原因，否则失败。

| area | 路径 |
|---|---|
| nginx / prometheus / grafana / loki / promtail / gitea | 各自目录（gitea → `config/`） |
| alerting | `alertmanager/`、`prometheus/rules.yml` |
| compose | 两份 compose 根文件 + `compose/` |
| python / ci | `python/`、`scripts/`、`.woodpecker/` |
| healthcheck | `scripts/healthcheck.py`、`.woodpecker/`、`scripts/README.md` |
| docs | `*.md` |

**2. validate**（与 policy 并行）  
YAML 能解析、无冲突标记；业务栈走 `include`、禁止混入 Woodpecker、必须有 gitea/nginx；第三方镜像钉 digest；扫私钥和 `AKIA…`。再跑 `test_cd_compose` / `test_cd_state`（影响范围、reload vs recreate、回滚顺序、baseline 校验，不起 Docker）。

**3. healthcheck**  
只跑 PR 和非 main 的 push。经 Nginx 打 `/healthz`（须 200）以及 `/gitea/` `/woodpecker/` `/prometheus/` `/grafana/`（2xx/3xx）。main 的探测放在 CD 末尾。这次失败只说明现网入口不健康，不回滚。

---

## CD 逐步说明

`.woodpecker/cd.yaml`：仅 main push，且 CI 已通过。仓库需 Trusted。挂 `/root/infra`、docker.sock、`/var/lib/infra-cd`。

状态在 Git 树外：`baseline.json`（上一成功 SHA）、`transaction.json`（phase / SHA / identity / touched_waves）、`deploy.lock`。基线缺失或 SHA 非法则失败。identity = `pipeline:<id>:sha:<40位>`，用来拒绝旧 Retry 和交叉覆盖。宿主机需先建该目录（0700）并写入当前成功 SHA。

**1. deploy-host**（`host_apply.py`）  
fetch 本趟 SHA，必须等于当前 main；核对 baseline 和未完成事务。写 `phase=deploying`，只 checkout compose 相关文件，不 reset 整树，不起 Woodpecker。`.env` / `app.ini` / secrets 不动。

**2. deploy-infra**（`cd_compose.py deploy`）  
锁内核对事务 → `diff baseline..release` → 按 DAG 波次刷新命中单元（backup → checkout → activate）。无关波次跳过。任一步失败：已 touched 的全部按 DAG 逆序回到 baseline 并 force-recreate（含已经成功的祖先）；未 touched 的不动。不部署 Woodpecker。

**3. healthcheck → finalize**  
同一套 Nginx 探测。失败则 `rollback --healthcheck`（按 touched_waves；空则 nginx+grafana），baseline 不改。成功则 `mark-healthcheck`，再 `--finalize`：`git reset --hard`，原子更新 `baseline.json`。没过健康检查，基线不前移。

---

## 失败时现网停在哪

| 失败点 | 容器 / Git 树 | baseline.json |
|---|---|---|
| CI policy / validate | 不动 | 不动 |
| CI healthcheck（非 main） | 不动（只读现网） | 不动 |
| `host_apply` 中途 | 可能已 checkout compose 文件；无事务或事务未用 | 不动 |
| 某波次 `activate` 失败，回滚成功 | touched 路径回到 baseline SHA 并 recreate | 不动 |
| 回滚过程中 compose 仍失败 | phase=`rollback_failed`，现网可能不一致，需要人看 | 不动 |
| 部署成功、CD healthcheck 失败，回滚成功 | 同「回滚成功」 | 不动 |
| finalize 之前进程被杀 | phase 可能停在 `deploying` / `healthcheck_passed` / `commit_pending`；下一趟会因未完成事务拒覆盖 | 未成功写完则仍是旧 SHA |

---

## 本机不碰现网的试跑

不需要本机已有外置 baseline：

```text
python3 scripts/cd_compose.py deploy nginx --dry-run
python3 scripts/cd_compose.py deploy --dry-run
python3 scripts/host_apply.py --dry-run --sha <40位SHA>
```

旧 CLI 仍可用：`deploy-waves`、`healthcheck-rollback`、直接写单元名（会改写成 `deploy <单元>`）。
)
