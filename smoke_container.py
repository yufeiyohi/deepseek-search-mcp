"""Container verification; upstream is explicitly MOCK, never a live API call."""
import asyncio
import os
import socket
import subprocess
import sys
import unittest

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from adapter import Settings, create_app, identity


class ContainerSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_real_socket_sdk_and_identity(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        host = f'127.0.0.1:{port}'
        seen = []
        def upstream(request):
            seen.append((identity.get(), str(request.url)))
            return httpx.Response(200, json={'results': [], 'title': 'MOCK', 'content': 'MOCK'})
        app = create_app(Settings('MOCK_ONLY', {'a': 'a'*40, 'b': 'b'*40}, [host], []), httpx.MockTransport(upstream))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='critical', proxy_headers=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(.01)
            self.assertTrue(server.started)
            url = f'http://{host}/deepseek'
            async with httpx.AsyncClient(trust_env=False) as client:
                for method in ('GET', 'POST', 'DELETE'):
                    for token in ('', 'wrong'):
                        r = await client.request(method, url, headers={'Authorization': 'Bearer '+token} if token else {})
                        self.assertEqual(r.status_code, 401)
                for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'}):
                    r = await client.post(url, headers={'Authorization': 'Bearer '+'a'*40, **headers})
                    self.assertEqual(r.status_code, 403)
            for profile in ('a', 'b'):
                async with streamablehttp_client(url, headers={'Authorization': 'Bearer '+profile*40}) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        self.assertEqual({x.name for x in (await client.list_tools()).tools}, {'search', 'fetch'})
                        for tool, args in [('search', {'query': 'MOCK'}), ('fetch', {'url': 'https://example.com'})]:
                            result = await client.call_tool(tool, args)
                            self.assertFalse(result.isError)
                            self.assertEqual(seen[-1][0], profile)
            self.assertEqual(len(seen), 4)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            sock.close()

    async def test_body_and_rate_limits(self):
        app = create_app(Settings('MOCK', {'a': 'a'*40, 'b': 'b'*40}, ['testserver'], []))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                h = {'Authorization': 'Bearer '+'a'*40}
                r = await client.post('/deepseek', headers=h, content=b'x'*16385)
                self.assertEqual(r.status_code, 413)
                for _ in range(119):
                    r = await client.get('/nonexistent', headers=h)
                    self.assertEqual(r.status_code, 404)
                self.assertEqual((await client.get('/nonexistent', headers=h)).status_code, 429)
                self.assertEqual((await client.get('/nonexistent', headers={'Authorization': 'Bearer '+'b'*40})).status_code, 404)

    async def test_missing_configuration_entrypoint(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MCP_', 'DEEPSEEK_'))}
        result = subprocess.run([sys.executable, '/app/adapter.py'], env=env, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'Invalid or missing configuration', result.stderr)

    async def test_nonroot_runtime(self):
        self.assertEqual(os.getuid(), 65532)
        self.assertEqual(os.getgid(), 65532)


if __name__ == '__main__':
    unittest.main(verbosity=2)
