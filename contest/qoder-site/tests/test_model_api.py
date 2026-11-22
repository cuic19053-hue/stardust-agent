import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
module = importlib.import_module('backend.app')
client = TestClient(module.app)

DRAFT = '本项目已实测，准确率达到 98.6%。'
EVIDENCE = '测试尚未开始，当前没有实测结果。'
VALID = {'status':'potential_conflict','draft_quote':'已实测','evidence_quote':'尚未开始','explanation':'状态不同，请核实是否指同一实验。','next_step':'核对实验记录。'}


def test_grounding_accepts_only_verbatim_quotes():
    assert module.validate_findings({'findings':[VALID]},DRAFT,EVIDENCE)[0]['status'] == 'potential_conflict'
    for change in ({'draft_quote':'未提供的数字'}, {'evidence_quote':'虚构的证据'}, {'evidence_quote':''}, {'status':'invented'}):
        with pytest.raises(ValueError):
            module.validate_findings({'findings':[{**VALID, **change}]},DRAFT,EVIDENCE)
    missing={**VALID,'status':'needs_evidence','evidence_quote':''}
    assert module.validate_findings({'findings':[missing]},DRAFT,'')[0]['status']=='needs_evidence'


def test_api_with_mocked_local_model():
    def respond(request):
        assert request.url.host == '127.0.0.1'
        sent=json.loads(request.content)
        assert sent['model']==module.MODEL
        assert '不凭空补数字' in sent['messages'][0]['content']
        return httpx.Response(200,json={'message':{'content':json.dumps({'findings':[VALID]},ensure_ascii=False)}})
    real_client=httpx.AsyncClient
    def local_client(*args,**kwargs):
        return real_client(transport=httpx.MockTransport(respond))
    with patch.object(module.httpx,'AsyncClient',side_effect=local_client):
        response=client.post('/api/review',json={'draft':DRAFT,'evidence':EVIDENCE})
    assert response.status_code == 200
    assert response.json()['findings'][0]['draft_quote'] == '已实测'
    assert response.headers['cache-control'] == 'no-store'


def test_rejects_untrusted_origin_host_and_invalid_input():
    assert client.post('/api/review',json={'draft':DRAFT},headers={'origin':'https://attacker.example'}).status_code==403
    assert client.post('/api/review',json={'draft':DRAFT},headers={'host':'attacker.example'}).status_code==403
    assert client.post('/api/review',json={'draft':' '} ).status_code==422
    assert client.post('/api/review',json={'draft':'x'*4001}).status_code==422
    assert client.post('/api/review',content='x'*24001,headers={'content-type':'application/json'}).status_code==413
    assert client.post('/api/review',content='{}',headers={'content-type':'text/plain'}).status_code==415


def test_unavailable_model_is_not_success():
    def respond(request):
        return httpx.Response(200,json={'message':{'content':'not valid JSON'}})
    real_client=httpx.AsyncClient
    with patch.object(module.httpx,'AsyncClient',side_effect=lambda *a,**kw: real_client(transport=httpx.MockTransport(respond))):
        response=client.post('/api/review',json={'draft':DRAFT,'evidence':EVIDENCE})
    assert response.status_code==502
    assert '无法通过原文校验' in response.json()['detail']


def test_extract_word_text_and_reject_invalid_files():
    import base64, io
    from docx import Document
    doc=Document(); doc.add_paragraph('研究目标：改善校园报修。')
    table=doc.add_table(rows=1, cols=2); table.cell(0,0).text='阶段';table.cell(0,1).text='计划'
    buf=io.BytesIO(); doc.save(buf)
    encoded=base64.b64encode(buf.getvalue()).decode()
    response=client.post('/api/extract',json={'filename':'case.docx','content':encoded})
    assert response.status_code==200
    assert '研究目标' in response.json()['text'] and '阶段 | 计划' in response.json()['text']
    assert client.post('/api/extract',json={'filename':'case.docx','content':'???'}).status_code==400
    assert client.post('/api/extract',json={'filename':'case.zip','content':base64.b64encode(b'hi').decode()}).status_code==400
    big=base64.b64encode(('文'*16001).encode()).decode()
    assert client.post('/api/extract',json={'filename':'case.txt','content':big}).status_code==400


def test_diagnosis_quote_validation_and_roles():
    from backend.doctor import validate_issues
    issue={'quote':'已实测','problem':'结果未提供记录','suggestion':'补充原始记录','question':'记录在哪里？'}
    assert validate_issues({'issues':[issue]},DRAFT,'evidence')[0]['role_name']=='真实性'
    with pytest.raises(ValueError):
        validate_issues({'issues':[{**issue,'quote':'虚构的原句'}]},DRAFT,'evidence')
    assert validate_issues({'issues':[]},DRAFT,'research')==[]
    unsafe={**issue,'suggestion':'把准确率目标设为98.6%'}
    assert '不要直接把实测值改成目标' in validate_issues({'issues':[unsafe]},DRAFT,'evidence')[0]['suggestion']
    assert client.post('/api/diagnose',json={'chunk':DRAFT,'role':'fake'}).status_code==422
    assert client.post('/api/diagnose',json={'chunk':DRAFT,'role':'custom','custom_focus':''}).status_code==422
    with patch.object(module,'ask_model',return_value={'issues':[issue]}) as mocked:
        response=client.post('/api/diagnose',json={'chunk':DRAFT,'role':'custom','custom_focus':'核对研究方法'})
    assert response.status_code==200 and response.json()['issues'][0]['role_name']=='自定义视角'
    assert mocked.call_args.args[1]['custom_focus']=='核对研究方法'


def test_rewrite_and_recheck_never_accept_unverified_quote_or_new_numbers():
    from backend.doctor import validate_recheck
    with pytest.raises(ValueError):
        validate_recheck({'status':'improved','quote':'虚构原句','reason':'说明'},'真实材料')
    assert validate_recheck({'status':'uncertain','quote':'真实','reason':'需要更多信息'},'真实材料')['status']=='uncertain'
    unchanged=client.post('/api/recheck',json={'original':'周期待确认','revised':'周期待确认','problem':'周期不明确'})
    assert unchanged.json()['status']=='unresolved'
    with patch.object(module,'ask_model',return_value={'rewrite':'已有1000名用户认可'}):
        response=client.post('/api/rewrite',json={'original':'计划调研','problem':'不明确','suggestion':'补充对象'})
    assert response.status_code==502
    with patch.object(module,'ask_model',return_value={'rewrite':'补充各阶段的周期以及计算资源。'}):
        response=client.post('/api/rewrite',json={'original':'周期待确定','problem':'不明确','suggestion':'补充周期'})
    assert response.status_code==502
    with patch.object(module,'ask_model',return_value={'rewrite':'本系统显著提升了服务质量。'}):
        response=client.post('/api/rewrite',json={'original':'本系统用于服务。','problem':'缺少依据','suggestion':'补充证据'})
    assert response.status_code==502
    with patch.object(module,'ask_model',return_value={'status':'improved','quote':'不存在','reason':'可以'}):
        response=client.post('/api/recheck',json={'original':'计划调研','revised':'计划访谈同学','problem':'不明确'})
    assert response.status_code==502
    with patch.object(module,'ask_model',return_value={'status':'improved','quote':'周期待补充','reason':'已经补充'}):
        response=client.post('/api/recheck',json={'original':'周期待确定','revised':'周期待补充','problem':'周期不明确'})
    assert response.json()['status']=='uncertain'


def test_generated_roles_must_be_distinct_and_grounded():
    from backend.doctor import validate_roles
    sample='研究目的：改善校园报修。研究方法：计划开展问卷调查。'
    roles={'roles':[{'name':'方法审阅','focus':'检查调查设计','basis_quote':'问卷调查'}, {'name':'需求审阅','focus':'检查研究目的','basis_quote':'改善校园报修'}]}
    assert len(validate_roles(roles,sample))==2
    with pytest.raises(ValueError):
        validate_roles({'roles':[roles['roles'][0],{**roles['roles'][1],'basis_quote':'未出现的信息'}]},sample)
    with pytest.raises(ValueError):
        validate_roles({'roles':[roles['roles'][0],{**roles['roles'][1],'name':'方法审阅'}]},sample)
    with patch.object(module,'ask_model',return_value=roles):
        response=client.post('/api/plan-roles',json={'sample':sample})
    assert response.status_code==200 and len(response.json()['roles'])==2


def test_plan_roles_count_follows_material_not_a_fixed_template():
    from backend.doctor import validate_roles
    sample='一、研究目的\n改善校园报修。\n二、预算\n总额 1200 元。'
    five={'roles':[{'name':f'视角{i}','focus':'检查对应内容','basis_quote':'研究目的'} for i in range(5)]}
    assert len(validate_roles(five,sample))==5
    for count in (1,6):
        many={'roles':[{'name':f'视角{i}','focus':'检查','basis_quote':'预算'} for i in range(count)]}
        with pytest.raises(ValueError):
            validate_roles(many,sample)
    def respond(request):
        system=json.loads(request.content)['messages'][0]['content']
        assert '数量由材料决定' in system and '固定模板' in system
        assert '选择2到4个' not in system
        return httpx.Response(200,json={'message':{'content':json.dumps(five,ensure_ascii=False)}})
    real_client=httpx.AsyncClient
    with patch.object(module.httpx,'AsyncClient',side_effect=lambda *a,**kw: real_client(transport=httpx.MockTransport(respond))):
        response=client.post('/api/plan-roles',json={'sample':sample})
    assert response.status_code==200 and len(response.json()['roles'])==5


def test_cross_review_needs_another_role_and_verifiable_citation():
    from backend.doctor import validate_cross_review
    context='计划开展问卷调查，但样本选择方式尚未明确。'
    verdict={'verdict':'challenge','quote':'样本选择方式尚未明确','reason':'应先确认取样方式。'}
    assert validate_cross_review(verdict,context)['verdict']=='challenge'
    for invalid in [{**verdict,'quote':'虚构引用'},{**verdict,'quote':''},{**verdict,'verdict':'invented'}]:
        with pytest.raises(ValueError): validate_cross_review(invalid,context)
    assert validate_cross_review({'verdict':'uncertain','quote':'','reason':'上下文不足。'},context)['verdict']=='uncertain'
    payload={'context':context,'original_quote':'问卷调查','problem':'方法不清楚','suggestion':'完善方法','author':'内容','reviewer':'方法','reviewer_focus':'样本选择'}
    with patch.object(module,'ask_model',return_value=verdict):
        assert client.post('/api/cross-review',json=payload).json()['verdict']=='challenge'
    assert client.post('/api/cross-review',json={**payload,'reviewer':'内容'}).status_code==422
    assert client.post('/api/cross-review',json={**payload,'original_quote':'伪造文字'}).status_code==422
    with patch.object(module,'ask_model',return_value={**verdict,'quote':'不存在'}):
        assert client.post('/api/cross-review',json=payload).status_code==502
    with patch.object(module,'ask_model',return_value={'verdict':'agree','quote':'','reason':'同意原意见。'}):
        assert client.post('/api/cross-review',json=payload).status_code==502


def test_rewrite_can_require_answer_to_cross_review():
    with patch.object(module,'ask_model',return_value={'rewrite':'计划访谈维修同学并记录样本选择方式。'}) as mocked:
        response=client.post('/api/rewrite',json={'original':'计划开展问卷调查','problem':'样本不明','suggestion':'写明对象','cross_note':'样本审阅（质疑）：应先确认取样方式。'})
    assert response.status_code==200
    assert 'cross_note' in mocked.call_args.args[0]
    assert '交叉复核' in mocked.call_args.args[0]
    assert '质疑' in json.dumps(mocked.call_args.args[1],ensure_ascii=False)
    with patch.object(module,'ask_model',return_value={'rewrite':'计划访谈维修同学。'}) as mocked:
        response=client.post('/api/rewrite',json={'original':'计划开展问卷调查','problem':'样本不明','suggestion':'写明对象'})
    assert response.status_code==200
    assert 'cross_note' not in mocked.call_args.args[0]
