# Lumori

当前同步：**2026-09-28**。近期开发截至 9 月 26 日；上一个带标签版本为 `v0.3.0`。参阅[版本变更](CHANGELOG.md)、[使用指南](docs/USER_GUIDE.md)、[内容导出](docs/CONTENT_EXPORTS.md)和[本次验证](docs/VALIDATION_20260928.md)。

用于教育内容与测评生成的 Web 应用。此仓库仅维护可部署的产品代码、运行配置和回归测试；实验报告、原始语料及论文材料保存在独立的私有研究仓库 `TheOverflowing/lumori-fyp`。

功能包括账号注册/登录和账号间数据隔离、课程与资料管理、可控难度出题、中英文界面、RAG 检索与引用、MinerU Standard/Advanced 文档解析，以及可选的 Docling 图片提取和视觉模型语义描述。

## 新机器部署（Docker Compose）

需要 Docker Engine/Desktop 与 Compose v2、可访问 Docker Hub / PyPI / Hugging Face 的网络，以及你的模型 API 配置。首次构建和下载解析模型可能耗时较长，模型和依赖不包含在 Git 仓库中。

```sh
git clone https://github.com/TheOverflowing/Lumori.git
cd Lumori
cp .env.example .env
# 编辑 .env，至少填写 TEXT_API_KEY 和 EMBEDDING_API_KEY。
docker compose up -d --build
docker compose logs -f init app
```

浏览器访问 **http://localhost:8765**，自行注册新账号。首次 `init` 完成后应用才会启动；再次启动会校验并复用已有模型。新部署是空白工作区，旧机器的用户和资料不会自动上传或迁入。

- 默认 `full` 镜像包含相互隔离的 MinerU 4.0.2 / Docling 2.128.0 Python 环境，以 CPU 为可移植运行基线。Advanced 在 CPU 上可能较慢，超时可调整 `DOCUMENT_WORKER_TIMEOUT`，不承诺 GPU 性能。
- 只试用 TXT/Markdown 时，可在 `.env` 设置 `LUMORI_BUILD_TARGET=core`。此模式不安装 MinerU/Docling，PDF/图片完整解析不可用；它不会自动替换你的解析策略。
- 视觉描述需要支持 `image_url` 输入的模型。填写 `VISION_*` 使用独立视觉接口；为空时沿用 `TEXT_*`，这不意味着该文本模型一定具备视觉能力。未勾选图片处理则不调用视觉接口。
- 当前配置保留既有文本/嵌入/重排模型标识。供应商权限、模型可用性与计费需要使用自己的账户确认。
- 运行数据存放在命名卷 `lumori_data`，模型在 `lumori_models`。普通 `docker compose down` 保留数据；**不要使用 `down -v`，除非确实要删除全部账号、资料和模型。**

## 服务器访问

默认仅绑定服务器本机 `127.0.0.1`，可以先用 SSH 隧道试运行：

```sh
ssh -L 8765:127.0.0.1:8765 your-user@your-server
```

随后访问自己电脑的 http://localhost:8765。

正式使用域名时，在前面配置 HTTPS 反向代理，将请求转发至 `127.0.0.1:8765`，并设置：

```dotenv
ALLOWED_HOSTS=localhost,127.0.0.1,[::1],learn.example.com
AUTH_COOKIE_SECURE=true
# 填写应用实际接收到的可信反向代理源 IP；Docker 下可能是 bridge 网关。
FORWARDED_ALLOW_IPS=127.0.0.1
```

代理必须保留原始 `Host`，正确设置 `X-Forwarded-Proto=https`。如果浏览器写入请求返回 `origin_forbidden`，检查可信代理 IP 和协议转发，不要关闭同源保护或把所有来源设为可信。可以用 `docker inspect` 检查容器网络网关，并按实际网络设置代理 IP。

若需要局域网 HTTP 试用，设置 `BIND_ADDRESS=0.0.0.0`，把服务器 IP 加入 `ALLOWED_HOSTS`，并保持 `AUTH_COOKIE_SECURE=false`。公网应使用 HTTPS。修改 `.env` 后执行 `docker compose up -d --force-recreate app`。

当前使用 SQLite 与进程内任务队列，部署为**单应用进程、单实例**。不要增加 Uvicorn workers 或横向扩容。注册入口目前开放；这是可部署的项目原型，尚未实现邮箱验证、密码找回或邀请制管理。

## 更新、备份与恢复

更新前停机备份完整数据目录，避免只复制 SQLite 主文件而漏掉 WAL：

```sh
mkdir -p backups
docker compose stop app
docker compose cp app:/srv/lumori/data "backups/data-$(date +%Y%m%d-%H%M%S)"
git pull --ff-only
docker compose up -d --build
```

备份包含账号信息及上传资料，应放在仓库外或受保护的位置。恢复时先停止应用，再将备份目录中的内容复制回 `/srv/lumori/data`，确保 UID/GID `10001:10001` 有读写权限。`.env` 也应单独安全备份。模型可按固定版本重新下载，不必和用户数据一起备份。

需要回滚时，检出先前标签并重新构建；涉及数据库结构变化时同时恢复匹配的停机备份。升级前阅读变更记录。

## 不使用 Docker 的开发环境

Python 3.12、Node.js 22（仅运行前端测试需要）。

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
cp .env.example .env
.venv/bin/python scripts/prepare_rag_tokenizer.py
.venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8765
```

此启动方式不自动安装解析器；需要完整文档功能时优先使用 Docker。若已有独立解析环境，可以通过 `MINERU_PYTHON`、`MINERU_RUNTIME`、`DOCLING_PYTHON`、`DOCLING_MODELS` 接入；模型下载脚本支持 `--root` 指定路径。旧版 Tesseract 路径另需系统 Poppler/Tesseract 中文语言包。

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/prepare_rag_tokenizer.py
.venv/bin/python -m pytest -q
node --test tests/frontend_*.test.mjs
```

测试中的模型接口和部分解析器是可控替身，测试通过不代表真实 API 调用成功或解析准确率。研究指标不在本仓库重复发布。

## 仓库边界

不提交 `.env`、API 密钥、账号数据库、上传材料、生成内容、模型权重、虚拟环境或实验报告。`deploy/models.json` 只保存部署必需的模型版本与校验值，不包含实验数据。第三方代码与模型遵守各自许可证，见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 当前生成流程与可选能力

默认使用 native Agent，逐题执行作者、盲解、复核及局部修复。网页中的“逐题子代理”按账号保存，默认关闭；开启后，新任务使用 `supervisor_v2` 规划并有界并发出题。并发上限默认 20，可设 1–40，任务数和调用额度仍会限制实际并发。

可选 DeepSeek Harness 现覆盖资料探索的模型/搜索阶段、课程讲解及逐题生成，但需要另行安装，不包含在标准 Docker 镜像内。资料下载、来源与许可证检查、权限、预算、检查点和组卷继续由应用控制。安装和协议见[研究仓库接入说明](https://github.com/TheOverflowing/lumori-fyp/blob/main/docs/agent/harness/README.md)。

Auto exploration 对新账号默认关闭；hybrid 搜索组合教学来源目录和 Wikipedia，Brave 全网搜索需独立密钥。它仍可能因缺少可收录资料或覆盖不足而停止。

已有语音和图片生成适配接口，需单独配置服务并审核结果；视频生成尚未实现。图片理解使用 `VISION_*`，生成新图片使用 `IMAGE_*`。

`MAX_DAILY_API_CALLS=100` 为每账号每 UTC 日的调用上限；`AGENT_MAX_CALLS=180` 为每任务上限，两者同时生效。它们不是金额上限，检索、复核、修复及失败尝试也会消耗额度。未返回费用的调用金额保持未知。

## 2026-09-28 更新

- 生成条件与未提交编辑支持本浏览器草稿恢复，按账号、课程及内容基础版本隔离；保留 30 天，不跨设备同步。
- 内容/资料/任务列表返回时恢复位置；长内容增加章节/题号目录和窄屏选择器，改进输入框与生成进度显示。
- 新子代理任务支持按题失败恢复和有限引文重评；单题耗尽后保留其他题目的检查点，整组通过后才保存完整草稿。
- Harness 复核使用程序分配的当前成品证据编号，继续检查来源归属及质量；编号有效不证明语义正确。
- 新增 `review_v2` 难度观察器，先盲解再核验答案、解释与来源。`DIFFICULTY_SHADOW_MODE=off` 为默认；启用后默认只观察前 1 题、最多额外 2 次调用，不改题、不自动返工，也不改变主流程发布门槛。

任务取消、续做、部分结果、单题修改、版本评价和 Word/PDF/Markdown 导出继续可用。`/healthz` 检查数据库与后台工作线程，`/livez` 检查服务存活。

生成草稿仍需人工审核。主流程可能在其他质量检查通过后放宽难度并标记“难度已调整”；观察器的抽样、边界或弃判不代表整份测验通过。自动测试、模型复核与教师评价分别记录，当前没有学生实测难度或教学效果的结论。
