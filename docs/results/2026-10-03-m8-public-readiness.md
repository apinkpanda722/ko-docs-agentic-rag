# M8 공개 전환 점검

<!-- BEGIN generated -->
## 결정론 점검 집계

발견 건수는 공개 판정이 아니다. 패턴은 참고 위치이며 매치 값은 기록하지 않는다.

```json
{
  "executed_at": "2026-10-02T09:16:11.656542+00:00",
  "head_commit": "b635ae386d96c0a5e6bd18322d0842af8f694b5e",
  "worktree": {
    "scanned_files": 1272,
    "scanned_blobs": 0,
    "oversized": 1,
    "undecodable": 0,
    "excluded_sensitive_worktree": 0,
    "findings": [
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "data/manifest/law_api.jsonl",
        "patterns": {},
        "overlap_shingles": 113,
        "max_overlap_chars": 48
      },
      {
        "path": "data/manifest/m7_probe.json",
        "patterns": {
          "absolute_path": 3
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "data/manifest/m7_security_set.json",
        "patterns": {
          "phone": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "data/manifest/table_golden.json",
        "patterns": {},
        "overlap_shingles": 1,
        "max_overlap_chars": 30
      },
      {
        "path": "docs/results/2026-09-27-m2-parsing.md",
        "patterns": {},
        "overlap_shingles": 39,
        "max_overlap_chars": 47
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "docs/results/2026-09-28-m4-golden-set.md",
        "patterns": {},
        "overlap_shingles": 16,
        "max_overlap_chars": 39
      },
      {
        "path": "docs/results/2026-09-30-m5-retrieval.md",
        "patterns": {},
        "overlap_shingles": 64,
        "max_overlap_chars": 51
      },
      {
        "path": "docs/results/2026-10-01-m6-agent-mcp.md",
        "patterns": {},
        "overlap_shingles": 80,
        "max_overlap_chars": 51
      },
      {
        "path": "docs/results/2026-10-02-m7-answer-eval-appendix.md",
        "patterns": {
          "absolute_path": 3
        },
        "overlap_shingles": 30,
        "max_overlap_chars": 39
      },
      {
        "path": "phases/m0-setup/step2.md",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m1-data-collection/step1.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m1-data-collection/step2.md",
        "patterns": {
          "law_oc": 2,
          "phone": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m4-golden-set/step0.md",
        "patterns": {},
        "overlap_shingles": 16,
        "max_overlap_chars": 45
      },
      {
        "path": "phases/m7-answer-eval/step3.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m8-wrap-up/step1.md",
        "patterns": {
          "absolute_path": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "scripts/test_execute.py",
        "patterns": {},
        "overlap_shingles": 1196,
        "max_overlap_chars": 75
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "src/kodocs/llm/gemini_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/llm/openai_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/mcp_server/server.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/mcp_server/tokens.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/agent/test_trace.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_golden_review.py",
        "patterns": {},
        "overlap_shingles": 92,
        "max_overlap_chars": 75
      },
      {
        "path": "tests/eval/test_golden_specs.py",
        "patterns": {},
        "overlap_shingles": 552,
        "max_overlap_chars": 75
      },
      {
        "path": "tests/eval/test_m6_report.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_grading.py",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_latency.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_security.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_parsing_score.py",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 33
      },
      {
        "path": "tests/eval/test_query_draft.py",
        "patterns": {},
        "overlap_shingles": 92,
        "max_overlap_chars": 75
      },
      {
        "path": "tests/eval/test_text_normalize.py",
        "patterns": {},
        "overlap_shingles": 9,
        "max_overlap_chars": 38
      },
      {
        "path": "tests/ingest/fixtures/law_body_full.json",
        "patterns": {
          "law_oc": 2,
          "phone": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/fixtures/law_body_scalar.json",
        "patterns": {
          "phone": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/fixtures/law_search_current_page1.json",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_http.py",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_law_api.py",
        "patterns": {
          "law_oc": 3,
          "phone": 4
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_manifest.py",
        "patterns": {
          "law_oc": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_anthropic_batch.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_gemini_chat.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_openai_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/mcp_server/conftest.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_articles.py",
        "patterns": {},
        "overlap_shingles": 3,
        "max_overlap_chars": 32
      },
      {
        "path": "tests/parsing/test_upstage_adapter.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_upstage_direct.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/test_config.py",
        "patterns": {
          "assignment": 10
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      }
    ]
  },
  "history": {
    "scanned_files": 0,
    "scanned_blobs": 2216,
    "oversized": 7,
    "undecodable": 0,
    "excluded_sensitive_worktree": 0,
    "findings": [
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "data/manifest/law_api.jsonl",
        "patterns": {},
        "overlap_shingles": 113,
        "max_overlap_chars": 48
      },
      {
        "path": "data/manifest/m7_probe.json",
        "patterns": {
          "absolute_path": 3
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "data/manifest/m7_security_set.json",
        "patterns": {
          "phone": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "data/manifest/table_golden.json",
        "patterns": {},
        "overlap_shingles": 1,
        "max_overlap_chars": 30
      },
      {
        "path": "docs/results/2026-09-27-m2-parsing.md",
        "patterns": {},
        "overlap_shingles": 39,
        "max_overlap_chars": 47
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "docs/results/2026-09-28-m4-golden-set.md",
        "patterns": {},
        "overlap_shingles": 16,
        "max_overlap_chars": 39
      },
      {
        "path": "docs/results/2026-09-30-m5-retrieval.md",
        "patterns": {},
        "overlap_shingles": 64,
        "max_overlap_chars": 51
      },
      {
        "path": "docs/results/2026-10-01-m6-agent-mcp.md",
        "patterns": {},
        "overlap_shingles": 80,
        "max_overlap_chars": 51
      },
      {
        "path": "docs/results/2026-10-02-m7-answer-eval-appendix.md",
        "patterns": {
          "absolute_path": 3
        },
        "overlap_shingles": 30,
        "max_overlap_chars": 39
      },
      {
        "path": "phases/m0-setup/step2.md",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m1-data-collection/step1.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m1-data-collection/step2.md",
        "patterns": {
          "law_oc": 2,
          "phone": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m4-golden-set/step0.md",
        "patterns": {},
        "overlap_shingles": 16,
        "max_overlap_chars": 45
      },
      {
        "path": "phases/m7-answer-eval/step3.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m8-wrap-up/step1.md",
        "patterns": {
          "absolute_path": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "scripts/test_execute.py",
        "patterns": {},
        "overlap_shingles": 1196,
        "max_overlap_chars": 75
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "src/kodocs/llm/gemini_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/llm/openai_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/mcp_server/server.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/mcp_server/tokens.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/agent/test_trace.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_golden_review.py",
        "patterns": {},
        "overlap_shingles": 92,
        "max_overlap_chars": 75
      },
      {
        "path": "tests/eval/test_golden_specs.py",
        "patterns": {},
        "overlap_shingles": 552,
        "max_overlap_chars": 75
      },
      {
        "path": "tests/eval/test_m6_report.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_grading.py",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_latency.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_security.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_parsing_score.py",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 33
      },
      {
        "path": "tests/eval/test_query_draft.py",
        "patterns": {},
        "overlap_shingles": 92,
        "max_overlap_chars": 75
      },
      {
        "path": "tests/eval/test_text_normalize.py",
        "patterns": {},
        "overlap_shingles": 9,
        "max_overlap_chars": 38
      },
      {
        "path": "tests/ingest/fixtures/law_body_full.json",
        "patterns": {
          "law_oc": 2,
          "phone": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/fixtures/law_body_scalar.json",
        "patterns": {
          "phone": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/fixtures/law_search_current_page1.json",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_http.py",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_law_api.py",
        "patterns": {
          "law_oc": 3,
          "phone": 4
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_manifest.py",
        "patterns": {
          "law_oc": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_anthropic_batch.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_gemini_chat.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_openai_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/mcp_server/conftest.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_articles.py",
        "patterns": {},
        "overlap_shingles": 3,
        "max_overlap_chars": 32
      },
      {
        "path": "tests/parsing/test_upstage_adapter.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_upstage_direct.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/test_config.py",
        "patterns": {
          "assignment": 10
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-10-02-m7-answer-eval.md",
        "patterns": {
          "absolute_path": 3
        },
        "overlap_shingles": 30,
        "max_overlap_chars": 39
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/llm/openai_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_openai_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_security.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_security.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_security.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_grading.py",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/llm/gemini_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_grading.py",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_gemini_chat.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/llm/gemini_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_gemini_chat.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_grading.py",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m7_grading.py",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m7-answer-eval/step3.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "phases/m7-answer-eval/step3.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-10-01-m6-agent-mcp.md",
        "patterns": {},
        "overlap_shingles": 80,
        "max_overlap_chars": 51
      },
      {
        "path": "tests/eval/test_m6_report.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-10-01-m6-agent-mcp.md",
        "patterns": {},
        "overlap_shingles": 9,
        "max_overlap_chars": 38
      },
      {
        "path": "docs/results/2026-10-01-m6-agent-mcp.md",
        "patterns": {},
        "overlap_shingles": 9,
        "max_overlap_chars": 38
      },
      {
        "path": "tests/eval/test_m6_report.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-10-01-m6-agent-mcp.md",
        "patterns": {},
        "overlap_shingles": 9,
        "max_overlap_chars": 38
      },
      {
        "path": "tests/eval/test_m6_report.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/agent/test_trace.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m6_report.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_m6_report.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "scripts/test_execute.py",
        "patterns": {},
        "overlap_shingles": 1196,
        "max_overlap_chars": 75
      },
      {
        "path": "src/kodocs/llm/openai_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/llm/test_openai_chat.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-09-30-m5-retrieval.md",
        "patterns": {},
        "overlap_shingles": 62,
        "max_overlap_chars": 51
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_golden_specs.py",
        "patterns": {},
        "overlap_shingles": 460,
        "max_overlap_chars": 75
      },
      {
        "path": "tests/eval/test_query_draft.py",
        "patterns": {},
        "overlap_shingles": 92,
        "max_overlap_chars": 75
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_golden_specs.py",
        "patterns": {},
        "overlap_shingles": 368,
        "max_overlap_chars": 75
      },
      {
        "path": "tests/eval/test_query_draft.py",
        "patterns": {},
        "overlap_shingles": 92,
        "max_overlap_chars": 75
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_golden_specs.py",
        "patterns": {},
        "overlap_shingles": 92,
        "max_overlap_chars": 75
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "src/kodocs/eval/query_draft.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 15,
        "max_overlap_chars": 40
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 16,
        "max_overlap_chars": 41
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 16,
        "max_overlap_chars": 41
      },
      {
        "path": "src/kodocs/eval/templates/m3_synthetic.md",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 32
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-09-27-m3-synthetic.md",
        "patterns": {},
        "overlap_shingles": 12,
        "max_overlap_chars": 41
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/synth/generate.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "docs/results/2026-09-27-m2-parsing.md",
        "patterns": {},
        "overlap_shingles": 39,
        "max_overlap_chars": 47
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_upstage_adapter.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_upstage_direct.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_upstage_adapter.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_upstage_direct.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/eval/test_parsing_score.py",
        "patterns": {},
        "overlap_shingles": 4,
        "max_overlap_chars": 33
      },
      {
        "path": "tests/parsing/test_articles.py",
        "patterns": {},
        "overlap_shingles": 3,
        "max_overlap_chars": 32
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_upstage_adapter.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "src/kodocs/parsing/run.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/parsing/test_upstage_adapter.py",
        "patterns": {
          "assignment": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/test_config.py",
        "patterns": {
          "assignment": 10
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_law_api.py",
        "patterns": {
          "law_oc": 3,
          "phone": 4
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": ".claude/skills/failure-patterns/SKILL.md",
        "patterns": {
          "law_oc": 1
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_manifest.py",
        "patterns": {
          "law_oc": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/ingest/test_law_api.py",
        "patterns": {
          "phone": 2
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      },
      {
        "path": "tests/test_config.py",
        "patterns": {
          "assignment": 10
        },
        "overlap_shingles": 0,
        "max_overlap_chars": 0
      }
    ]
  },
  "paths": {
    "current": {},
    "history": {}
  },
  "email_domains": {
    "author": {
      "gmail.com": {
        "count": 327,
        "category": "personal_service"
      },
      "liveuou.kr": {
        "count": 21,
        "category": "other"
      },
      "mediazen.co.kr": {
        "count": 5,
        "category": "other"
      }
    },
    "committer": {
      "github.com": {
        "count": 21,
        "category": "other"
      },
      "gmail.com": {
        "count": 327,
        "category": "personal_service"
      },
      "mediazen.co.kr": {
        "count": 5,
        "category": "other"
      }
    }
  },
  "shingles": {
    "status": "available",
    "sources": [
      {
        "path": "data/interim/law",
        "files": 267
      },
      {
        "path": "data/interim/parsed",
        "files": 359
      }
    ]
  },
  "commit_inventory": {
    "definition": "서로 다른 (파일, 해시) 쌍: 커밋|commit JSON 키의 값 또는 커밋|commit|code_commit이 있는 줄의 7~40자 소문자 16진 토큰. SHA-256은 제외.",
    "pairs": 25,
    "distinct_hashes": 15,
    "files": 19,
    "paths": [
      "data/manifest/m5_config.json",
      "data/manifest/m5_dev.json",
      "data/manifest/m5_index.json",
      "data/manifest/m5_test_results/run-01.json",
      "data/manifest/m6_config.json",
      "data/manifest/m6_dev.json",
      "data/manifest/m6_dev_invalid/run-01.json",
      "data/manifest/m7_latency.json",
      "data/manifest/m8_audit.json",
      "data/manifest/parse_scores.json",
      "docs/results/2026-09-26-m1-data-collection.md",
      "docs/results/2026-09-27-m2-parsing.md",
      "docs/results/2026-09-27-m3-synthetic.md",
      "docs/results/2026-09-28-m4-golden-set.md",
      "docs/results/2026-09-30-m5-retrieval.md",
      "docs/results/2026-10-01-m6-agent-mcp.md",
      "docs/results/2026-10-02-m7-answer-eval-appendix.md",
      "docs/results/2026-10-02-m7-answer-eval.md",
      "docs/results/2026-10-03-m8-public-readiness.md"
    ],
    "existing": 15,
    "missing": 0,
    "rev_parse": [
      {
        "path": "src/kodocs/eval/m4_report.py",
        "line": 408
      },
      {
        "path": "src/kodocs/eval/m5_dev.py",
        "line": 507
      },
      {
        "path": "src/kodocs/eval/m5_report.py",
        "line": 1147
      },
      {
        "path": "src/kodocs/eval/m5_test.py",
        "line": 841
      },
      {
        "path": "src/kodocs/eval/m6_dev.py",
        "line": 885
      },
      {
        "path": "src/kodocs/eval/m8_audit.py",
        "line": 268
      },
      {
        "path": "src/kodocs/eval/m8_audit.py",
        "line": 460
      },
      {
        "path": "src/kodocs/eval/m8_audit.py",
        "line": 461
      },
      {
        "path": "src/kodocs/eval/parsing_report.py",
        "line": 127
      },
      {
        "path": "src/kodocs/eval/parsing_report.py",
        "line": 912
      },
      {
        "path": "src/kodocs/eval/parsing_score.py",
        "line": 1043
      },
      {
        "path": "src/kodocs/eval/synth_report.py",
        "line": 160
      },
      {
        "path": "src/kodocs/eval/synth_report.py",
        "line": 969
      },
      {
        "path": "src/kodocs/retrieval/index_build.py",
        "line": 1034
      }
    ],
    "rev_parse_files": 10,
    "rev_parse_calls": 14
  },
  "hygiene": {
    "LICENSE": false,
    "README.md": true,
    ".env.example": true,
    "env_in_history": false
  },
  "refs": {
    "heads": 20,
    "remotes": 20
  },
  "exclusions": {
    "max_blob_bytes": 5242880,
    "worktree_sensitive_reason": ".env·로컬 원문·평문 토큰 파일은 직접 열지 않음; 이력 blob은 점검",
    "binary_reason": "UTF-8 디코드 불가 또는 NUL 포함"
  },
  "github": {
    "status": "미실행: 사용자 실행 필요"
  }
}
```
<!-- END generated -->

## 사용자 결정 (사용자 보존 영역)

- [ ] 공개 여부(사용자 결정). 결정: 
  - 근거: 생성 영역 전체, [위협 모델](../THREAT_MODEL.md).
- [ ] 이력 처리. 결정: 
  - **기본안: 이력 유지.** 결과 문서·manifest의 커밋 참조(m5_config.code_commit, M7 측정 코드 커밋, rev-parse 기록 코드)가 이력 재작성 후 없는 커밋을 가리키고, 결과를 보기 전에 규칙을 커밋했다는 시간순 근거가 깨진다. 새 저장소(스쿼시)도 같은 이유로 제외한다. 근거: [ADR-026](../ADR.md), 생성 영역 commit_inventory.
  - 이력 정리 대안(`filter-repo --mailmap` + old→new SHA 대응표 + 문서 내 해시 갱신)의 비용: 회사 메일을 반드시 지워야 하는 경우만 고려. 대상 파일 수·해시 수·rev-parse 위치는 생성 영역 commit_inventory의 files·pairs·distinct_hashes·rev_parse_files·rev_parse_calls 참조. 실행 절차나 스크립트는 만들지 않는다.
- [ ] 작성자 이메일 확인. 결정: 
  - 사용자 설명: liveuou.kr 21건은 GitHub 웹 머지 커밋(계정 이메일), 회사 메일 도메인은 5건. 스캐너의 생성 영역 email_domains와 대조: 
  - [ ] 앞으로의 커밋 이메일(noreply)을 사용자가 GitHub에서 직접 설정.
- [ ] LICENSE. 결정: 
  - 코드의 기본 후보 MIT. 범위: "합성 데이터·문서는 같은 라이선스, 실제 규정 원문은 저장소에 없음". 공개 직전 사용자 최종 결정. 근거: [ADR-002](../ADR.md), 생성 영역 hygiene·paths·shingles.
- [ ] 슁글 겹침 검토. 결정: 
  - 생성 영역 worktree·history findings의 건수·최대 길이가 큰 파일을 직접 검토한다. 법령 상투구 오탐 가능성이 있어 스캐너는 판정하지 않는다. 원문 없으면 건너뜀이며 통과가 아니다.
- [ ] GitHub 이슈·PR·댓글 점검 결과(`make m8-audit-gh`) 확인. 결정: 
  - 근거: 생성 영역 github 및 data/manifest/m8_audit_gh.json. 공개하면 refs/pull/*도 함께 보인다는 점 확인(로컬 refs에는 없을 수 있음).
- [ ] 공개 직전 `make m8-audit` 재실행. 결정: 
  - 이후 커밋도 점검 대상에 들어간다. 근거: 생성 영역 executed_at·head_commit. manifest와 생성 영역은 재실행마다 덮어쓴다.
