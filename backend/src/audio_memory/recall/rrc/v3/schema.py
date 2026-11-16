def validate_answer(answer, known):
    if not isinstance(answer, dict) or not isinstance(answer.get("answer"), str) or not answer["answer"].strip():
        raise ValueError("invalid_answer_text")
    support = answer.get("support")
    if not isinstance(support, list):
        raise ValueError("invalid_support")
    for item in support:
        if not isinstance(item, dict):
            raise ValueError("invalid_support_item")
        anchors = item.get("anchors")
        if not isinstance(item.get("claim"), str) or not item["claim"].strip() or not isinstance(anchors, list) or not anchors or not all(isinstance(a, str) and a in known for a in anchors):
            raise ValueError("invalid_claim_or_anchor")
    if not isinstance(answer.get("unresolved"), list):
        raise ValueError("invalid_unresolved")
