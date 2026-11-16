from pathlib import Path

prompt = Path(__file__).with_name('read-answer-prompt.txt').read_text()


def validate(answer, *, retry_missing):
    supported = answer.get('has_supported_answer')
    coverage = answer.get('citation_coverage')
    if type(supported) is not bool or coverage not in ('all', 'some', 'none', 'not_applicable'):
        raise ValueError('invalid_v4_status')
    if any(not isinstance(gap, str) or not gap.strip() for gap in answer['unresolved']):
        raise ValueError('invalid_v4_gap')
    if not supported and not answer['unresolved']:
        raise ValueError('missing_unanswered_question')
    if retry_missing and (supported or coverage != 'not_applicable') and not answer['support']:
        raise ValueError('missing_claim_citations')


def describe(result, citations):
    if result['status'] == 'awaiting_user':
        return dict(answer_status='needs_clarification', citation_status='not_applicable')
    answer = result.get('answer') or {}
    supported = answer.get('has_supported_answer', False)
    status = ('partial' if answer.get('unresolved') else 'complete') if supported else 'insufficient_evidence'
    coverage = answer.get('citation_coverage')
    expected = {ref for item in answer.get('support', []) for ref in item['anchors']}
    if not citations:
        citation = 'not_applicable' if not supported and coverage == 'not_applicable' else 'missing'
    elif len(citations) != len(expected) or coverage != 'all':
        citation = 'partial'
    else:
        citation = 'linked'
    return dict(answer_status=status, citation_status=citation)
