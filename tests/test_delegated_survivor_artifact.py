import json

from uta.language.java.generation.mutation_context import write_delegated_survivors
from uta.language.java.phases.delegated_quality import _survivor_instructions


def test_uses_only_current_invocation_and_keeps_all_groups(tmp_path):
    current = tmp_path / '.uta_cache/pit-compat/current'
    current.mkdir(parents=True)
    xml = '<mutations>' + ''.join(
        '<mutation status="SURVIVED"><mutatedClass>example.Target</mutatedClass>'
        f'<mutatedMethod>method{i}</mutatedMethod><lineNumber>{i+2}</lineNumber>'
        '<mutator>NEGATE_CONDITIONALS</mutator><description>negated conditional</description>'
        '<sourceFile>Target.java</sourceFile></mutation>' for i in range(8)
    ) + '</mutations>'
    (current / 'mutations.xml').write_text(xml)
    stale = tmp_path / '.uta_cache/pit-compat/stale'
    stale.mkdir()
    (stale / 'mutations.xml').write_text(xml)
    command = ['mvn', f'-Duta.pit.compat.evidence={current}/completion.xml']
    artifact = write_delegated_survivors(str(tmp_path), {'command': command}, ['example.Target'])
    data = json.loads(artifact.read_text())
    assert len(data['targets']['example.Target']) == 8
    assert data['reports'] == [str(current / 'mutations.xml')]
    assert write_delegated_survivors(str(tmp_path), {'command': ['mvn']}, ['example.Target']) is None


def test_mutation_prompt_requires_all_groups_and_separates_equivalence():
    text = _survivor_instructions({
        'failure_stage': 'mutation_fix', 'survivor_artifact': '/gate/repair-survivors.json',
    })
    assert '/gate/repair-survivors.json' in text
    assert 'ALL remaining actionable groups' in text
    assert 'label untouched groups unattempted, never equivalent' in text
    assert _survivor_instructions({'failure_stage': 'coverage_fix'}) == ''


def test_missing_artifact_is_explicit_not_permission_to_guess():
    text = _survivor_instructions({'failure_stage': 'mutation_fix'})
    assert 'unavailable' in text
    assert 'do not infer equivalence' in text
