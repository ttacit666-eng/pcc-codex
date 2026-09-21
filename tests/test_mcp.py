import asyncio
import json
import time
import httpx
import jwt
import pytest
from pydantic import ValidationError
from cryptography.hazmat.primitives.asymmetric import rsa
from pcc.server import Verifier,build
from pcc.controller import Controller
import pcc.server as server

def test_authenticated_mcp_transport(tmp_path,caplog):
    async def go():
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cfg={'issuer':'https://issuer.example.test','resource':'https://pcc.example.test/mcp','client_ids':['chatgpt-test']}
        v=Verifier(cfg,key.public_key());claims={'iss':cfg['issuer'],'aud':cfg['resource'],'sub':'owner','iat':int(time.time()),'exp':int(time.time())+120,'scope':'pcc:operate','azp':'chatgpt-test'}
        token=jwt.encode(claims,key,algorithm='RS256')
        assert await v.verify_token(token)
        # Auth0 uses a two-audience token for a custom API plus OIDC UserInfo.
        assert await v.verify_token(jwt.encode({**claims,'aud':[cfg['resource'],'https://issuer.example.test/userinfo']},key,algorithm='RS256'))
        for patch in ({'aud':'wrong'},{'iss':'https://wrong'},{'exp':1},{'scope':''},{'azp':'other'}):
            assert await v.verify_token(jwt.encode({**claims,**patch},key,algorithm='RS256')) is None
        assert await v.verify_token('bad') is None
        m=build(Controller(tmp_path/'state'),cfg,v);app=m.streamable_http_app()
        async with m.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://pcc.example.test') as client:
                headers={'Accept':'application/json, text/event-stream','Content-Type':'application/json'}
                payload={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'PCC test','version':'1'}}}
                no=await client.post('/mcp',headers=headers,json=payload);assert no.status_code==401
                assert 'resource_metadata' in no.headers['www-authenticate']
                sentinel='PCC_SYNTHETIC_PRIVATE_SENTINEL'
                for bad_header in ('Basic '+sentinel,'Bearer '+sentinel):
                    bad=await client.post('/mcp?diagnostic='+sentinel,headers={**headers,'Authorization':bad_header},json=payload)
                    assert bad.status_code==401
                metadata=await client.get('/.well-known/oauth-protected-resource/mcp')
                assert metadata.status_code==200
                headers['Authorization']='Bearer '+token
                resp=await client.post('/mcp',headers=headers,json=payload);assert resp.status_code==200
                listed=await client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
                tools={x['name']:x for x in listed.json()['result']['tools']}
                assert tools['pcc_submit']['annotations']['readOnlyHint'] is False
                assert tools['pcc_submit']['annotations']['openWorldHint'] is True
                assert tools['pcc_status']['annotations']['readOnlyHint'] is True
                result=await client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'pcc_capabilities','arguments':{}}})
                assert result.status_code==200;assert not result.json()['result'].get('isError')
                ingress=[json.loads(r.getMessage().removeprefix('PCC_INGRESS ')) for r in caplog.records if r.name=='pcc.ingress']
                assert len(ingress)==7
                assert [r['sequence'] for r in ingress]==list(range(1,8))
                assert [(r['auth_header_present'],r['bearer_prefix_matches'],r['status']) for r in ingress[:3]]==[(False,False,401),(True,False,401),(True,True,401)]
                assert ingress[3]['path_category']=='metadata' and ingress[3]['method']=='GET'
                assert all(r['path_category']=='mcp' and r['method']=='POST' and r['status']==200 and r['auth_header_present'] and r['bearer_prefix_matches'] for r in ingress[4:])
                assert all(set(r)=={'sequence','sample_time','path_category','method','status','auth_header_present','bearer_prefix_matches'} for r in ingress)
                diagnostic='\n'.join(r.getMessage() for r in caplog.records if r.name in ('pcc.ingress','pcc.auth'))
                assert sentinel not in diagnostic and token not in diagnostic and 'diagnostic=' not in diagnostic
    asyncio.run(go())

def test_verifier_failure_classification_is_redacted(caplog,monkeypatch):
    async def go():
        sentinel='PCC_SYNTHETIC_EXCEPTION_PRIVATE_SENTINEL'
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cfg={'issuer':'https://issuer.example.test','resource':'https://pcc.example.test/mcp','client_ids':['chatgpt-test']}
        verifier=Verifier(cfg,key.public_key())
        claims={'iss':cfg['issuer'],'aud':cfg['resource'],'sub':'owner','iat':int(time.time()),'exp':int(time.time())+120,'scope':'pcc:operate','azp':'chatgpt-test'}
        def latest():
            return [r.getMessage() for r in caplog.records if r.name=='pcc.auth']
        async def rejected(token,label,stage='jwt_validation'):
            caplog.clear()
            assert await verifier.verify_token(token) is None
            assert latest()==['PCC_AUTH '+label+' stage='+stage]
            assert sentinel not in '\n'.join(latest()) and token not in '\n'.join(latest())
        for field in ('exp','iat','sub','aud','iss'):
            token=jwt.encode({k:v for k,v in claims.items() if k!=field},key,algorithm='RS256')
            await rejected(token,'missing_required_claim_'+field)
        await rejected(jwt.encode({**claims,'iat':sentinel},key,algorithm='RS256'),'issued_at_invalid')
        await rejected(jwt.encode({**claims,'iat':int(time.time())+3600},key,algorithm='RS256'),'token_not_yet_valid')
        await rejected(jwt.encode(claims,sentinel*3,algorithm='HS256'),'algorithm_not_allowed')
        if hasattr(jwt.exceptions,'InvalidSubjectError'):
            await rejected(jwt.encode({**claims,'sub':123},key,algorithm='RS256'),'invalid_subject_type')
        if hasattr(jwt.exceptions,'InvalidJTIError'):
            await rejected(jwt.encode({**claims,'jti':123},key,algorithm='RS256'),'token_id_invalid')
        await rejected(jwt.encode({**claims,'scope':[sentinel]},key,algorithm='RS256'),'attribute_invalid','claim_checks')
        assert Verifier.failure_reason(jwt.MissingRequiredClaimError(sentinel))=='missing_required_claim'

        token=jwt.encode(claims,key,algorithm='RS256')
        class BrokenJWKS:
            def __init__(self,error):self.error=error
            def get_signing_key_from_jwt(self,token):raise self.error
        verifier.key=None
        for kind,label in ((jwt.PyJWKClientError,'public_key_lookup_failed'),(getattr(jwt.exceptions,'PyJWKClientConnectionError',None),'public_key_network_failed')):
            if kind is not None:
                verifier.jwks=BrokenJWKS(kind(sentinel))
                await rejected(token,label,'key_lookup')
        verifier.jwks=BrokenJWKS(RuntimeError(sentinel))
        await rejected(token,'verification_failed','key_lookup')
        verifier.key=key.public_key()
        validation=ValidationError.from_exception_data('AccessToken',[{'type':'string_type','loc':('subject',),'input':{'private':sentinel}}])
        def broken_access_token(**kwargs):raise validation
        monkeypatch.setattr(server,'AccessToken',broken_access_token)
        await rejected(token,'access_token_schema_invalid','access_token_schema')
        # Successful jwt.decode is not sufficient to emit a misleading verified log.
        assert not any(message=='PCC_AUTH verified' for message in latest())
        assert Verifier.failure_reason(RuntimeError(sentinel))=='verification_failed'
        for name,label in (('PyJWKSetError','public_key_set_invalid'),('PyJWKError','public_key_invalid'),('InvalidKeyError','verification_key_invalid'),('MissingCryptographyError','cryptography_unavailable')):
            kind=getattr(jwt.exceptions,name,None)
            if kind is not None:assert Verifier.failure_reason(kind(sentinel))==label
        assert Verifier.failure_reason(TypeError(sentinel))=='type_invalid'
        assert Verifier.failure_reason(ValueError(sentinel))=='value_invalid'
    asyncio.run(go())

def test_bounded_clock_skew_preserves_expiry_and_authentication(caplog):
    async def go():
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        wrong_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cfg={'issuer':'https://issuer.example.test','resource':'https://pcc.example.test/mcp','client_ids':['chatgpt-test']}
        now=int(time.time())
        claims={'iss':cfg['issuer'],'aud':cfg['resource'],'sub':'owner','iat':now-10,'exp':now+600,'scope':'pcc:operate','azp':'chatgpt-test'}
        strict=Verifier(cfg,key.public_key())
        tolerant=Verifier({**cfg,'jwt_clock_skew_seconds':180},key.public_key())
        assert strict.clock_skew_seconds==0
        for patch in ({'iat':now+120},{'nbf':now+120},{'iat':now+120,'nbf':now+120}):
            token=jwt.encode({**claims,**patch},key,algorithm='RS256')
            assert await strict.verify_token(token) is None
            assert await tolerant.verify_token(token)
        for patch in ({'iat':now+190},{'nbf':now+190},{'exp':now-30},{'exp':now},{'aud':'wrong'},{'iss':'https://wrong'},{'azp':'other'},{'scope':''},{'sub':123}):
            caplog.clear()
            assert await tolerant.verify_token(jwt.encode({**claims,**patch},key,algorithm='RS256')) is None
            assert not any(r.name=='pcc.auth' and r.getMessage()=='PCC_AUTH verified' for r in caplog.records)
        assert await tolerant.verify_token(jwt.encode(claims,wrong_key,algorithm='RS256')) is None
        assert await tolerant.verify_token(jwt.encode(claims,'synthetic-only-hmac-secret-that-is-not-a-credential',algorithm='HS256')) is None
        for field in ('exp','iat','sub','aud','iss'):
            assert await tolerant.verify_token(jwt.encode({k:v for k,v in claims.items() if k!=field},key,algorithm='RS256')) is None
        for allowed in (0,1,179,180):
            assert Verifier({**cfg,'jwt_clock_skew_seconds':allowed},key.public_key()).clock_skew_seconds==allowed
        for forbidden in (-1,181,True,False,1.0,'180',None):
            with pytest.raises(ValueError,match='integer from 0 to 180'):
                Verifier({**cfg,'jwt_clock_skew_seconds':forbidden},key.public_key())
    asyncio.run(go())
