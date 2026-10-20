"""DeepSeek tools-only adapter. No model endpoints and no runtime mock switch."""
import asyncio
import contextvars
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.responses import JSONResponse

# SDK validation errors can contain caller input; do not emit them to logs.
logging.disable(logging.CRITICAL)
identity = contextvars.ContextVar('identity', default=None)


@dataclass
class Settings:
    api_key: str
    tokens: dict[str, str]
    hosts: list[str]
    origins: list[str]

    def __post_init__(self):
        if not self.api_key.strip() or not self.tokens or not self.hosts:
            raise ValueError('Missing required configuration')
        if any(not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', p) or not isinstance(t,str) or len(t)<32 or not t.isascii() for p,t in self.tokens.items()):
            raise ValueError('Invalid profile token configuration')
        if len(set(self.tokens.values())) != len(self.tokens):
            raise ValueError('Tokens must be unique per profile')
        if any('*' in h for h in self.hosts + self.origins):
            raise ValueError('Use explicit hosts and origins')

    @classmethod
    def from_env(cls):
        def secret(name):
            filename = os.environ.get(name+'_FILE')
            return Path(filename).read_text().strip() if filename else os.environ.get(name,'')
        try:
            return cls(secret('DEEPSEEK_API_KEY'),json.loads(secret('MCP_TOKENS')),json.loads(os.environ.get('MCP_ALLOWED_HOSTS','[]')),json.loads(os.environ.get('MCP_ALLOWED_ORIGINS','[]')))
        except Exception:
            raise ValueError('Invalid or missing configuration') from None


class Guard:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.windows = defaultdict(deque)
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        async def reject(status):
            await JSONResponse({'error':'Request rejected'}, status_code=status)(scope,receive,send)
        headers = scope.get('headers',[])
        auth = [v for k,v in headers if k == b'authorization']
        supplied = auth[0][7:] if len(auth)==1 and auth[0].startswith(b'Bearer ') else b''
        owner = None
        for profile, token in self.settings.tokens.items():
            if hmac.compare_digest(supplied,token.encode('ascii')):
                owner = profile
        if owner is None:
            return await reject(401)
        hosts = [v.decode('latin1') for k,v in headers if k==b'host']
        origins = [v.decode('latin1') for k,v in headers if k==b'origin']
        if len(hosts)!=1 or hosts[0] not in self.settings.hosts or (origins and (len(origins)!=1 or origins[0] not in self.settings.origins)):
            return await reject(403)
        now = time.monotonic()
        window = self.windows[owner]
        while window and window[0] <= now-60:
            window.popleft()
        if len(window)>=120 or self.active>=8:
            return await reject(429)
        window.append(now)
        self.active += 1
        marker = identity.set(owner)
        try:
            body = bytearray()
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message['type']=='http.disconnect':
                        return
                    body.extend(message.get('body',b''))
                    if len(body)>16384:
                        return await reject(413)
                    if not message.get('more_body',False):
                        break
            used = False
            async def replay():
                nonlocal used
                if not used:
                    used = True
                    return {'type':'http.request','body':bytes(body),'more_body':False}
                return await receive()
            await self.app(scope,replay,send)
        except TimeoutError:
            await reject(408)
        finally:
            identity.reset(marker)
            self.active -= 1


def validate_url(url):
    try:
        p = urlsplit(url)
        host = p.hostname
        if len(url)>8192 or p.scheme not in ('http','https') or not host or p.username is not None or p.password is not None or any(ord(c)<33 for c in url) or '\\' in url:
            raise ValueError()
        _ = p.port
        host = host.rstrip('.').lower()
        if host=='localhost' or host.endswith(('.localhost','.local','.internal')) or '.' not in host and ':' not in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Reject ambiguous legacy numeric/hex IPv4 spellings too.
            if all(re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)',part) for part in host.split('.')):
                raise ValueError()
        else:
            if not address.is_global:
                raise ValueError()
    except Exception:
        raise ValueError('Invalid public HTTP(S) URL') from None


def create_app(settings, upstream_transport=None):
    mcp = FastMCP('DeepSeek Search and Fetch', stateless_http=True, json_response=True, streamable_http_path=os.environ.get('MCP_PATH','/deepseek'),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=settings.hosts,allowed_origins=settings.origins))

    async def forward(tool, payload):
        if identity.get() is None:
            raise ValueError('Authentication required')
        try:
            async with asyncio.timeout(35):
                async with httpx.AsyncClient(transport=upstream_transport,timeout=httpx.Timeout(30,connect=5),follow_redirects=False,trust_env=False) as client:
                    async with client.stream('POST','https://api.deepseek.com/v1/tools/'+tool,headers={'Authorization':'Bearer '+settings.api_key},json=payload) as response:
                        if response.status_code!=200:
                            raise ValueError()
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks)>2097152:
                                raise ValueError()
                        data = json.loads(chunks)
            budget = 20000
            def text(value, maximum):
                nonlocal budget
                result = value[:min(maximum,budget)] if isinstance(value,str) else ''
                budget -= len(result)
                return result
            if tool=='search':
                if not isinstance(data.get('results'),list):
                    raise ValueError()
                return {'results':[{k:text(row.get(k),8192 if k=='content' else 2048) for k in ('title','url','snippet','summary','content')} for row in data['results'][:payload['count']] if isinstance(row,dict)], 'output_limit_chars':20000}
            return {'title':text(data.get('title'),1024),'content':text(data.get('content'),19000),'output_limit_chars':20000}
        except Exception:
            raise ValueError('Upstream request failed or response exceeded limits; no automatic retry') from None

    @mcp.tool()
    async def search(query: Annotated[str,Field(min_length=1,max_length=128,strict=True)], count: Annotated[int,Field(ge=1,le=50,strict=True)] = 10, need_content: Annotated[bool,Field(strict=True)] = False) -> dict:
        """Search the web with DeepSeek Search only; successful results may incur API charges."""
        if not query.strip():
            raise ValueError('Query must not be blank')
        return await forward('search',{'query':query,'count':count,'need_content':need_content})

    @mcp.tool()
    async def fetch(url: Annotated[str,Field(min_length=1,max_length=8192,strict=True)]) -> dict:
        """Fetch public HTTP(S) content through DeepSeek Fetch only; may incur API charges."""
        validate_url(url)
        return await forward('fetch',{'url':url})

    app = mcp.streamable_http_app()
    app.add_middleware(Guard,settings=settings)
    return app


def application():
    return create_app(Settings.from_env())


if __name__=='__main__':
    import uvicorn
    uvicorn.run(application(),host='0.0.0.0',port=8080,access_log=False,log_level='critical',proxy_headers=False)
