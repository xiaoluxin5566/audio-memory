import json


def test_prepare_is_local_and_disabled_plan_cannot_run(tmp_path):
    from audio_memory.analysis.beta8_writing_cli import main
    source = tmp_path / "transcript.json"
    source.write_text(json.dumps([{"segment_id": "s1", "source_file": "file", "text": "完整输入"}]))
    output = tmp_path / "run"
    assert main(["prepare", "--input", str(source), "--output", str(output)]) == 0
    plan = json.loads((output / "execution-plan.json").read_text())
    assert plan["limits"]["allow_paid"] is False
    assert plan["counts"]["P1_windows"] == 1
    assert plan["counts"]["total_formula"] == "W + 1 + H + N"
    assert plan["counts"]["P4_cards"] is None
    assert not (output / "requests").exists()
    assert main(["run", "--input", str(source), "--output", str(output), "--authorization", str(output / "execution-plan.json")]) == 2


def test_import_scores_writes_separate_artifact_without_changing_first_draft(tmp_path):
    from audio_memory.analysis.beta8_writing_cli import main
    from audio_memory.analysis.beta8_writing_scoring import pending_score
    from audio_memory.analysis.beta8_writing_store import WritingStore
    store = WritingStore(tmp_path / "run", {"test": "score_import"})
    card = {"card_id": "card-1", "markdown": "# 完整初稿\n\n预算只有三万元。", "status": "complete"}
    score = pending_score(card, {"context_scope": ["s1"]})
    score.update(status="scored", dimensions={"factual_accuracy": 15, "important_coverage": 15, "analysis_depth": 25, "actionability": 30, "expression_structure": 15}, read_scope=["s1"], strengths="示例评价", weaknesses="无扣分点")
    store.write("cards/card-1.json", card)
    store.write_text("cards/card-1.md", card["markdown"])
    store.write("scoring-packet.json", {"cards": [{"card": card, "score": pending_score(card, {})}]})
    before = (store.root / "cards/card-1.md").read_bytes()
    source = tmp_path / "scores.json"
    source.write_text(json.dumps([score]))
    assert main(["import-scores", "--output", str(store.root), "--scores", str(source)]) == 0
    files = list(store.root.glob("scores-*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["cards"][0]["raw_total"] == 100
    assert saved["project_scoring_api_requests"] == 0
    assert (store.root / "cards/card-1.md").read_bytes() == before
