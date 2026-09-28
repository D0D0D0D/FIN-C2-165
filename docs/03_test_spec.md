# Test Specification — FIN-C2-165 Trade Finance LC Document Preflight Agent

> **カバレッジ2層構造**（`parts/TESTING.md` 準拠）: 汎用防御（checkpoint scrubber・provenance ID 照合・
> LLM parse fail-closed・scoped retrieval fail-closed・payload-store scope/replay）は**部品同梱テスト（層1）**
> `tests/parts/<part-id>/` が担保。ドメイン接合部（3モード E2E・tenant 隔離・Stage1 決定論・S-3 断定 lexicon）は
> `tests/unit/` と `tests/integration/` が担保。
>
> 各項目のカバー元:
> - Stage1 決定論（数値 tolerance・呈示期限 on_time/at_risk/late・書類欠落） → `tests/unit/test_stage1_and_s3.py`
> - S-3 断定 lexicon（提示可否/支払可否/真正性の検知＋redaction＋降格）＋構造遮断（verdict field 排除） → `tests/unit/test_stage1_and_s3.py`
> - scope 越境 ingest / cross-tenant provenance / 捏造 ID / feedback 承認必須 → `tests/unit/test_adversarial_regressions.py`
> - 3モード E2E（ingest→preflight→feedback）＋ tenant 隔離 → `tests/integration/test_lc_preflight_workflow.py`
> - entry-adapter 認証・tenant scope（client header 詐称拒否・token-gated STG default） → `tests/integration/test_adapter_scope_auth.py`
> - prompt 応答契約（宣言だけから応答構築→Stage4 assembler/evidence/output 射影） → `tests/unit/test_draft_response_contract.py`
> - 実 SQLite backend の保存scope投影・server-side tenant filter → `tests/integration/test_scoped_retrieval_sqlite.py`
> - payload-store scope/replay fail-closed → `tests/proof_of_boundary/test_payload_store_scope.py`
> - checkpoint sentinel（生本文非直列化） → `tests/parts/ingest-row-pipeline/test_checkpoint_scrubber.py`
> - forged/hallucinated ID → `tests/parts/evidence-gate/test_evidence_gate.py`
> - LLM parse fail-closed → `tests/parts/llm-draft-stage/test_draft_node.py`
> - PB-6 invoke 実行順序（全 src/nodes ノード） → `tests/proof_of_boundary/test_pb_invoke_order.py`

## Layer 1 / Framework Compliance (TC)

| TC-ID | Test | Expected Result | Result |
|---|---|---|---|
| TC-01 | State は flat TypedDict（Pydantic/dataclass 無し） | 型チェック pass | PASS (`test_state_safety.py`) |
| TC-02 | SecurityViolationError 発火（捏造 ID / verdict field / cross-tenant） | エラー raise | PASS (`test_adversarial_regressions.py` / `test_stage1_and_s3.py`) |
| TC-03 | src/ に JWT/credential 無し | gate-credential-scan PASS | PASS (CI) |
| TC-04 | InvocationContext は configurable 経由のみ・直接構築なし | `from_state` のみ | PASS |
| TC-05 | emit_trace_event を各 execute で最低1回 | ドメインイベント発火 | PASS (`test_pb_invoke_order.py` が全ノード走査) |
| TC-06 | `_security_gate_input` 非バイパス（`_extra_*` フックのみ） | @final 直定義なし | PASS |
| TC-07 | `_security_gate_output` 非バイパス（S-3 は `_extra_*` フック） | @final 直定義なし | PASS |
| TC-08 | required_trust_level 宣言・強制（全ノード VERIFIED_EXTERNAL） | S-1 拒否動作 | PASS (`test_pb_invoke_order.py`) |

## Layer 2 / Proof-of-Boundary (PB)

| PB-ID | Boundary | Expected Result | Result |
|---|---|---|---|
| PB-1 | BaseNode → AuditLogger（emit_trace_event 発火） | 沈黙失敗なし | PASS |
| PB-2 | State serialization（primitives only） | Pydantic/dataclass 無し | PASS (`test_state_safety.py`) |
| PB-3 / R-1 / R-5 | L1 → 実 SQLite KB backend（保存行由来 scope を hit に返し、tenant を SQL WHERE で絞る）。**クエリは手書きせず `_recall_query_terms()`＝本番 Stage1 と同じ組み立てで作る** | 同scope 1 hit・別scope 0 hit・scope欠落 error | PASS (`test_scoped_retrieval_sqlite.py`) |
| PB-3 / R-1（end-to-end） | 実 HTTP `/ingest` → `/preflight` で蓄積ループが閉じる（本番経路のクエリで想起する） | ingest 3 行 → preflight が該当 review point を想起・無関係行は想起しない | PASS (`test_stage2_recall_end_to_end.py`) |
| PB-4 | Import isolation（Level 0 非 import） | AST scan 0 violations | PASS (`test_import_isolation.py`) |
| PB-5 | Checkpoint safety（生本文 payload-store 外部化） | 生本文 non-in-state | PASS (`test_payload_store_scope.py` / part `test_checkpoint_scrubber.py`) |
| PB-6 | Invoke 実行順序（S-1→node_start→S-2→execute→S-3→node_complete） | 順序検証 pass | PASS (`test_pb_invoke_order.py`) |
| PB-7 | HITL interrupt propagation | — | **Auto-waived: `hitl.enabled=false`（非 HITL）** |

## Layer 2 / Domain

| ID | Coverage | Result |
|---|---|---|
| D1 | Stage1 数値 tolerance（within/beyond） | PASS |
| D2 | Stage1 呈示期限（on_time / at_risk / late・on_board 起点・BL issue fallback・UCP600 21日既定） | PASS |
| D3 | Stage1 書類欠落検出 | PASS |
| D4 | Stage1 scope 欠落 fail-closed | PASS |
| S3-1 | 禁止 verdict field（payment/presentation/authenticity）構造遮断 | PASS |
| S3-2 | 断定 lexicon 検知（EN/JA・全半角 NFKC バイパス含む）→ redaction + `requires_human_review` 降格 | PASS（PASS/BLOCK 両方向） |
| S3-3 | claim→source 実在（捏造 ID → SecurityViolationError） | PASS (part + domain) |
| S3-4 | tenant 越境（別 tenant の source ID → fail-closed） | PASS |
| E-1 | 3モード E2E（ingest→preflight→feedback） | PASS |
| E-2 | tenant 隔離（別 tenant は KB 想起ゼロ） | PASS |
| F-1 | feedback KB 還流は kb_promoted 明示承認のみ | PASS |
| F-2 | KB promotion は verified caller_id を approver に使用（payload approved_by 詐称不可）・allowlist 外は `promotion_not_authorized` fail-closed / allowlist 内のみ promote 成功 | PASS |
| F-3 | `/preflight` case_ref は常に server 採番・client 提供 case_ref 無視 | PASS |
| I-7 / I-8 | 実 `PayloadStore` を直接 resolver 注入し、trusted scope/session を constructor bind。state 偽装で置換不可 | PASS (part + domain) |
| L-4 / L-5 | `llm=None` のみ success degradation。provider/parse 障害は安定 error_code、例外文非露出 | PASS (part) |
| V-1 / V-2 / V-3 | malformed evidence block、全自由文 path、数値 token 完全一致 | PASS (part + domain) |
| N-6 | `preflight` / `ingest` → `Operation.WRITE_INGEST`、`feedback` → `Operation.WRITE_FEEDBACK`（`/invoke` も実 mode で同じ写像）。token/STG write は403 | PASS (part + HTTP integration) |

## Layer 3 / Operational

| ID | Coverage | Result |
|---|---|---|
| X-1 | 本番 `config/agent.yaml` が memory/HITL を無効化 | PASS |
| X-2 | server entry-adapter 認証（client header 詐称拒否・token-gated STG default・anonymous 非救済） | PASS |
| X-3 | `/invoke` 単一エンドポイント契約（envelope パース→mode dispatch→同一 `_run` 経路・不正 envelope/未知 mode は 400 fail-closed・preflight case_ref は `/invoke` 経由でも server 採番） | PASS |
| X-4 | request-scoped graph/node 構築（認証後に trusted scope/session を bind、共有 node 後付け無し） | PASS |
| X-5 | Stage1 所見の候補化（LLM が semantic のみ返しても欠落書類・許容差超過が候補に出る・本文は server 由来・重複しない） | PASS (`test_deterministic_surfacing.py`) |
| X-6 | `requires_human_review` が Stage1 所見で flag=true（問題が無ければ false のまま） | PASS (`test_deterministic_surfacing.py`) |
| X-7 | `evidence_map.claim_id` が最終候補リストと 1:1（reconcile drop・決定論補完後もずれない） | PASS (`test_deterministic_surfacing.py`) |
| X-8 | Stage2 想起が LLM の引用なしでも `bank_inquiry_points` に出る（本文は KB 由来・引用済みは重複しない） | PASS (`test_deterministic_surfacing.py`) |
| X-9 | KB 自由文の関門: `/ingest` が credential 行を reject（理由に値を出さない）・KB に既にある credential は envelope に出ない・連絡先はマスクされ業務文は残る・Title Case 業務語は壊さない | PASS (`test_stage2_recall_end_to_end.py` / `test_deterministic_surfacing.py`) |
| X-10 | 検索語から filler（3 文字以下・stopword）が落ちる（`at_risk`→"risk" / `certificate_of_origin`→"certificate origin"） | PASS (`test_deterministic_surfacing.py`) |
| X-11 | 正常な LLM 経路（縮退でない）で recall 補完が働く — Fake LLM が実測どおり `bank_inquiry_points: []` を返す E2E | PASS (`test_stage2_recall_end_to_end.py`) |
| X-18 | KB に生の連絡先が蓄積されない（`/ingest` の書き込み時にマスクされ、業務文は検索可能なまま残る） | PASS (`test_stage2_recall_end_to_end.py`) |
| X-13 | LLM が KB の secret を複写して同じ source を引用しても envelope に出ない（KB 境界で落ちる） | PASS (`test_stage2_recall_end_to_end.py`) |
| X-14 | LLM が KB 由来でない credential を自分で書いても envelope に出ない（Stage4 の最終検査が唯一の防御になる経路） | PASS (`test_stage2_recall_end_to_end.py`) |
| X-15 | recall の抑止が担当者に見える（`requires_human_review` に理由・値は載せない） | PASS (`test_stage2_recall_end_to_end.py`) |
| X-16 | `bearer_token` 誤検出で正当な業務行（bearer B/L）が reject されない・フィールド境界をまたぐ偽陽性も作らない | PASS (`test_stage2_recall_end_to_end.py`) |
| X-17 | 重なった PII findings（credit_card ∩ phone_jp）で文字列が壊れない・短い書類識別子（bl/awb/coo）が検索語に残る | PASS (`test_deterministic_surfacing.py`) |
| X-12 | `cited_source_ids`（state 内部・顧客 envelope には出ない）が `evidence_map` の source union と一致 | PASS (`test_deterministic_surfacing.py`) |

## Execution

`.venv/bin/python -m pytest -q`（repo ルート） — 615 passed / 2 skipped（skip は PB-7 HITL 無効のみ）。

> **実 LLM 1 回転（2026-08-13）**: 注入シームに実 LLM クライアントを刺して invoke した。
> `status: success`・候補 8 件すべてに引用 ID・捏造 ID なし・断定表現なし・
> 出力が非空であることを確認。この回で **Stage2 の想起が本番経路で 0 件**であることを検出し（下記）、
> 修正後に ingest → preflight の end-to-end で想起されることを再確認した。
>
> **2026-08-13 の修正で追加した回帰テスト（すべて変異検査で証明力を確認済み）**:
> Stage2 想起の end-to-end（クエリを旧実装へ戻す／識別子の語割りを外す の 2 変異で赤化）/
> 決定論所見の候補化・`requires_human_review` の Stage1 発火・`evidence_map.claim_id` の 1:1
> （各補強を外す変異と、許容差内も所見にする過剰検知変異で赤化）。
>
> **敵対レビュー（High 1 / Medium 4）への対応で追加**: KB 自由文の関門（出力側 credential /
> ingest 拒否 / PII マスク / 業務語非破壊）・検索語の filler 除外・`cited_source_ids` と
> `evidence_map` の整合・**正常 LLM 経路の E2E**（それまで E2E は LLM 未設定＝縮退経路しか通っておらず、
> `_ensure_recalled_review_points` が一度も実行されていなかった）。
> 🔴 ingest 拒否テストは**初版の入力が既存の語ベース検査に先に当たっていて新ゲートを検証していなかった**
> （変異を入れても緑のまま）。`_PROHIBITED_ROW_TOKENS` の語を含まない入力に差し替えて赤化を確認した。
> 🔴 同じ回で **`cited_source_ids` の E2E テストも vacuous** だった — このフィールドは state 内部の値で
> HTTP 応答には出ないため常に `None` になり、`or` のフォールバックで自動的に通っていた。
> Stage4 の result を直接見る単体テストへ移設し、変異で赤化することを確認した。

> 敵対レビュー2ラウンド（Critical/High 計18指摘）への対応で追加した回帰テスト:
> S-3 verdict キー変種（camelCase/全角/accept系）・自由文再帰走査・ネスト smuggling / Stage4 構造化 reconcile
> （claim_type 値照合・矛盾drop・uncited semantic降格・LLM 偽装backing不可） / feedback case存在・event_id冪等 /
> KB cross-tenant upsert非破壊 / Stage1 invoice authoritative / authoritative source 引用受理。
>
> 認可と案件参照の境界を固定するために追加した回帰テスト:
> KB promotion の approver 詐称拒否（allowlist 外 caller）・allowlist 内 caller promote 成功・audit approver=caller_id /
> `/preflight` case_ref server 採番・client 提供 case_ref 無視。

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | ローカル CLI（Azure OpenAI `gpt-5.5`）を `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['facts', 'inconsistency_candidates', 'bank_inquiry_points', 'correction_suggestions', 'evidence_map', 'mode', 'gate_kind'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（ローカル CLI と鍵が要るため）。
