# scripts

ci-scope: cross-scope
- .gitignore: 外置 CD 状态目录需要忽略，避免运行时文件进入仓库
- scripts/README.md: 同步外置 baseline、事务和 ECS 初始化说明

CI / CD 脚本（进仓库、由 Woodpecker 用 infra-python 跑）：
- `check_scope.py`：按 feat/fix 前缀限制路径；越界看 commit/PR 里的 `ci-scope: cross-scope`
- `ci_validate.py`：YAML、digest、密钥、冲突标记
- `healthcheck.py`：通过 Nginx 反向代理检查服务入口
- `cd_state.py`：外置成功基线 `/var/lib/infra-cd/baseline.json`、发布事务 `/var/lib/infra-cd/transaction.json` 和部署锁 `/var/lib/infra-cd/deploy.lock`；基线只在成功 finalize 后原子更新，Git 锁仍为 `/root/infra/.cd-git.lock`
- `host_apply.py`：fetch 本趟 SHA，读取并固定外置 baseline，创建发布事务；checkout `docker-compose.yml`、`docker-compose.woodpecker.yml` 和 `compose/`。不 reset 整树、不 up Woodpecker。`--finalize` 只接受已通过健康检查的同一事务，成功后原子更新 baseline
- `cd_compose.py`：CLI 入口（`deploy` / `rollback --healthcheck` / `mark-healthcheck`）。实现在 `scripts/cd/`：`linux.py`/`docker.py` 是 git 与 compose 原语，`bridge.py` 按 Unit 规格做 reload/recreate/oneshot（不写死服务名），`catalog.py` 是本仓库的单元和波次，`orchestrate.py` 管事务和 DAG。`deploy` 比较固定 transaction 的 baseline/release SHA，再按依赖波次 checkout 受影响路径；可 `deploy nginx prometheus` 或 `deploy --wave alerting` 收窄范围。配置变更对 Nginx、Prometheus、Alertmanager 使用 reload，Loki、Promtail、Grafana 等使用 force-recreate，`promtail-sd` 构建上下文变化执行 build+recreate。无关服务跳过。任意波次失败或总健康检查失败时，所有已触碰波次按 DAG 逆序回滚并 force-recreate。绝不部署 woodpecker-server/agent。旧写法 `deploy-waves`、`healthcheck-rollback`、单独单元名仍可用。

业务 Compose 由根文件通过 `include` 聚合，按依赖组拆在 `compose/` 下；Woodpecker 保持独立，不进入 include：
- `compose/core.yml`：gitea、nginx
- `compose/alerting.yml`：am-config、alertmanager
- `compose/metrics.yml`：node_exporter、prometheus
- `compose/logging.yml`：loki、promtail-sd、promtail
- `compose/grafana.yml`：grafana

两份 compose、一个共享网 `infra_gitea`（业务栈创建，Woodpecker `external`）：
- 业务：`docker compose -p infra -f docker-compose.yml --project-directory /root/infra up -d`
- 控制面（手维，CD 不跑）：`docker compose -p woodpecker -f docker-compose.woodpecker.yml --project-directory /root/infra up -d`
- 必须用不同 `-p`，否则一边 `up` 会把另一边当成 orphan
- 现网 WP 容器仍属 project `infra` 时不要对 WP 做 `down`/`up`；要用新文件接管时先 stop 同名容器，卷已钉 `infra_woodpecker-*`
- 根 `docker-compose.yml` 只负责 include 业务依赖组和声明共享网络/卷；include 文件使用 `project_directory: .`，保持 `.env`、`python/` 等相对路径以项目根为基准。

流水线拆成 `.woodpecker/woodpecker.yaml`（CI，不挂卷）和 `.woodpecker/cd.yaml`（仅 main；要仓库 Trusted 才能用 volumes）。CD 只有一个 `deploy-infra` 编排 step，内部波次为：`core(gitea) → core-proxy(nginx) → alerting → foundation → dependent → grafana`。

第一期 CD 合入后，宿主机需要先 build 一次 `infra-python:3.12`（镜像里新加了 docker-cli）：
`docker compose up -d --build promtail-sd`

本机试跑（动作 dry-run 不需要本机存在外置 baseline）：
`python scripts/cd_compose.py deploy nginx --dry-run`
`python scripts/cd_compose.py deploy --dry-run`
`python scripts/host_apply.py --dry-run --sha <40位SHA>`

ECS 初始化（合并后由运维步骤执行，不进 Git）：
- 创建 `/var/lib/infra-cd`，权限 `root:root 0700`
- 创建 `/var/lib/infra-cd/baseline.json`，权限 `root:root 0600`
- 写入最近一次完整成功发布的 `success_sha`
- CD 容器将 `/var/lib/infra-cd` 以同路径挂载；baseline 缺失、损坏或 SHA 不存在时 fail closed

以后可能放：
- app.ini.example 同步小工具
- 备份 / 健康检查钩子
- 一次性迁移脚本

约定：
- 这里不要放密钥（用 env 或不入库的配置）
- 优先短小、有说明的 shell / Python
- 可以先空着，有真实脚本再提交

