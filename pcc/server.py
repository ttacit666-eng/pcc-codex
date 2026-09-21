"""Authenticated Streamable HTTP MCP; all grants are local administration only."""
import asyncio
from datetime import datetime, timezone
from itertools import count
import json
import logging
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import jwt
from pydantic import ValidationError
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken,TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from .controller import Controller
from .paths import BASE,load,plain

class IngressDiagnosticMiddleware:
    """Log bounded request metadata outside authentication, never header values."""
    def __init__(self,app):
        self.app=app;self.sequence=count(1)

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        path=scope.get('path','')
        category='mcp' if path in ('/mcp','/mcp/') else ('metadata' if path in ('/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/mcp') else 'other')
        method=scope.get('method','')
        if method not in ('GET','POST','DELETE','PUT','PATCH','HEAD','OPTIONS'):method='OTHER'
        headers=scope.get('headers',())
        auth_present=any(name.lower()==b'authorization' for name,_ in headers)
        # Only compare the fixed scheme prefix; never decode, persist or log values.
        bearer=any(name.lower()==b'authorization' and value[:7].lower()==b'bearer ' for name,value in headers)
        record={'sequence':next(self.sequence),'sample_time':datetime.now(timezone.utc).isoformat(),'path_category':category,'method':method,'status':None,'auth_header_present':auth_present,'bearer_prefix_matches':bearer}
        async def observed_send(message):
            if message['type']=='http.response.start':record['status']=message['status']
            await send(message)
        try:
            await self.app(scope,receive,observed_send)
        finally:
            logging.getLogger('pcc.ingress').warning('PCC_INGRESS %s',json.dumps(record,separators=(',',':')))

class DiagnosticFastMCP(FastMCP):
    def streamable_http_app(self):
        app=super().streamable_http_app()
        app.add_middleware(IngressDiagnosticMiddleware)
        return app

class Verifier(TokenVerifier):
    @staticmethod
    def diagnostic(reason):
        # Fixed labels only: never log tokens, claims, exception text or user data.
        logging.getLogger('pcc.auth').warning('PCC_AUTH %s',reason)
    @staticmethod
    def failure_reason(error):
        if isinstance(error,jwt.MissingRequiredClaimError):
            claim=getattr(error,'claim',None)
            return 'missing_required_claim_'+claim if claim in ('exp','iat','sub','aud','iss') else 'missing_required_claim'
        # Match subclasses first. Only fixed labels leave this function; exception
        # messages and validation details can contain credential material.
        kinds=[
            (jwt.InvalidAudienceError,'audience_mismatch'),
            (jwt.InvalidIssuerError,'issuer_mismatch'),
            (jwt.ExpiredSignatureError,'expired'),
            (jwt.InvalidSignatureError,'signature_invalid'),
            (jwt.InvalidIssuedAtError,'issued_at_invalid'),
            (jwt.ImmatureSignatureError,'token_not_yet_valid'),
            (jwt.InvalidAlgorithmError,'algorithm_not_allowed'),
            (getattr(jwt.exceptions,'InvalidSubjectError',None),'invalid_subject_type'),
            (getattr(jwt.exceptions,'InvalidJTIError',None),'token_id_invalid'),
            (getattr(jwt.exceptions,'PyJWKClientConnectionError',None),'public_key_network_failed'),
            (jwt.PyJWKClientError,'public_key_lookup_failed'),
            (getattr(jwt.exceptions,'MissingCryptographyError',None),'cryptography_unavailable'),
            (getattr(jwt.exceptions,'PyJWKSetError',None),'public_key_set_invalid'),
            (getattr(jwt.exceptions,'PyJWKError',None),'public_key_invalid'),
            (getattr(jwt.exceptions,'InvalidKeyError',None),'verification_key_invalid'),
            (jwt.DecodeError,'format_invalid'),
            (ValidationError,'access_token_schema_invalid'),
            (TypeError,'type_invalid'),
            (AttributeError,'attribute_invalid'),
            (ValueError,'value_invalid'),
        ]
        return next((label for kind,label in kinds if kind is not None and isinstance(error,kind)),'verification_failed')
    def __init__(self,config,public_key=None):
        self.cfg=config;self.key=public_key
        self.clock_skew_seconds=config.get('jwt_clock_skew_seconds',0)
        if type(self.clock_skew_seconds) is not int or not 0<=self.clock_skew_seconds<=180:
            raise ValueError('jwt_clock_skew_seconds must be an integer from 0 to 180')
        if urlsplit(config['issuer']).scheme!='https' or urlsplit(config['resource']).scheme!='https':raise ValueError('HTTPS OAuth issuer/resource required')
        self.jwks=None
        if public_key is None:
            if urlsplit(config['jwks_url']).scheme!='https':raise ValueError('HTTPS JWKS required')
            self.jwks=jwt.PyJWKClient(config['jwks_url'],timeout=10)
    async def verify_token(self,token):
        stage='key_lookup'
        try:
            key=self.key or (await asyncio.to_thread(self.jwks.get_signing_key_from_jwt,token)).key
            stage='jwt_validation'
            claims=jwt.decode(token,key,algorithms=['RS256'],audience=self.cfg['resource'],issuer=self.cfg['issuer'],leeway=self.clock_skew_seconds,options={'require':['exp','iat','sub','aud','iss']})
            # PyJWT applies leeway to exp too. Keep the original expiry boundary:
            # configured skew only permits a bounded future iat/nbf, not stale tokens.
            if int(claims['exp'])<=time.time():
                self.diagnostic('expired');return None
            stage='claim_checks'
            scopes=claims.get('scope','').split()
            if 'pcc:operate' not in scopes:
                self.diagnostic('missing_required_scope');return None
            if not isinstance(claims['sub'],str):
                self.diagnostic('invalid_subject_type');return None
            client=claims.get('azp') or claims.get('client_id')
            if client not in self.cfg['client_ids']:
                self.diagnostic('client_not_allowed');return None
            stage='access_token_schema'
            result=AccessToken(token=token,subject=claims['sub'],client_id=client,scopes=scopes,expires_at=int(claims['exp']),resource=self.cfg['resource'])
            self.diagnostic('verified')
            return result
        except Exception as error:
            self.diagnostic(self.failure_reason(error)+' stage='+stage)
            return None

def build(controller,config,verifier=None):
    m=DiagnosticFastMCP('PCC independent Plus executor',host='127.0.0.1',port=config.get('port',8876),stateless_http=True,json_response=True,
       instructions='For user-requested <进行pcc协作模式>, discover the bound project and saved execution_mode, plan a bounded task and submit. PCC_HOST_TRUSTED is explicitly authorized Windows Full Access, NOT strict sandbox isolation; do not re-ask per approved action. Admin/system changes and new external targets require authorization. Mode cannot be overridden by the caller. Only the activation phrase without a task does not execute. Query the SAME task after timeout. Return real usage and independently review frozen evidence; LOCAL_CHECK is not REVIEW. Never request credentials or invent capabilities.',
       token_verifier=verifier or Verifier(config),
       auth=AuthSettings(issuer_url=config['issuer'],resource_server_url=config['resource'],required_scopes=['pcc:operate'],validate_token_resource=True),
       transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=['127.0.0.1:*','localhost:*',urlsplit(config['resource']).netloc],allowed_origins=[config['resource'].split('/mcp')[0]]))
    def subject():
        token=get_access_token()
        if token is None or not token.subject:raise PermissionError('authenticated subject required')
        return token.subject
    ro=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
    write=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True)
    security={'securitySchemes':[{'type':'oauth2','scopes':['pcc:operate']}]}
    @m.tool(annotations=ro,meta=security)
    def pcc_capabilities()->dict:
        """Read authorized projects and grant versions. No task or shell execution."""
        return controller.capabilities(subject())
    @m.tool(annotations=write,meta=security)
    def pcc_submit(project:str,grant_version:int,goal:str,plan:dict,request_label:str='')->dict:
        """Queue ONE Plus task. May write approved files, quarantine deletions, install pinned wheels and transfer with approved targets. Plan: read_files, expected_outputs; publish [{artifact,destination}]; delete [relative file]; dependencies [approved name]; uploads [{artifact,target}]; optional downloads [{target,destination}] into frozen task input. Read pcc_project first. No caller-supplied URL, credentials or permission overrides. Identical goal/plan/label returns existing task; new label only for user-explicitly distinct work. Platform confirmations remain required."""
        return controller.submit(subject(),project,grant_version,goal,plan,request_label)
    @m.tool(annotations=ro,meta=security)
    def pcc_project(project:str)->dict:
        """Read saved approved project bounds and destinations; no secrets."""
        g=controller.authorized(subject(),project)
        return {k:v for k,v in g.items() if k not in ('subject',)}
    @m.tool(annotations=ro,meta=security)
    def pcc_status(task_id:str)->dict:
        """Query original task after timeout. UNKNOWN/RECOVERY_REQUIRED never means retry. No push notification is promised."""
        return controller.status(subject(),task_id)
    @m.tool(annotations=ro,meta=security)
    def pcc_result(task_id:str,offset:int=0,limit:int=20)->dict:
        """Read durable result, receipt and paginated artifact manifest. Independently review, do not rename LOCAL_CHECK to REVIEW."""
        return controller.result(subject(),task_id,offset,limit)
    @m.tool(annotations=ro,meta=security)
    def pcc_artifact(task_id:str,path:str,offset:int=0,limit:int=12000)->dict:
        """Read a frozen task artifact page, bound to raw-byte SHA256; never arbitrary filesystem reads."""
        return controller.artifact(subject(),task_id,path,offset,limit)
    @m.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=False),meta=security)
    def pcc_cancel(task_id:str)->dict:
        """Request cancellation; does not undo effects or prove child processes have stopped."""
        return controller.cancel(subject(),task_id)
    @m.resource('pcc://skill')
    def skill()->str:return (BASE/'plugins/pcc/skills/pcc/SKILL.md').read_text(encoding='utf-8')
    return m

def serve(config_path):
    cfg=load(config_path)
    if cfg.get('deployment_enabled') is not True:raise PermissionError('deployment disabled; obtain consolidated approval first')
    controller=Controller()
    # A persistent lock excludes multiple worker services. Never auto-break a stale owner lock.
    lock=controller.root/'service.lock';f=lock.open('x');f.write(str(__import__('os').getpid()));f.close()
    controller.reconcile()
    stop=threading.Event()
    def worker():
        while not stop.wait(.5):
            if (controller.root/'disabled.flag').exists():continue
            with controller.db() as c:r=c.execute("SELECT id FROM tasks WHERE status='QUEUED' ORDER BY created LIMIT 1").fetchone()
            if r:
                try:controller.execute(r['id'])
                except Exception:controller.update(r['id'],'RECOVERY_REQUIRED')
    t=threading.Thread(target=worker,daemon=True);t.start()
    try:build(controller,cfg).run(transport='streamable-http')
    finally:
        stop.set();t.join(timeout=3)
        # Retain owner lock if executor is still in flight.
        if not t.is_alive():lock.unlink()
