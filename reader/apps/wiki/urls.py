from django.urls import path

from . import views

urlpatterns = [
    path("wiki", views.index, name="wiki_index"),
    # 标签总览（2026-11-16）：全库词表按覆盖页数降序，单次词单独成组。
    # ★ 不是 `wiki/tags/{tag}` 这种通配：筛选走首页已有的 `?tag=`（与 `?src=`
    #   同一套 query 语义），本页只负责「让你看见有哪些词」。两个入口一个筛选，
    #   免得多造一条能筛页的 URL 规则（那等于第二份筛选判据）。
    path("wiki/tags", views.tags, name="wiki_tags"),
    # 只读候选查询页（第十一步）：能翻全部候选、能丢弃/改判，不做审批、不花钱
    path("wiki/candidates", views.candidates, name="wiki_candidates"),
    # ★ 单页详情用 query 参数 p=wiki/<domain>/<slug>.md 而不是 URL 通配：
    #   页相对路径里有 `<domain>/`，做成 <path:rel> 会和 /wiki/approve 这类端点
    #   抢前缀；而且 core.resolve_page() 本来就要对整串做「留在 wiki/ 之下」的校验。
    path("wiki/page", views.page, name="wiki_page"),
    # 审批 / 退稿只接受 POST（GET 会改文件 = 能被预取或被预览链接误触发）
    path("wiki/approve", views.approve, name="wiki_approve"),
    path("wiki/discard", views.discard, name="wiki_discard"),
    # 只读审批浮层（第五步 5a）：GET 拉片段，只看 .drafts/ 不写不改。
    # ★ `wiki_approval`（浮层）≠ `wiki_approve`（通过草稿），两个名字极像，别写错。
    path("wiki/approval", views.approval, name="wiki_approval"),
    path("wiki/approval/draft", views.approval_draft, name="wiki_approval_draft"),
    # 审批浮层标题直改（2026-11-11）：GET 取编辑/展示态片段（只读），POST 保存
    # （改名走与审批对话同一套 wa.set("title") 链，不是第二条写路径）
    path("wiki/approval/title", views.approval_title, name="wiki_approval_title"),
    path("wiki/approval/title/save", views.approval_title_save, name="wiki_approval_title_save"),
    # 审批浮层标签就地改（2026-11-16）：只吃 POST，回 chip 片段；仍只写 .drafts/
    # （不碰 wiki/），入库照旧只有 commit / approve 一条路。
    path("wiki/approval/tags", views.approval_tags, name="wiki_approval_tags"),
    # 「找找相关页」（2026-11-11）：审批期唯一一次 LLM 推荐，落点在
    # core.wiki_related.recommend；结果带指纹缓存，渲染路径只读缓存、零 LLM。
    path("wiki/approval/related", views.approval_related,
         name="wiki_approval_related"),
    # 「可能相关的已入库页」→ 一键改判为并入（2026-11-11）：只接受 POST，
    # 落点在 core.ingest.merge_draft_into，视图不复制目标合法性判据。
    path("wiki/approval/merge-into", views.approval_merge_into,
         name="wiki_approval_merge_into"),
    # 审批对话（第五步 5b）：GET 懒加载 / POST 发送（调 wiki_loop.run）/ POST 清空
    path("wiki/approval/chat", views.approval_chat, name="wiki_approval_chat"),
    path("wiki/approval/chat/send", views.approval_chat_send, name="wiki_approval_chat_send"),
    # SSE 流式发送（2026-11-19），与老 send 并存，前端失败可回退。
    path("wiki/approval/chat/stream/send", views.approval_chat_stream_send,
         name="wiki_approval_chat_stream_send"),
    path("wiki/approval/chat/clear", views.approval_chat_clear, name="wiki_approval_chat_clear"),
    # 「重编结论」一键按钮（2026-11-22）：只允许 POST（会调 LLM 花钱、写草稿），
    # 落点 wiki_loop._polish_one（与审批对话 polish 工具、编译收尾同一份判据）。
    path("wiki/approval/repolish", views.approval_repolish, name="wiki_approval_repolish"),
    # 审批标记切换：POST 切换草稿的已审批/未审批状态
    path("wiki/approval/toggle", views.approval_toggle, name="wiki_approval_toggle"),
    # 整批入库（第五步 5c，改动 F）：POST 一次把 .drafts/ 全验后全动。
    # ★ 三个极像的名字，别再混：wiki_approval（浮层）/ wiki_approve（单页通过）
    #   / wiki_approval_commit（整批入库）。
    path("wiki/approval/commit", views.approval_commit, name="wiki_approval_commit"),
    # 草稿编辑（阶段 2.3b）：预览 + 保存，都只接受 POST
    path("wiki/draft/preview", views.draft_preview, name="wiki_draft_preview"),
    path("wiki/draft/save", views.draft_save, name="wiki_draft_save"),
    # 入库页编辑（阶段 2.3c）：预览 + 保存，保存后跳回详情页
    path("wiki/page/preview", views.page_preview, name="wiki_page_preview"),
    path("wiki/page/save", views.page_save, name="wiki_page_save"),
    # 拉入审批编辑（第六步）：把已入库页复制成「替换稿」草稿，走审批 → commit 覆盖
    path("wiki/page/edit", views.page_edit, name="wiki_page_edit"),
    # 请求删除（第八步）：产出「替换稿的空形态」（edits + 空候选），同样是走审批
    path("wiki/page/delete", views.page_delete, name="wiki_page_delete"),
    # 断链三动作（第九步）：人在断链清单里点「改指向 / 降级」→ 落 edits 补链草稿
    path("wiki/relink/apply", views.relink_action, name="wiki_relink_apply"),
    # 删页提案的逐条去向（第十步）：页还在、候选清单还在时，人点「留 → 去 X / 废」
    path("wiki/retire/apply", views.retire_apply, name="wiki_retire_apply"),
    # 入库分诊 + 提取（阶段 2.2）：一次 wiki_extract 调用（花 wiki 池的钱）
    path("wiki/ingest", views.ingest_run, name="wiki_ingest"),
    path("wiki/candidate", views.candidate_status, name="wiki_candidate"),
    # 编译成草稿（阶段 2.3a）：S1 分组 + 每页一次生成，比提炼贵，同样要人点
    path("wiki/compile", views.compile_run, name="wiki_compile"),
    # 编译中的状态条（htmx 轮询）：按钮该不该灰由服务端说，前端只负责问
    path("wiki/compile/state", views.compile_watch, name="wiki_compile_state"),
    # 划线 / 批注（阶段 1：锚定 (source_type, source_id)，软引用）
    path("wiki/highlight", views.highlight, name="wiki_highlight"),
    path("wiki/annotation", views.annotation, name="wiki_annotation"),
    path("wiki/mark/delete", views.mark_delete, name="wiki_mark_delete"),
    path("wiki/mark/edit", views.mark_edit, name="wiki_mark_edit"),
    # 对话（阶段 2.1）：GET 懒加载片段，POST 提问/清空。
    #   */stream/send 是 SSE 流式端点（2026-11-19），与老 send 并存；前端优先走流式，
    #   失败回退老端点。老端点一行没动，保证非流式测试桩全绿。
    path("wiki/chat", views.chat, name="wiki_chat"),
    path("wiki/chat/send", views.chat_send, name="wiki_chat_send"),
    path("wiki/chat/stream/send", views.chat_stream_send, name="wiki_chat_stream_send"),
    path("wiki/chat/clear", views.chat_clear, name="wiki_chat_clear"),
]
