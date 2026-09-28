# Template Design Specification — FIN-C2-165 Trade Finance LC Document Preflight Agent

> This document describes the implemented architecture, data flow, and safety boundaries of the template.

## 1. Position in AgentCore Architecture

- **Agent Class**: `TradeFinanceLCPreflightAgent`（`src/graph/graph.py`）
- **L1 Base**: `AgentBaseGraph`（L1 直継承。固定 5 ノード backbone。`AutonomousBaseGraph` は不採用 — 自律 think-act ループを持たない固定パイプラインのため）
- **Composition Pattern**: `main` スロットに `GraphNode` サブクラス（`LCPreflightGraphNode`）を置き、内側 domain workflow（`src/graph/domain_workflow_graph.py` の `DomainWorkflowGraph`）へ委譲する Cat 2 標準構成。**外側は `AgentBaseGraph`（固定 5 ノード backbone）／内側 `DomainWorkflowGraph` は `BaseGraph`（fully custom topology で Stage1-4 を自由に配線）**。内側に `AgentBaseGraph` は使わない（`AgentBaseGraph` は pre_process/main/post_process の 3 スロット固定で Stage1-4 の 4 段トポロジーを表現できない — 2026-07-21 SDK introspection で確認）
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 禁止 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override）
  - Graph: composition（`register_nodes()` でノード差し込み。外側 `add_edges()` は override しない）

## 2. Architecture Overview

### 2-1. 3 モード構成（single-entry・mode 分岐）

本テンプレートは蓄積型データ資産（`lc_terms_registry` ＋ `shipment_packets` ＋ `bank_return_history`）の
ライフサイクルを担う 3 モードを、**単一の `AgentBaseGraph`** で処理する。`src/api/server.py` が 3 つの
HTTP ルートを受け、同一グラフを `mode` 付きで invoke する（複数グラフは立てない — AgentCore single-entry 設計と整合）。

| HTTP ルート | mode 値 | 用途 | DB 書き込み |
|---|---|---|---|
| `POST /ingest` | `ingest` | SKU / 品目→必要証憑ルール、過去の銀行差戻し理由・照会履歴（CSV/Excel 由来の行データ）を正規化して蓄積 | `lc_terms_registry` / `bank_return_history` へ upsert（テナント区画キー付き） |
| `POST /invoke` | `preflight`（既定） | 新規貨物パケットの L/C 条項×船積書類プリフライト。矛盾候補・要修正・要照会論点を根拠付きで抽出したブリーフを返す | `shipment_packets` へケースヘッダ INSERT（監査用・参照 ID 化） |
| `POST /feedback` | `feedback` | L/C 担当が確定・編集した論点を明示承認＋監査イベント付きで反映。承認済み照合観点を KB へ還流 | `feedback_audit_events` 追記＋KB upsert（承認済み source-linked 照合観点のみ） |

- `mode` は server.py がルートに応じて `user_input`（JSON 直列化ペイロード）へ注入する。L/C 担当が mode を意識することはない。
- 存在しない案件参照への `/feedback` は `status: "error"`（`error_log` に理由）を返す。同一イベント再送は冪等。
- `/preflight` はケースヘッダを INSERT するため read-only 操作ではない。`entry-adapter` の既存 write policy
  `WRITE_INGEST`（standalone token / STG default を拒否し middleware identity を必須化）を適用する。

### 2-2. Node Configuration（外側 backbone — 固定 5 ノード）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | framework 既定（トレース初期化・InvocationContext 解決） | user_input | validated_input | InitializeNode (default) |
| pre_process | テナント/権限解決 fail-closed・入力検証・S-2 取引情報最小化・案件参照 ID 化 | validated_input | case_ref, tenant_id, mode | PreProcessNode override |
| main | `LCPreflightGraphNode`（GraphNode）→ 内側 domain workflow へ委譲（Stage1-4） | case_ref, tenant_id, mode | preflight_brief, evidence_map, status | GraphNode subclass |
| post_process | S-3 根拠リンク実在性/対応検証・取引情報再走査・提示可否/支払可否/真正性判定の構造遮断・出力整形 | preflight_brief, evidence_map | formatted_output | PostProcessNode override |
| finalize | framework 既定（final_output surfacing） | formatted_output | final_output | FinalizeNode (default) |

> `initialize` / `finalize` は `super().register_nodes()` が自動注入。テンプレは pre_process / main / post_process の 3 スロットを埋める。

### 2-3. 内側 domain workflow（`LCPreflightGraphNode` が委譲する Stage 1-4）

`main` の `GraphNode`（`LCPreflightGraphNode`）は内側の `domain_workflow_graph.py`（`DomainWorkflowGraph`・**`BaseGraph` 直継承**・Stage1-4 の custom topology）へ委譲する。
内側は `extract_input(state) -> str`（親 state を user_input 文字列へ直列化）と `merge_output(state, sub_result) -> dict`
（サブグラフ結果を親 state へ明示マッピング）で親子境界を切る。

> **⚠️ tenant_id の subgraph 受け渡し（fail-closed 契約・レビュー指摘対応）**: `GraphNode` は内側へ
> `extract_input()` が返す **string と context のみ**を渡す（context に tenant_id フィールドは無い — §3-bis）。
> したがって **`extract_input()` は、pre_process が検証済みで外側 state に載せた `tenant_id`（＝ `entry-adapter` が
> 検証済み `request.state` から注入した値・クライアント payload 由来ではない）を、直列化ペイロードに必ず含める**。
> Stage2 の KB 検索・payload-store の全操作はこの内側に運ばれた tenant_id で区画する。tenant_id 欠落時は
> Stage2 実行前に fail-closed（`status="error"`）。クライアント payload 内の tenant 値は使わない。

| Stage | 主要ロジック | 出力 |
|---|---|---|
| Stage 1（決定論チェック） | 必須書類の有無、数値項目の突合、呈示期限を決定論的に検証（`shared/tools` 相当・LLM 不使用）。**この出力は immutable authoritative facts** — 後続 Stage で上書き・再判定させない | `docs_present` / `field_diff` / `deadline_status`（immutable） |
| Stage 2（過去差戻し想起） | 同一テナント KB を検索セーフな射影（source-linked な過去差戻し理由・照会観点、その source ID・品質/provenance）で検索。結論ラベルは継承しない。**検索クエリは Stage1 が「問題あり」と判定した所見（欠落書類名・許容差超過フィールド名・`late`/`at_risk`）を検索語として組む**（`_recall_query_terms`）。KB 側の `summary` は自由文なので、構造体を直列化した文字列を投げると一致しない。識別子は語へ割る（`insurance_certificate` → `insurance certificate`）。所見が無ければクエリは空＝想起なしが正しい | `recalled_review_points`（過去 ID・照会観点） |
| Stage 3（LLM 当てはめ） | LLM が L/C 条項・特約と多言語書類（invoice/packing list/B-L）を UCP600 / ISBP に当てはめ、**意味的照合のみ**を行い矛盾候補・要修正・要照会論点を抽出。**書類有無・数値一致・呈示期限は Stage1 の結果を事実として受け取り再判定しない**（プロンプトで Stage1 facts を「確定事実」として渡し、LLM 出力の該当領域は Stage4 で Stage1 と矛盾しないか検証）。事実と推論を分離 | プリフライトブリーフのドラフト |
| Stage 4（claim→source 検証） | 全論点に L/C 条項 / 書類リンクを要求。claim→source 対応・テナント境界・禁止フィールドを検査。**Stage3 の矛盾候補が Stage1 の決定論結果と食い違う場合は Stage1 を優先し `requires_human_review`** | `evidence_map` / `requires_human_review` |

### 2-4. Data Flow

```
START → initialize → pre_process → main (LCPreflightGraphNode → domain workflow) → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

内側 workflow: `Stage1 (deterministic) → Stage2 (KB recall) → Stage3 (LLM apply) → Stage4 (claim→source verify)`

### 2-5. State Definition（flat TypedDict）

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `preflight` / `feedback` | Yes |
| `case_ref` | `str` | 案件参照 ID（生の取引情報は保持しない） | Yes |
| `tenant_id` | `str` | KB パーティションキー（顧客区画・fail-closed） | Yes |
| `docs_present` | `dict` | Stage1 必須書類有無 | preflight |
| `field_diff` | `dict` | Stage1 数値突合差分 | preflight |
| `deadline_status` | `str` | Stage1 呈示期限判定 | preflight |
| `recalled_review_points` | `list` | Stage2 過去差戻し照合観点（source-linked） | preflight |
| `preflight_brief` | `dict` | facts / inconsistency_candidates / bank_inquiry_points / correction_suggestions | preflight |
| `evidence_map` | `dict` | claim→source 対応マップ（L/C 条項・書類リンク） | preflight |
| `requires_human_review` | `bool` | 根拠不足・低信頼時の人間確認フラグ | preflight |
| `status` | `str` | AgentStatus 値（`success` / `error` / `retry`） | Yes |

**State Constraints (mandatory):**
- Flat TypedDict only（primitives + JSON-serializable types）
- No JWT, API keys, credentials in State（checkpoint DB leakage）
- InvocationContext via `config["configurable"]` only（not in State）
- No Pydantic models, dataclass, arbitrary Python objects（msgpack incompatible）
- **生の L/C 全文・船積書類全文・取引情報は State / checkpoint に載せない**（案件参照 ID・照合結果メタのみ）

### 2-6. 出力契約（モード別 envelope・S-3 で構造的に強制）

出力はモードごとに異なる envelope を返す。post_process が `mode` に応じて確定形へ整形する。

**(a) `preflight` モード — 6 フィールド固定**（5 業務フィールド + review 状態。内部 state の `preflight_brief` + `evidence_map` を整形）:

| フィールド | 内容 | S-3 要件 |
|---|---|---|
| `facts` | Stage1 の決定論的事実（書類有無・数値・期限） | evidence_map に source 必須 |
| `inconsistency_candidates` | 矛盾候補（断定でなく候補） | 各候補に L/C 条項/書類 link 必須・本文断定 lexicon 走査 |
| `bank_inquiry_points` | 要銀行照会論点 | 同上 |
| `correction_suggestions` | 修正提案 | 同上・「銀行は受理すべき」等の断定を降格 |
| `evidence_map` | 上記各主張 → source（L/C 条項 ID・書類リンク ID）の対応マップ | claim→source 実在検証（evidence-gate） |
| `requires_human_review` | `{flag: bool, reasons: [str]}`（1 フィールド・ネスト dict）。①S-3 が断定 lexicon 検知・根拠欠落・低信頼を検出したとき ②**Stage1 が問題を検出したとき**（`missing_required_document:{doc}` / `field_outside_tolerance:{field}` / `presentation_deadline:{late\|at_risk}`）に flag=true・reasons に理由 | S3-2/S3-5 の返し先 ＋ Stage1 所見 |

> **決定論所見の載せ方（実装で確定）**: `inconsistency_candidates` の決定論項目
> （`missing_doc` / `field_diff` / `deadline`）は **LLM の列挙に依存させず、Stage4 が Stage1 facts から
> 機械的に補完する**（`_ensure_deterministic_candidates`）。Stage3 プロンプトは意味照合（`semantic`）へ
> 誘導するため、実 LLM は決定論 claim をまず返さない。補完しないと欠落書類・許容差超過が `facts` にしか
> 現れず、**LLM 失敗時の縮退経路の方が候補リストが厚くなる**という逆転が起きる。本文は
> `_canonical_deterministic` が生成する server 由来の文字列のみを使う（事実文を LLM に書かせない）。
>
> **Stage2 想起の載せ方（実装で確定）**: 想起した過去差戻し観点は Stage3 プロンプトに渡すが、
> **LLM が引用しなくても Stage4 が `bank_inquiry_points` へ機械的に載せる**
> （`_ensure_recalled_review_points`）。実 LLM は L/C 特約と書類の照合に集中し、渡した
> `recalled_review_points` を引用しないことが実測で確認された。載せないと蓄積した知識が
> 担当者に届かず、Stage2 を直した意味が出力に現れない。本文は **KB に保存された review point
> そのもの**を使う（LLM に書かせない）。LLM が既に引用した source_id は重複させない。
>
> 🔴 **KB 自由文の関門（`_safe_kb_text`）— 書き込み時に適用する**: framework の S-3 出力ゲートは
> **result のトップレベル文字列しか走査しない**（インストール済み wheel の
> `FunctionNode._security_gate_output` を実測。`preflight_brief` / `formatted_output` のような
> ネスト dict は素通りする）。したがって `/ingest` で入った review point に credential や連絡先が
> 混ざっていると、そのまま担当者の envelope に出る。**二重防御**で塞ぐ:
> 🔴 **主たる適用点は書き込み（`_write_ingest_row`）**。KB に生値を蓄積させない — 読み出し時に毎回
> マスクする形では原文が store に残り、DB ダンプ・checkpoint・見落とした読み出し経路 1 つで
> **過去に取り込んだ全行が露出する**。確立した形も書き込み時に model 可視値を 1 度だけ導出する
> （Professional Services Engagement Acceptance Evidence Agent の `decision_rationale_safe` / 部品 `accumulation-ledger` の 3 値分離）。
> **本テンプレは `_raw` / `_audit` を持つ監査 DB 層を持たない**ため、KB に載るのは `_safe` 相当のみで
> 原文は保持しない（credential 行は ingest で reject するので原文自体を受け取らない）。
> 具体的には:
> ①`/ingest` 時に `detect_credentials` で credential を含む行を reject（理由に値を echo しない）
> ②同じく `/ingest` 時に **KB へ書く `summary` / `snippet` を PII マスクしてから upsert する**
> ③出力に載せる直前にも再検査する — この関門が無かった時期に入った移行前データを守るための
> 多層防御であって、これ単独に頼らない。検出は `shared.security` の
> `detect_credentials` / `detect_pii` を使う（framework が備える検出器を自前正規表現で再実装しない）。
> `name` 判定は Title Case 2 連続に当たるため**マスク対象から外す**（"Commercial Invoice" のような
> 業務語を壊し、誰も守らない）。この関門は縮退経路（`_stage3_fallback`）にも同じく適用する
> — そちらはベースラインから同じ露出があった。
>
> **多層防御の配置（レビュー round-2 High 対応）**: 関門は **KB 境界（Stage2 出口）が主**。
> 表示直前だけを見る形では、**LLM が recall の本文を自分の所見にコピーして同じ source を引用した
> 場合に素通りする**（引用済み判定でスキップされる。実測で `sk-...` が envelope に到達）。
> Stage2 の出口で落とせば、そのレコードの ID が `allowed_source_ids` から消えるため、
> 引用を捏造しても Stage4 の evidence-gate が弾く。加えて **Stage4 で最終候補の本文を再検査**する
> — LLM が KB 由来でない credential を自分で書いた場合はこの層だけが防ぐ。
> **落としたことは隠さない**: `kb_recall_suppressed` / `candidate_suppressed` の監査イベント（件数のみ・
> 値は載せない）と `requires_human_review.reasons` に `*_suppressed:credential` を積む。
> 過去観点が静かに消えると担当者は「そういう事例が無かった」と読む。
>
> **`bearer_token` は post-filter する**: `detect_credentials` の docstring が明示するとおり
> このクラスは十分長い `Bearer <token>` 全般に当たる。貿易金融では bearer bill of lading が
> 業務語で、実測でも `Bearer` + 船荷証券番号（`BILL` + 16 桁）を含む一文が誤検出された
> （検出器自身が走査する文書なので、ここでは一致する文字列そのものは書かない）。
> **トレードオフは明示**: review point に貼られた本物の HTTP bearer token はこのクラスでは
> 捕まらない。API キー / JWT / AWS キー / 接続文字列は従来どおり block する。
>
> **`evidence_map` の `claim_id`**: 最終候補リストに対して `"{section}:{index}"` で採番する。
> Stage3 が raw draft から作った位置ベースの ID は、Stage4 の reconcile（矛盾候補の除去）と
> 決定論補完で位置がずれるため、そのままでは別の claim を指す。

> 計 6 フィールド = 5 業務フィールド（`facts` / `inconsistency_candidates` / `bank_inquiry_points` / `correction_suggestions` / `evidence_map`）+ `requires_human_review`（review 状態）。`review_reasons` は独立フィールドでなく `requires_human_review.reasons`。

> **S3-2 の redaction/抑制**: 本文断定 lexicon を検知したテキストは、**その断定 span を除去（redact）したうえで
> `requires_human_review.flag=true` にし `requires_human_review.reasons` に理由を積む**。抑制後もなお安全でない場合は
> 当該フィールドを空にして人間確認へ回す（unsafe text をそのまま返さない）。

**(b) `ingest` モード — `ingest_summary` envelope**（`ingest-row-pipeline` の契約）:

| フィールド | 内容 | S-3 要件 |
|---|---|---|
| `written_ids` | 正常に upsert された行の ID リスト（tenant 区画付き） | 生の取引情報を含めない（ID とカウントのみ） |
| `rejected` | `[{row_ref, reason}]` 品質ゲート/サニタイズで弾いた行 | reason に生データを露出しない |
| `ingest_stats` | `{accepted_count, rejected_count}` | — |

**(c) `feedback` モード — feedback result envelope**:

| フィールド | 内容 | S-3 要件 |
|---|---|---|
| `case_ref` | 対象案件参照 ID | — |
| `recorded_events` | 追記した `feedback_audit_events` の ID リスト | 承認済み・source-linked 照合観点のみ KB 還流 |
| `kb_updated` | KB 還流有無（bool） | 還流は明示承認（承認フラグ）付きのみ |

> 3 モードとも `presentation_approved` / `payment_verdict` / `discrepancy_final` / `authenticity_verdict`
> 相当フィールドを持たない（下記構造遮断はモード共通）。

**構造遮断（🔴 安全境界）**:
- **① フィールドレベル**: `presentation_approved` / `payment_verdict` / `discrepancy_final` / `authenticity_verdict`
  に相当するフィールドは envelope に持たない。
- **② 本文レベル（レビュー High #1 対応）**: フィールド排除だけでは `correction_suggestions` 等の**自由記述テキスト内**に
  「銀行は受理/提示/支払すべき」「書類は真正である」といった断定が混入しうる。S-3 で **断定 lexicon（提示可否・
  支払可否・真正性の確定表現）を全 user-visible テキストに対して走査し、検知したら `requires_human_review` へ降格
  または fail-closed** する（安全な本文を残せる場合は降格し、残せない場合は fail-closed とする）。
- **位置づけ**: この lexicon は**製品安全の緩和策**であって銀行審査基準の遵守を確立するものではない。基準の最終検証は
  顧客法務・銀行の審査に残る（正典 §2-4）。lexicon は顧客/業務側がオーナーの managed item とする。

## 3. Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, permissions, credential handle) — `config["configurable"]` 経由
- [x] SecurityViolationError — claim→source 不一致・禁止フィールド検知時に raise
- [x] S-2: `_extra_security_gate_input()` — 取引情報最小化・テナント権限検証・入力サイズ制限（pre_process ノードは FunctionNode 派生のためフック拡張）
- [x] S-3: `_extra_security_gate_output()` — **`LCPreflightGraphNode`（GraphNode）境界に置く**。(a) 根拠リンク実在性検証・claim→source 対応（`evidence-gate` 部品）、(b) 禁止フィールド（payment_verdict 等）検知、(c) **本文断定 lexicon 走査**（提示可否・支払可否・真正性の確定表現を全 user-visible テキストに走査 → `requires_human_review` 降格 or fail-closed・§2-6 ②）。filtering + provenance 検証の複合
- [x] S-4: `emit_trace_event()` — 各 `execute()` 内で最低 1 回のドメインイベント（例: `lc_preflight_started` / `kb_recall_done` / `claim_source_verified`）

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass（pre_process / post_process）→ framework `@final` gate 自動実行、`_extra_*` フックで拡張
> - `GraphNode`（main = `LCPreflightGraphNode`）→ S-3 provenance 検証を `_extra_security_gate_output()` に置く（サブグラフ境界が照合の正本が届く唯一の場所）

### Composition Pattern

- **Pattern**: GraphNode (subgraph)
- **Composition target**: `domain_workflow_graph.py` の `DomainWorkflowGraph`（内側 **`BaseGraph`** 直継承サブグラフ・Stage1-4 custom topology）
- **Error propagation strategy**: propagate（内側エラーは `SubgraphError` として親へ伝播。main は上流 `status="error"` を無条件パススルー）

## 3-bis. SDK 契約の実機確認（agenticstar-agentcore 1.0.0・2026-07-21 introspection）

design が依拠する framework シンボルを venv（wheel 1.0.0）で実在確認済み（指示書手順8.5・手戻り防止）:

| 確認項目 | 実機結果 | 設計への反映 |
|---|---|---|
| `InvocationContext` 実フィールド | `correlation_id / session_id / thread_id / parent_trace_id / caller_id / caller_trust_level / secrets / hitl_allowed` | ⚠️ **`tenant_id` / `org_id` は存在しない** → `tenant_id` は **State フィールド**として持ち、値は `entry-adapter` が検証済み `request.state` から注入（`ctx.tenant_id` 参照禁止・088 kb_service 方式） |
| `AgentBaseGraph.get_output()` | `state["formatted_output"] or state["result"]` を `output` に拾う | post_process は `formatted_output` を返す（§2-2） |
| `GraphNode` 抽象メソッド | `get_subgraph` / `extract_input` / `merge_output`（`_build_subgraph` ではない） | `LCPreflightGraphNode` はこの3メソッドを実装。cat2-graph-skeleton 部品の `_build_subgraph()` は部品内部ヘルパで `get_subgraph()` から呼ばれる |
| `GraphNode.error_strategy` / `propagate_hitl` | 両方存在（クラス変数） | propagate / False を設定（§6） |
| `FunctionNode._extra_security_gate_input/output` | 両方存在（GA `@final` + フック契約） | pre/post は `_extra_*` フックで拡張・`_security_gate_*` 直定義しない（§3） |

## 4. Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## 5. LLM 注入シーム

LLM は具体モデル ID / クライアントをテンプレに書かない。実体の注入シームは
`config["services"]["llm"]` であり、top-level の `config["llm"]` は
`temperature` / `max_tokens` の tuning mapping としてのみ扱う。Marketplace adapter は
credential-free な遅延 wrapper を outer graph の構築前に services へ差し込み、接続値を
各 `complete()` 呼び出し時の bound secrets から解決する。未配線時だけ Stage3 は
deterministic fallback に移る。

Marketplace はデプロイ単位で `STG_DEFAULT_TENANT_ID` の1テナントに固定する。任意の
`VERIFIED_EXTERNAL` caller に共有 KB の ingest / feedback を開かないため、入力の mode は
常に `preflight` へ固定し、seed 済み `_ReadOnlyKBBackend` は `register_case` を no-op、その他の
永続書込を拒否する。これは HTTP adapter の request-scoped write 経路とは別の read-only 境界である。

## 5-bis. 再利用部品（共通部品ライブラリ `parts/`）

> ⚠️ **開発者・レビュアー向け注意（重要）**: 本節で指す「部品（parts）」は、**本チームで独自に作成・運用している
> 社内共通部品ライブラリ `wamuroCommonparts`（ローカル `1000AG/parts/`）** を指す。AgentCore SDK／framework
> の公式提供物ではない。蓄積型・Cat2 テンプレで繰り返し出た実バグ・レビュー指摘を**写経方式**（`import` 禁止・
> 正本を repo 内へ copy-in）で再利用するための独自資産である。`tools/apply_part.py <part-id> <target-repo>` で
> copy-in し、使い方の正本は各部品の `USAGE.md`、テスト項目は `TESTING.md` に従う。**この部品を使う実装（MR-B）は、
> 部品側の fail-closed 原則・scope キー契約を崩さないこと。**

本テンプレは蓄積型 Cat2 の典型構造（3 モード・tenant 区画・KB 還流・provenance 検証）のため、以下の部品を採用する。
正本 = `1000AG/parts/`（GitLab: `aiagentasset/wamurocommonparts`）・台帳 `parts/registry/parts_index.yaml`。
実装フェーズで `python3 parts/tools/apply_part.py <part-id> <repo>` を実行して copy-in する手順。

**適用部品一覧（part-id / version / 本 AG での用途 / adaptation_points の埋め方）:**

| part-id | ver | 本 AG での用途 | 対応設計 | adaptation_points（このテンプレで埋める値） | 採否 |
|---|---|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.4 | main の `LCPreflightGraphNode`（GraphNode → 内側 workflow 委譲）の骨格 | §2-3 | `mode_specs`= ingest/preflight/feedback。request-scoped 内側 graph を lock 付き lazy cache。`error_strategy`=propagate。`propagate_hitl`=False | ✅ 必須 |
| `entry-adapter` | 0.4.0 | `/ingest` `/preflight` `/feedback` 3 ルートの入口認証。payload の tenant 詐称を弾く | §2-1 / server.py | `SCOPE_KEYS=("tenant_id",)`。副作用に従い preflight / ingest を `WRITE_INGEST`、feedback を `WRITE_FEEDBACK` へ対応。write は middleware identity 必須 | ✅ 必須 |
| `evidence-gate` | 0.2.0 | Stage4 の claim→source 検証（LLM 引用 ID が Stage1/2 実レコード ID 集合に全存在するか機械検証・malformed claim/捏造 ID を fail-closed） | §2-3 Stage4 / §3 S-3 | `ProvenanceContract`= L/C 条項 ID・書類リンク ID キー。`free_text_paths`= 矛盾候補本文パス。`claim_text_keys`= evidence_map の claim 本文キー | ✅ 必須 |
| `scoped-retrieval` | 0.3.0 | Stage2 過去差戻し KB 検索を tenant_id 区画で fail-closed | §2-3 Stage2 | `scope_keys=("tenant_id",)`。backend は SQL/vector 側で絞り、保存行由来 tenant_id を hit に返す。`normalize_query`= 書込側と共有 | ✅ 採用 |
| `payload-store` | 0.4.0 | 生の L/C 全文・船積書類を checkpoint 外に短命保持（TTL・single-use replay 防止） | §2-5 State 制約 | `scope_keys=("tenant_id",)`、`put/resolve/delete(scope=..., session_id=...)`。digit-free ref。unscoped get() 不採用 | ✅ 採用 |
| `ingest-row-pipeline` | 0.3.1 | `/ingest` の証憑ルール・過去差戻し行データ正規化 + checkpoint 隔離 + `ingest_summary` 契約 | §2-1 ingest | AuthContext scope/session を認証後の request-scoped node constructor に注入。scalar 全型を隔離。書込= tenant 区画付き upsert | ✅ 採用 |
| `llm-draft-stage` | 0.2.0 | Stage3 LLM 当てはめ（`config["llm"]` 注入・モデル ID を書かない） | §2-3 Stage3 | LLM 未設定だけ deterministic fallback。provider/parse 障害は stable error_code で fail-closed。prompt は検証定数から response contract を生成 | ✅ 採用 |
| `output-envelope` | 0.1.2 | post_process の確定 6 フィールド envelope 整形（AgentCore envelope 機構・§2-6 の確定形へ） | §2-6 出力契約 | `mode`/`gate_kind` 宣言。`facts` / 3候補欄 / `evidence_map` / `requires_human_review`。degradation folding | ✅ 採用 |
| `ingest-file-sanitizer` | 0.1.0 | CSV/Excel 由来ファイルの S-2 内容 sniff サニタイズ（マジックバイト判定・未知形式 reject） | §2-1 ingest | マジックバイト allowlist= CSV/Excel。索引テキスト品質ゲート | ⚠️ 要判断（下記） |

> **部品の骨格改変禁止**: copy-in されたファイルの `# PART:` マーカーは消さない。各部品の `USAGE.md` の
> adaptation_points **だけ**をドメインに合わせて埋め、fail-closed 分岐・allowlist・キャッシュ等の骨格は
> 書き換えない（必要なら「要ユーザー判断」へ）。部品同梱テスト `tests/parts/{part-id}/` は削除せず緑を維持。
> テスト計画（`docs/03_test_spec.md`）は `parts/TESTING.md` 層2（適用部品の接合部）＋層3（横断）を引き写す。

**scope キー（tenant_id）の扱い（部品共通の最重要契約）**: 本 AG は §6 の判断どおり **tenant row-level partition を採用する**
（顧客固有の取引パターン・過去差戻しは「顧客内で混ぜてはいけない区画」が実在する蓄積型 moat）。したがって
`scoped-retrieval` / `payload-store` / `ingest-row-pipeline` の scope キーは **リクエスト由来（検証済み `request.state` から）で
per-request に解決し、欠落時は fail-closed**（`status=error`）で配線する。**config 固定値 + `"default"` 無言フォールバックは
禁止**（parts README のアンチパターン）。

**`ingest-file-sanitizer` 要否（MR-B 冒頭で確定）**: `/ingest` が生のファイル（CSV/Excel バイト列）を受けるなら必須
（拡張子でなく内容 sniff で形式判定）。行データが正規化済み JSON で入るなら不要。decision §3 は「CSV/Excel 由来の
行データ」とだけ述べ入力形態を未確定にしているため、MR-B の実装ブリーフ冒頭で入力契約を確定して採否を決める。

## 6. Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定 3 モードパイプライン。自律 think-act ループなし |
| Composition pattern | Standalone | GraphNode (subgraph) | **GraphNode** | Stage1-4 の domain workflow を内側サブグラフに閉じ、S-3 provenance を境界で検証 |
| HITL | 有効 | 無効 | **無効（memory_enabled: false）** | 対話状態は不要。L/C 担当のレビューは feedback モードで DB へ。checkpoint HITL 不使用（PB-7 は Auto-waived — non-HITL） |
| tenant scope | deployment-level | row-level partition key | **row-level partition key** | 顧客内に混ぜてはいけない区画（自社取引パターン）が実在。KB は tenant_id で fail-closed 区画化 |
| Stage2 の検索クエリ | Stage1 facts を構造体のまま直列化 | Stage1 の「問題あり」所見を検索語に展開 | **検索語に展開**（2026-08-13） | 構造体直列化では `["insurance_certificate"]}` のようなトークンになり、自由文 `summary` に決して一致しない。**実 ingest → preflight の end-to-end で想起 0 件を実測**して確定（対照実験: 同形の JSON 断片を summary に持つレコードは一致した）。蓄積型 moat が write-only になっていた |
| 決定論所見の候補化 | LLM に列挙させる（プロンプト強化） | Stage4 が facts から機械補完 | **機械補完**（2026-08-13） | 事実の列挙を LLM の気まぐれに委ねない。プロンプト強化では出し忘れが検出できず、`facts` を読める運用者にしか届かない。本文は server 由来の canonical 文字列のみ |
| Stage2 想起の出力反映 | プロンプトに渡すだけ（LLM の引用に委ねる） | 引用が無ければ Stage4 が機械的に載せる | **機械的に載せる**（2026-08-13） | 修正後の実 LLM 1 回転で、想起は成功（`degradation_reason: None`）したのに **KB 由来の引用がゼロ**だった。渡すだけでは蓄積が担当者に届かない |
| KB 自由文の出力露出 | framework S-3 に任せる | ingest 拒否 ＋ 出力直前の再検査 | **二重防御**（2026-08-13・レビュー High） | 実測で framework S-3 はトップレベル文字列のみ走査＝ネストした brief は素通り。ingest だけでは移行前データを守れず、出力側だけでは KB を汚したままになる |
| KB 自由文のマスク時点 | 読み出し時に毎回マスク | **書き込み時にマスクして KB に生値を残さない** | **書き込み時**（2026-08-13・ユーザー指摘） | 読み出し時マスクでは KB が汚染されたまま蓄積し、DB ダンプ／checkpoint／見落とした読み出し経路で全行が露出する。確立した形（Professional Services Engagement Acceptance Evidence Agent / `accumulation-ledger` の 3 値分離）も書き込み時に導出する。本テンプレは監査 DB 層を持たないため `_raw` は保持しない |
| KB 自由文の関門の位置 | 表示直前（`_ensure_recalled_review_points`）だけ | KB 境界（Stage2 出口）を主とし Stage4 で再検査 | **KB 境界＋Stage4**（2026-08-13・レビュー round-2 High） | 表示直前だけでは、LLM が本文をコピーして同じ source を引用した場合に「引用済み」判定でスキップされ素通りする（実測で envelope に到達）。境界で落とせば ID ごと消えるので引用の捏造も効かない |
| 抑止の可視性 | 静かに落とす | 監査イベント＋`requires_human_review` | **可視化**（2026-08-13） | 過去観点が黙って減ると担当者は「事例が無かった」と読む。値は載せず件数と理由コードのみ |
| `bearer_token` の扱い | 他クラスと同じく block | post-filter して通す | **post-filter**（2026-08-13） | 検出器自身が「十分長い Bearer 全般に当たるので caller が post-filter せよ」と docstring で指示。貿易金融は bearer B/L が業務語で、block すると正当な過去事例が KB に入らない。**本物の HTTP bearer token は捕まらないというトレードオフを受容** |
| 検索語の filler | 識別子をそのまま語割り | stopword を落とす（長さでは切らない） | **stopword のみ**（2026-08-13・レビュー Medium round-1/2） | `at_risk`→"at"、`certificate_of_origin`→"of" が全保存行に部分一致し、順位付けの無い backend では正しい recall が top_k から押し出される。ただし**長さで切ると `bl` / `awb` / `coo` / `fcr` という実在の貿易書類識別子が消え**、B/L だけが欠落書類の案件でクエリが空になる（レビュー round-2） |
| 決定論候補の `cited_source_ids` | Stage1 fact を暗黙の source とし空リスト（現状維持） | fact ごとに server 採番の source ID を作って引用させる | **現状維持・別課題**（2026-08-13） | 空 source は `_reconcile_candidates` が確立した既存設計（"the Stage1 fact is its source"）。evidence-gate が空リストを検証ゼロ回で通す点はレビュー Medium として妥当だが、Stage1 の fact source 採番は設計変更の規模。`requires_human_review` が立つので実害は緩和されている。**未対応の既知事項として記録** |
| `requires_human_review` の発火条件 | S-3 検知のみ（従来） | S-3 検知 ＋ Stage1 所見 | **両方**（2026-08-13） | 必須書類欠落＋許容差超過という差戻し確実な提示で `flag: false` が返っていた。安全側検知と業務側所見は別軸だが、担当者が見るフラグは 1 つ |

## 7. DB Schema とデータ統治

4 テーブルを tenant 区画キー付きで保持する。**各フィールドを「監査用原記録（audit-only・モデル不可視）」と
「retrieval-safe projection（KB 検索・LLM 可視）」に分類**する。
KB へのベクトル化・LLM プロンプト投入は safe 列のみ。生の取引情報・書類全文は audit 側に閉じる。

### 7-1. `shipment_packets`（案件ヘッダ・監査正本）

| カラム | 型 | 分類 | 備考 |
|---|---|---|---|
| `case_ref` | str (PK) | safe | 案件参照 ID（server 生成・§8） |
| `tenant_id` | str | safe | 区画キー（fail-closed） |
| `created_at` / `updated_at` | ts | safe | — |
| `window_status` | enum | safe | `open`/`preflighted`/`presented`/`closed` |
| `raw_payload_ref` | str | **audit-only** | payload-store の参照 ID（生 L/C・書類は out-of-state・§8） |

### 7-2. `lc_terms_registry`（L/C 条項・証憑ルール）

| カラム | 型 | 分類 | 備考 |
|---|---|---|---|
| `term_id` | str (PK) | safe | 条項/ルール ID |
| `tenant_id` | str | safe | 区画キー |
| `doc_requirement` | json | safe | 必要書類・数量許容差・取引条件（Stage1 参照） |
| `term_pattern_summary` | text | safe（projection） | L/C 特約の**要約**（原文全文は audit） |
| `raw_lc_text_ref` | str | **audit-only** | 原文は payload-store 参照 |

### 7-3. `bank_return_history`（過去銀行差戻し・照会）

| カラム | 型 | 分類 | 備考 |
|---|---|---|---|
| `return_id` | str (PK) | safe | 差戻し ID |
| `tenant_id` | str | safe | 区画キー |
| `review_point` | text | safe（projection） | 照合観点（source-linked・Stage2 想起対象） |
| `inquiry_reason` | text | safe（projection） | 照会理由（結論ラベルは含めない） |
| `quality_flag` | enum | safe | ingest 品質ゲートのスコア（低品質は検索重み減） |
| `raw_return_notice_ref` | str | **audit-only** | 原通知は payload-store 参照 |

> **⚠️ 支払可否/提示可否/真正性の確定ラベルは safe 列に置かない**（Stage2 が結論を継承しないため。§2-3 Stage2）。

### 7-4. `feedback_audit_events`（feedback 監査・追記専用）

| カラム | 型 | 分類 | 備考 |
|---|---|---|---|
| `event_id` | str (PK) | safe | — |
| `case_ref` / `tenant_id` | str | safe | — |
| `event_type` | enum | safe | `point_confirmed`/`point_edited`/`kb_promoted` |
| `approved_by` | str | audit-only | 承認者（L/C 担当）・KB 還流は本フラグ必須 |
| `payload` | json | **audit-only** | 編集内容（生データ・KB へは promote されない限り不可視） |

## 8. 生書類の取り扱い契約

**原則**: 生の L/C 全文・船積書類は **State / checkpoint に一切載せない**（§2-5）。`payload-store` で out-of-state 管理する。

### 8-1. ルート別 payload 形状・入力形態・生書類の ingress

**生書類（L/C 本文・invoice/packing list/B-L）は、クライアント（L/C 担当のフロント）が `/preflight` の
HTTP リクエストボディに JSON 化した抽出済みテキスト構造として送る（v1 は生 PDF/画像バイトを受けない）。**
server（entry-adapter 認証後）が受信直後にその生テキストを payload-store へ put し、以降グラフには参照 ID のみ渡す。
＝「生書類はクライアント→server の HTTP body に一度だけ現れ、State には入らず即 out-of-state 化される」。

| ルート | クライアントが送る payload | bytes/file 受入 | server の処理 | case_ref 生成 |
|---|---|---|---|---|
| `/ingest` | 正規化済み JSON 行（証憑ルール・過去差戻し） | ❌（v1 は JSON のみ・PDF 拒否 = 正典§2-4 既定） | tenant 区画付き upsert | 不要（登録系） |
| `/preflight` | **case メタ + 抽出済み L/C テキスト + 書類抽出テキスト（JSON）** | ❌（生 PDF/画像は受けない・抽出はクライアント責務） | 受信直後に生テキストを `payload-store.put` → 参照 ID 化。case_ref 採番。グラフには case_ref + payload_ref + 数値メタのみ渡す | **server が生成**（新規時） |
| `/feedback` | `case_ref` + 承認済み論点（生書類を再送しない） | ❌ | audit event 追記・承認済み観点のみ KB promote | 既存 case_ref 指定 |

> **v1 の入力形態確定**: 生 PDF/画像のパースは AG のスコープ外（クライアントが抽出テキストを用意）。
> したがって `ingest-file-sanitizer` は **v1 不採用**（§5-bis の「要判断」はこれで確定）。将来ファイル受入時に採用。
> 抽出テキストは JSON 文字列フィールドとして入り、S-2 で取引情報最小化・サイズ制限を掛ける。

### 8-2. payload-store put/get/delete シーケンス（single-use・TTL）

```
/preflight 受信（entry-adapter が認証・tenant を verified state に確定）
  │
  ├─ server: payload-store.put(raw_lc + raw_docs, bind=(tenant_id, session_id), ttl=preflight完了まで)
  │            → payload_ref を得る。case_ref を採番し shipment_packets に raw_payload_ref=payload_ref を記録
  │
  ├─ graph invoke（case ペイロード = case_ref + plan_draft_ref + メタ。生本文は渡さない）
  │    ※ envelope 上のキー名は mode 別: preflight=`plan_draft_ref` / ingest=`ingest_payload_ref` /
  │      feedback=`feedback_payload_ref`（`server.py` `_REF_KEYS`）
  │    └─ 内側 Stage3: payload-store.get(payload_ref, requester_scope=(tenant_id, session_id))
  │                     → scope 不一致は fail-closed（unscoped get 不採用）。single-use なので取得後は消費印
  │
  └─ post_process 完了 → server: payload-store.delete(payload_ref)（完了時必ず削除・TTL は保険）
```

- **Stage1/3 に届く safe projection**: Stage1 は数値・書類有無・期限の**抽出済みフィールド**のみ（生画像/PDF でなく構造化値）。Stage3 は L/C 特約要約 + 書類の抽出テキスト（payload-store 経由・scope 検証済み）。
- **binding**: payload_ref は `(tenant_id, session_id)` に束縛。別 tenant/session からの get は fail-closed。
- **single-use**: get は 1 回のみ有効（replay 防止・`payload-store` 部品契約）。

## 9. Stage1 決定論ルール・S-3 受入マトリクス

### 9-1. Stage1 マッチング/期限ルール（LLM 不使用・immutable facts）

| チェック | ルール | 出力 |
|---|---|---|
| 必須書類有無 | `lc_terms_registry.doc_requirement` の required 書類が case に全て存在するか | `docs_present: {doc_type: bool}` |
| 数値突合 | invoice 金額・数量が L/C の金額・数量許容差（tolerance）内か（完全一致でなく tolerance 判定） | `field_diff: {field: {lc, doc, within_tolerance}}` |
| 呈示期限 | **on-board/船積日**（B/L の on-board date。無ければ B/L 発行日で代替し `date_basis` を注記）を起点。`applicable_period` = L/C に presentation period 指定があればその日数、無ければ 21日（UCP600 既定）。`as_of_date`（リクエスト指定・省略時 server 受信日）で残日数評価: `days_remaining` = min(L/C expiry, on-board + applicable_period) − as_of_date。残 < 0 → `late` / 残 ≤ 閾値（既定 3日・config）→ `at_risk` / それ以外 → `on_time` | `deadline_status: on_time/late/at_risk` + `date_basis` + `applicable_period` + `days_remaining` |

### 9-2. S-3 受入マトリクス（テスト可能な形・decision §12 #4）

| # | 受入条件 | 拒否/降格トリガー | テスト |
|---|---|---|---|
| S3-1 | 禁止フィールド不在 | envelope に payment/presentation/authenticity 相当キーが出たら fail-closed | PB: 出力スキーマ検査 |
| S3-2 | 本文断定 lexicon 不在 | user-visible テキストに提示可否/支払可否/真正性の断定表現 → `requires_human_review` 降格 | PB: lexicon 走査（正常な書き方も否定文も両方） |
| S3-3 | claim→source 実在 | 論点の引用 ID が Stage1/2 実レコード集合に無い（捏造）→ SecurityViolationError | PB: 捏造 ID 注入で raise |
| S3-4 | tenant 越境なし | 別 tenant の source ID を参照 → fail-closed | PB: 別区画 ID で fail-closed |
| S3-5 | 根拠必須 | link 無しの論点 → `requires_human_review` | PB: link 欠落論点 |
| S3-6 | 候補 dict は `claim_type: str`（宣言 enum）/ `target: str` / `description: str` / `cited_source_ids: list[str]` の全フィールド必須・閉じた型付き集合 | LLM が宣言外キー、欠落、型違反、未宣言 enum を返したら `SecurityViolationError` で fail-closed。型付き allowlist 射影後に既存の事実照合・引用判定を行う | PB: ネスト dict / list 内 dict / 非 str / 未宣言 enum を Stage4 が拒否 |

> テスト計画（`docs/03_test_spec.md`）は `parts/TESTING.md` 層2（適用部品接合部）＋層3（横断）＋上記 S3-1〜5 を PB 化する。

## 10. 照合観点の承認フロー（decision §12 #5）と残る受入条件

### 10-1. 照合観点の受領・承認フロー（枠は Design 確定）

顧客承認の照合観点は、**AG が構造を定義し（Design 確定）、顧客固有の値だけを PoC/運用で埋める**:
- **受領**: 照合観点は `bank_return_history.review_point`（source-linked）として `/ingest` で構造化投入する（§7-3）。
- **承認**: `/feedback` で L/C 担当が `point_confirmed` / `point_edited` すると `feedback_audit_events` に承認者付きで記録され（§7-4）、`kb_promoted` フラグが立った観点だけが KB（retrieval-safe projection）へ還流し Stage2 想起対象になる。
- **未承認の観点は KB に載らない**（承認フラグ必須・§2-6(c)）。＝「顧客が承認した照合観点だけが再利用される」統治が schema・フローで担保される。

### 10-2. 残る受入条件（人間・PoC 段階）

- 顧客データの export 可否、IAM、統治契約は PoC 開始前に顧客窓口・法務が確認する。
- 証憑ルールの実データや IAM 詳細などの顧客固有値は PoC で設定する。
