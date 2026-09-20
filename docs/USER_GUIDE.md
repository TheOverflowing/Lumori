# 使用指南 · User guide

本指南对应当前本地应用。界面只保留操作、状态和必要的反馈，功能说明集中在这里。

This guide describes the current local app. The interface keeps actions, status, and essential feedback; explanations live here.

## 账号与课程 · Accounts and courses

注册或登录后，新建课程并导入资料。课程、文件、生成材料、任务及评价归当前账号所有；检索限于当前账号所选课程。不同账号使用相同功能。学习页目前也需要所属账号登录，尚无跨账号分享。邮箱用于登录，目前没有邮件验证或忘记密码流程。

Register or sign in, create a course, and import your sources. Courses, files, generated materials, jobs, and evaluations belong to the signed-in account. Retrieval uses that account's selected course. All accounts have the same features. Learning pages currently remain private to the owner; cross-account sharing, email verification, and password recovery are not available.

## 导入与解析 · Import and parsing

支持 PDF、UTF-8 TXT/Markdown、PNG 和 JPEG；单文件最多 10 MB、300 页。同一课程内重复导入相同文件不会新增副本。PDF 和照片在后台处理，可在“任务记录”查看进度和失败原因。

Supported formats are PDF, UTF-8 TXT/Markdown, PNG, and JPEG, with a limit of 10 MB and 300 pages per file. Importing identical file contents into the same course does not create another copy. PDF and image parsing runs in the background; check **Jobs** for progress or errors.

| 选项 · Option | 说明 · Meaning |
| --- | --- |
| Standard | 默认 MinerU 解析档位。The default MinerU parsing tier. |
| Advanced | MinerU 的另一解析档位，可能耗时更长；并不保证每份文档都更准确。An alternative MinerU tier that may take longer; it is not guaranteed to be more accurate on every document. |
| 理解配图并加入检索 · Understand figures and include them in retrieval | 可选。Docling 裁切配图，再将图片及相关文字发送给已配置的视觉模型，生成说明和关键词并建立图片检索索引。需要视觉与嵌入服务。Optional. Docling crops figures; the configured vision model receives the crops and related text to produce descriptions and keywords for indexing. Vision and embedding services are required. |

TXT 和 Markdown 直接读取文字，不使用 MinerU 档位。配图理解默认关闭；关闭它不会关闭扫描件 OCR。配图理解与生成新图片是两项独立功能。

TXT and Markdown are read directly without a MinerU tier. Figure understanding is off by default; turning it off does not disable OCR for scanned pages. Understanding source figures and generating new images are separate features.

在“文字与原图”中逐页对照解析结果。页面有文字只说明有提取结果，不代表内容完整或识别正确。“暂无解析报告”表示旧资料没有逐页报告；重新解析可创建报告，但随后需要重建文字索引。

Use **Text and images** to compare extracted text with the original page. A page containing extracted text is not proof of complete or accurate recognition. **No parsing report yet** means a legacy file has no page-level report. Reparse it to create one, then rebuild its text index.

## 索引与文件管理 · Indexing and file controls

解析完成后，点击“建立索引”。只有索引可用且已开启的文件，才会参与新的检索。重新解析，或更换嵌入模型、地址及相关参数后，需要重建索引。

After parsing, select **Index**. A file needs a usable index and an enabled switch to participate in new retrieval. Rebuild the index after reparsing or changing the embedding model, endpoint, or relevant parameters.

| 操作 · Action | 结果 · Result |
| --- | --- |
| 开关开启 · Switch on | 文件的有效文字和图片索引可用于生成时的检索。Usable text and figure indexes are eligible for retrieval during generation. |
| 开关关闭 · Switch off | 保留文件、索引和历史引用；新的检索跳过该文件。重新开启可复用兼容索引。The file, indexes, and historical citations remain; new retrieval skips the file. Re-enabling can reuse compatible indexes. |
| 删除 · Delete | 文件移入“最近删除”并关闭。原文件和索引仍保留，不释放磁盘空间，也没有自动清理期限。Moves the file to **Recently deleted** and disables it. The source and indexes stay on disk; there is no automatic expiry. |
| 恢复文件 · Restore file | 恢复后开关仍关闭。手动开启后，可继续使用兼容索引。The restored file remains disabled. Turn it on to reuse compatible indexes. |

停用或删除不会撤销已经进入模型生成阶段的任务，也不会改写已有材料。已有材料的历史来源仍可由所属账号查看。已删除文件需先恢复才能重新解析、建索引或编辑图片说明；再次上传相同文件不会自动恢复它。

Disabling or deleting does not cancel a job that has already entered model generation or rewrite existing materials. Historical sources remain available to the owner. Restore a deleted file before reparsing, indexing, or editing figure descriptions. Re-uploading identical contents does not restore it automatically.

## 生成与难度 · Generation and difficulty

填写学习目标、输出语言和目标学生的已学基础。讲解材料选择“讲解深度”；测验和作业可统一难度，也可按简单、中等、困难分别指定整数题数。每档允许为 0，总和必须等于题目数量；一次请求 1–50 题，可以全部为难题。改变总题数后需自行调整分配，系统不会重新平摊。

Enter learning objectives, output language, and learners' prior knowledge. Lessons use **Explanation depth**. Quizzes and assignments support one difficulty for all questions or integer counts for Easy, Medium, and Hard. Each count may be zero; the total must match the requested 1–50 questions. All questions may be Hard. Changing the total does not redistribute the counts automatically.

测验与作业默认直接展示题目，不添加题前的学习目标与背景讲解。需要时可开启“添加题前讲解”；题干中的作答条件、场景、数据以及答案解析始终保留。切换到讲解材料时，该开关隐藏，讲解内容照常生成。该选择只影响新任务，已有材料不会自动改动。

Quizzes and assignments show questions directly by default, without introductory learning objectives or teaching sections. Turn on **Include an introduction** when these are useful. Conditions, scenarios and data needed to answer each question, and the answer explanations, remain part of the material. The switch is hidden for lessons, which retain their teaching content. This choice applies to new requests and does not rewrite existing materials.

测验与作业会逐题生成、检查和保存进度。中断后可在任务详情“继续生成”，已通过的题目会保留；未完成的接口请求可能已计费。全部题目通过检查后才产生完整草稿，仍需核对答案。每题至少约 3 次文本调用，50 题超出现有每日 100 次默认额度，系统会提前提示。

Quizzes and assignments are generated and checked one question at a time. After an interruption, use **Continue generation** in the job details to keep completed questions; an interrupted API request may already have been billed. A complete draft is saved only after all questions pass, and answers still need review. Each question needs at least about three text calls, so 50 questions exceed the default daily allowance of 100 calls.

新任务给每题至少三轮普通生成与修正机会；提前通过即可继续下一题。三轮后若只是格式差异，系统会自动尝试兼容已有内容，例如保留六个选项、识别 `(B)` 这样的答案标记，或保留超过六项的概念列表，再继续解题与复核。格式兼容不会删掉题目内容，也不会跳过答案与资料检查；它仍可能因调用额度、网络或服务故障而中断。

New jobs allow at least three ordinary attempts per question, proceeding as soon as a candidate passes. If only formatting differences remain, the app automatically tries to retain and adapt the existing content—for example, six choices, an answer marked `(B)`, or more than six concept labels—then continues solving and reviewing it. Adaptation does not discard question content or skip answer and source checks. API allowances, network interruptions and service failures can still stop processing.

内容仍有问题且保存了完整逐题进度时，可选择“继续修正”，为未通过的题目再增加三轮尝试。已经通过的题目、原输入和历史记录会保留，原任务的调用计数继续累计。旧任务保留原先的普通尝试规则；点击继续修正才明确增加新的机会。

When content still needs correction and a complete question checkpoint is available, select **Continue refining** for three further attempts on the unfinished question. Passed questions, your request and previous records remain saved, and the same job's call count continues accumulating. Older jobs keep their original ordinary-attempt policy; choosing to continue explicitly grants the additional attempts.

| 难度 · Level | 设计目标 · Design target |
| --- | --- |
| 简单 · Easy | 识别或解释单一知识点。Identify or explain one concept. |
| 中等 · Medium | 在变化的情境中选择并应用方法，关联相关概念。Choose and apply a method in a changed context, connecting relevant concepts. |
| 困难 · Hard | 整合多个概念，分析约束、比较方案并论证结论。Combine concepts, analyze constraints, compare approaches, and justify conclusions. |

难度相对于目标学生及其已学基础定义，不由题干长度决定。系统检查题数和目标档位，并对生成题目进行模型复核；模型复核不等于教师验证，也不代表学生实际答题难度。实际难度还需结合学生表现验证。

若仅难度与目标不一致，新任务最多进行三轮调整和复核；仍未匹配时，系统优先保留最接近目标且通过其他检查的结果。页面分别显示目标难度与模型判断，并标记“已保留结果，难度有偏差”。资料支持、答案正确性及歧义检查不会因此放宽；接口故障与调用上限仍可能中断任务。失败后可在进度页选择“返回修改”，恢复上次的学习目标、学生基础、题数与分配等输入；已有可续做记录时仍可选择继续生成。

For new requests with a difficulty mismatch only, the system makes up to three calibration attempts, then retains the nearest candidate that passed its other checks. Requested and assessed difficulty remain separate, labeled **Result retained with a difficulty difference**. Source support, answer correctness, ambiguity checks and API limits still apply. After a failure, **Edit request** restores the saved inputs; **Continue generation** remains available when a resumable checkpoint exists.

Difficulty is relative to the learners and their prior knowledge, not the length of a question. The system checks counts and target levels and requests a model review of generated questions. Model review does not establish teacher approval or empirical student difficulty; those need human judgment and student performance data.

## 生成前补充信息 · Clarifying your request

生成页面默认开启“生成前追问”。点击“开始生成”后，系统先检查学习目标：可以直接处理就继续；如果缺少会影响主题或具体情况的信息，会显示一个补充问题。日常说法不一定需要追问，通用讲解或练习请求也可以直接进行。此检查会增加最多一次文本模型调用和等待时间；不需要时可以关闭开关。

**Ask before generating** is on by default. After you select **Generate**, the app checks the learning objective. It proceeds directly when possible, or asks one follow-up when missing information changes the subject or specific situation. Everyday wording and broad requests for explanations or practice do not automatically need clarification. The check adds up to one text-model call and some waiting time; turn it off to proceed directly.

补充窗口可展开查看原学习目标。可以填写最多 2000 字符，也可以点选建议回答后修改，再提交生成。只补充自己确定的信息，不需要提供本来想学习的答案。系统保留原目标和真实回答，不会用模型猜测的回答代替你；未选择的选项不算已确认条件。

Expand the original-request section to see your learning objective. Enter up to 2,000 characters, or choose a suggested answer and edit it, then submit. Supply only information you know; you do not need to provide the answer you came to learn. The app preserves your original objective and actual reply, and does not treat unselected options as confirmed facts.

如果不知道，可选择“不确定，按原输入继续”；条件仍不明确时，结果可能给出分情况说明或提示资料不足。提交前取消或按 Escape 会返回原表单。检查服务暂不可用时，可以明确选择“按原输入继续生成”，也可以取消后重试。取消不会保证已发出的模型调用不计费。

If you do not know, choose **Not sure — use original request**. Unresolved conditions may lead to a conditional explanation or insufficient-evidence feedback. Before submitting, cancel or press Escape to return to the original form. If the check is unavailable, choose **Generate using original request**, or cancel and retry. Cancelling does not guarantee that an already-sent model request will not be billed.

检查结果有效期为 15 分钟，只用于当前账号、当前生成条件的一次生成。改变主题、课程或其他条件，或等待过久，需要重新检查；已接受的生成任务不会因为这个期限到期而失效。补充问题由模型生成，仍可能有误；可以自行纠正问题中的假设或直接返回修改学习目标。技术记录与开发验证见[需求检查接入说明](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/rag/CLARIFICATION_RUNTIME_20260920.md)。

A check is valid for 15 minutes and one generation with the same account and settings. Change the topic, course, or other settings—or wait too long—and a new check is needed. An already accepted generation does not expire with the check. Model-generated follow-ups may be mistaken; correct their assumptions in your reply or return to edit the objective. See the [implementation and development record](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/rag/CLARIFICATION_RUNTIME_20260920.md) for details.

## 自动探索 · Auto exploration

如果没有上传资料，或现有材料只覆盖部分学习目标，可以在左下角“设置”开启“自动探索”。默认关闭，选择按当前账号保存在这台浏览器中。开启后，课程、文本模型和嵌入模型仍需准备好；无需先上传文件。关闭时，需要有已启用且索引就绪的课程资料。

If you have no uploaded sources, or existing materials cover only part of your objective, enable **Auto exploration** in **Settings** at the bottom of the sidebar. It is off by default and remembered per account in this browser. A course and configured text and embedding models are still required; uploading a file first is optional in this mode. With exploration off, enabled course sources with ready indexes are required.

系统先检查已有依据，必要时查找公开参考资料。下载并解析后，先建立临时索引，再按缺少的知识点查找相关段落；通过检查的资料会保存到当前课程，用于继续生成。默认“公开课程目录”只从预设公开课程网址中寻找材料，然后联网读取；它不是全网搜索。维护者配置 Brave Search 后才可进行实时网页搜索。可以在设置的“资料搜索”一行查看当前提供方。

The app checks existing evidence and finds public references when needed. After downloading and parsing, it creates a temporary index and retrieves passages for each missing concept. Sources that pass the evidence check are saved to the current course for generation. The default **Public course directory** searches a preset collection of public course URLs and then reads them online; it is not a full web search. Live web search requires Brave Search configuration by the operator. Check **Source search** in Settings to see the current provider.

只有通过筛选且允许自动保存的来源才会进入当前课程，标记为“自动发现”。可保存来源包括已有明确政策的目录资料，以及网页明确声明受支持开放许可的内容；没有明确许可的材料不会自动收录。可在资料列表和生成内容的引用面板打开原始来源，并像上传的文件一样停用或删除。资料归当前账号所有；关闭探索不会移除已保存资料。搜索使用提炼的公共概念词，不直接把上传文件全文交给搜索服务；资料覆盖与选择仍使用已配置的文本模型。

Only selected sources with an established storage policy are added to the current course, marked **Discovered source**. Eligible sources include reviewed catalog entries and pages declaring a supported open license; materials without an established license are not automatically saved. Open their original links from the source list or a generated item's references; disable or delete them like uploaded files. They belong to the current account, and turning exploration off does not remove them. Search uses extracted public concept terms rather than directly sending uploaded files in full to the search service; evidence checks and source selection still use the configured text model.

探索默认最多两轮、接纳五份资料，并有调用和时间上限。它会增加等待和可能的接口费用，不保证每个主题都有适合的公开资料。当前自动探索不处理配图，PDF 使用 Standard 解析。进度页会显示搜索、读取、筛选和索引状态；完成后可选择“查看结果”。AI 选择和引用存在都不等于内容已经由教师核验，仍需对照原文检查。

By default, exploration allows up to two rounds and five accepted sources, with call and time limits. It adds waiting time and may incur API charges; suitable public material is not guaranteed for every topic. It does not analyze figures, and discovered PDFs use Standard parsing. The progress page shows searching, reading, selection and indexing; choose **View result** when generation finishes. AI selection and existing citations do not establish teacher verification—check the output against the source.

如果缺少的是你的具体题目、图片或教师要求，网上资料不能代替，系统会提示补充。探索达到上限或仍缺乏依据时不会强行生成，已成功保存的资料会保留。更改开关会使尚未提交的需求检查失效；已经提交的任务保持原设置。探索阶段中断后请新建任务，避免盲目重复可能已经计费的请求。实现与实验边界见[自动探索记录](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/rag/AUTO_EXPLORATION_V1.md)。

Public sources cannot replace your missing original question, image, or teacher-specific requirements; the app will ask for the needed information. If limits are reached or evidence remains insufficient, generation stops and successfully saved sources remain. Changing the switch invalidates an unsubmitted detail check; submitted tasks keep their original settings. After an interrupted exploration, start a new task to avoid blindly repeating potentially billed requests. See the [implementation and evaluation record](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/rag/AUTO_EXPLORATION_V1.md) for details.

## 生成进度 · Generation progress

在“设置 → 进度显示”中选择“简洁”或“详细”，默认使用详细视图。选择按账号保存在当前浏览器，切换视图只改变展示，不会重新生成或增加模型调用。简洁视图突出当前状态；详细视图在左侧列出阶段与耗时，右侧展示选中阶段的动画和处理记录。手机上阶段导航移到上方。

Choose **Simple** or **Detailed** under **Settings → Progress view**. Detailed is the default. The choice is remembered per account in this browser; switching changes the display only, without starting generation or making model calls. Simple emphasizes the current status. Detailed places stages and timings alongside the selected stage's animation and activity record; on phones the stage navigation moves above the detail area.

生成请求提交成功后，会进入独立的进度页。页面根据后端当前步骤显示检索、规划（适用时）、生成与检查、保存等阶段；逐题流程会显示已通过系统检查的题数。动画表示任务仍在处理，不表示估算百分比或预计剩余时间。

After a generation request is accepted, its progress page shows the backend's current stage: retrieval, planning where applicable, writing and checking, then saving. Question workflows report the number that passed the system's checks. Animation indicates ongoing work, not an estimated percentage or remaining time.

“处理记录”展示系统实际采取的步骤与决策摘要，例如选入多少资料片段、融合为何回到原查询、哪道题通过检查或进入修正。新记录区分“需调整格式”“需修正内容”“需补充依据”、重复题与难度差异，并记录开始兼容格式及兼容完成。简洁和详细视图均保留同一个任务的恢复入口；不展示模型内部思维链，也不编造推理文本。点击阶段可以回看记录，再选择“跟随当前步骤”回到实时阶段。

The activity record shows actual actions and decision summaries, such as selected source excerpts, why fusion returned to the original query, or which question needs revision. New records distinguish formatting, content, source support, duplicate questions and difficulty differences, and record when format adaptation starts and finishes. Both progress views retain the same job's recovery actions. They do not expose internal model reasoning or invent a thought transcript. Select a stage to review its record, then choose **Follow current stage** to return to the live step.

阶段用时由服务端记录，表示执行该阶段所用的时间。续做会累计各次实际执行时间，中间等待恢复或离线的时间不计入。计时未知显示“—”；中断造成部分记录缺失时，用“≥”标记已记录的下限。旧任务不会补造时间或详细记录。页面中的累计用时不是从提交到完成的墙钟总时长，也不等同于模型接口调用延迟。

Stage durations are recorded by the server and measure time spent executing each stage. Resuming adds actual execution intervals, excluding the wait before resuming and offline time. Unknown timings show “—”; “≥” marks the recorded lower bound when an interruption leaves part of the duration unknown. Historical timings and activity records are not fabricated. Cumulative processing time differs from the wall-clock time between submission and completion and from model API latency alone.

可以离开页面，之后从“任务记录”重新打开运行中或未完成任务的进度；已成功任务可直接打开结果。刷新进度页不会重新提交生成。全部完成后，选择“查看结果”进入内容核查，页面不会自动跳走。资料不足时可调整要求或补充资料；可恢复的中断任务提供“继续生成”，保留完整失败题目进度的任务提供“继续修正”。连接暂时中断时，页面保留最后收到的状态并提示刷新，不能据此认定任务失败。

You can leave and reopen progress for running or unfinished jobs from **Activity**; successful jobs can open their results directly. Reloading the progress page does not submit another generation. When complete, select **View result** to inspect the material; the page stays in place until you choose. Insufficient sources require adjusted objectives or more material. Resumable interruptions offer **Continue generation**; jobs retaining a complete failed-question checkpoint offer **Continue refining**. If live updates lose their connection, the last known state remains visible with a refresh action; that does not itself mean the job failed.

当前审核后的语音与配图任务也使用这套进度页。视频及更多媒体类型可扩展同一阶段接口；尚未执行的媒体功能不会显示为已完成步骤。旧任务没有保存阶段历史时，只展示已知任务状态。

Post-approval audio and image jobs use the same progress page. The stage contract can also support video and other media later, without showing unused features as completed steps. Older jobs without recorded milestones show only their known job status.

## 审核与评价 · Review and evaluation

“内容与审核”跟随当前所选课程，只显示该课程的记录。每条记录使用对应材料类型的图标，并在标题下显示讲解、测验或作业。状态筛选、标题搜索、待审核计数及运行中／失败任务都限定在同一课程；切换课程后，状态恢复为“全部”，搜索清空。

**Content and review** follows the selected course and shows only its records. Each record uses the matching material icon and a Lesson, Quiz or Assignment label beneath the title. Status filters, title search, the pending-review count, and running or failed jobs all stay within that course. Switching courses resets the status filter to **All** and clears the search.

在“内容与审核”中对照原文、答案及引用，确认后再使用学习页。系统检查结构与引用标识，但引用存在不代表它支持结论。编辑内容会保存为新版本，并需要重新审核；原引用标识仍保留，应再次核对是否支持修改后的内容。

In **Content and review**, check the sources, answers, and citations before approving text and using the learning page. Structural and citation-ID checks do not establish factual support. Editing saves a new version that needs review again. Citation IDs remain, so check that the sources still support the edited content.

整体评价使用 1–5 分，1 分最低、5 分最高。逐题难度评价可全部留空；如果开始填写，则需完成所有题目，也可选择“无法判断”。评价与逐题难度可分别导出 CSV。学习页默认折叠答案，可主动查看。

Overall ratings run from 1 (lowest) to 5 (highest). Question-level difficulty ratings may all be left blank; once started, every question needs a rating, including **Uncertain** where appropriate. Overall and question-level evaluations have separate CSV exports. Answers are initially hidden on the learning page and can be revealed.

“内容评价”默认折叠；保存后显示已评价的版本，再展开会回填评分、观察记录及逐题难度。修改评价会更新当前记录并保留历史，不会把同一版本重复计为多个样本。编辑内容产生新版本后，需要单独评价。评分用于质量记录与分析，不会自动训练模型或改变已生成内容。

**Content rating** is collapsed by default. A saved rating shows its version; reopening restores the scores, notes and question-level judgments. Updates preserve history while keeping one current observation per material version. A newly edited content version starts unrated. Ratings record feedback; they do not train the model or change generated content automatically.

### 下载生成内容 · Download generated materials

详情顶部的“导出”可选择 PDF、Word（DOCX）或 Markdown。题目默认包含答案与解析，可取消勾选以导出练习用版本。PDF/Word 包含正文、题目及可读的数字引用和来源列表；附件文件名包含内容标题与版本。界面语言决定导出栏目名称，原文语言保持不变。导出期间若版本已变化，页面会提示刷新，防止下载与当前审核版本不一致。

Select **Export** at the top of the material details and choose PDF, Word (DOCX), or Markdown. For questions, clear **Include answers and explanations** to produce a practice copy. PDF/Word include numbered citations and a source list. Their filenames include the title and version. Interface language controls section labels; authored text remains unchanged. If the content version changes, refresh before exporting again.

导出使用当前已保存的版本：仅题目模式生成的材料不会在导出时补加讲解；旧材料已有的讲解也不会因新默认值而被移除。“包含答案与解析”只控制导出中的答案部分，与生成前的“添加题前讲解”互不替代。

Exports use the saved version. Question-only materials do not gain an introduction during export, and existing introductions in older materials remain intact. **Include answers and explanations** controls the exported answer section separately from **Include an introduction** on the generation form.

PDF/Word 中，同一文件的多个片段共用一个参考编号，文末只列一条来源并合并相关页码；文件名相同但实际不同的文档不会误合并。网页仍可逐片段查看原文。证据 JSON 已收纳到“导出 → 更多导出”；已批准内容旁的“打开学习视图”与导出按钮保持统一样式。

In PDF/Word, passages from the same document share one reference number and a single bibliography entry with combined page locations. Different documents with matching filenames remain separate. The review page still provides passage-level inspection. Find evidence JSON under **Export → More exports**; approved content has a matching **Open learning view** control beside Export.

语音、配图生成后会出现“下载”入口。未来视频可以接入同一下载接口；目前尚未接入视频生成。所有导出与媒体下载均要求登录，并验证所属账号。

Generated audio and images have a **Download** action. The same endpoint supports future video files; video generation is not yet connected. All document and media downloads require the owning account to sign in.

## 图片说明与媒体 · Figure descriptions and media

原文图注、AI 图片说明和人工编辑说明分别显示。AI 说明及关键词可能有遗漏或错误，应对照原图核验。保存人工修改后，该图片原有检索索引失效；点击“重试或更新图片索引”使新说明可被检索。文字索引与图片处理状态分别查看。

Original captions, AI figure descriptions, and manually edited descriptions are labeled separately. AI descriptions and keywords can omit or misread details; compare them with the source image. Saving an edited description invalidates that figure's old search index. Select **Retry or update figure index** to make the revised description searchable. Text indexing and figure processing have separate status indicators.

生成的文字经确认后，可调用已配置的语音或图片服务，并分别审核媒体。修改文字产生的新版本不会沿用旧媒体到学习页。视频目前尚未支持。

After text approval, configured speech and image services can generate media for separate review. A new text version does not carry old media into its learning page. Video generation is not available yet.

## 设置与模型连接 · Settings and model connections

左下角“设置”将生成偏好和模型连接放在同一个窗口。“融合模式”默认关闭，按当前账号保存在这台浏览器中；换浏览器或清除网站存储后需要重新选择。开关立即用于后续的新请求，不修改正在执行的任务或服务端共用的模型配置。

**Settings** at the bottom of the sidebar groups generation preferences and model connections. **Fusion mode** is off by default and is remembered per account in this browser. A different browser or cleared site storage requires a new choice. Changes apply to subsequent new requests, without modifying running tasks or the shared server model configuration.

开启融合后，系统可生成一个保守的检索改写，将原输入和改写分别检索，再合并候选。它可能增加一次文本调用和一次嵌入调用，也可能理解错误，不保证更好的召回。选择“不确定”或在检查失败后按原文继续时，不额外猜测和改写；改写不可用时回到原查询。算法、限制和验证记录见[融合模式接入记录](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/rag/QUERY_FUSION_RUNTIME_20260920.md)。

When enabled, fusion may create a conservative search rewrite, retrieve with both queries, and combine their candidates. It may add one text call and one embedding call, and does not guarantee better retrieval. Continuing without clarification does not trigger a speculative rewrite; unavailable rewriting falls back to the original query. See the [runtime record](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/rag/QUERY_FUSION_RUNTIME_20260920.md) for the algorithm and evidence boundaries.

“模型连接”展示配置状态。模型地址、名称和密钥由本地维护者在 `.env` 中设置，保存后重启应用；网页不显示密钥。“已填写配置”只表示配置完整，实际连接需通过调用验证。

**Model connections** shows configuration status. The local operator sets endpoints, model names, and keys in `.env`, then restarts the app. Keys are not displayed in the browser. **Configured** indicates that fields are present; a successful call is still needed to verify connectivity.

调用数按账号和 UTC 日期统计，包括失败尝试；次数上限不是费用上限。账号的数据相互隔离，但服务端模型配置共用。详细安装、配置和限制见[项目 README](../README.md)，实验与研究指标见 [RAG 实验档案](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/rag/README.md)和[文档解析实验](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/parsing/README.md)。

Calls are counted per account and UTC date, including failed attempts. A call limit is not a spending cap. Accounts have separate data but share the server's model configuration. See the [project README](../README.md) for setup and limits, and the [RAG archive](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/rag/README.md) and [document parsing archive](https://github.com/TheOverflowing/lumori-fyp/blob/ca1-2026-09-20/docs/parsing/README.md) for research results.
