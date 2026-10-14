from skillexpand import schema as S


def test_legacy_skill_roundtrip_preserves_record_identity():
    skill = S.Skill(skill_id='test', version=0, name='Test',
                    family_id='family', description='Test', body='Inspect then repair.')
    record = S.to_dict(skill)
    assert 'resources' not in record and 'package_mode' not in record
    assert S.to_dict(S.from_dict(S.Skill, record)) == record
    packaged = S.Skill(**{**skill.__dict__, 'package_mode': True,
                         'resources': {'reference.txt': 'reference'}})
    value = S.to_dict(packaged)
    assert value['resources'] == {'reference.txt': 'reference'}
    assert value['package_mode'] is True
    assert S.to_dict(S.from_dict(S.Skill, value)) == value
